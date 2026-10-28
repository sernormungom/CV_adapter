"""Every write to `tailored_cvs`. The Application-owned CV artifact (P-009).

Four rules live here, all at the write boundary rather than in a route or a
client guard, so a second caller cannot forget one.

**A generation's inputs are frozen before any work starts.** `request_generation`
snapshots the consultant's profile YAML, their style rules, their base CV state
(which supplies presentation and photo), and the job posting, and stores the lot
on the row. The handler opens no file and re-reads no record: it renders the
snapshot. That is what makes a generation reproducible minutes later, in another
process, from a re-queued run — and what stops a profile edit landing mid-flight
from changing a document a human already approved the inputs for.

**The snapshot's hash identifies the generation; the run id does not.** The
workflow row is enqueued *after* the artifact is marked `generating`, and its
`generation_run_id` is written after that again, so a handler that depended on
the run id could be claimed and executed before that reference exists. Instead
`(input_hash, generation_attempt)` — carried in the run's own idempotency key —
is the coordination fact, and the handler's effect is a single conditional
`UPDATE` on it. That satisfies the runner's contract exactly: **at-least-once
invocation, exactly-once effect** (`backend/workflows/handlers/probe.py`).

**Retry means two different things and they are kept apart.** Re-requesting a
generation that is still in flight returns the existing run and starts nothing —
the double-clicked button, or the POST retried after the 60-second edge, which is
the whole reason P-006 exists. Re-requesting one whose run has *finished* is a
deliberate new attempt: `generation_attempt` increments, so the idempotency key
differs and a real run is created. Without that counter the second case would
collide with the first key, `enqueue` would hand back the finished run, and the
retry would silently do nothing forever.

**A failed generation never destroys a good CV.** `status` describes the last
generation, not whether content exists. A regeneration that fails leaves the
previous `cv_state`, `revision`, and `content_hash` untouched and records a
bounded error — the consultant keeps an editable document and is told the last
attempt failed.

Locks extend the order the other two services already use — **Job row → pair row
→ Application row → tailored CV row** — so nothing here can deadlock against
`backend/jobs/service.py` or `backend/applications/service.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.applications import service as applications_service
from backend.cv_pipeline.context_assembler import assemble_context
from backend.cv_state import service as cv_state_service
from backend.cv_state.service import (
    MODE_APPLICATION,
    STALE_WRITE_MESSAGE,
    CVArtifact,
)
from backend.db.models import (
    Application,
    Job,
    TailoredCV,
    TailoredCVStatus,
    User,
    WorkflowRun,
    WorkflowStatus,
)
from backend.db.models.tailored_cv import MAX_ERROR_MESSAGE
from backend.tailored_cv.keys import (
    GENERATION_WORKFLOW_TYPE,
    GenerationKey,
    input_ref,
    parse_generation_key,
)
from backend.workflows import service as workflows_service


class TailoredCVServiceError(RuntimeError):
    """A caller-fixable problem with a tailored-CV request."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


#: Every service error a route in this domain can see. `request_generation`
#: reuses the Application service's row lock, so its error type reaches the
#: caller too — the same composition `applications.service` makes with Jobs.
SERVICE_ERRORS = (
    TailoredCVServiceError,
    applications_service.ApplicationServiceError,
    cv_state_service.CVStateError,
)

TERMINAL_APPLICATION_MESSAGE = (
    "This application has ended, so there is nothing left to tailor a CV for. "
    "Its existing tailored CV can still be opened and downloaded."
)
GENERATION_IN_PROGRESS_MESSAGE = (
    "A generation is already running for this application. Wait for it to "
    "finish before starting a different one."
)
SAVE_DURING_GENERATION_MESSAGE = (
    "A generation is running for this application and is about to replace this "
    "CV. Wait for it to finish, then edit the result."
)
CROSS_ARTIFACT_MESSAGE = (
    "That edit belongs to a different application's CV and was not saved."
)
NO_PROFILE_MESSAGE = (
    "This consultant has no imported CV profile yet, so there is nothing to "
    "tailor. Import a CV first."
)
INVALID_PROFILE_MESSAGE = (
    "This consultant's stored profile could not be read, so a CV cannot be "
    "generated from it."
)
NO_PHOTO_MESSAGE = (
    "This consultant has no photo on their base CV yet, so a tailored CV "
    "cannot be generated. Upload a photo first."
)
NO_CONTENT_MESSAGE = (
    "This tailored CV has not been generated yet."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# The generation key vocabulary lives in `keys.py`, a leaf module, and is
# re-exported here so callers have one import. See that module for why the
# handler must not reach into this one for it.
__all__ = [
    "GENERATION_WORKFLOW_TYPE",
    "GenerationKey",
    "SERVICE_ERRORS",
    "TailoredCVServiceError",
    "artifact",
    "build_input_snapshot",
    "commit_generated_state_once",
    "current_key",
    "current_run",
    "get_by_id",
    "get_for_application",
    "input_ref",
    "list_for_consultant",
    "lock_tailored",
    "parse_generation_key",
    "record_generation_failure",
    "request_generation",
    "save_state",
    "snapshot_hash",
]


# ── Reads ─────────────────────────────────────────────────────────────────


def get_for_application(
    db: Session, application: Application
) -> TailoredCV | None:
    return db.scalar(
        select(TailoredCV).where(TailoredCV.application_id == application.id)
    )


def get_by_id(db: Session, tailored_cv_id) -> TailoredCV | None:
    try:
        key = uuid.UUID(str(tailored_cv_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.get(TailoredCV, key)


def current_key(tailored: TailoredCV) -> GenerationKey:
    """The generation key this artifact currently stands at."""
    return GenerationKey(
        tailored_cv_id=tailored.id,
        input_hash=tailored.input_hash,
        attempt=tailored.generation_attempt,
    )


def current_run(db: Session, tailored: TailoredCV) -> WorkflowRun | None:
    """The run for this artifact's current generation, found by its key.

    **Not by a stored reference.** A `generation_run_id` column could only be
    written after `enqueue` committed, and a second concurrent request that took
    this artifact's row lock inside that window read a null reference, concluded
    nothing was in flight, and started a duplicate generation — caught by
    `tests/integration/test_tailored_cv_pg.py::test_two_concurrent_generate_requests_yield_one_artifact`.
    The key is already unique per business intent (`workflow_runs.idempotency_key`),
    so looking it up removes both the window and the second source of truth.
    """
    return db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.idempotency_key == current_key(tailored).format()
        )
    )


def _run_is_active(run: WorkflowRun | None) -> bool:
    """Whether a generation is genuinely still in flight.

    `status == 'generating'` alone is not enough. A process that died between
    marking the row and enqueuing its run leaves the artifact claiming to be
    generating with no run behind it, and refusing every later request would
    strand the Application permanently. So "in flight" means a run exists *and*
    has not reached a terminal state.
    """
    return run is not None and run.status in {
        WorkflowStatus.queued.value,
        WorkflowStatus.running.value,
    }


def artifact(tailored: TailoredCV) -> CVArtifact:
    """The tailored CV in the same envelope the base CV uses.

    Same shape, different address: the editor has one loader and one saver, and
    the *mode* is what differs rather than the payload's shape (P-009's
    "explicit editor modes").
    """
    return CVArtifact(
        mode=MODE_APPLICATION,
        id=str(tailored.id),
        revision=tailored.revision,
        content_hash=tailored.content_hash or "",
        state=tailored.cv_state or {},
        # `content_changed_at`, not the row's `updated_at`: recording a *failed*
        # generation is a write, and reporting it as the document's last update
        # would tell a client the CV changed when it demonstrably did not.
        updated_at=(
            tailored.content_changed_at.isoformat()
            if tailored.content_changed_at
            else None
        ),
    )


# ── The input snapshot ────────────────────────────────────────────────────


def build_input_snapshot(db: Session, application: Application) -> dict:
    """Freeze every input this generation is approved to use.

    Deliberately carries **no timestamp**. The snapshot is hashed, and the hash
    is what makes a repeated request recognisable as the same generation; a
    clock reading in it would make every double-click a different generation and
    turn the idempotent path into a conflict.
    """
    consultant = db.get(User, application.consultant_id)
    job = db.get(Job, application.job_id)
    if consultant is None or job is None:
        # Both are FK targets of the Application the caller was authorized
        # against, so this is a concurrently erased row rather than a bad
        # request. Answer honestly instead of raising an attribute error.
        raise TailoredCVServiceError(
            "That application no longer exists.", status=404
        )

    try:
        context = assemble_context(consultant.username, job.description)
    except FileNotFoundError:
        raise TailoredCVServiceError(NO_PROFILE_MESSAGE, status=409) from None
    except ValueError:
        # profile.md exists but has no YAML fence, or is not a mapping — an
        # operator-fixable data problem, not a server fault. Same distinction
        # `backend/matching/service.py` draws.
        raise TailoredCVServiceError(
            INVALID_PROFILE_MESSAGE, status=409
        ) from None

    base = cv_state_service.load_base(consultant.username)
    # Refuse before generation starts rather than let `state_builder` fall back
    # to a blank photo slot (V3-P-023). Same rule as that fallback: a stored
    # reference, never an inline data: URL.
    photo = base.state.get("photoSrc")
    if not (isinstance(photo, str) and photo and not photo.startswith("data:")):
        raise TailoredCVServiceError(NO_PHOTO_MESSAGE, status=409)
    return {
        "consultant_username": consultant.username,
        "profile_yaml": context["profile_yaml"],
        "style_md": context["style_md"],
        "job_id": str(job.id),
        "job_title": job.title,
        "job_company": job.company or "",
        "job_description": context["job_description"],
        # A *copy* of the base CV, for presentation and the photo reference.
        # Copied in, never written back — that is the P-009 boundary.
        "base_cv_state": base.state,
        "base_cv_revision": base.revision,
    }


def snapshot_hash(snapshot: dict) -> str:
    return cv_state_service.content_hash(snapshot)


# ── Row locking ───────────────────────────────────────────────────────────


def lock_tailored(db: Session, tailored: TailoredCV) -> TailoredCV:
    """Reload the artifact under `FOR UPDATE`, refusing a vanished row.

    Selecting with `populate_existing` rather than `Session.refresh`, for the
    two reasons `applications.service._lock_and_reload` documents and measured:
    a plain select would hand back the identity map's stale attributes with the
    lock uselessly taken, and `refresh` on a deleted row raises
    `InvalidRequestError`, which is not a service error and escapes as a 500.
    """
    locked = db.scalars(
        select(TailoredCV)
        .where(TailoredCV.id == tailored.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if locked is None:
        raise TailoredCVServiceError(
            "That tailored CV no longer exists.", status=404
        )
    return locked


def _get_or_create(
    db: Session,
    application: Application,
    snapshot: dict,
    input_hash: str,
    actor: User,
) -> tuple[TailoredCV, bool]:
    """The artifact for this Application, created if it is the first request.

    Returns `(tailored_cv, created)`. Insert-and-let-the-index-decide, like
    `jobs.service.create_job` and `applications.service.apply`: a
    check-then-insert lets two concurrent Generate requests both find nothing
    and both insert, and the unique `application_id` index is what actually
    guarantees one artifact.

    **This deliberately does not commit.** It is called with the Application's
    row lock held, and that lock lasts only as long as the transaction: a commit
    here would release it before `request_generation` finishes deciding, letting
    a concurrent close land in the gap and leaving work enqueued for an
    Application that is already terminal (`S3-P1-R1` finding 4). The flush is
    enough to make the unique index adjudicate.
    """
    tailored = TailoredCV(
        application_id=application.id,
        status=TailoredCVStatus.generating.value,
        revision=0,
        input_snapshot=snapshot,
        input_hash=input_hash,
        generation_attempt=0,
        created_by_id=actor.id,
    )
    try:
        with db.begin_nested():
            db.add(tailored)
            db.flush()
    except IntegrityError:
        existing = get_for_application(db, application)
        if existing is None:
            # Only reachable if the winner rolled back between our failed
            # insert and this read. Say so rather than inventing a row.
            raise TailoredCVServiceError(
                "Could not start the generation; please try again.", status=409
            ) from None
        return existing, False
    return tailored, True


# ── Generation ────────────────────────────────────────────────────────────


def request_generation(
    db: Session, application: Application, *, actor: User
) -> tuple[TailoredCV, WorkflowRun, bool]:
    """Accept a generation durably. Returns `(tailored_cv, run, started)`.

    `started=False` means an identical generation was already in flight and this
    request joined it rather than creating a second one — the retry contract
    P-006 exists for. A different set of inputs while one is in flight is a
    conflict (409), because two generations racing to fill one artifact would
    make which document survives a matter of scheduling.
    """
    # Application row lock first: whether this Application is still live has to
    # be decided against the persisted row, not against a copy loaded before a
    # concurrent close committed. Extends the fixed order (Job → pair →
    # Application → tailored) rather than adding to it.
    #
    # Nothing between here and the `enqueue` below commits, so this lock is held
    # for the whole decision. That is load-bearing in both directions: a
    # concurrent close cannot land after the terminal check and still see work
    # enqueued for it, and the artifact + run become visible together.
    application = applications_service.lock_application(db, application)
    if application.is_terminal:
        raise TailoredCVServiceError(TERMINAL_APPLICATION_MESSAGE, status=409)

    snapshot = build_input_snapshot(db, application)
    input_hash = snapshot_hash(snapshot)

    tailored, created = _get_or_create(
        db, application, snapshot, input_hash, actor
    )
    tailored = lock_tailored(db, tailored)

    in_flight = current_run(db, tailored)
    if tailored.is_generating and _run_is_active(in_flight):
        if tailored.input_hash == input_hash:
            # The retried POST / double-clicked button. Join the run in flight;
            # start nothing.
            return tailored, in_flight, False
        raise TailoredCVServiceError(GENERATION_IN_PROGRESS_MESSAGE, status=409)

    # A genuinely new attempt: fresh snapshot, fresh key, fresh run. The row
    # this function just created is already attempt 0; anything else counts up,
    # so an explicit retry after a finished run gets a key that has not been
    # used and therefore a run that will actually execute. The previously
    # generated content (if any) is deliberately left in place until this
    # attempt succeeds — a failed regeneration must not cost the consultant a
    # working document.
    attempt = (
        tailored.generation_attempt
        if created
        else tailored.generation_attempt + 1
    )
    tailored.input_snapshot = snapshot
    tailored.input_hash = input_hash
    tailored.generation_attempt = attempt
    tailored.status = TailoredCVStatus.generating.value
    tailored.error_code = None
    tailored.error_message = None

    key = GenerationKey(
        tailored_cv_id=tailored.id, input_hash=input_hash, attempt=attempt
    )
    # `enqueue` commits this Session, so the artifact row, its `generating`
    # state and its run row land in **one** commit, releasing the Application
    # lock at the same instant — and it is the last write, so there is no later
    # reference for a concurrent request to find missing. The ordering matters in
    # the other direction too: a run claimable before the row says `generating`
    # would find nothing to do and the generation would be lost.
    run = workflows_service.enqueue(
        db,
        workflow_type=GENERATION_WORKFLOW_TYPE,
        idempotency_key=key.format(),
        subject_type="tailored_cv",
        subject_id=str(tailored.id),
        input_ref=input_ref(tailored.id),
    )
    db.refresh(tailored)
    return tailored, run, True


def commit_generated_state_once(
    db: Session, key: GenerationKey, state: dict
) -> tuple[TailoredCV, bool]:
    """The handler's atomic business effect. Returns `(artifact, performed)`.

    One conditional `UPDATE`, never a read followed by a write. Two invocations
    of the same run can be live at once (the runner's lock-loss contract), and a
    superseded attempt can still be executing while a newer one is enqueued; the
    `input_hash`/`generation_attempt`/`status` predicate lets exactly one of them
    change the row, and every other invocation observes the committed result and
    performs no effect of its own.
    """
    now = _now()
    updated = db.execute(
        update(TailoredCV)
        .where(
            TailoredCV.id == key.tailored_cv_id,
            TailoredCV.input_hash == key.input_hash,
            TailoredCV.generation_attempt == key.attempt,
            TailoredCV.status == TailoredCVStatus.generating.value,
        )
        .values(
            cv_state=state,
            content_hash=cv_state_service.content_hash(state),
            revision=TailoredCV.revision + 1,
            status=TailoredCVStatus.ready.value,
            generated_at=now,
            content_changed_at=now,
            error_code=None,
            error_message=None,
        )
        .returning(TailoredCV.revision)
    ).scalar_one_or_none()
    if updated is not None:
        db.commit()
        return _reload(db, key.tailored_cv_id), True

    # No row changed: a sibling invocation already landed this generation, or a
    # newer attempt superseded it. End this transaction before observing the
    # winner, so READ COMMITTED sees it even if this Session holds a stale
    # instance — the same reason `workflows.service.commit_result_ref_once`
    # rolls back here.
    db.rollback()
    return _reload(db, key.tailored_cv_id), False


def record_generation_failure(
    db: Session, key: GenerationKey, *, code: str, message: str
) -> bool:
    """Mark this attempt failed, if it is still the current one.

    Guarded by the same predicate as the success path, and for the same reason:
    a superseded or already-completed attempt raising afterwards must not
    downgrade a good artifact. Returns whether the row was changed.
    """
    result = db.execute(
        update(TailoredCV)
        .where(
            TailoredCV.id == key.tailored_cv_id,
            TailoredCV.input_hash == key.input_hash,
            TailoredCV.generation_attempt == key.attempt,
            TailoredCV.status == TailoredCVStatus.generating.value,
        )
        .values(
            status=TailoredCVStatus.failed.value,
            error_code=code[:64],
            error_message=message[:MAX_ERROR_MESSAGE],
        )
    )
    db.commit()
    return result.rowcount > 0


def _reload(db: Session, tailored_cv_id: uuid.UUID) -> TailoredCV:
    tailored = db.scalars(
        select(TailoredCV)
        .where(TailoredCV.id == tailored_cv_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if tailored is None:
        raise TailoredCVServiceError(
            "That tailored CV no longer exists.", status=404
        )
    return tailored


# ── Editing ───────────────────────────────────────────────────────────────


def save_state(
    db: Session,
    tailored: TailoredCV,
    *,
    state: dict,
    expected_revision: int,
    expected_id: str,
    actor: User,
) -> TailoredCV:
    """Save an edited tailored CV. Refuses stale and cross-artifact writes.

    Three refusals, all 409, all decided against the persisted row under its
    lock:

    - **cross-artifact** — the editor states which artifact it loaded, and a
      body addressed at a different one is refused rather than written. A
      Consultant with two Applications open is one stale tab away from saving
      one position's CV over another's, and neither authorization nor the URL
      alone catches that: both artifacts are legitimately theirs.
    - **mid-generation** — a generation about to land would overwrite the edit
      moments later, so the edit is refused with an explanation instead of
      being accepted and then silently lost.
    - **stale revision** — someone else (or another tab) wrote since this was
      loaded. Refused rather than merged: the editor holds a whole document, so
      merging would mean guessing per field which absence was intentional.

    After all three, **a content-identical save is a no-op** (V3-P-022/
    V3-P-033): no column moves — not `revision`, not `content_changed_at` (what
    the artifact reports as its `updated_at`), not `updated_by_id`. The editor
    autosaves every 30 seconds regardless of edits. Decided under the
    same row lock, against the row's stored `content_hash`; a missing one
    counts as changed.
    """
    if not isinstance(state, dict):
        raise TailoredCVServiceError("A CV state must be a JSON object.")
    tailored = lock_tailored(db, tailored)

    if str(tailored.id) != str(expected_id):
        raise TailoredCVServiceError(CROSS_ARTIFACT_MESSAGE, status=409)
    if tailored.is_generating and _run_is_active(current_run(db, tailored)):
        raise TailoredCVServiceError(
            SAVE_DURING_GENERATION_MESSAGE, status=409
        )
    if expected_revision != tailored.revision:
        raise TailoredCVServiceError(STALE_WRITE_MESSAGE, status=409)

    clean = cv_state_service.strip_identity(state)
    new_hash = cv_state_service.content_hash(clean)
    if tailored.content_hash and new_hash == tailored.content_hash:
        # Nothing on the row has been assigned, so nothing is dirty. End the
        # transaction anyway: it holds this row's `FOR UPDATE` lock, which
        # must not outlive the decision.
        #
        # Detach first, so the reply carries the values read *under* the lock.
        # A rollback expires every attached instance, and the route's reply
        # would then re-read the row after the lock is gone: a concurrent
        # delete would surface as a 500, and a concurrent real edit would hand
        # this client the other writer's revision — which its next autosave
        # would then pass the stale check with, overwriting that edit.
        db.expunge(tailored)
        db.rollback()
        return tailored

    tailored.cv_state = clean
    tailored.content_hash = new_hash
    tailored.revision = tailored.revision + 1
    tailored.content_changed_at = _now()
    tailored.updated_by_id = actor.id
    db.commit()
    db.refresh(tailored)
    return tailored


# ── Rights paths ──────────────────────────────────────────────────────────


def list_for_consultant(db: Session, consultant_id: uuid.UUID) -> list[TailoredCV]:
    """Every tailored CV belonging to one Consultant, oldest first.

    Joined through `applications`, which is where the ownership actually lives —
    this table has no Consultant column, deliberately, so there is one answer to
    "whose CV is this" rather than two that can disagree.
    """
    return list(
        db.scalars(
            select(TailoredCV)
            .join(Application, Application.id == TailoredCV.application_id)
            .where(Application.consultant_id == consultant_id)
            .order_by(TailoredCV.created_at)
        )
    )
