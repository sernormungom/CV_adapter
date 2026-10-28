"""Minimal audit trail for privacy actions (GDPR Art 5(2) accountability).

Records *that* an erasure/export happened, who did it, and when — never the
erased content itself. `target_username_hash` is a truncated SHA-256 (see
`backend.privacy.service._username_hash`), not reversible to the original
username.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey


class AuditLog(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_username_hash: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True
    )
