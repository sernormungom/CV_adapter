"""Notification inbox endpoints.

A logged-in user reads and manages their own notifications. No role gate beyond
authentication: recipients are scoped by `user_id == current_user.id`, so a TA
sees their notifications, an admin theirs.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_current_user
from backend.db.models import User
from backend.db.session import get_db
from backend.notifications import service

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    items = service.list_for_user(
        db, user.id, unread_only=unread_only, limit=limit
    )
    return {
        "notifications": [n.to_dict() for n in items],
        "unread": service.unread_count(db, user.id),
    }


@router.get("/unread-count")
def unread_count(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return {"unread": service.unread_count(db, user.id)}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        nid = uuid.UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid notification id")
    if not service.mark_read(db, user.id, nid):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"read": notification_id}


@router.post("/read-all")
def mark_all_read(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return {"marked": service.mark_all_read(db, user.id)}
