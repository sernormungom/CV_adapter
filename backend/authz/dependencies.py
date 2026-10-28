"""FastAPI dependencies that apply `backend.authz.policy`.

Authentication (who is calling) lives in `backend/auth/dependencies.py`.
Authorization (may they) is decided only by `policy`; these wrappers exist to
attach a policy call to a route without putting the rule in the route.

Consultant-scoped access is gated by `config.AUTH_ENFORCED`, read at call time
so it can differ per environment and be monkeypatched in tests. The flag cannot
turn auth off in production — see `backend/config.py`.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from backend import config
from backend.auth.dependencies import get_current_user, get_optional_user
from backend.auth.service import normalize_username
from backend.authz import policy
from backend.db.models import User, UserRole
from backend.db.session import get_db


def require_roles(*roles: UserRole | str):
    """Dependency factory: allow only callers holding at least one of `roles`."""

    def dependency(user: User = Depends(get_current_user)) -> User:
        if not policy.has_any_role(user, *roles):
            raise HTTPException(
                status_code=403, detail="Insufficient permissions"
            )
        return user

    return dependency


def check_consultant_access(
    db: Session, user: User | None, consultant_username: str
) -> None:
    """Raise 401/403/404 if `user` may not access the consultant. No-op when
    enforcement is off. Safe to call inline from handlers (form/body identity)."""
    if not config.AUTH_ENFORCED:
        return
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # An admin may operate on any consultant username — including a data folder
    # with no separate consultant account behind it, which powers the admin
    # "view-as" flow. This is an admin capability, not a role bypass: it does
    # not make the admin a consultant, and `ensure_consultant_access` still
    # governs every non-admin.
    if policy.is_admin(user):
        return
    policy.ensure_consultant_access(
        db, user, normalize_username(consultant_username)
    )


def require_username_access(
    username: str,
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> User | None:
    """For endpoints with a `username` path param."""
    check_consultant_access(db, user, username)
    return user
