"""Every write to `cv_imports`, and the transient upload it manages.

**The upload is saved to the shared profile volume before enqueueing, and
deleted after the handler's last required stage on every path.** ECS Fargate
tasks have no shared local disk, but `backend.config.DATA_DIR` is EFS
(`docs/DECISIONS.md`) — the same volume `profile.md` and `cv_state.json` live
on — so a file saved under a consultant's profile directory is visible to
whichever task's runner claims the run, including after a restart. That is
what "polling survives restart" requires: the upload is a *reference*
(`cv_imports.source_ref`), never bytes, in exactly the sense
`workflow_runs.input_ref` already is.

**One row per consultant, and the same retry vocabulary as `tailored_cv`.**
Re-requesting an import whose upload hashes the same as one already
`processing` *and active* joins it and starts nothing — the double-click, or
the POST retried after the 60-second edge. A different upload while one is in
flight is a conflict (409): two imports racing to write one consultant's
`profile.md` would make which facts survive a matter of scheduling.
Re-requesting after a finished attempt (succeeded or failed) is a deliberate
new attempt: `attempt` increments, so the idempotency key differs and a real
run is created.

**A `processing` row with no active run is reconciled, not restarted
(`S4-P1-R2`/`S4-P1-R3`).** `backend/cv_importer/handler.py`'s
`commit_import_success_once` can fail *after* `write_profile()` has already
landed its marker on disk (a live process, not a crash) — the row is
deliberately left `processing` rather than `failed` for that case (see the
handler's module docstring). The *only* way to try again is a fresh request
that gets a brand-new `attempt` number, since `workflow_runs.idempotency_key`
is unique and the stuck attempt's own run is already terminal — but the
handler's marker check is (and must stay) an *exact* match on
`(cv_import_id, input_hash, attempt)`, not merely `(cv_import_id, input_hash)`:
an early version relaxed it to the latter and that silently broke the
*ordinary* case of re-uploading the same bytes after a genuinely finished
import to re-merge into a profile that has since changed (`S4-P1-R3` finding
1) — a new attempt's key can never equal an old attempt's exact marker, by
design, and it must not pretend otherwise. So `request_import` itself detects
the *specific* case a new attempt cannot: a `processing` row whose run is not
active, requested again with the *same* content. `_try_reconcile_stuck_attempt`
checks whether that stuck attempt's own unchanged key already matches the
on-disk marker and, if so, finalizes it directly — no new attempt, no *new*
run, no merge — before ever reaching the "create a new attempt" code below.
That finalization covers the attempt's existing workflow run as well as its
row (`S4-P1-R4`): the run is terminally `failed`, which was honest when the
handler raised and becomes a lie the moment the marker proves the effect, and
`routes._envelope` reports both halves together. If the
marker does not match (the process died before `write_profile` ever ran, the
plain `S4-P1-R1` crash case), reconciliation is skipped and a genuine new
attempt proceeds exactly as usual, merging fresh.

Locking is scoped to this table's own row — there is no Job/Application/
tailored-CV chain to extend, because an import belongs to a consultant, not to
a shared or Application-owned object.

**`source_ref` is never cleared before its file is confirmed gone
(`S4-P1-R7`).** The earlier design cleared it in the same terminal `UPDATE`
that recorded success or failure, then deleted the file afterward in the
handler's `finally` — so a process death between that commit and the delete,
or the delete itself failing (a real `OSError`, not merely "already gone"),
left a file with no reference anywhere pointing at it, forever: nothing
durable said "this needs cleaning up." `delete_upload` now reports whether the
file is confirmed absent, and only a confirmed delete triggers
`clear_source_ref_if_matches` — a second, separate, best-effort commit. A row
can therefore be `succeeded`/`failed` while `source_ref` still names an
already-gone-or-not file; that is deliberate; nothing reads `source_ref` off a
terminal row as if it still needed the bytes.

That still leaves two windows this module does not close, because neither has
a row to hold the reference at all — the file was saved, but no reference to
it was ever durably committed:

- between `save_upload()` writing the file and `request_import()`'s first
  commit (a crash right there leaves an orphan no row ever named); and
- a confirmed delete failure on a consultant who never imports again (the
  reference the row still carries is discoverable, but nothing revisits it
  unless that consultant is later offboarded —
  `backend/privacy/service.py::erase_consultant`'s `shutil.rmtree(profile_dir)`
  removes it unconditionally, needing no reference at all).

**Deliberately accepted for this pilot, not fixed by a sweep.** A short-lived
daily-retention-sweep mechanism was built and reviewed for these two windows
(`S4-P1-R7`/`R8`) and then removed as a product decision: it recovers a case
this disposable-data pilot's own posture does not require recovering
automatically — a stray, small, uploader-only file that costs disk and
nothing else — and its "which reference still represents active work" logic
was itself where `S4-P1-R8`'s two High findings lived. A future production
posture that actually needs this guarantee should reintroduce it deliberately
rather than resurrect the removed one from history; the reordering above (the
part that has real, low-risk value — a row honestly shows a lingering
reference rather than silently claiming to be clean) stays.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import config
from backend.cv_importer.experience_writer import compute_profile_stats
from backend.cv_importer.keys import (
    IMPORT_WORKFLOW_TYPE,
    SCRATCH_DIRNAME,
    ImportKey,
    input_ref,
    result_ref,
)
from backend.cv_importer.preview import PreviewError, read_profile_document
from backend.db.models import CVImport, User, WorkflowRun, WorkflowStatus
from backend.db.models.cv_import import MAX_ERROR_MESSAGE
from backend.workflows import service as workflows_service

logger = logging.getLogger(__name__)

#: Conservative — real CVs are a few hundred KB to a few MB; this leaves
#: headroom without inviting an unbounded upload onto the profile volume.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

#: mime type -> the extension `cv_parser.extract_text` dispatches on internally
#: (it switches on mime type, not extension, but the extension keeps the saved
#: filename legible for an operator looking at the scratch directory).
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
}

class CVImportServiceError(RuntimeError):
    """A caller-fixable problem with a CV-import request."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


SERVICE_ERRORS = (CVImportServiceError,)

UNSUPPORTED_TYPE_MESSAGE = (
    "Only PDF, Word, and Markdown documents (.pdf, .doc, .docx, .md, .markdown) "
    "are supported."
)

#: Extensions admitted by decoding as UTF-8 text rather than by content-type —
#: browsers report `.md`/`.markdown` content-type inconsistently (F-025,
#: `docs/plans/v3/decisions.md#v3-p-005`), so content-type is not the gate for
#: these two the way it is for PDF/Word.
_TEXT_EXTENSIONS = (".md", ".markdown")
TOO_LARGE_MESSAGE = "That file is too large to import."
IMPORT_IN_PROGRESS_MESSAGE = (
    "An import is already running for this consultant. Wait for it to finish "
    "before starting a different one."
)
NO_TEXT_MESSAGE = "No text could be extracted from that file."

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "CVImportServiceError",
    "IMPORT_IN_PROGRESS_MESSAGE",
    "MAX_UPLOAD_BYTES",
    "SERVICE_ERRORS",
    "clear_source_ref_if_matches",
    "commit_import_success_once",
    "current_run",
    "delete_upload",
    "finish_upload_cleanup",
    "get_by_id",
    "get_for_consultant",
    "record_import_failure",
    "refresh",
    "request_import",
    "save_upload",
]


# ── The transient upload ─────────────────────────────────────────────────


def _scratch_dir(username: str) -> Path:
    return config.DATA_DIR / username / SCRATCH_DIRNAME


def save_upload(
    username: str, *, filename: str, content_type: str, data: bytes
) -> tuple[Path, str]:
    """Validate and persist an uploaded CV to the shared profile volume.

    Returns `(path, input_hash)`. Raises `CVImportServiceError` (400) on an
    unsupported type or an oversized file — before anything is written to
    disk.
    """
    lower_name = filename.lower()
    text_ext = next((e for e in _TEXT_EXTENSIONS if lower_name.endswith(e)), None)
    if text_ext is not None:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise CVImportServiceError(UNSUPPORTED_TYPE_MESSAGE) from None
        ext = text_ext
    else:
        ext = ALLOWED_CONTENT_TYPES.get(content_type)
        if ext is None:
            raise CVImportServiceError(UNSUPPORTED_TYPE_MESSAGE)
    if len(data) > MAX_UPLOAD_BYTES:
        raise CVImportServiceError(TOO_LARGE_MESSAGE)

    input_hash = hashlib.sha256(data).hexdigest()
    scratch = _scratch_dir(username)
    scratch.mkdir(parents=True, exist_ok=True)
    dest = scratch / f"{uuid.uuid4().hex}{ext}"
    dest.write_bytes(data)
    return dest, input_hash


def delete_upload(path: str | Path | None) -> bool:
    """Remove a transient uploaded file. Tolerates None/missing.

    Returns whether the path is now **confirmed absent** — true for "already
    gone" as well as "just removed", false only for a genuine `OSError` (an
    EFS hiccup, a permission problem). Logged rather than swallowed: a caller
    that went on to clear its last durable reference to this file regardless
    of this return is exactly the `S4-P1-R7` defect (see the module
    docstring) — the failure has to be visible to whoever decides whether the
    reference can be cleared.
    """
    if not path:
        return True
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("cv_import: could not delete transient upload %s", path)
        return False


def clear_source_ref_if_matches(
    db: Session, cv_import_id: uuid.UUID, source_ref: str
) -> None:
    """Clear `source_ref` once its file is confirmed deleted.

    Guarded on matching the **exact path** this caller just deleted, not
    merely the row's id: a delete confirmation for an old attempt's file must
    not clear a newer attempt's live reference if it lands late. Best-effort —
    if this commit itself fails, the row still names a file that is already
    gone, which is harmless: the next request for this consultant overwrites
    the reference outright.
    """
    db.execute(
        update(CVImport)
        .where(CVImport.id == cv_import_id, CVImport.source_ref == source_ref)
        .values(source_ref=None)
    )
    db.commit()


def finish_upload_cleanup(
    db: Session, cv_import_id: uuid.UUID, source_ref: str | Path | None
) -> bool:
    """Delete `source_ref`'s file, and clear the row's reference to it only
    once that delete is confirmed. Best-effort at both steps — see
    `delete_upload` and `clear_source_ref_if_matches`. The one function both
    the handler's `finally` and `_try_reconcile_stuck_attempt` call, so the
    ordering is stated once rather than at each call site.

    Returns whether the file is now confirmed absent (mirrors
    `delete_upload`'s own return) — true for "already gone", `source_ref`
    empty, or just deleted; false only for a genuine delete failure.
    """
    if not source_ref:
        return True
    ref = str(source_ref)
    deleted = delete_upload(ref)
    if deleted:
        try:
            clear_source_ref_if_matches(db, cv_import_id, ref)
        except Exception:  # noqa: BLE001 - best-effort; a stale reference is harmless
            # A failed `db.commit()` leaves the Session's transaction unusable
            # until rolled back — SQLAlchemy does not do this automatically
            # (`S4-P1-R8` finding 2). Both call sites of this function reuse
            # `db` for more work right after returning: `handle_cv_import`'s
            # `finally` is followed by the runner's own
            # `mark_succeeded`/`mark_failed` on the *same* Session, and
            # `_try_reconcile_stuck_attempt`'s caller reads the row again to
            # build the response. Swallowing the exception without this left
            # both stranded — the next statement raised `InvalidRequestError`,
            # so a workflow that had already succeeded stayed `running`
            # forever.
            db.rollback()
            logger.exception(
                "cv_import %s: deleted %s but could not clear source_ref",
                cv_import_id, ref,
            )
    return deleted


# ── Reads ─────────────────────────────────────────────────────────────────


def get_for_consultant(db: Session, username: str) -> CVImport | None:
    return db.scalar(
        select(CVImport).where(CVImport.consultant_username == username)
    )


def get_by_id(db: Session, cv_import_id) -> CVImport | None:
    try:
        key = uuid.UUID(str(cv_import_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.get(CVImport, key)


def refresh(db: Session, cv_import_id: uuid.UUID) -> CVImport | None:
    """A fresh read of one row, bypassing the identity map's cached instance.

    Used by the handler to re-check this row's state *after* acquiring the
    profile-write lock, so a sibling invocation that already committed the
    write (the runner's at-least-once-invocation contract,
    `backend/workflows/service.py::recover_interrupted`) is observed rather
    than raced — the same care `tailored_cv.service.lock_tailored`/`_reload`
    take with `populate_existing`.
    """
    return db.scalars(
        select(CVImport)
        .where(CVImport.id == cv_import_id)
        .execution_options(populate_existing=True)
    ).one_or_none()


def current_key(cv_import: CVImport) -> ImportKey:
    return ImportKey(
        cv_import_id=cv_import.id,
        input_hash=cv_import.input_hash,
        attempt=cv_import.attempt,
    )


def current_run(db: Session, cv_import: CVImport) -> WorkflowRun | None:
    """The run for this row's current attempt, found by its key.

    **Not by a stored reference** — see the module and model docstrings for
    why: a `cv_import_run_id` column could only be written after `enqueue`
    committed, reopening exactly the window Slice 3 closed for `tailored_cvs`.
    """
    return db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.idempotency_key == current_key(cv_import).format()
        )
    )


def _run_is_active(run: WorkflowRun | None) -> bool:
    return run is not None and run.status in {
        WorkflowStatus.queued.value,
        WorkflowStatus.running.value,
    }


# ── Row locking / creation ───────────────────────────────────────────────


def _lock(db: Session, cv_import: CVImport) -> CVImport:
    locked = db.scalars(
        select(CVImport)
        .where(CVImport.id == cv_import.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if locked is None:
        raise CVImportServiceError("That import no longer exists.", status=404)
    return locked


def _get_or_create(
    db: Session, username: str, actor: User
) -> tuple[CVImport, bool]:
    """The row for this consultant, created if this is the first import ever.

    Insert-and-let-the-index-decide, like `tailored_cv.service._get_or_create`:
    two concurrent first-imports both find nothing and both insert, and the
    unique `consultant_username` index is what actually guarantees one row.
    Deliberately does not commit — see `request_import`.
    """
    cv_import = CVImport(
        consultant_username=username,
        status="processing",
        attempt=0,
        input_hash="",
        created_by_id=actor.id,
    )
    try:
        with db.begin_nested():
            db.add(cv_import)
            db.flush()
    except IntegrityError:
        existing = get_for_consultant(db, username)
        if existing is None:  # pragma: no cover - only if the winner rolled back
            raise CVImportServiceError(
                "Could not start the import; please try again.", status=409
            ) from None
        return existing, False
    return cv_import, True


# ── Reconciling a stuck attempt ───────────────────────────────────────────


def _try_reconcile_stuck_attempt(db: Session, cv_import: CVImport) -> CVImport | None:
    """Finalize a `processing`-but-not-active row's *own* attempt directly, if
    its write already landed — **the row and its workflow run together**, so
    the two halves of the status envelope agree. Returns the finalized row, or
    `None` if the on-disk marker does not match this row's current
    `(input_hash, attempt)` — meaning the write itself never happened (the
    process died before reaching it), and the caller should create a genuinely
    new attempt instead.

    Deliberately checks the row's own, **unchanged** key — never a different
    attempt's, and never ignoring `attempt` the way a since-reverted fix once
    did (`S4-P1-R3` finding 1): matching on content hash alone let a *new*,
    legitimate re-upload of the same bytes after a *successful* prior import
    silently skip merging into a profile that could have changed since.
    """
    key = current_key(cv_import)
    stuck_source_ref = cv_import.source_ref
    try:
        document = read_profile_document(cv_import.consultant_username)
    except PreviewError:
        # An unreadable/corrupt `profile.md` says nothing about whether this
        # attempt's write landed, and must not turn an upload into a 500 the
        # consultant can never get past: fall through to a genuine new
        # attempt, whose handler records a bounded failure if it cannot read
        # the file either.
        return None
    if document is None:
        return None
    marker = document.get("metadata", {}).get("_last_import_key")
    if marker != key.format():
        return None
    # The run for this attempt is terminally `failed` — the handler raised
    # after its write landed, and the runner recorded that honestly at the
    # time. Now that the marker has proved the effect, leaving it `failed`
    # would make the status envelope contradict itself: `routes._envelope`
    # looks the run up by this same key and would report a failed run beside a
    # succeeded import (`S4-P1-R4`). Upgrading a stale `failed` row is
    # explicitly allowed — a committed effect outranks an earlier wrong guess
    # (`backend/workflows/service.py`).
    #
    # **Staged, not committed, and staged before the row's own update**, so
    # that `commit_import_success_once`'s single commit publishes the run and
    # the row together (`S4-P1-R5`). Two commits, in either order, leave an
    # interruption window in which a poll reads one record finalized and the
    # other not — and that is the route's durable polling contract, not merely
    # an audit trail. If the guarded row update matches nothing (a sibling
    # reconciled first), that helper rolls back, which discards this staged
    # transition with it. The request's `FOR UPDATE` lock on the import row is
    # held across both writes and released by that one commit.
    stuck_run = current_run(db, cv_import)
    if stuck_run is not None:
        workflows_service.stage_succeeded(
            db, stuck_run, result_ref=result_ref(cv_import.id, key.attempt)
        )
    result = {"success": True, **compute_profile_stats(document)}
    finalized, _performed = commit_import_success_once(db, key, result=result)
    # The stuck attempt's own raw upload may still be on disk if its handler
    # invocation's own cleanup failed, or never ran at all (a real crash
    # rather than a live finalize-commit failure). `write_profile()` already
    # consumed it — nothing further needs these bytes — so retrying the
    # delete here, under this attempt's own unchanged reference, recovers it
    # as soon as this consultant's own import is next touched (`S4-P1-R7`).
    finish_upload_cleanup(db, cv_import.id, stuck_source_ref)
    return finalized


# ── The durable request ──────────────────────────────────────────────────


def request_import(
    db: Session,
    username: str,
    *,
    actor: User,
    source_path: Path,
    source_filename: str,
    content_type: str,
    input_hash: str,
) -> tuple[CVImport, WorkflowRun | None, bool]:
    """Accept an import durably. Returns `(cv_import, run, started)`.

    `started=False, run=<the in-flight run>` means an identical upload was
    already in flight and this request joined it — this call's own freshly
    saved upload is redundant (the in-flight run owns its own `source_ref`)
    and is deleted immediately.

    `started=False, run=None` means this request instead **reconciled** a
    stuck attempt (see `_try_reconcile_stuck_attempt`) — nothing new was
    enqueued because there was nothing left to do.
    """
    cv_import, created = _get_or_create(db, username, actor)
    cv_import = _lock(db, cv_import)

    in_flight = current_run(db, cv_import)
    if cv_import.is_processing and _run_is_active(in_flight):
        delete_upload(source_path)
        if cv_import.input_hash == input_hash:
            return cv_import, in_flight, False
        raise CVImportServiceError(IMPORT_IN_PROGRESS_MESSAGE, status=409)

    if cv_import.is_processing and cv_import.input_hash == input_hash:
        # Not active (the check above would have returned already) — a
        # stuck attempt whose own finalize commit failed after its write
        # already landed. Reconcile it directly, under its own key, rather
        # than creating a new attempt whose key could never match that
        # marker. A different upload here is *not* refused (unlike the
        # active-run case above): abandoning a stuck attempt for a fresh one
        # is reasonable, and falls through to "genuinely new attempt" below.
        reconciled = _try_reconcile_stuck_attempt(db, cv_import)
        if reconciled is not None:
            delete_upload(source_path)
            return reconciled, None, False

    # A genuinely new attempt. Drop any reference left by a prior attempt
    # (defensive — the handler already deletes its own upload in a `finally`
    # on every path) before pointing the row at this one. A plain
    # `delete_upload`, not `finish_upload_cleanup`: `source_ref` is
    # unconditionally overwritten a few lines below regardless of whether this
    # delete succeeds, so there is no reference left to conditionally clear. A
    # failed delete here leaves a small stray file on disk — accepted for this
    # pilot; it is cleaned up unconditionally if this consultant is ever
    # offboarded (`backend/privacy/service.py::erase_consultant`).
    delete_upload(cv_import.source_ref)

    attempt = cv_import.attempt if created else cv_import.attempt + 1
    cv_import.input_hash = input_hash
    cv_import.attempt = attempt
    cv_import.status = "processing"
    cv_import.source_filename = source_filename[:255]
    cv_import.content_type = content_type
    cv_import.source_ref = str(source_path)
    cv_import.error_code = None
    cv_import.error_message = None
    cv_import.created_by_id = actor.id

    key = ImportKey(cv_import_id=cv_import.id, input_hash=input_hash, attempt=attempt)
    # `enqueue` commits this Session, so the row's `processing` state and its
    # run land in one commit — same reasoning as
    # `tailored_cv.service.request_generation`.
    run = workflows_service.enqueue(
        db,
        workflow_type=IMPORT_WORKFLOW_TYPE,
        idempotency_key=key.format(),
        subject_type="cv_import",
        subject_id=str(cv_import.id),
        input_ref=input_ref(cv_import.id),
    )
    db.refresh(cv_import)
    return cv_import, run, True


# ── The handler's atomic effects ─────────────────────────────────────────


def commit_import_success_once(
    db: Session, key: ImportKey, *, result: dict
) -> tuple[CVImport, bool]:
    """The handler's atomic business effect for a successful import.

    One conditional `UPDATE`, guarded by `(input_hash, attempt, status)` —
    never a read-then-write. A superseded attempt, or a sibling invocation that
    already landed this one (the runner's at-least-once-invocation contract),
    changes no row and performs no effect. Mirrors
    `tailored_cv.service.commit_generated_state_once` exactly.
    """
    updated = db.execute(
        update(CVImport)
        .where(
            CVImport.id == key.cv_import_id,
            CVImport.input_hash == key.input_hash,
            CVImport.attempt == key.attempt,
            CVImport.status == "processing",
        )
        .values(
            status="succeeded",
            result_roles_count=result.get("roles_count"),
            result_skills_count=result.get("skills_count"),
            result_open_gaps=result.get("open_gaps"),
            result_blocking_gaps=result.get("blocking_gaps"),
            # `source_ref` is deliberately left as-is here — see the module
            # docstring's `S4-P1-R7` note. It is cleared only by
            # `clear_source_ref_if_matches`, once the file it names is
            # confirmed deleted, never as part of the status transition.
            error_code=None,
            error_message=None,
        )
        .returning(CVImport.id)
    ).scalar_one_or_none()
    if updated is not None:
        db.commit()
        return _reload(db, key.cv_import_id), True

    # No row changed: a sibling invocation already landed this attempt. End
    # this transaction before observing the winner, so READ COMMITTED sees it
    # even if this Session holds a stale instance.
    db.rollback()
    return _reload(db, key.cv_import_id), False


def record_import_failure(
    db: Session, key: ImportKey, *, code: str, message: str
) -> bool:
    """Mark this attempt failed, if it is still the current one.

    Guarded by the same predicate as the success path, for the same reason: a
    superseded or already-completed attempt raising afterwards must not
    downgrade a good row. Returns whether the row was changed.
    """
    result = db.execute(
        update(CVImport)
        .where(
            CVImport.id == key.cv_import_id,
            CVImport.input_hash == key.input_hash,
            CVImport.attempt == key.attempt,
            CVImport.status == "processing",
        )
        .values(
            # `source_ref` left as-is — same reasoning as
            # `commit_import_success_once`.
            status="failed",
            error_code=code[:64],
            error_message=message[:MAX_ERROR_MESSAGE],
        )
    )
    db.commit()
    return result.rowcount > 0


def _reload(db: Session, cv_import_id: uuid.UUID) -> CVImport:
    cv_import = refresh(db, cv_import_id)
    if cv_import is None:
        raise CVImportServiceError("That import no longer exists.", status=404)
    return cv_import
