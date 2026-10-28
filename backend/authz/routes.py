"""The authorization policy's own read surface — `/api/me`.

    GET /api/me/consultants   which Consultants the caller may act for

One endpoint, and deliberately so. Every consultant-scoped action in the app —
create a Job, score, Reject, withdraw a Reject, Apply, move an Application —
takes a `consultant_username` and is authorized against
`policy.operable_consultant_ids`. A client has to offer the user a choice of
that context, and until now it guessed at the set from the caller's roles: TA
and Admin both read `GET /api/ta/consultants`, which is a *TA-assignment*
listing and requires an explicit `ta_id` from an Admin. The Admin's picker
therefore came back empty (a swallowed 400) and fell back to the Admin's own
consultant membership, so the pilot Admin+Consultant could act only for
themselves while the API would have allowed every Consultant.

The fix is not a special case for Admin but the removal of the guess: this
returns exactly what the policy says the caller may operate, so the picker and
the access decision are the same set by construction. It is a projection of the
policy, not a new capability — it discloses no Consultant the caller could not
already reach.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_current_user
from backend.authz import policy
from backend.db.models import User
from backend.db.session import get_db

router = APIRouter(prefix="/api/me", tags=["authz"])


@router.get("/consultants")
def my_consultants(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The Consultant contexts this caller may act in.

    A Consultant gets themselves, a TA their assigned Consultants, an Admin
    every Consultant. An account that reaches none — an unrelated TA with no
    assignments — gets an empty list, which is an honest answer rather than an
    error: they are authenticated, they simply have no context to work in.
    """
    consultants = policy.operable_consultants(db, actor)
    return {
        "consultants": [
            {
                "id": str(c.id),
                "username": c.username,
                "full_name": c.full_name,
            }
            for c in consultants
        ],
        "total": len(consultants),
    }
