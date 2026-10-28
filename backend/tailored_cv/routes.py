"""HTTP surface for the tailored CV — `/api/applications/{id}/cv`.

    POST /api/applications/{application_id}/cv/generate   enqueue (idempotent)
    GET  /api/applications/{application_id}/cv            the artifact + state
    PUT  /api/applications/{application_id}/cv            save an edit
    GET  /api/applications/{application_id}/cv/status     durable progress

The artifact is addressed as a sub-resource of its Application because that is
what it belongs to (P-009), and it gives every route here one authorization
call: `policy.ensure_application_access`, which answers **404** rather than 403
for an actor who may not reach the Application — an Application's existence is
itself content, and so is the fact that a CV was tailored for it. Consultant
self, related TA, unrelated TA, and Admin therefore behave here exactly as they
do on `/api/applications` itself, with no second rule to keep in step.

It is a separate router from `backend/applications/routes.py`, sharing the
prefix, so the tailored-CV domain keeps its own module boundary instead of
growing inside the Application domain. Generation is **202 Accepted**: the work
is durable and the response is a run to poll, never a connection held open —
the whole point of P-006, given a generation can outlast the verified 60-second
edge.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_current_user
from backend.authz import policy
from backend.db.models import User
from backend.db.session import get_db
from backend.tailored_cv import service
from backend.tailored_cv.service import SERVICE_ERRORS

router = APIRouter(prefix="/api/applications", tags=["tailored-cv"])


# ── Schemas ───────────────────────────────────────────────────────────────


class TailoredCVWrite(BaseModel):
    state: dict
    #: The revision the client loaded. Required, and not defaulted: a default
    #: would turn "this client has not thought about concurrency" into
    #: "overwrite whatever is there".
    expected_revision: int = Field(ge=0)
    #: Which artifact the client believes it is editing. The URL says which
    #: Application; this says which CV, and a mismatch is refused — see
    #: `service.save_state` for the case that makes it necessary.
    tailored_cv_id: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _handle(exc: Exception) -> HTTPException:
    """Map a service error to its HTTP status.

    Every route below routes its service errors through here. These exception
    types have no global handler, so an unmapped one escapes as a 500 with the
    intended status lost — the defect
    `tests/test_jobs.py::test_every_job_route_maps_a_service_error_rather_than_500`
    was added for, and which
    `tests/test_tailored_cv.py::test_every_tailored_cv_route_maps_a_service_error_rather_than_500`
    holds here.
    """
    return HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))


def _require_artifact(db: Session, application):
    """This Application's tailored CV, or 404.

    **Every** service call a route makes goes through the error boundary,
    including this lookup. Leaving it outside was a real defect the guard test
    caught: `get_for_application` can raise a service error, and an unmapped one
    escapes as a 500 with the intended status lost — the same shape as the Job
    close route's escaping `JobServiceError` (`docs/handoffs/slice-2-applications.md`,
    S2-P1-R3).
    """
    try:
        tailored = service.get_for_application(db, application)
    except SERVICE_ERRORS as exc:
        raise _handle(exc) from exc
    if tailored is None:
        raise HTTPException(status_code=404, detail=service.NO_CONTENT_MESSAGE)
    return tailored


def _envelope(db: Session, tailored, *, include_state: bool) -> dict:
    """The artifact's metadata, its run (for polling), and optionally the CV."""
    run = service.current_run(db, tailored)
    body = {
        "tailored_cv": tailored.to_dict(),
        "run": run.to_status_dict() if run is not None else None,
    }
    if include_state:
        body["artifact"] = (
            service.artifact(tailored).to_dict() if tailored.has_content else None
        )
    return body


# ── Routes ────────────────────────────────────────────────────────────────


@router.post("/{application_id}/cv/generate", status_code=202)
def generate(
    application_id: str,
    response: Response,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Accept a generation durably. **202**, with a run to poll.

    202 when this request started a generation, **200 when an identical one was
    already in flight** — nothing new was created, and the caller still gets the
    run so the UI can poll it rather than dead-ending on an error. Same
    convention as the duplicate paths on `POST /api/jobs` and
    `POST /api/applications`.
    """
    application = policy.ensure_application_access(db, actor, application_id)
    try:
        tailored, run, started = service.request_generation(
            db, application, actor=actor
        )
    except SERVICE_ERRORS as exc:
        raise _handle(exc) from exc
    if not started:
        response.status_code = 200
    return {"started": started, **_envelope(db, tailored, include_state=False)}


@router.get("/{application_id}/cv")
def get_tailored_cv(
    application_id: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The tailored CV for this Application, with its state when one exists.

    404 when nothing has been generated yet — the Application is reachable, the
    artifact simply does not exist, and saying so lets the client offer Generate
    instead of rendering an empty CV as if it were a real one.
    """
    application = policy.ensure_application_access(db, actor, application_id)
    return _envelope(db, _require_artifact(db, application), include_state=True)


@router.get("/{application_id}/cv/status")
def get_generation_status(
    application_id: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Durable progress for the poller. Never the CV content.

    Separate from the artifact route so a client polling every couple of seconds
    is not shipped the whole document each time — and so a page that only needs
    "is it done yet" never asks for CV content at all.
    """
    application = policy.ensure_application_access(db, actor, application_id)
    return _envelope(db, _require_artifact(db, application), include_state=False)


@router.put("/{application_id}/cv")
def save_tailored_cv(
    application_id: str,
    payload: TailoredCVWrite,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Save an edited tailored CV.

    Enforced in the service, not here: stale, cross-artifact, and
    mid-generation writes are refused with 409. The route contributes
    authorization and nothing else.
    """
    application = policy.ensure_application_access(db, actor, application_id)
    tailored = _require_artifact(db, application)
    try:
        tailored = service.save_state(
            db,
            tailored,
            state=payload.state,
            expected_revision=payload.expected_revision,
            expected_id=payload.tailored_cv_id,
            actor=actor,
        )
    except SERVICE_ERRORS as exc:
        raise _handle(exc) from exc
    return _envelope(db, tailored, include_state=True)
