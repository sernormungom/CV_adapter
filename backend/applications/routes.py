"""HTTP surface for the Application domain — `/api/applications`.

    POST   /api/applications                     Apply (idempotent)
    GET    /api/applications                     list what the caller may reach
    GET    /api/applications/{application_id}    one Application
    POST   /api/applications/{application_id}/status   bounded stage change

Every access decision is a named predicate from `backend/authz/policy.py`; this
module contains no role comparison of its own. An actor who may not reach an
Application gets **404** rather than 403 — see `policy.ensure_application_access`.

Apply takes the same three-step authorization as Reject does, deliberately, so
there is one shape to reason about for "an action taken for a Consultant on a
shared Job":

1. `ensure_job_access` — may the caller reach this Job at all? (404 if not)
2. `ensure_consultant_access` — may they act for this Consultant? (403 if not)
3. `ensure_consultant_operates_job` — does that Consultant belong to this Job's
   Site? (403 if not — S1 replaced the per-Job link with Site membership; a TA
   serving two Consultants at different Sites must not apply on behalf of the
   one outside this Job's Site)

A Consultant applying for someone else fails at step 2; an unrelated TA fails
at step 1 and learns nothing about the Job.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.applications import service
from backend.applications.service import SERVICE_ERRORS
from backend.auth.dependencies import get_current_user
from backend.auth.service import normalize_username
from backend.authz import policy
from backend.db.models import Application, Job, User
from backend.db.session import get_db
# The route layer composes the projection, the way `jobs/routes.py` reaches for
# `applications_service`. The Application *domain* still knows nothing about
# tailored CVs — the dependency runs one way, from the artifact to what it
# belongs to.
from backend.tailored_cv import service as tailored_cv_service

router = APIRouter(prefix="/api/applications", tags=["applications"])


# ── Schemas ───────────────────────────────────────────────────────────────


class ApplyRequest(BaseModel):
    job_id: str
    #: Which Consultant is being applied for. A Consultant applying for
    #: themselves still sends their own username, so the route has one shape.
    consultant_username: str


class StatusRequest(BaseModel):
    status: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _handle(exc: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))


def _job_reference(job: Job | None) -> dict | None:
    """Just enough of the Job to make an Application row legible in a list.

    Not the description: an Application response is about the Application, and
    the full posting body is a Job fact the caller can fetch from `/api/jobs`
    if they want it. Keeping it out also stops this projection from quietly
    becoming a second, differently-authorized way to read Job content.
    """
    if job is None:
        return None
    return {
        "id": str(job.id),
        "title": job.title,
        "company": job.company or "",
        "location": job.location or "",
        "status": job.status,
    }


def _detail(db: Session, application: Application) -> dict:
    """The Application, its Consultant, a small Job reference, and its CV's state.

    The Consultant is always the caller's own permitted relationship — access to
    an Application *is* access to that one Consultant's data — so there is no
    cross-Consultant projection to scope here, unlike the shared Job.

    `tailored_cv` is the artifact's **metadata only** (`TailoredCV.to_dict`):
    whether one exists, whether a generation is running, which revision, and a
    bounded failure reason. A client needs that to offer Generate rather than
    Open, and offering both and letting one fail is worse. The CV itself is
    served by `/api/applications/{id}/cv`, one Application at a time — a list of
    Applications must not become a way to read every tailored CV in it.
    """
    consultant = db.get(User, application.consultant_id)
    tailored = tailored_cv_service.get_for_application(db, application)
    return {
        **application.to_dict(),
        "consultant_username": consultant.username if consultant else "",
        "job": _job_reference(db.get(Job, application.job_id)),
        "tailored_cv": tailored.to_dict() if tailored is not None else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def apply(
    payload: ApplyRequest,
    response: Response,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Apply a Consultant to a Job. Idempotent under retry and double-click.

    201 when an Application was created, **200 when a live one already existed**
    — nothing was created, and the caller still gets the Application so the UI
    can show it rather than dead-ending on an error. Same convention as the
    duplicate path on `POST /api/jobs`.
    """
    job = policy.ensure_job_access(db, actor, payload.job_id)
    consultant = policy.ensure_consultant_access(
        db, actor, normalize_username(payload.consultant_username)
    )
    policy.ensure_consultant_operates_job(db, job, consultant)
    try:
        application, created = service.apply(db, job, consultant, actor=actor)
    except SERVICE_ERRORS as exc:
        raise _handle(exc) from exc
    if not created:
        response.status_code = 200
    return {"created": created, "application": _detail(db, application)}


@router.get("")
def list_applications(
    status: str | None = Query(
        default=None, description="applied | submitted | closed | withdrawn"
    ),
    job_id: str | None = Query(default=None),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # `reachable` carries the Admin `None` sentinel meaning "no filter" and is
    # passed through untouched. Coercing it with `or []` is what once turned
    # "show the Admin everything" into "show the Admin nothing" on the Job list
    # (docs/handoffs/slice-1-jobs.md, S1-P1-R2).
    reachable = policy.operable_consultant_ids(db, actor)
    try:
        applications = service.list_applications(
            db, consultant_ids=reachable, status=status, job_id=job_id
        )
    except SERVICE_ERRORS as exc:
        raise _handle(exc) from exc
    applications = [a for a in applications if policy.can_access_application(db, actor, a)]
    return {
        "applications": [_detail(db, a) for a in applications],
        "total": len(applications),
    }


@router.get("/{application_id}")
def get_application(
    application_id: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    application = policy.ensure_application_access(db, actor, application_id)
    return _detail(db, application)


@router.post("/{application_id}/status")
def set_status(
    application_id: str,
    payload: StatusRequest,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Move the Application to a later stage.

    Enforced in the service, not here: status only moves forward, re-requesting
    the current status is a no-op, and a terminal Application does not move at
    all. The route contributes authorization and nothing else.

    A closed Job does **not** block this. Closing a posting refuses new
    evaluations of it; an Application already in flight still has to be closed
    out honestly, and refusing that would strand it live forever.
    """
    application = policy.ensure_application_access(db, actor, application_id)
    try:
        application, changed = service.transition(
            db, application, status=payload.status, actor=actor
        )
    except SERVICE_ERRORS as exc:
        raise _handle(exc) from exc
    return {"changed": changed, "application": _detail(db, application)}
