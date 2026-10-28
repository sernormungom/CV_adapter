"""HTTP surface for durable CV import — `/api/profiles/{username}/cv-import`.

    POST /api/profiles/{username}/cv-import           upload + enqueue (idempotent)
    GET  /api/profiles/{username}/cv-import/status     durable progress
    GET  /api/profiles/{username}/cv-import/preview    the current profile, as
                                                        editor form fields

Authorized the same way the other `/api/profiles/{username}/...` routes are
(`require_username_access`, alongside it in `backend/main.py`'s photo and
accepted-positions routes, and `backend/cv_state/routes.py`'s base CV): self,
the consultant's assigned TA, or an Admin (including one acting on a profile
directory with no separate consultant account). A username is not secret the
way an Application's existence is, so this uses the same 403-for-unauthorized
convention as the base CV rather than the Job/Application 404-secrecy one.

Accepting the upload is **202 Accepted** — the work is durable and the response
is a run to poll, never a connection held open for the extraction/LLM work
behind it (`docs/evidence/m1-timeout-inventory.md`). `preview` is a separate,
content-carrying read so the frequent status poll never ships the whole
profile on every tick — the same split `tailored_cv/routes.py` makes between
`/cv/status` and `/cv`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_current_user
from backend.authz.dependencies import require_username_access
from backend.auth.service import normalize_username
from backend.cv_importer import service
from backend.cv_importer.preview import PreviewError, load_preview
from backend.cv_importer.service import SERVICE_ERRORS
from backend.db.models import User
from backend.db.session import get_db

router = APIRouter(prefix="/api/profiles", tags=["cv-import"])


def _handle(exc: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))


def _envelope(db: Session, cv_import) -> dict:
    run = service.current_run(db, cv_import)
    return {
        "cv_import": cv_import.to_dict(),
        "run": run.to_status_dict() if run is not None else None,
    }


@router.post(
    "/{username}/cv-import",
    status_code=202,
    dependencies=[Depends(require_username_access)],
)
async def import_cv(
    username: str,
    response: Response,
    file: UploadFile = File(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Accept a CV upload durably. **202**, with a run to poll.

    202 when this request started an import, **200 when an identical upload was
    already in flight** — the caller still gets the run so the UI can poll it
    rather than dead-ending on an error. Same convention as
    `POST /api/applications/{id}/cv/generate`.
    """
    uname = normalize_username(username)
    data = await file.read()
    try:
        source_path, input_hash = service.save_upload(
            uname,
            filename=file.filename or "cv",
            content_type=file.content_type or "",
            data=data,
        )
    except SERVICE_ERRORS as exc:
        raise _handle(exc) from exc

    try:
        cv_import, run, started = service.request_import(
            db,
            uname,
            actor=actor,
            source_path=source_path,
            source_filename=file.filename or "cv",
            content_type=file.content_type or "",
            input_hash=input_hash,
        )
    except SERVICE_ERRORS as exc:
        service.delete_upload(source_path)
        raise _handle(exc) from exc
    except Exception:
        # `S4-P1-R1` finding 2: an operational failure here (e.g. the
        # `enqueue` commit itself failing) leaves no durable row/run for any
        # handler to ever find and clean up through — this request's own
        # upload is the only reference to the file, so it has to be deleted
        # right here, before the original error is allowed to propagate
        # unmasked (never converted, never swallowed).
        service.delete_upload(source_path)
        raise
    if not started:
        response.status_code = 200
    return {"started": started, **_envelope(db, cv_import)}


@router.get(
    "/{username}/cv-import/status",
    dependencies=[Depends(require_username_access)],
)
def get_import_status(username: str, db: Session = Depends(get_db)) -> dict:
    """Durable progress for the poller. Never profile content."""
    uname = normalize_username(username)
    cv_import = service.get_for_consultant(db, uname)
    if cv_import is None:
        raise HTTPException(status_code=404, detail="No import has been started yet.")
    return _envelope(db, cv_import)


@router.get(
    "/{username}/cv-import/preview",
    dependencies=[Depends(require_username_access)],
)
def get_import_preview(username: str) -> dict:
    """The consultant's current `profile.md`, as editor form fields.

    Reads fresh from disk on every call rather than from any workflow-adjacent
    row — `cv_imports` deliberately carries no profile content
    (`backend/db/models/cv_import.py`). 404 when no profile exists yet.
    """
    uname = normalize_username(username)
    try:
        form_data = load_preview(uname)
    except PreviewError as exc:
        raise _handle(exc) from exc
    if form_data is None:
        raise HTTPException(status_code=404, detail="No profile has been imported yet.")
    return {"form_data": form_data}
