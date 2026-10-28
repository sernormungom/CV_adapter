"""HTTP surface for the Job domain — `/api/jobs`.

    POST   /api/jobs                      create at the acting TA's/Admin's Site
    GET    /api/jobs                      list what the caller may reach
    GET    /api/jobs/{job_id}             one Job with its links and reviews
    PATCH  /api/jobs/{job_id}             edit the manual-entry fields
    POST   /api/jobs/{job_id}/close       close it (idempotent)
    GET    /api/jobs/{job_id}/score       advisory score for one Consultant
    POST   /api/jobs/{job_id}/reject      record the Reject marker (idempotent)
    DELETE /api/jobs/{job_id}/reject      withdraw it (idempotent)

Every access decision is a named predicate from `backend/authz/policy.py`; this
module contains no role comparison of its own. An actor who may not reach a Job
gets **404** rather than 403 — see `policy.ensure_job_access` for why.

Two response rules that are easy to lose in a later edit:

- **A Job's facts are shared; its relationships are not.** `_job_detail` returns
  only the Consultant links and review markers the caller may see. There is no
  Link action any more (S1): a Job distributes to every actively-looking
  Consultant at its Site automatically, and `JobDecisionPair` materializes
  lazily at the first Reject/score/Apply on a pair, not at creation.
- **Score and Reject require an open Job** and answer **409** on a closed one
  (`service.ensure_job_is_open`). List, fetch, edit, and close still work — a
  closed Job stays visible with its state explicit.
- **Create answers 200, not 201, for an exact or near duplicate.** `status` is
  `"created"`, `"duplicate"` (exact content match), or `"near_duplicate"`
  (Tier 1's description-only match, `service.find_near_duplicate`) — all three
  carry a usable Job and none of the latter two insert a new row. A matched
  Job the caller may not `policy.can_access_job` (an opted-out Consultant's
  own near-duplicate submission, say) still answers with its `status` and no
  new row, but `"job"` is `null` — the match is never itself an access grant.

There is no Maybe endpoint, no Maybe value, and no translation of one. Apply
lives in its own domain (`POST /api/applications`), because an Application is a
row with a lifecycle rather than a marker; what this module gained in Slice 2 is
the *other* half of the one-live-decision rule — Reject refuses a pair that
already has a live Application, and can be withdrawn so that refusal is not a
dead end (`docs/DECISIONS.md` P-008).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import select, func, or_

from backend.applications import service as applications_service
from backend.auth.dependencies import get_current_user
from backend.auth.service import normalize_username
from backend.authz import policy
from backend.db.models import Job, ReviewDecision, User
from backend.db.session import get_db
from backend.jobs import service
from backend.jobs.service import JobServiceError
from backend.matching.cache import read_score
from backend.db.models.matching import JobConsultantMatch

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ── Schemas ───────────────────────────────────────────────────────────────


class JobWrite(BaseModel):
    """The manual-entry fields. Shared by create and edit so the two cannot
    drift into accepting different shapes of the same Job."""

    title: str
    description: str
    company: str = ""
    location: str = ""
    source_url: str = ""
    close_date: str | None = None


class JobCreate(JobWrite):
    pass  # Site and consultant inputs from clients are ignored.


class RejectRequest(BaseModel):
    # Forbid extras: a legacy client still sending a `reason` must be told it
    # is no longer collected (422), not silently dropped (V3-P-020).
    model_config = ConfigDict(extra="forbid")

    consultant_username: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _consultant_in_scope(db: Session, actor: User, username: str) -> User:
    """The Consultant `actor` is acting for, or 403/404 from the policy."""
    return policy.ensure_consultant_access(db, actor, normalize_username(username))


def _job_detail(db: Session, job: Job, actor: User) -> dict:
    """The Job, plus **only the relationship data `actor` may see**.

    Job *facts* are shared — that is the point of a shared Job. Who else is
    linked to it, and whether they rejected it, are not: they are another
    Consultant's relationship and evaluation data.

    The distinction matters because linking is cheap and unprivileged. Anyone
    may submit a public posting, and if it is already in the system the
    duplicate path silently links them to the very same row — so an unscoped
    projection would let a Consultant discover who else is being put forward for
    a job, and see that they declined it, just by pasting a posting they
    found. Access to the Job is not access to the other relationships on
    it, so the projection is scoped to the same set that decides access.
    """
    visible = policy.operable_consultant_ids(db, actor)   # None = Admin, no filter
    return {
        **job.to_dict(),
        "consultants": [
            {"id": str(c.id), "username": c.username, "full_name": c.full_name}
            for c in service.linked_consultants(db, job)
            if visible is None or c.id in visible
        ],
        "reviews": [
            r.to_dict()
            for r in service.reviews_for_job(db, job)
            if visible is None or r.consultant_id in visible
        ],
        # Slice 2. Scoped by the same set, for the same reason: which
        # Consultants are being put forward for a posting is relationship data,
        # not a Job fact, and the duplicate path hands anyone a link to the row.
        # **Live Applications only**, matching the list projection exactly —
        # the two Job views answer "is this pair live on this posting", and the
        # per-pair history is `GET /api/applications?job_id=...`.
        "applications": [
            a.to_dict()
            for a in applications_service.live_applications_for_job(
                db, job, visible
            )
        ],
    }


def _handle(exc: JobServiceError) -> HTTPException:
    """Map a service error to its HTTP status.

    **Every** route that calls into the service must route its errors through
    here. `JobServiceError` has no global handler, so an unmapped one escapes as
    a 500 with the service's intended status lost — which is exactly what
    happened when `lock_job` gained a 404 for a vanished Job and the close route
    was the one caller without the wrapper.
    `tests/test_jobs.py::test_every_job_route_maps_a_service_error_rather_than_500`
    fails if a route is added or changed without it.
    """
    return HTTPException(status_code=exc.status, detail=str(exc))


# ── Routes ────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_job(
    payload: JobCreate,
    response: Response,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    policy.ensure_job_creator(actor)
    try:
        data = service.build_input(
            title=payload.title,
            description=payload.description,
            company=payload.company,
            location=payload.location,
            source_url=payload.source_url,
            close_date=payload.close_date,
        )
        result = service.create_job(db, data, actor=actor)
    except JobServiceError as exc:
        raise _handle(exc) from exc
    # 200, not 201, for an exact or near duplicate: nothing was created. The
    # caller still gets the Job so the UI can open it instead of dead-ending
    # on an error — but only when they may actually reach that Job. A
    # duplicate/near-duplicate match is a Job the caller did not submit, so
    # `create_job`'s own site-only precheck (`service.find_near_duplicate`)
    # is not itself an access grant: an actor who could not `GET` this Job
    # (an opted-out Consultant probing near-identical text, say) must not
    # receive its id, title, company, or description just because their
    # submission happened to collide with it.
    if result.is_duplicate or result.is_near_duplicate:
        response.status_code = 200
        if not policy.can_access_job(db, actor, result.job):
            return {"status": result.status, "job": None}
    return {"status": result.status, "job": _job_detail(db, result.job, actor)}


@router.get("")
def list_jobs(
    status: str | None = Query(default=None, description="open | closed"),
    consultant_username: str | None = Query(default=None),
    minimum_score: float | None = Query(default=None, ge=0, le=100),
    order: str = Query(default="newest", pattern="^(newest|score)$"),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    reachable = policy.operable_consultant_ids(db, actor)
    selected = _consultant_in_scope(db, actor, consultant_username) if consultant_username else (actor if policy.is_consultant(actor) and not policy.is_admin(actor) else None)
    if status and status not in {"open", "closed"}:
        raise HTTPException(status_code=400, detail="Unknown job status")
    query = select(Job)
    if not policy.is_admin(actor):
        query = query.where(Job.site_id == actor.site_id)
    if selected is not None:
        query = query.where(Job.site_id == selected.site_id)
        if not selected.is_active or not selected.actively_looking:
            query = query.where(Job.status == "closed", Job.id.in_(select(service.JobDecisionPair.job_id).where(service.JobDecisionPair.consultant_id == selected.id)))
    if status:
        query = query.where(Job.status == status)
    results = {}
    if selected is not None:
        for job in db.scalars(query.order_by(Job.id)).all():
            if job.is_open:
                results[job.id] = read_score(db, job, selected)
        query = query.outerjoin(JobConsultantMatch, (JobConsultantMatch.job_id == Job.id) & (JobConsultantMatch.consultant_id == selected.id))
        # A closed Job never gets a job_consultant_matches row (it was never
        # eligible for scoring) and must not be confused with one that failed
        # to score: exclude closed Jobs from the unscored count, and never let
        # minimum_score drop one — it stays listed regardless of any
        # score-based control (G9), the same as before this filter existed.
        unscored = db.scalar(select(func.count()).select_from(
            query.where(Job.status == "open", JobConsultantMatch.match_score.is_(None)).subquery()
        ))
        if minimum_score is not None:
            query = query.where(or_(Job.status != "open", JobConsultantMatch.match_score >= minimum_score))
        if order == "score":
            query = query.order_by(JobConsultantMatch.match_score.desc().nulls_last())
    else:
        unscored = 0
    jobs = list(db.scalars(query.order_by(Job.created_at.desc(), Job.id)))
    # Which Consultants visible to this caller have already reviewed each Job,
    # so the list can show it without a request per row. `reachable` is passed
    # through untouched: it carries the Admin `None` sentinel that both
    # selectors read as "no filter", and coercing it here is what previously
    # made the list and the detail view disagree for an Admin.
    reviews = service.reviewed_job_ids(db, reachable)
    # The same, for live Applications (Slice 2), so a row can show "Applied"
    # and disable a control the server would refuse anyway. Same sentinel, same
    # scoping, one query for the page.
    applications = applications_service.applied_job_ids(db, reachable)
    return {
        "jobs": [
            {
                **job.to_summary(),
                "score": results.get(job.id),
                "reviews": {
                    str(cid): decision
                    for cid, decision in reviews.get(job.id, {}).items()
                },
                "applications": {
                    str(cid): status
                    for cid, status in applications.get(job.id, {}).items()
                },
            }
            for job in jobs
        ],
        "total": len(jobs),
        "unscored_count": unscored,
    }


@router.get("/{job_id}")
def get_job(
    job_id: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = policy.ensure_job_access(db, actor, job_id)
    return _job_detail(db, job, actor)


@router.patch("/{job_id}")
def edit_job(
    job_id: str,
    payload: JobWrite,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = policy.ensure_job_access(db, actor, job_id)
    try:
        data = service.build_input(
            title=payload.title,
            description=payload.description,
            company=payload.company,
            location=payload.location,
            source_url=payload.source_url,
            close_date=payload.close_date,
        )
        service.update_job(db, job, data)
    except JobServiceError as exc:
        raise _handle(exc) from exc
    return _job_detail(db, job, actor)


@router.post("/{job_id}/close")
def close_job(
    job_id: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = policy.ensure_job_access(db, actor, job_id)
    try:
        service.close_job(db, job)
    except JobServiceError as exc:
        raise _handle(exc) from exc
    return _job_detail(db, job, actor)


@router.get("/{job_id}/score")
def score_job(
    job_id: str,
    consultant_username: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The advisory score for one Consultant. Computed live, stored nowhere.

    200 for an authorized caller on an **open** Job, including when the
    Consultant has no usable profile or matching config: the body then carries
    `scored: false` and a `reason`. A missing score is not a reason to hide or
    reject a Job (CLAUDE.md invariant 7).

    A **closed** Job is 409 — it is not an active score candidate. Note the two
    are different answers to different questions: `scored: false` means "we
    cannot advise on this pair", 409 means "this posting is over". Collapsing
    the closed case into `scored: false` would have made a dead vacancy look
    like a data-quality problem.
    """
    job = policy.ensure_job_access(db, actor, job_id)
    consultant = _consultant_in_scope(db, actor, consultant_username)
    policy.ensure_consultant_operates_job(db, job, consultant)
    try:
        service.ensure_job_is_open(job)
    except JobServiceError as exc:
        raise _handle(exc) from exc
    result = read_score(db, job, consultant)
    if result is None:
        from backend.matching.service import _unscored, REASON_OPTED_OUT
        return _unscored(REASON_OPTED_OUT, {"consultant_username": consultant.username, "job_id": str(job.id)})
    return result


@router.post("/{job_id}/reject")
def reject_job(
    job_id: str,
    payload: RejectRequest,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Record this Consultant's Reject. Idempotent — repeating it is a no-op
    that returns the one existing marker. 409 on a closed Job, and 409 when the
    pair already holds a live Application (Slice 2: a pair holds one live
    decision, not two contradictory ones). An existing marker survives closure
    untouched."""
    job = policy.ensure_job_access(db, actor, job_id)
    consultant = _consultant_in_scope(db, actor, payload.consultant_username)
    policy.ensure_consultant_operates_job(db, job, consultant)
    try:
        review, created = service.record_review(
            db,
            job,
            consultant,
            decision=ReviewDecision.reject.value,
            actor=actor,
        )
    except JobServiceError as exc:
        raise _handle(exc) from exc
    return {"created": created, "review": review.to_dict()}


@router.delete("/{job_id}/reject")
def withdraw_reject(
    job_id: str,
    consultant_username: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Withdraw this Consultant's Reject. Idempotent.

    Slice 2 makes Reject and Apply mutually exclusive for a pair, so undoing a
    Reject has to be possible or one mistaken click would bar a Consultant from
    a posting permanently. It is a separate, explicit command rather than a side
    effect of Apply: removing a recorded human decision is itself a human
    decision (CLAUDE.md invariant 9).

    Authorized exactly like recording one — reaching the Job is not enough, the
    caller must also be entitled to act for this Consultant. Allowed on a closed
    Job: closure refuses *new* evaluations, and this removes one.
    """
    job = policy.ensure_job_access(db, actor, job_id)
    consultant = _consultant_in_scope(db, actor, consultant_username)
    policy.ensure_consultant_operates_job(db, job, consultant)
    try:
        return {"withdrawn": service.withdraw_review(db, job, consultant)}
    except JobServiceError as exc:
        raise _handle(exc) from exc
