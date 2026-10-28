"""FastAPI dependencies for **authentication** — who is calling.

- `get_current_user` — require a valid session; 401 otherwise.
- `get_optional_user` — resolve a session if present, else None (for pages that
  render differently when logged out).

Authorization (may they do it) is not here: it lives in `backend/authz/policy.py`,
with the route wrappers in `backend/authz/dependencies.py` (`require_roles`).
"""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from backend.auth.service import resolve_session
from backend.config import SESSION_COOKIE_NAME
from backend.db.models import User
from backend.db.session import get_db


def get_optional_user(
    request: Request, db: Session = Depends(get_db)
) -> User | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return resolve_session(db, token)

def get_current_user(
    user: User | None = Depends(get_optional_user),
) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user
