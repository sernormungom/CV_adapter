"""The durable CV-import handler — the runner's second real payload.

Follows `backend/tailored_cv/handler.py`'s shape at this domain's boundary:
at-least-once invocation, exactly-once effect. Extraction (`cv_parser`,
`experience_extractor`) and the merge/write (`experience_writer`,
`profile_merger`) are the same proven components the old synchronous
`POST /api/import-cv` used — untouched, and reused exactly as
`docs/plans/v2-pilot.md` Slice 4 calls for.

**The profile write is the one part of this pipeline that is not naturally
idempotent, and that is why it is guarded twice — once against a live
sibling, and once against a dead one.**

*Against a live sibling* (two invocations racing while both processes are
alive): unlike tailored-CV generation — which *overwrites* `cv_state`
wholesale, so a second invocation producing the same content is harmless —
`experience_writer.write_profile` *merges* the new extraction into whatever
`profile.md` currently holds. Two live invocations of the same attempt (the
runner's documented at-least-once contract,
`backend/workflows/service.py::recover_interrupted`) each calling the merge
would each merge the *same* new extraction into an already-merged
`profile.md`, duplicating every block and evidence item it added. So the
write, and the row's `succeeded` commit, both happen inside
`cv_state_service.exclusive_profile_write` (serializing them against any other
write to this consultant's `profile.md`, across ECS tasks, the same way
`cv_state.service.save_base` serializes `cv_state.json`). The row is **re-read
fresh, under that lock**, to confirm this attempt has not already been landed
by the sibling invocation that held the lock first — and the success commit
happens *before the lock releases*, not after: committing outside the lock
would leave a window in which a second invocation, unblocked the instant the
first releases, re-reads the row before that first invocation's commit has
landed, still sees `processing`, and writes the profile a second time. A
sibling that already committed is observed, not raced.

*Against a dead sibling* (`S4-P1-R1` finding 1): the lock above only
coordinates *live* contenders — it is released the instant its holder's
process dies, by the OS, whether or not that holder ever got to run its own
`finally`. A crash between `write_profile()` landing on disk and
`commit_import_success_once` committing therefore leaves `profile.md` already
merged while the row still reads `processing` — with no live holder left to
observe. Recovery then invokes the same key again, and there is no database
row to consult that would say "the file already reflects this attempt": the
row itself is the thing that never got written. So the fact has to live in
the file being checked, written in the exact same atomic operation as the
content it describes — `write_profile`'s `import_marker` parameter embeds this
attempt's key into `profile.md`'s own `metadata._last_import_key` when it
writes (now atomically, temp file plus rename, matching
`cv_state.service.save_base`'s write). Before calling `write_profile`, the
handler reads the *current* on-disk document (`preview.read_profile_document`)
and compares its marker to this attempt: a match means a prior invocation's
write already landed and only the row needs finalizing (its stats are
recomputed from the already-written document via
`experience_writer.compute_profile_stats`, not re-derived by merging again); a
mismatch (or no marker at all) means the merge genuinely has not happened yet.

*Against a live process whose finalize commit itself fails* (`S4-P1-R2`
finding 1, corrected by `S4-P1-R3`): the process does not die and the marker
lands on disk exactly as intended, but `commit_import_success_once` — the
database write that would have recorded `succeeded` — raises (a transient
connection error, a rolled-back transaction). This is not a crash: the
`except`/`finally` machinery below runs normally. But naively treating it like
any other failure (`_record_failure`, flipping the row to `failed`) would be
actively wrong, not merely imprecise: the content is already durably correct,
and a subsequent attempt — which must use a *new* `attempt` number to get a
workflow run that actually executes, since `workflow_runs.idempotency_key` is
unique and the old key's run is already terminal — would carry a key that can
never equal the marker written under the old attempt number.

A first fix relaxed the marker match to `(cv_import_id, input_hash)` alone,
dropping `attempt` from the comparison — this closed the finding, but broke
something else (`S4-P1-R3` finding 1): it also matched a genuinely *new*,
legitimate re-upload of the same bytes requested *after* a prior import had
already **succeeded**, silently skipping the merge into a profile that could
have changed in the meantime (a manual edit, a chat profile patch) since that
successful import. Matching on content hash alone cannot distinguish "this
exact stuck attempt's own write already landed" from "this content happens to
be identical to some unrelated, already-finished import" — only the *row's own
current attempt* can answer that, and only *before* a new attempt is created,
because creating one already commits to a fresh, non-matching key.

So this handler's own marker check stays an **exact** match on
`(cv_import_id, input_hash, attempt)` — this attempt's key and no other's — and
the reconciliation instead happens one layer up, in
`service.request_import`/`_try_reconcile_stuck_attempt`: when a request finds
the row `processing` with no active run (the stuck-commit case) and the same
uploaded content, it checks whether *that exact stuck attempt's own* marker is
already on disk and, if so, finalizes it directly — no new attempt, no new
run, no merge — before the "create a new attempt" code ever runs. A `commit_
import_success_once` failure is still never passed to `_record_failure` here:
the row is left exactly as `processing`, the honest state — the content is
fine, only the bookkeeping is unfinished — which is what makes it visible to
`request_import` as a case to reconcile rather than a `failed` import to
silently re-merge on the next attempt.

The upload is deleted after the handler's last required stage on every path —
success, failure, and a superseded/already-landed no-op alike — because none
of those leave anything still needing the file. **The row's reference to it is
cleared only once that delete is confirmed** (`S4-P1-R7`): the earlier design
cleared `source_ref` in the same terminal commit that recorded success or
failure, then deleted the file afterward here — so a process death between
that commit and the delete, or the delete itself failing (a real `OSError`,
not "already gone"), left a file with no reference anywhere, forever.
`service.finish_upload_cleanup` is the one place that orders this correctly;
see `backend/cv_importer/service.py`'s module docstring for the two windows
even that cannot close and the retention sweep that backstops them.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from backend.cv_importer import service
from backend.cv_importer.cv_parser import extract_text
from backend.cv_importer.experience_extractor import extract_experience
from backend.cv_importer.experience_writer import compute_profile_stats, write_profile
from backend.cv_importer.keys import (
    IMPORT_WORKFLOW_TYPE,
    PROFILE_LOCK_FILENAME,
    ImportKey,
    parse_import_key,
    result_ref,
)
from backend.cv_importer.preview import read_profile_document
from backend.cv_state.service import exclusive_profile_write
from backend.db.models.workflow import WorkflowRun
from backend.workflows import registry
from backend.workflows import service as workflows_service
from backend.workflows.service import WorkflowError

logger = logging.getLogger(__name__)


async def _extract(raw_text: str) -> tuple[dict | None, list[dict]]:
    if not raw_text.strip():
        return None, [{"kind": "unparseable", "context": "no extractable text"}]
    data, errors, _raw = await extract_experience(raw_text)
    return data, errors


def _summarize(errors: list[dict] | None) -> str:
    """A bounded, operator-safe reason. Never the free-text `context` field,
    which can quote the model's raw output or CV text — same discipline as
    `tailored_cv.handler._summarize`."""
    kinds = sorted({str((e or {}).get("kind", "error")) for e in errors or []})
    return ", ".join(kinds)[:200]


class _FinalizeCommitFailed(Exception):
    """`write_profile` succeeded (the marker is durably on disk) but
    `commit_import_success_once` did not. Caught separately from every other
    failure so the outer handler does not call `_record_failure` for it — see
    the module docstring's third guard."""


def handle_cv_import(db: Session, run: WorkflowRun) -> str:
    """Extract and merge this consultant's uploaded CV into `profile.md`, exactly once."""
    key = parse_import_key(run.idempotency_key)
    if key is None:  # pragma: no cover - only a hand-written run row
        raise WorkflowError(
            "bad_import_key", "the run's idempotency key is not a CV-import key"
        )

    cv_import = service.get_by_id(db, key.cv_import_id)
    if cv_import is None:
        raise WorkflowError("import_deleted", "the CV import record no longer exists")

    # Superseded by a newer attempt, or already landed by a sibling invocation.
    if (
        cv_import.input_hash != key.input_hash
        or cv_import.attempt != key.attempt
        or not cv_import.is_processing
    ):
        logger.info(
            "cv_import %s: run %s is not the current attempt; no effect",
            cv_import.id, run.id,
        )
        return _result_ref(cv_import)

    source_path = Path(cv_import.source_ref) if cv_import.source_ref else None
    username = cv_import.consultant_username

    try:
        if source_path is None or not source_path.exists():
            raise WorkflowError(
                "source_missing", "the uploaded file is no longer available"
            )

        workflows_service.record_stage(db, run, "extracting_text", progress=15)
        raw_text = extract_text(source_path, cv_import.content_type or "")

        workflows_service.record_stage(db, run, "extracting_experience", progress=40)
        data, errors = asyncio.run(_extract(raw_text))
        if data is None:
            raise WorkflowError("extraction_failed", _summarize(errors))

        workflows_service.record_stage(db, run, "writing_profile", progress=75)
        with exclusive_profile_write(username, PROFILE_LOCK_FILENAME):
            # Re-read fresh, *inside* the lock: a sibling invocation may have
            # already written profile.md while this invocation was blocked
            # waiting for it. See the module docstring.
            current = service.refresh(db, key.cv_import_id)
            if (
                current is None
                or current.input_hash != key.input_hash
                or current.attempt != key.attempt
                or not current.is_processing
            ):
                logger.info(
                    "cv_import %s: superseded while waiting for the profile "
                    "lock; no effect", key.cv_import_id,
                )
                return _result_ref(current or cv_import)

            existing_document = read_profile_document(username)
            # Exact match on (cv_import_id, input_hash, attempt) — this
            # attempt's own key, and no other's. Content-hash-only matching
            # was tried and reverted (`S4-P1-R3` finding 1): it let a *new*,
            # legitimate re-upload of the same bytes after a *successful*
            # prior import silently skip merging into a profile that could
            # have changed since. A stuck prior attempt whose own write
            # landed but whose commit failed is instead reconciled directly,
            # under its own unchanged key, by
            # `service.request_import`/`_try_reconcile_stuck_attempt` —
            # *before* a new attempt (and therefore a new, non-matching key)
            # is ever created. By the time this handler runs for a given key,
            # a marker match here can only mean a live sibling of *this exact*
            # attempt landed first.
            already_written = (
                existing_document is not None
                and existing_document.get("metadata", {}).get("_last_import_key")
                == key.format()
            )
            if already_written:
                # A sibling invocation of this exact attempt already wrote
                # profile.md and is racing this one for the lock (the
                # `S4-P1-R1` live- and dead-sibling cases). The document
                # already reflects this attempt — recompute its stats rather
                # than merging again.
                logger.info(
                    "cv_import %s: profile.md already carries this attempt's "
                    "marker; finalizing without merging again", key.cv_import_id,
                )
                result = {"success": True, **compute_profile_stats(existing_document)}
            else:
                result = asyncio.run(
                    write_profile(
                        data,
                        source_tmp_path=None,
                        source_filename=None,
                        username=username,
                        import_marker=key.format(),
                    )
                )

            if not result.get("success"):
                raise WorkflowError(
                    "profile_invalid",
                    "; ".join(result.get("validation_errors") or [])[:200],
                )

            # **Committed before the lock releases.** The write and the row's
            # `succeeded` status have to become visible together: a commit
            # made *after* releasing the lock leaves a window in which a
            # sibling invocation, blocked on the lock the whole time, acquires
            # it, re-reads the row, still sees `processing` (this commit has
            # not landed yet), and calls `write_profile` a second time — the
            # exact duplicate-merge defect this lock exists to prevent, just
            # moved one step later.
            try:
                cv_import, performed = service.commit_import_success_once(
                    db, key, result=result
                )
            except Exception as commit_exc:
                # The marker is already durable; only the bookkeeping failed.
                # See the module docstring's third guard — this must not reach
                # `_record_failure` below.
                db.rollback()
                raise _FinalizeCommitFailed(type(commit_exc).__name__) from commit_exc
        if not performed:
            logger.info(
                "cv_import %s: another invocation already committed this "
                "attempt; no duplicate effect", cv_import.id,
            )
        return _result_ref(cv_import)
    except _FinalizeCommitFailed:
        logger.error(
            "cv_import %s: profile.md carries this attempt's marker but the "
            "row could not be finalized; leaving it `processing` rather than "
            "recording a failure the on-disk content already contradicts — a "
            "later attempt will recognise the marker and finalize instead of "
            "merging again", key.cv_import_id,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - classified and recorded below
        _record_failure(db, key, exc)
        raise
    finally:
        # Deleted on every path — success, failure, and a superseded/
        # already-landed no-op alike (docs/plans/v2-pilot.md §1's
        # transient-source-deletion floor). The row's reference is cleared
        # only once that delete is confirmed, never as part of a status
        # transition — see `service.finish_upload_cleanup` (`S4-P1-R7`).
        service.finish_upload_cleanup(db, key.cv_import_id, source_path)


def _result_ref(cv_import) -> str:
    return result_ref(cv_import.id, cv_import.attempt)


def _record_failure(db: Session, key: ImportKey, exc: BaseException) -> None:
    """Mirror the failure onto the row, safely and conditionally."""
    if isinstance(exc, WorkflowError):
        code, message = exc.code, exc.message
    else:
        # Never `str(exc)`: a parser or LLM/HTTP client error can embed CV text
        # or a token, and this row is shown to users.
        code, message = "unexpected_error", type(exc).__name__
    try:
        db.rollback()
        service.record_import_failure(db, key, code=code, message=message)
    except Exception:  # noqa: BLE001 - the run's own failure record still stands
        logger.exception(
            "cv_import %s: could not record the import failure", key.cv_import_id
        )


# Registered against the leaf-module constant, never `service.…` — see
# `keys.py` and `tailored_cv.handler`'s identical comment for why.
registry.register(IMPORT_WORKFLOW_TYPE, handle_cv_import)
