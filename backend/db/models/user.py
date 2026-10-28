"""User, role-membership, and tenancy models.

A single `users` table holds every account. Role membership is a **many-to-many**
association (`user_roles`), not a scalar column: V2 Pilot needs an account that
is genuinely both Admin and Consultant (`docs/DECISIONS.md` P-004), and the V1
single-`role` column forced that case to be faked with an
"admin counts as a consultant" special case in the authorization path. Holding
two roles is now an ordinary membership fact, so authorization asks
`user.has_role(...)` and never needs a bypass.

The TA↔consultant relationship is likewise an explicit many-to-many association
so a consultant can, in principle, be served by more than one TA.

`username` is the normalised login handle and is the same value used today as
`consultant_id` to key the file-based stores (profile.md, verdicts, ...) — this
keeps the file stores addressable by username while identity lives in the DB.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey


class UserRole(str, enum.Enum):
    admin = "admin"
    ta = "ta"
    consultant = "consultant"


ROLE_VALUES: frozenset[str] = frozenset(r.value for r in UserRole)

# Which role a multi-role account is *presented* as: landing page after login,
# the "who am I" line in the UI. Display only — never an authorization input.
# Authorization always asks about a specific membership (`has_role`), so an
# Admin+Consultant is fully both regardless of what this returns.
_ROLE_DISPLAY_PRECEDENCE = (
    UserRole.admin.value,
    UserRole.ta.value,
    UserRole.consultant.value,
)

_ROLE_CHECK = "role IN ('admin', 'ta', 'consultant')"


class UserRoleAssignment(UUIDPrimaryKey, Base):
    """One role membership. A user has one row per role they hold."""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role", name="uq_user_role"),
        CheckConstraint(_ROLE_CHECK, name="ck_user_roles_role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)


class User(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    # Normalised login handle; also the key for the file-based stores.
    username: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    full_name: Mapped[str | None] = mapped_column(String(255))
    # Null until the user accepts their invite and sets a password.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Login throttling (STAB-2). Consecutive failed password checks since the
    # last success or lockout, and the end of an active lockout. Counters and a
    # timestamp only — no credential material, and nothing that identifies the
    # source of an attempt (throttling is per account, not per IP; see
    # `backend/auth/service.py::_record_failed_login`).
    # `server_default` as well as the ORM default: a row inserted by anything
    # that is not this mapper (the raw-SQL fixtures in tests/integration/, a
    # psql session) must not have to know this column exists to satisfy its
    # NOT NULL.
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Primarily for TAs (Stockholm / Malmö / Göteborg); used to scope jobs.
    city: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Primarily for consultants; a TA toggles this off between assignments to
    # mark them as not currently job-seeking.
    actively_looking: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Who provisioned this account (admin who created the TA, TA who created
    # the consultant). Null for the bootstrap admin.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Every V2 Pilot account belongs to the one configured Site.  This is
    # identity/configuration data, not an authorization predicate (P-005).
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    site: Mapped["Site"] = relationship(back_populates="users")

    # Eager-loaded (`selectin`) on purpose: every authorization decision reads
    # roles, and callers routinely hand a *detached* User to a policy helper
    # (the `actors` test fixture, background tasks). A lazy load would raise
    # DetachedInstanceError there, and the fix must not be "check a scalar
    # column instead".
    role_assignments: Mapped[list[UserRoleAssignment]] = relationship(
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def roles(self) -> frozenset[str]:
        """Every role this account holds."""
        return frozenset(a.role for a in self.role_assignments)

    def has_role(self, role: UserRole | str) -> bool:
        """Whether this account holds `role`. The authorization primitive."""
        return (role.value if isinstance(role, UserRole) else role) in self.roles

    @property
    def primary_role(self) -> str:
        """Display/landing role only — see `_ROLE_DISPLAY_PRECEDENCE`.

        Never use this to decide access: an Admin+Consultant's primary role is
        `admin`, but they are still a Consultant for every authorization
        purpose. Ask `has_role` instead.
        """
        held = self.roles
        for role in _ROLE_DISPLAY_PRECEDENCE:
            if role in held:
                return role
        return ""


class TAConsultant(UUIDPrimaryKey, Base):
    """Association: which consultants a Talent Advisor serves."""

    __tablename__ = "ta_consultants"
    __table_args__ = (
        UniqueConstraint("ta_id", "consultant_id", name="uq_ta_consultant"),
    )

    ta_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    consultant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    ta: Mapped[User] = relationship(foreign_keys=[ta_id])
    consultant: Mapped[User] = relationship(foreign_keys=[consultant_id])
