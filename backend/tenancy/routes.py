"""TA-facing tenancy management: consultant assignments.

All endpoints require the `ta` or `admin` role. A TA operates on their own
tenancy; an admin must name the target TA via `?ta_id=`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.auth import service as auth_service
from backend.authz.dependencies import require_roles
from backend.auth.service import AuthError, normalize_username
from backend.config import APP_BASE_URL
from backend.db.models import TAConsultant, User, UserRole
from backend.db.session import get_db
from backend.authz import policy as authz

router = APIRouter(prefix="/api/ta", tags=["tenancy"])

_ta_or_admin = require_roles(UserRole.ta, UserRole.admin)


def _resolve_ta_id(db: Session, actor: User, ta_id: str | None) -> uuid.UUID:
    """A TA acts on themselves; an admin must pass an explicit ta_id.

    An admin-supplied id is verified to name an account that holds the TA role.
    Parsing it as a UUID is not enough: every assignment written under this id
    becomes an authorization fact (a TA reaches a consultant *through* the
    relationship), so a well-formed id pointing at a consultant would grant
    that consultant TA-shaped reach and misroute their notifications.
    """
    if authz.is_admin(actor):
        if not ta_id:
            raise HTTPException(status_code=400, detail="admin must specify ta_id")
        try:
            parsed = uuid.UUID(ta_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid ta_id")
        if not authz.holds_ta_role(db, parsed):
            raise HTTPException(
                status_code=404,
                detail="No Talent Advisor account with that ta_id",
            )
        return parsed
    return actor.id


# ── Schemas ───────────────────────────────────────────────────────────────

class ConsultantOut(BaseModel):
    id: str
    username: str
    email: str
    full_name: str | None
    city: str | None
    actively_looking: bool


class AddConsultantRequest(BaseModel):
    email: str


class SetLookingRequest(BaseModel):
    actively_looking: bool


# ── Consultant assignments ────────────────────────────────────────────────

@router.get("/consultants")
def list_consultants(
    ta_id: str | None = Query(default=None),
    actor: User = Depends(_ta_or_admin),
    db: Session = Depends(get_db),
) -> dict:
    target = _resolve_ta_id(db, actor, ta_id)
    consultants = authz.consultants_for_ta(db, target)
    return {
        "consultants": [
            ConsultantOut(
                id=str(c.id),
                username=c.username,
                email=c.email,
                full_name=c.full_name,
                city=c.city,
                actively_looking=c.actively_looking,
            ).model_dump()
            for c in consultants
        ],
        "total": len(consultants),
    }


@router.post("/consultants", status_code=201)
def add_consultant(
    body: AddConsultantRequest,
    ta_id: str | None = Query(default=None),
    actor: User = Depends(_ta_or_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Add a consultant to the TA's list by email.

    If the email already belongs to a consultant account, assign them to the TA.
    Otherwise create the account, link it to the TA, and return an invite link.
    """
    target = _resolve_ta_id(db, actor, ta_id)
    email_norm = body.email.strip().lower()
    if not email_norm:
        raise HTTPException(status_code=400, detail="Email is required")

    username_norm = normalize_username(email_norm)
    existing = db.scalar(
        select(User).where(
            or_(User.email == email_norm, User.username == username_norm)
        )
    )

    if existing is not None:
        # An account holding the consultant role can be advised — including one
        # that also holds admin. A TA-only account cannot be someone's
        # consultant; a TA who *also* holds the consultant role can.
        if authz.is_ta(existing) and not authz.is_consultant(existing):
            raise HTTPException(
                status_code=409,
                detail="That email belongs to a Talent Advisor account, which can't be added as a consultant.",
            )
        authz.ensure_same_site_assignment(db.get(User, target), existing)
        already = authz.is_assigned(db, target, existing.id)
        if not already:
            db.add(TAConsultant(ta_id=target, consultant_id=existing.id))
            db.commit()
        return {
            "mode": "assigned",
            "already": already,
            "username": existing.username,
            "email": existing.email,
            "full_name": existing.full_name,
        }

    # No account yet → provision + invite, linked to the target TA.
    try:
        user, raw_invite = auth_service.create_user(
            db,
            email=email_norm,
            role=UserRole.consultant.value,
            created_by=actor,
            ta_id=target,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    return {
        "mode": "invited",
        "username": user.username,
        "email": user.email,
        "invite_url": f"{APP_BASE_URL}/html/setup-password.html?token={raw_invite}",
    }


@router.post("/consultants/{username}", status_code=201)
def assign_consultant(
    username: str,
    ta_id: str | None = Query(default=None),
    actor: User = Depends(_ta_or_admin),
    db: Session = Depends(get_db),
) -> dict:
    target = _resolve_ta_id(db, actor, ta_id)
    advisee = authz.get_advisee_by_username(db, username)
    if advisee is None:
        raise HTTPException(status_code=404, detail="Consultant not found")
    authz.ensure_same_site_assignment(db.get(User, target), advisee)
    if authz.is_assigned(db, target, advisee.id):
        return {"assigned": username, "already": True}
    db.add(TAConsultant(ta_id=target, consultant_id=advisee.id))
    db.commit()
    return {"assigned": username, "already": False}


@router.post("/consultants/{username}/looking")
def set_consultant_looking(
    username: str,
    body: SetLookingRequest,
    ta_id: str | None = Query(default=None),
    actor: User = Depends(_ta_or_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Toggle a consultant's "actively looking" flag.

    Only the assigned TA (or an admin) may flip it.
    """
    target = _resolve_ta_id(db, actor, ta_id)
    advisee = authz.get_advisee_by_username(db, username)
    if advisee is None:
        raise HTTPException(status_code=404, detail="Consultant not found")
    if not authz.is_assigned(db, target, advisee.id):
        raise HTTPException(status_code=404, detail="Assignment not found")
    from backend.db.models.matching import JobConsultantMatch
    from sqlalchemy import delete
    db.refresh(advisee, with_for_update=True)
    advisee.actively_looking = body.actively_looking
    if not body.actively_looking:
        db.execute(delete(JobConsultantMatch).where(JobConsultantMatch.consultant_id == advisee.id))
    db.commit()
    return {"username": username, "actively_looking": advisee.actively_looking}


@router.delete("/consultants/{username}")
def unassign_consultant(
    username: str,
    ta_id: str | None = Query(default=None),
    actor: User = Depends(_ta_or_admin),
    db: Session = Depends(get_db),
) -> dict:
    target = _resolve_ta_id(db, actor, ta_id)
    # Role-agnostic: an assigned advisee may be a consultant or an admin (dual
    # role), so look them up by username rather than via the consultant-only helper.
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not authz.is_assigned(db, target, user.id):
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.execute(
        TAConsultant.__table__.delete().where(
            (TAConsultant.ta_id == target)
            & (TAConsultant.consultant_id == user.id)
        )
    )
    db.commit()
    return {"unassigned": username}
