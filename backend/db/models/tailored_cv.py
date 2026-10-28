"""The Application-owned tailored CV — a separate artifact identity (P-009).

V1 had one CV per consultant: generating for a position overwrote
`data/profiles/<username>/cv_state.json`, so the tailored document *was* the base
document and there was nothing to compare a regeneration against. P-009 calls
preventing that "a correctness boundary, not UI polish", and this table is the
boundary: the base CV keeps its own storage identity
(`backend/cv_state/service.py`, file-backed with its own revision and hash) and
each Application gets its own row here.

Four things here are load-bearing:

**One tailored CV per Application, enforced by the database.** `application_id`
is unique, so a retried or double-clicked Generate cannot create a second
artifact for the same Application even if two requests race a check-then-insert.
An Application that ends and is applied to again is a *new* Application row
(Slice 2's partial live-pair index), and therefore a new tailored CV — history
is kept by the Application, not by stacking artifacts here.

**The generation inputs are snapshotted, and the handler reads nothing else.**
`input_snapshot` holds every approved input (the consultant's profile YAML, the
style rules, the base CV state that supplies presentation and photo, and the job
posting text) as it stood when a human asked for the generation. The handler
runs minutes later, in a different process, possibly twice — so it opens no
file: editing `profile.md` mid-generation cannot change what is produced, and a
re-queued invocation regenerates from the same inputs rather than from whatever
disk happens to hold now.

**`(input_hash, generation_attempt)` is the generation's identity, and there is
no stored run reference at all.** Those two fields *are* the workflow's
idempotency key, so the run for this artifact is found by looking that key up —
never by a column pointing at it. That is not a stylistic choice: an earlier
version stored a `generation_run_id`, which could only be written *after* the
enqueue committed, and a second concurrent request that took the artifact's lock
in that window read a null reference, concluded nothing was in flight, and
started a duplicate generation
(`tests/integration/test_tailored_cv_pg.py::test_two_concurrent_generate_requests_yield_one_artifact`
is what caught it). With the key as the only link there is no window, and no
second source of truth to disagree with the first.

It is also what makes the handler's effect exactly-once under the runner's
at-least-once invocation contract (`backend/workflows/handlers/probe.py`): the
effect is an `UPDATE ... WHERE input_hash = :hash AND generation_attempt = :n
AND status = 'generating'`, so an invocation whose snapshot has been superseded —
or whose sibling already landed — changes no row and performs no effect.

**`status` describes the generation, not whether content exists.** A failed
*re*generation leaves the previously generated `cv_state` and `revision` intact
and sets `status='failed'` — the consultant keeps a usable tailored CV and is
told the last attempt failed, rather than losing the document to a transient
LLM error. "Is there content" is `revision > 0`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey

# JSONB on Postgres, plain JSON on SQLite (the fast gate). Same pattern as
# CVPublication and Notification.
_JSON = JSONB().with_variant(JSON(), "sqlite")


class TailoredCVStatus(str, enum.Enum):
    """The state of this artifact's most recent generation.

    `generating` — a workflow run is (or should be) producing content.
    `ready`      — the last generation produced content.
    `failed`     — the last generation failed; any previously generated
                   content is still present and still editable.
    """

    generating = "generating"
    ready = "ready"
    failed = "failed"


TAILORED_CV_STATUS_VALUES: frozenset[str] = frozenset(
    s.value for s in TailoredCVStatus
)

_TAILORED_CV_STATUS_CHECK = "status IN ('generating', 'ready', 'failed')"

#: Hard cap on a persisted generation error message, matching `workflow_runs`.
#: A handler failure must never write an unbounded upstream payload here either.
MAX_ERROR_MESSAGE = 500


class TailoredCV(UUIDPrimaryKey, Timestamps, Base):
    """One Application's tailored CV."""

    __tablename__ = "tailored_cvs"
    __table_args__ = (
        CheckConstraint(
            _TAILORED_CV_STATUS_CHECK, name="ck_tailored_cvs_status"
        ),
        CheckConstraint("revision >= 0", name="ck_tailored_cvs_revision"),
    )

    # Unique: one tailored CV per Application (P-009). The constraint is the
    # contract, not a prior SELECT — two concurrent Generate requests both pass
    # a check-then-insert.
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TailoredCVStatus.generating.value,
        index=True,
    )

    #: Bumped by every content write — a generation that lands and every editor
    #: save. The client sends the revision it loaded and a write that does not
    #: match it is refused, so a stale tab cannot overwrite newer content.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: sha256 of the canonical `cv_state`. NULL until the first generation
    #: lands; `revision > 0` and this being set are the same condition.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: The editor's `captureFormState()` snapshot — never rendered HTML, so the
    #: same shared render core draws it in the editor and any viewer.
    cv_state: Mapped[dict | None] = mapped_column(_JSON)

    #: The approved generation inputs, frozen when a human asked. See the module
    #: docstring: the handler reads this and nothing else.
    input_snapshot: Mapped[dict] = mapped_column(_JSON, nullable=False)
    #: sha256 of the canonical `input_snapshot`. Identifies the generation, is
    #: half of its workflow idempotency key, and guards the handler's effect.
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Which generation this is, counted from 0. Part of the workflow
    #: idempotency key, so a *retry after a failure* gets a genuinely new run
    #: while a *retried request for an in-flight generation* does not: without
    #: it, re-requesting the same inputs after a failure would collide with the
    #: original key, `enqueue` would hand back the finished run, and nothing
    #: would ever execute again.
    generation_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    #: When the last *successful generation* landed. Distinguishes a generated
    #: document from a hand-edited one.
    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    #: When the CV content last changed — by a generation or by an edit.
    #: `updated_at` cannot answer this: any write touches it, including
    #: recording a *failed* generation, so a client showing "last updated"
    #: would claim the document changed when it demonstrably did not. Same
    #: reason `applications.status_changed_at` exists beside `updated_at`.
    content_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    #: The last generation failure, bounded and operator-safe — the same
    #: discipline `workflow_runs` applies, because this is shown to users too.
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(MAX_ERROR_MESSAGE))

    # Who asked for the first generation, and who last wrote content. SET NULL
    # so erasing an actor does not delete a surviving Consultant's artifact.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def has_content(self) -> bool:
        return self.revision > 0 and self.cv_state is not None

    @property
    def is_generating(self) -> bool:
        return self.status == TailoredCVStatus.generating.value

    def to_dict(self) -> dict:
        """Metadata only — never `cv_state` or `input_snapshot`.

        Both carry CV content, and a list of artifacts is not a way to read
        every one of them. The content is served by the artifact route, one
        Application at a time, behind that Application's own authorization.
        """
        return {
            "id": str(self.id),
            "application_id": str(self.application_id),
            "status": self.status,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "has_content": self.has_content,
            "generation_attempt": self.generation_attempt,
            "generated_at": (
                self.generated_at.isoformat() if self.generated_at else None
            ),
            "content_changed_at": (
                self.content_changed_at.isoformat()
                if self.content_changed_at
                else None
            ),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
