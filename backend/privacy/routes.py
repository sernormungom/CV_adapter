"""Data-subject rights endpoints (Art 15/17/20) — admin-only.

See docs/legal/decisions.md Session 2. Export and erasure act on the
same "consultant data" definition; erasure ordering is documented in
`backend/privacy/service.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend.authz.dependencies import require_roles
from backend.db.models import User, UserRole
from backend.db.session import get_db
from backend.privacy import service
from backend.privacy.service import PrivacyError

router = APIRouter(prefix="/api/privacy", tags=["privacy"])

_admin_only = require_roles(UserRole.admin)


@router.get("/consultants/{username}/export")
def export_consultant(
    username: str,
    actor: User = Depends(_admin_only),
    db: Session = Depends(get_db),
) -> Response:
    try:
        zip_bytes = service.export_consultant(db, username, actor)
    except PrivacyError as exc:
        raise _to_http(exc)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{username}-export.zip"'
        },
    )


@router.delete("/consultants/{username}")
def erase_consultant(
    username: str,
    actor: User = Depends(_admin_only),
    db: Session = Depends(get_db),
) -> dict:
    try:
        counts = service.erase_consultant(db, username, actor)
    except PrivacyError as exc:
        raise _to_http(exc)
    return {"erased": username, "counts": counts}


def _to_http(exc: PrivacyError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))
