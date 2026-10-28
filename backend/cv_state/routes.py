"""The base half of the editor's explicit mode routing — `/api/cv/base`.

    GET /api/cv/base/{username}   load the consultant's own working CV
    PUT /api/cv/base/{username}   save it, stating the revision it is based on

The Application half is `/api/applications/{id}/cv`
(`backend/tailored_cv/routes.py`). **There is no endpoint that serves both.**
That is the point of P-009's "explicit editor modes": a client picks an address,
not a payload flag, so a tailored save cannot reach base storage by getting one
field wrong. The two return the same `CVArtifact` envelope, so the editor still
has one loader and one saver.

This replaces `GET/POST /api/profiles/{username}/cv-state`, which lived in
`backend/main.py` — CV business logic in the module the plan (§5) says composes
routers and lifecycle only — and accepted any write unconditionally.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth.service import normalize_username
from backend.authz.dependencies import require_username_access
from backend.cv_state import service
from backend.cv_state.service import CVStateError

router = APIRouter(prefix="/api/cv", tags=["cv-state"])


class BaseCVWrite(BaseModel):
    state: dict
    #: The revision the client loaded. Required and unforgiving: defaulting it
    #: would turn "I did not think about concurrency" into "overwrite whatever
    #: is there", which is the behaviour this route exists to end.
    expected_revision: int = Field(ge=0)


def _handle(exc: CVStateError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/base/{username}", dependencies=[Depends(require_username_access)])
def get_base_cv(username: str) -> dict:
    """The consultant's base CV plus its revision and content hash.

    Returns an empty state at revision 0 for a consultant who has never saved
    one, rather than 404: the editor's job is to let them create it.
    """
    try:
        return service.load_base(normalize_username(username)).to_dict()
    except CVStateError as exc:
        raise _handle(exc) from exc


@router.put("/base/{username}", dependencies=[Depends(require_username_access)])
def save_base_cv(username: str, payload: BaseCVWrite) -> dict:
    """Save the base CV. 409 if the revision is stale.

    A stale write is refused rather than merged: the editor holds a whole
    document, so "merging" would mean picking a winner per field with no way to
    tell an intentional deletion from an absent one.
    """
    try:
        artifact = service.save_base(
            normalize_username(username),
            payload.state,
            expected_revision=payload.expected_revision,
        )
    except CVStateError as exc:
        raise _handle(exc) from exc
    return artifact.to_dict()
