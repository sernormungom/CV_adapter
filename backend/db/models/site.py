"""The single-Site V2 Pilot schema.

Site records deployment/configuration intent.  They are deliberately absent from
authorization: P-005 keeps the pilot single-Site until the dedicated promotion
adds Site-aware ownership invariants and negative tests.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey


class Site(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "sites"

    code: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="site")

