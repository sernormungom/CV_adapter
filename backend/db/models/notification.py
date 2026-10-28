"""TA/admin-facing notifications.

The row itself IS the in-app notification (always written, shown in the
dashboard inbox). Email/SMS are best-effort external channels recorded in
`channels_sent`. `dedupe_key` lets a writer avoid re-notifying about the same
(consultant, job): the service skips creation when a row with the same
(user_id, dedupe_key) already exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey

# JSONB on Postgres, plain JSON on SQLite (tests).
_JSON = JSONB().with_variant(JSON(), "sqlite")


class Notification(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # Fast dedupe lookups by (recipient, dedupe_key).
        Index("ix_notifications_user_dedupe", "user_id", "dedupe_key"),
    )

    # Recipient. Usually a TA; an admin for held-config alerts.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Event type: match_found | cv_ready | position_accepted | cv_viewed | config_hold
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    # What the notification is about (nullable for non-consultant/non-job events).
    consultant_username: Mapped[str | None] = mapped_column(String(100))
    job_id: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    # Type-specific structured data (score, job title, cv url, viewer ip, ...).
    payload: Mapped[dict] = mapped_column(_JSON, default=dict, nullable=False)
    # Channels actually dispatched, e.g. ["inapp", "email", "sms"].
    channels_sent: Mapped[list] = mapped_column(_JSON, default=list, nullable=False)
    # Idempotency key for writers; null = no dedupe.
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def read(self) -> bool:
        return self.read_at is not None

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "type": self.type,
            "consultant_username": self.consultant_username,
            "job_id": self.job_id,
            "title": self.title,
            "body": self.body,
            "payload": self.payload or {},
            "channels_sent": self.channels_sent or [],
            "read": self.read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
