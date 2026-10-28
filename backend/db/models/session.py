"""Session and invite models.

Both store a SHA-256 *hash* of the secret token, never the raw token — the raw
value lives only in the user's cookie (session) or invite URL. Lookups hash the
incoming value and match on the hash, so a DB leak does not expose live tokens.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey
from backend.db.models.user import User


class UserSession(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    user: Mapped[User] = relationship()


class Invite(UUIDPrimaryKey, Timestamps, Base):
    """One-time link for a provisioned account to set its first password."""

    __tablename__ = "invites"

    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    user: Mapped[User] = relationship()
