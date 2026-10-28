"""A consultant's durable CV import (V2 Slice 4) — restores async Import CV.

V1's `POST /api/import-cv` ran text extraction plus up to two LLM calls
(`extract_experience`, and `merge_profiles` when a profile already existed) on
the open HTTP request — the same 60-second-edge defect P-006 exists to remove
(`docs/evidence/m1-timeout-inventory.md` #1), and it wrote `profile.md`
regardless of which artifact the editor had open. Slice 3 disabled the route
rather than carry a known 504 path; this table is the durable replacement.

**One row per consultant, like the base CV.** Import updates `profile.md`, the
consultant's own facts store — there is one authoritative import state per
consultant, not one per upload, mirroring how `cv_state.json` is a singleton
rather than a history. `consultant_username` is unique, decided by the database
the same way `tailored_cvs.application_id` is: two concurrent first-imports
both pass a check-then-insert, and the unique index picks the winner.

**`(input_hash, attempt)` is the generation's identity, exactly as
`tailored_cvs` does it, and for the same reason: there is no column pointing at
a workflow run.** The run is found by looking up the idempotency key built from
this row's id, hash and attempt — never by a stored reference, which is what
let a concurrent tailored-CV request start a duplicate generation before Slice
3 removed the column entirely
(`tests/integration/test_tailored_cv_pg.py::test_two_concurrent_generate_requests_yield_one_artifact`).
Re-requesting a hash that is already `processing` joins the run in flight;
re-requesting after a finished run is a deliberate new attempt, so `attempt`
increments and the key differs — without it a retry after a failure would
collide with the finished key and never execute again.

**No CV content lives here — only a filename, counts, and references.**
`source_ref` is a path to the transient uploaded file on the shared profile
volume (EFS), never its bytes; the handler deletes that file after its last
required stage on every path, including failure
(`docs/plans/v2-pilot.md` §1's transient-source-deletion floor). **Cleared only
once that delete is confirmed** (`S4-P1-R7`) — never in the same write that
records success or failure, since a row can be terminal while its file still
needs cleaning up (a delete that failed, or a process that died before
attempting it). See `backend/cv_importer/service.py`'s module docstring for
the ordering and the residual risk this pilot accepts rather than closing with
a scheduled sweep. `result_*` are the same non-content counts
`cv_importer.experience_writer.write_profile` already returns
(`roles_count`, `skills_count`, `open_gaps`, `blocking_gaps`) — enough for a
client to show what happened without a second copy of profile content sitting
in a workflow-adjacent row.

**`status` describes the last import attempt, not whether a profile exists.**
A failed *re*-import leaves `profile.md` exactly as the last successful import
left it — the writer is only ever invoked with the freshly extracted data, and
a hard extraction/validation error is recorded here and raised before the
writer is called at all, so there is nothing to roll back on disk.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey

CV_IMPORT_STATUS_VALUES: frozenset[str] = frozenset(
    {"processing", "succeeded", "failed"}
)

_CV_IMPORT_STATUS_CHECK = "status IN ('processing', 'succeeded', 'failed')"

#: Matches `tailored_cvs.error_message` / `workflow_runs.error_message` — a
#: handler failure must never write an unbounded upstream payload here either.
MAX_ERROR_MESSAGE = 500


class CVImport(UUIDPrimaryKey, Timestamps, Base):
    """One consultant's durable CV import state."""

    __tablename__ = "cv_imports"
    __table_args__ = (
        CheckConstraint(_CV_IMPORT_STATUS_CHECK, name="ck_cv_imports_status"),
        CheckConstraint("attempt >= 0", name="ck_cv_imports_attempt"),
    )

    # Plain string, no FK to `users` — like `CVPublication.consultant_username`
    # and `Notification.consultant_username`. An admin may import into a profile
    # directory with no separate consultant account behind it (the "view-as"
    # capability `require_username_access` already documents), so this cannot
    # require a matching User row to exist.
    consultant_username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="processing", index=True
    )

    #: Counted from 0, like `tailored_cvs.generation_attempt` and for the same
    #: reason: part of the workflow idempotency key, so a retry after a
    #: *finished* attempt gets a genuinely new run while a retried request for
    #: one still *in flight* does not.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: sha256 of the uploaded bytes. Identifies this attempt's inputs and is
    #: half of the workflow idempotency key; also how a double-submitted
    #: identical upload is recognised as the same request rather than a
    #: conflicting new one.
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The original filename, for display only — not a secret, not content.
    source_filename: Mapped[str | None] = mapped_column(String(255))
    #: The uploaded file's content type, captured at upload time so the handler
    #: does not need to re-derive it from a filename extension.
    content_type: Mapped[str | None] = mapped_column(String(128))
    #: A reference to the transient uploaded file on the shared profile volume
    #: — a path, never the file's bytes. Cleared only once that file's delete is
    #: confirmed (`S4-P1-R7`), which can be well after this row itself goes
    #: terminal — see `cv_importer/service.py::finish_upload_cleanup`.
    source_ref: Mapped[str | None] = mapped_column(String(500))

    #: Non-content stats from the last successful import — the same counts
    #: `experience_writer.write_profile` already returns. Null until a success.
    result_roles_count: Mapped[int | None] = mapped_column(Integer)
    result_skills_count: Mapped[int | None] = mapped_column(Integer)
    result_open_gaps: Mapped[int | None] = mapped_column(Integer)
    result_blocking_gaps: Mapped[int | None] = mapped_column(Integer)

    #: The last failure, bounded and operator-safe — same discipline as
    #: `workflow_runs`/`tailored_cvs`, because this is shown to users too.
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(MAX_ERROR_MESSAGE))

    # Who asked for the most recent attempt. SET NULL so erasing an actor (a TA
    # who imported on a consultant's behalf) does not delete a surviving
    # consultant's import row.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_processing(self) -> bool:
        return self.status == "processing"

    def to_dict(self) -> dict:
        """Metadata and non-content counts only — never profile content."""
        result = None
        if self.status == "succeeded":
            result = {
                "roles_count": self.result_roles_count,
                "skills_count": self.result_skills_count,
                "open_gaps": self.result_open_gaps,
                "blocking_gaps": self.result_blocking_gaps,
            }
        return {
            "id": str(self.id),
            "consultant_username": self.consultant_username,
            "status": self.status,
            "attempt": self.attempt,
            "source_filename": self.source_filename,
            "result": result,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
