"""Application state changes. Every write to `applications`.

Three rules live here, all of them at the write boundary rather than in a route
or a client guard, so a second caller of this service cannot forget one:

**Apply is idempotent through the database, not through a prior lookup.**
`apply()` inserts and lets the partial unique index
`uq_application_live_pair` decide. A `SELECT ... then INSERT` would let two
concurrent Apply requests — a double-clicked button, a retried request whose
first attempt is still running past the 60-second edge — both find nothing and
both insert. The IntegrityError path is therefore the *expected* path for a
retry, not an error path: it re-reads the live row and reports `created=False`.

**A Consultant–Job pair holds at most one live decision.** Reject and Apply are
the same question answered two ways, so they are mutually exclusive rather than
merely separate tables: `apply()` refuses a pair that already carries a Reject
marker, and `backend/jobs/service.record_review` refuses a pair that already
carries a live Application. Each conflict names the action that resolves it —
withdraw the Reject, or withdraw the Application — because a rule with no way
out is a dead end, not an invariant. The two write different tables, so unlike
Apply's own idempotency this rule cannot be an index; both sides take the
pair's `job_decision_pairs` row lock first (`jobs.service.lock_pair`) so a
concurrent Apply and Reject cannot both read "no conflict" and both commit.

**Status only ever moves forward, judged against the persisted row.** One rule,
`APPLICATION_STATUS_ORDER`: a transition must reach a strictly higher rank.
Re-requesting the status the Application already holds is an idempotent no-op
(the double-clicked Submit), and anything lower or sideways is a conflict. The
terminal guarantee is not a second rule that could drift — `closed` and
`withdrawn` simply share the top rank, so nothing outranks them. The rule is
evaluated after reloading the row under `FOR UPDATE`, because two overlapping
requests hold their own copies and validating a stale one can revive a terminal
Application — which in turn lets the pair end up both live and rejected.

Every write here that depends on the Job being open, or on the pair's other
decision, takes its locks in one fixed order: **Job row, then pair row, then the
Application row**. Same order everywhere, so nothing in this slice can deadlock
against `backend/jobs/service.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import (
    APPLICATION_STATUS_ORDER,
    APPLICATION_STATUS_VALUES,
    LIVE_APPLICATION_STATUSES,
    TERMINAL_APPLICATION_STATUSES,
    Application,
    ApplicationStatus,
    Job,
    JobReview,
    TailoredCV,
    User,
)
from backend.jobs.service import (
    JobServiceError,
    ensure_job_is_open,
    lock_job,
    lock_pair,
)


class ApplicationServiceError(RuntimeError):
    """A caller-fixable problem with an Application request."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


#: Both service error types a route in this domain can see. `apply()`
#: deliberately reuses the Job service's closed-Job guard rather than
#: re-implementing the predicate, so its exception type reaches the caller too.
SERVICE_ERRORS = (ApplicationServiceError, JobServiceError)

#: The Apply side of the one-live-decision rule. Its mirror image — the message
#: Reject returns when a live Application already exists — is
#: `jobs.service.LIVE_APPLICATION_MESSAGE`; each service owns the text it emits,
#: and each names the action that resolves the conflict.
REJECTED_PAIR_MESSAGE = (
    "This job is already rejected for that consultant. Withdraw the reject "
    "before applying."
)


# ── Pair-level reads (also used by the coherence guards) ──────────────────


def live_application(
    db: Session, job_id: uuid.UUID, consultant_id: uuid.UUID
) -> Application | None:
    """The pair's live Application, or None. At most one can exist."""
    return db.scalar(
        select(Application).where(
            Application.job_id == job_id,
            Application.consultant_id == consultant_id,
            Application.status.in_(LIVE_APPLICATION_STATUSES),
        )
    )


def _reject_marker(
    db: Session, job_id: uuid.UUID, consultant_id: uuid.UUID
) -> JobReview | None:
    return db.scalar(
        select(JobReview).where(
            JobReview.job_id == job_id,
            JobReview.consultant_id == consultant_id,
        )
    )


# ── Apply ─────────────────────────────────────────────────────────────────


def apply(
    db: Session, job: Job, consultant: User, *, actor: User
) -> tuple[Application, bool]:
    """Apply `consultant` to `job`. Returns `(application, created)`.

    Idempotent: a repeat while an Application is live returns that same row with
    `created=False`, so a double-click or a retried request never produces a
    second row. Once the pair's Application has reached a terminal status the
    pair may be applied to again, which creates a genuinely new Application —
    the historical row stays.
    """
    # Job row first, then the pair row. `lock_job` reloads the Job under a row
    # lock so the open-state decision cannot be made from a copy loaded before a
    # concurrent close committed; `lock_pair` does the same job for the
    # Apply/Reject rule, which spans two tables and so cannot be an index. The
    # order matches `jobs.service.record_review` exactly, so they cannot
    # deadlock against each other.
    lock_job(db, job)
    ensure_job_is_open(job)
    lock_pair(db, job, consultant)

    if _reject_marker(db, job.id, consultant.id) is not None:
        raise ApplicationServiceError(REJECTED_PAIR_MESSAGE, status=409)

    application = Application(
        job_id=job.id,
        consultant_id=consultant.id,
        status=ApplicationStatus.applied.value,
        created_by_id=actor.id,
        status_changed_at=datetime.now(timezone.utc),
        status_changed_by_id=actor.id,
    )
    try:
        # The context-manager form releases the savepoint on success and rolls
        # it back on the IntegrityError, leaving the enclosing transaction
        # usable — the same shape `jobs.service.create_job` uses.
        with db.begin_nested():
            db.add(application)
    except IntegrityError:
        # The expected retry path: the partial unique index rejected a second
        # live row for this pair.
        existing = live_application(db, job.id, consultant.id)
        if existing is None:
            # Only reachable if the winner was rolled back between our failed
            # insert and this read. Say so honestly rather than inventing a
            # row; the caller can retry.
            raise ApplicationServiceError(
                "Could not record the application; please try again.", status=409
            ) from None
        return existing, False

    db.commit()
    db.refresh(application)
    return application, True


# ── Status transitions ────────────────────────────────────────────────────


def _rank(status: str) -> int:
    return APPLICATION_STATUS_ORDER[status]


def _lock_and_reload(db: Session, application: Application) -> Application:
    """Take the Application row's lock and return it with fresh attributes.

    Selecting rather than `Session.refresh`, for two measured reasons:

    - **A concurrently deleted row must be a 404, not a 500.** `refresh` raises
      `InvalidRequestError` ("Could not refresh instance") in that case — *not*
      `ObjectDeletedError`, which is what an earlier version caught, so the
      handler never fired and a GDPR erasure overlapping a status request
      escaped as a server error. `one_or_none()` returns `None` instead and the
      caller answers honestly.
    - **`populate_existing` is mandatory.** For a row already in the identity
      map a plain select returns the *existing* instance and leaves its loaded
      attributes alone, so the lock would be taken and the stale status read
      anyway — quietly restoring the exact defect the lock exists to prevent.
    """
    locked = db.scalars(
        select(Application)
        .where(Application.id == application.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if locked is None:
        raise ApplicationServiceError(
            "That application no longer exists.", status=404
        )
    return locked


def lock_application(db: Session, application: Application) -> Application:
    """Take the Application row's lock and return it with fresh attributes.

    The public form of `_lock_and_reload`, for the Slice 3 tailored-CV service:
    that domain hangs off an Application and has to decide "is this Application
    still live" against the persisted row, for exactly the reason `transition`
    does. It extends the fixed lock order rather than adding to it — **Job row →
    pair row → Application row → tailored CV row** — so a tailored-CV write and
    an Application transition cannot deadlock against each other.
    """
    return _lock_and_reload(db, application)


def transition(
    db: Session, application: Application, *, status: str, actor: User
) -> tuple[Application, bool]:
    """Move `application` to `status`. Returns `(application, changed)`.

    The rule is the whole of it: the new status must outrank the current one.
    Re-requesting the current status is an idempotent no-op rather than a
    conflict, because the request that produces it is a retried or
    double-clicked button and refusing it would report a failure where nothing
    is wrong. Anything of lower or equal rank that is not the current status —
    including one terminal status asking to become the other — is refused.

    **The rule is applied to the persisted row, not to the caller's copy.** Two
    overlapping requests routinely load the same `applied` Application; if one
    commits `closed` while the other is still deciding, the second would
    otherwise validate `submitted` against its own stale `applied` and revive a
    terminal row. That is not merely a lost ordering: a Reject can legitimately
    land the moment the Application goes terminal, so the revived row leaves the
    pair holding *both* decisions — the invariant this slice exists to keep. So
    the row is reloaded under `FOR UPDATE` first (`_lock_and_reload`); a request
    that lost the race reads the winner's status and answers 409, and one whose
    row was erased meanwhile gets a 404 rather than a 500.
    """
    if status not in APPLICATION_STATUS_VALUES:
        # Reached by any client still speaking V1's `application_status`
        # vocabulary (`to_apply`, `cv_ready`, `link_opened`). There is no
        # translation layer; those values do not exist in V2 (P-008).
        raise ApplicationServiceError(f"Unknown application status: {status}")

    application = _lock_and_reload(db, application)

    if status == application.status:
        return application, False

    if _rank(status) <= _rank(application.status):
        raise ApplicationServiceError(
            f"An application cannot move from '{application.status}' to "
            f"'{status}'.",
            status=409,
        )

    application.status = status
    application.status_changed_at = datetime.now(timezone.utc)
    application.status_changed_by_id = actor.id

    # A confirmed terminal action (Slice 5, docs/plans/v2-pilot.md §7) disables
    # any public link and deletes the tailored artifact atomically with the
    # status move — one commit publishes both effects or neither. Reachable
    # exactly once per Application: the rank check above already proves
    # `application.status` (the *old* value, read moments ago under this same
    # row lock) was not terminal, since nothing outranks a terminal status.
    if status in TERMINAL_APPLICATION_STATUSES:
        # Local import: cv_publish.service imports lock_application from this
        # module (S5-P2, to serialize Publish against this same terminal move),
        # so a module-level import here would be a genuine circular import —
        # applications.service is the lower-level module in the documented
        # lock order (Job → pair → Application → tailored CV row) and must not
        # depend on a higher one at import time.
        from backend.cv_publish.service import disable_for_application

        disable_for_application(db, application.id)
        tailored_cv = db.scalar(
            select(TailoredCV).where(TailoredCV.application_id == application.id)
        )
        if tailored_cv is not None:
            db.delete(tailored_cv)

    try:
        db.commit()
    except IntegrityError:
        # Only reachable by moving *into* a live status, which the ordering
        # already forbids — `applied` is the lowest rank, so nothing can
        # transition back into the live band. Kept so a future status added in
        # the wrong place fails loudly instead of corrupting the pair.
        db.rollback()
        raise ApplicationServiceError(
            "That consultant already has a live application for this job.",
            status=409,
        ) from None
    db.refresh(application)
    return application, True


# ── Reads ─────────────────────────────────────────────────────────────────


def get_application(db: Session, application_id: str) -> Application | None:
    try:
        key = uuid.UUID(str(application_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.get(Application, key)


def list_applications(
    db: Session,
    *,
    consultant_ids: Sequence[uuid.UUID] | None,
    status: str | None = None,
    job_id: str | None = None,
) -> list[Application]:
    """Applications for `consultant_ids`, newest first.

    `consultant_ids=None` means unrestricted (Admin); an **empty** sequence
    means the actor reaches no Consultant and therefore no Application. The two
    are not the same — that is the same sentinel contract `jobs.service`
    carries, and collapsing them with `or []` is exactly what once hid every
    review marker from the Admin (`docs/handoffs/slice-1-jobs.md`, S1-P1-R2).
    """
    query = select(Application)
    if consultant_ids is not None:
        ids = list(consultant_ids)
        if not ids:
            return []
        query = query.where(Application.consultant_id.in_(ids))
    if status:
        if status not in APPLICATION_STATUS_VALUES:
            raise ApplicationServiceError(f"Unknown application status: {status}")
        query = query.where(Application.status == status)
    if job_id:
        try:
            query = query.where(Application.job_id == uuid.UUID(str(job_id)))
        except (ValueError, AttributeError, TypeError):
            raise ApplicationServiceError("job_id must be a UUID.") from None
    return list(
        db.scalars(query.order_by(Application.created_at.desc(), Application.id))
    )


def live_applications_for_job(
    db: Session, job: Job, consultant_ids: Sequence[uuid.UUID] | None
) -> list[Application]:
    """This Job's **live** Applications, scoped to the Consultants the caller
    may see.

    Live only, and the Job *list* projection (`applied_job_ids`) is live only
    too, so the two Job views cannot disagree — the property the Slice 1 review
    found broken for review markers and which a second pair of projections would
    otherwise be free to break again. A map keyed by Consultant cannot represent
    a pair's history anyway, so making detail richer than list would have built
    the divergence in by construction. The full per-pair history is
    `GET /api/applications?job_id=...`, which is where it belongs.

    Same sentinel contract as `list_applications`: `None` is Admin's "no
    filter", an empty sequence reaches nothing.
    """
    if consultant_ids is not None and not consultant_ids:
        return []
    query = select(Application).where(
        Application.job_id == job.id,
        Application.status.in_(LIVE_APPLICATION_STATUSES),
    )
    if consultant_ids is not None:
        query = query.where(Application.consultant_id.in_(list(consultant_ids)))
    return list(db.scalars(query.order_by(Application.created_at)))


def applied_job_ids(
    db: Session, consultant_ids: Sequence[uuid.UUID] | None
) -> dict[uuid.UUID, dict[uuid.UUID, str]]:
    """`{job_id: {consultant_id: status}}` for the given Consultants.

    One query for a whole Job list page, so rendering N Jobs does not issue N
    Application lookups. Mirrors `jobs.service.reviewed_job_ids`, sentinel
    included, so the Job list and Job detail projections cannot disagree about
    what an actor may see.

    Only *live* Applications are reported. A Job the pair applied to and then
    closed is not "applied" any more for the purpose of offering the Apply
    control, and reporting it as such would disable a legitimate re-Apply.
    """
    if consultant_ids is not None and not consultant_ids:
        return {}
    query = select(
        Application.job_id, Application.consultant_id, Application.status
    ).where(Application.status.in_(LIVE_APPLICATION_STATUSES))
    if consultant_ids is not None:
        query = query.where(Application.consultant_id.in_(list(consultant_ids)))
    out: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    for job_id, consultant_id, status in db.execute(query):
        out.setdefault(job_id, {})[consultant_id] = status
    return out
