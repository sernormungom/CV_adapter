"""Notification store + dispatch.

Module-level functions. Writing a
notification always persists the in-app row; `dispatch` additionally fans out to
email/SMS best-effort. `notify` does both in one call. Dedupe is enforced here
(not by a DB constraint) for dialect portability: a writer passes a `dedupe_key`
and `create` returns None when one already exists for that recipient.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.models.notification import Notification
from backend.notifications import channels

# Notification type constants.
MATCH_FOUND = "match_found"
CV_READY = "cv_ready"
POSITION_ACCEPTED = "position_accepted"
CV_VIEWED = "cv_viewed"
CONFIG_HOLD = "config_hold"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def exists_with_dedupe_key(
    db: Session, user_id: uuid.UUID, dedupe_key: str
) -> bool:
    return (
        db.scalar(
            select(Notification.id).where(
                Notification.user_id == user_id,
                Notification.dedupe_key == dedupe_key,
            )
        )
        is not None
    )


def create(
    db: Session,
    *,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str | None = None,
    consultant_username: str | None = None,
    job_id: str | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> Notification | None:
    """Persist an in-app notification. Returns None if deduped."""
    if dedupe_key and exists_with_dedupe_key(db, user_id, dedupe_key):
        return None
    n = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        consultant_username=consultant_username,
        job_id=job_id,
        payload=payload or {},
        channels_sent=["inapp"],
        dedupe_key=dedupe_key,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def dispatch(
    db: Session,
    notification: Notification,
    *,
    email_to: str | None = None,
    sms_to: str | None = None,
) -> list[str]:
    """Fan out to external channels best-effort; record what was sent."""
    sent = list(notification.channels_sent or [])
    body = notification.body or notification.title
    if email_to and channels.send_email(
        to=email_to, subject=notification.title, body=body
    ):
        sent.append("email")
    if sms_to and channels.send_sms(to=sms_to, body=notification.title):
        sent.append("sms")
    notification.channels_sent = sent
    db.commit()
    return sent


def notify(
    db: Session,
    *,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str | None = None,
    consultant_username: str | None = None,
    job_id: str | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
    email_to: str | None = None,
    sms_to: str | None = None,
) -> Notification | None:
    """Create the in-app notification and dispatch external channels in one call."""
    n = create(
        db,
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        consultant_username=consultant_username,
        job_id=job_id,
        payload=payload,
        dedupe_key=dedupe_key,
    )
    if n is None:
        return None
    if email_to or sms_to:
        dispatch(db, n, email_to=email_to, sms_to=sms_to)
    return n


def list_for_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> list[Notification]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
    return list(db.scalars(stmt))


def unread_count(db: Session, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )


def mark_read(
    db: Session, user_id: uuid.UUID, notification_id: uuid.UUID
) -> bool:
    n = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    if n is None:
        return False
    if n.read_at is None:
        n.read_at = _now()
        db.commit()
    return True


def mark_all_read(db: Session, user_id: uuid.UUID) -> int:
    rows = db.scalars(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    ).all()
    now = _now()
    for n in rows:
        n.read_at = now
    db.commit()
    return len(rows)
