"""Published CVs: Application-owned capability links (ADR-009, D-013, P-010).

A consultant (or their TA) publishes their Application-owned tailored CV
(`backend.db.models.TailoredCV`) as a shareable link for an anonymous hiring
manager. **Only the token's SHA-256 hash is ever stored** (`token_hash`, via
`backend.auth.security.generate_token`/`hash_token` — the same primitive
`UserSession`/`Invite` already use): the raw token is minted once,
returned to the caller in the publish/republish response, and never persisted,
logged, or echoed back afterward. The public link is a **fragment** URL
(`/cv/view#<raw-token>`) so the token is never sent to the server in a path or
query string and never appears in a `Referer` header — see
`backend/cv_publish/routes.py` for the header-based capability-route contract.

There is exactly **one active publication per `application_id`** (rebound from
the old `(consultant_username, job_id)` pair — an Application, not a bare Job,
is the owned artifact, and a Consultant may hold more than one Application
against the same Job over time). `consultant_username`/`job_id` remain as
server-derived denormalized display columns; a client never sets them.
Republishing rotates the token (P-010: "Regenerate invalidates the previous
link") and refreshes the snapshot from the tailored CV's *current* state.

`created_by_id` records the human who published (for accountability); the TA to
*notify* on a view is resolved at view time from the consultant's assignment
(`authz.ta_id_for_consultant_username`), falling back to the creator.

Data minimisation (ADR-009): a view records a count + timestamps only — never
the viewer's IP or user-agent. `revoked` is set both by an explicit Revoke and
by the Application reaching a terminal status (`backend/applications/service.py`)
— **expiry alone blocks access but never deletes anything**; only a revoked
row is ever hard-deleted, and only by the retention purge
(`backend/cv_publish/service.py::purge_revoked`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey

# JSONB on Postgres, plain JSON on SQLite (tests). Same pattern as Notification.
_JSON = JSONB().with_variant(JSON(), "sqlite")


class CVPublication(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "cv_publications"
    __table_args__ = (
        # One active (non-revoked) publication per Application. A revoked row is
        # excluded so a fresh publish for the same Application can succeed after
        # a revoke. Partial index → supported on both Postgres and SQLite; the
        # predicate is spelled per-dialect so both build it.
        Index(
            "uq_cv_publications_active_application",
            "application_id",
            unique=True,
            postgresql_where=text("NOT revoked"),
            sqlite_where=text("NOT revoked"),
        ),
    )

    # SHA-256 hex digest of the raw token — never the token itself. Looked up
    # via `backend.auth.security.hash_token(raw)`, exactly like session/API/
    # invite tokens.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # The Application this publication is derived from (P-009: the tailored CV,
    # not the base profile, is what gets published). One per Application while
    # active — see the partial index above.
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Denormalized display/authz-adjacent columns, derived server-side from the
    # Application at publish time — never client-supplied.
    consultant_username: Mapped[str] = mapped_column(String(100), nullable=False)
    job_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    # A sanitized copy of the tailored CV's `cv_state` snapshot at publish time
    # (never rendered HTML). Rendered client-side by the shared render core.
    cv_state: Mapped[dict] = mapped_column(_JSON, nullable=False)
    # A publication-owned copy of the consultant's photo, named by this row's
    # `id` (never by the token); nullable because a CV may carry no photo.
    photo_path: Mapped[str | None] = mapped_column(String(500))
    # Who clicked Publish (accountability). SET NULL so deleting the user does
    # not delete the publication out from under an active link.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def to_dict(self) -> dict:
        """Metadata only — never `cv_state`, `token_hash`, or a raw token.

        The raw token is returned by the publish/republish route response, as
        a sibling field, never through this method.
        """
        return {
            "id": str(self.id),
            "application_id": str(self.application_id),
            "consultant_username": self.consultant_username,
            "job_id": self.job_id,
            "title": self.title,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "view_count": self.view_count,
            "last_viewed_at": self.last_viewed_at.isoformat()
            if self.last_viewed_at
            else None,
            "revoked": self.revoked,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
