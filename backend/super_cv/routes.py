"""Authenticated HTTP surface for SuperCV profile data: a lossless read and a
revision/content-hash-checked whole-document save."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_optional_user
from backend.auth.service import normalize_username
from backend.authz.dependencies import check_consultant_access
from backend.db.models import User
from backend.db.session import get_db
from backend.super_cv import service
from backend.super_cv.service import ProfileServiceError

router = APIRouter(prefix="/api/super-cv", tags=["super-cv"])


class SaveProfileRequest(BaseModel):
    """The whole document plus the revision/hash the client loaded it at.

    ``content_hash`` is required here even though ``save_profile`` treats it
    as optional (super-cv.md G6): this route is the only caller with no way
    to know whether the loaded document is stale beyond what the client
    reports, so it cannot be allowed to opt out of the importer-drift guard
    acceptance 1 added. A route caller that omits or nulls it gets 422, not a
    save that silently disables the hash comparison.
    """

    profile: dict
    revision: int
    content_hash: str


def _handle(exc: ProfileServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/{username}")
def get_profile(
    username: str,
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return the lossless profile projection for an authorized consultant."""
    check_consultant_access(db, user, username)
    try:
        return service.load_profile(normalize_username(username)).to_dict()
    except ProfileServiceError as exc:
        raise _handle(exc) from exc


@router.put("/{username}")
def put_profile(
    username: str,
    payload: SaveProfileRequest,
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> dict:
    """Save the whole document, for the same access set as the GET.

    A mismatched revision or content hash — including drift a completed CV
    import left behind without bumping the revision — is refused with 409
    rather than merged (super-cv.md G6).
    """
    check_consultant_access(db, user, username)
    try:
        return service.save_profile(
            normalize_username(username),
            payload.profile,
            expected_revision=payload.revision,
            expected_content_hash=payload.content_hash,
        ).to_dict()
    except ProfileServiceError as exc:
        raise _handle(exc) from exc

