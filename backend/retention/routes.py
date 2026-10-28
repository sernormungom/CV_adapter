"""Admin-only endpoint to run the retention purges on demand.

`backend/main.py` also runs the same purges automatically — a daily lifespan
background task (`_retention_purge_loop`) fires `run_retention_purges` at
startup and every 24h. This endpoint is for running one immediately rather
than waiting for the next scheduled cycle.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.authz.dependencies import require_roles
from backend.db.models import User, UserRole
from backend.db.session import get_db
from backend.retention.service import run_retention_purges

router = APIRouter(prefix="/api/admin/retention", tags=["retention"])

_admin_only = require_roles(UserRole.admin)


@router.post("/run")
def run_purges(
    actor: User = Depends(_admin_only),
    db: Session = Depends(get_db),
) -> dict:
    return run_retention_purges(db)
