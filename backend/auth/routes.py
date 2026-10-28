"""Auth HTTP endpoints: login, logout, me, invite acceptance, provisioning."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import service
from backend.auth.dependencies import get_current_user
from backend.auth.service import AuthError
from backend.authz import policy
from backend.config import (
    APP_BASE_URL,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_HOURS,
)
from backend.db.models import User, UserRole
from backend.db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Where each role lands after login. Keyed by `User.primary_role`, which is a
# display concept — a multi-role account lands on its highest-precedence
# dashboard and can navigate to the others. Not an access decision: the server
# still authorizes every route independently.
_LANDING = {
    UserRole.admin.value: "/html/admin-dashboard.html",
    UserRole.ta.value: "/html/ta-dashboard.html",
    UserRole.consultant.value: "/html/cv-importer.html",
}


def _landing_for(user: User) -> str:
    return _LANDING.get(user.primary_role, "/html/cv-importer.html")


# ── Schemas ───────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    identifier: str  # username or email
    password: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


class CreateUserRequest(BaseModel):
    email: str
    # One role, or several for a multi-role account. `role` is the convenient
    # single-role form; `roles` wins if both are sent.
    role: str | None = None
    roles: list[str] | None = None
    full_name: str | None = None
    city: str | None = None
    username: str | None = None
    site_id: uuid.UUID | None = None

    def requested_roles(self) -> list[str]:
        if self.roles:
            return list(dict.fromkeys(self.roles))
        return [self.role] if self.role else []


class UserOut(BaseModel):
    id: str
    email: str
    username: str
    full_name: str | None
    # Every role held — the authoritative field. Clients deciding what to show
    # must test membership here, not compare `primary_role`.
    roles: list[str]
    # Display/landing hint only (highest-precedence role held).
    primary_role: str
    city: str | None
    site_id: str
    is_active: bool

    @classmethod
    def of(cls, u: User) -> "UserOut":
        return cls(
            id=str(u.id),
            email=u.email,
            username=u.username,
            full_name=u.full_name,
            roles=sorted(u.roles),
            primary_role=u.primary_role,
            city=u.city,
            is_active=u.is_active,
            site_id=str(u.site_id),
        )


class LoginResponse(BaseModel):
    user: UserOut
    landing: str


class CreateUserResponse(BaseModel):
    user: UserOut
    invite_url: str


class ConsultantSummary(BaseModel):
    username: str
    email: str
    full_name: str | None
    city: str | None


# ── Helpers ───────────────────────────────────────────────────────────────

def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
        path="/",
    )


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    user = service.authenticate(db, body.identifier, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    raw = service.create_session(db, user)
    _set_session_cookie(response, raw)
    return LoginResponse(user=UserOut.of(user), landing=_landing_for(user))


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    # Idempotent: revoke the session token if present, always clear the cookie.
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        service.revoke_session(db, token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.of(user)


@router.post("/invite/accept", response_model=LoginResponse)
def accept_invite(
    body: AcceptInviteRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    try:
        user = service.accept_invite(db, body.token, body.password)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    # Auto-login on successful acceptance.
    raw = service.create_session(db, user)
    _set_session_cookie(response, raw)
    return LoginResponse(user=UserOut.of(user), landing=_landing_for(user))


@router.get("/consultants", response_model=list[ConsultantSummary])
def list_consultants(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ConsultantSummary]:
    """All consultant accounts, for the admin view-as selector."""
    policy.ensure_admin(actor)
    users = policy.users_with_role(db, UserRole.consultant)
    return [
        ConsultantSummary(
            username=u.username,
            email=u.email,
            full_name=u.full_name,
            city=u.city,
        )
        for u in users
    ]


@router.post("/users", response_model=CreateUserResponse, status_code=201)
def create_user(
    body: CreateUserRequest,
    creator: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CreateUserResponse:
    requested = body.requested_roles()
    policy.ensure_can_create_roles(creator, requested)
    try:
        user, raw_invite = service.create_user(
            db,
            email=body.email,
            roles=requested,
            full_name=body.full_name,
            city=body.city,
            username=body.username,
            site_id=body.site_id,
            created_by=creator,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    invite_url = f"{APP_BASE_URL}/html/setup-password.html?token={raw_invite}"
    return CreateUserResponse(user=UserOut.of(user), invite_url=invite_url)


# ── Admin oversight ──────────────────────────────────────────────────────

@router.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_user_sessions(
    user_id: uuid.UUID,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Force-expire every live session for a user. Admin only.

    Incident-response containment (`docs/legal/breach-runbook.md` §2 step 1):
    use when a laptop/device is reported lost or stolen and its owner's cloud
    session might still be valid on it (e.g. saved in a browser).

    Since STAB-2 an admin password reset (`make reset-password`) revokes
    sessions too, so this route is no longer the only way to close them. It is
    still the right tool when the *credential* is not in question and only the
    open session is: it needs no new password to be chosen, communicated out of
    band, and typed in again by a user who did nothing wrong.
    """
    policy.ensure_admin(actor)
    count = service.revoke_all_sessions_for_user(db, user_id)
    return {"user_id": str(user_id), "sessions_revoked": count}


@router.get("/sites")
def list_sites(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    policy.ensure_ta_or_admin(actor)
    from backend.sites.service import active_sites
    return {"sites": [{"id": str(s.id), "code": s.code, "name": s.name} for s in active_sites(db)]}
