"""The Application domain — Apply is a first-class row, not a verdict field.

V1 recorded "we are going for this one" as `application_status` inside a
`verdicts` JSON blob, alongside a `yes`/`no`/`maybe` opinion. Slice 2 splits the
two apart (`docs/DECISIONS.md` P-008): Reject stays a `job_reviews` marker, and
Apply creates an `Application` row that owns its own identity and lifecycle.

Three things here are load-bearing:

**One live Application per Consultant–Job pair, enforced by the database.** The
partial unique index `uq_application_live_pair` covers only the live statuses,
so a retried or double-clicked Apply cannot produce a second row while an
Application is live, and a pair whose Application ended may be applied to again
without dropping the historical row. As with `jobs.content_hash`, the constraint
is the contract: a check-then-insert lets two concurrent submissions both pass.

**The status set is small, closed, and strictly ordered.** Four values, one
rule: a status may only move to a *strictly higher* rank
(`APPLICATION_STATUS_ORDER`). The two terminal values share the top rank, so
neither can turn into the other and nothing can leave a terminal state — the
terminal guarantee falls out of the ordering rather than needing a second rule
that could drift from it. `docs/plans/v2-pilot.md` §3 asks for "three or four
useful stages"; this is the four, and it is deliberately not an extensible event
framework.

**An Application carries no CV.** P-009 keeps the base CV and each
Application-tailored CV on separate storage identities; the tailored artifact,
its generation workflow, and the editor mode are Slice 3. Nothing here stores
CV content, and a column that did would be that slice arriving early.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey


class ApplicationStatus(str, enum.Enum):
    """The four pilot stages of an Application.

    `applied`   — Apply recorded. The Consultant is going for this Job; the
                  tailored CV that follows is Slice 3's, not this row's.
    `submitted` — the Consultant has been put forward to the client.
    `closed`    — terminal. The process ended (filled, declined, withdrawn by
                  the client); the pilot does not model the outcome further.
    `withdrawn` — terminal. We pulled out before or after submission.

    Both terminal values exist because they answer different questions about a
    pair: `withdrawn` says the decision was reversed on our side, `closed` says
    the opportunity itself is over. Collapsing them would lose the distinction
    that decides whether re-applying later makes sense.
    """

    applied = "applied"
    submitted = "submitted"
    closed = "closed"
    withdrawn = "withdrawn"


APPLICATION_STATUS_VALUES: frozenset[str] = frozenset(
    s.value for s in ApplicationStatus
)

#: The one transition rule: a status may only move to a **strictly higher**
#: rank. `closed` and `withdrawn` share the top rank, which is what makes every
#: terminal property true without a second rule — neither can become the other,
#: and no terminal status can move at all, because nothing outranks them.
APPLICATION_STATUS_ORDER: dict[str, int] = {
    ApplicationStatus.applied.value: 0,
    ApplicationStatus.submitted.value: 1,
    ApplicationStatus.closed.value: 2,
    ApplicationStatus.withdrawn.value: 2,
}

#: A pair's Application is *live* while it holds one of these. This is the set
#: the partial unique index covers and the set the Apply/Reject coherence rule
#: reads, so the two can never disagree about what "live" means.
LIVE_APPLICATION_STATUSES: frozenset[str] = frozenset(
    {ApplicationStatus.applied.value, ApplicationStatus.submitted.value}
)

TERMINAL_APPLICATION_STATUSES: frozenset[str] = (
    APPLICATION_STATUS_VALUES - LIVE_APPLICATION_STATUSES
)

_APPLICATION_STATUS_CHECK = (
    "status IN ('applied', 'submitted', 'closed', 'withdrawn')"
)

#: The partial-index predicate, spelled once. Kept as a module constant so the
#: model, the migration, and any test that reasons about it quote the same list
#: of live statuses rather than three hand-copied ones.
LIVE_STATUS_SQL = "status IN ('applied', 'submitted')"


class Application(UUIDPrimaryKey, Timestamps, Base):
    """One Consultant's Application to one Job."""

    __tablename__ = "applications"
    __table_args__ = (
        CheckConstraint(_APPLICATION_STATUS_CHECK, name="ck_applications_status"),
        # One *live* Application per (Job, Consultant). Terminal rows are
        # excluded, so history accumulates while the live-uniqueness guarantee
        # holds. The predicate is spelled per-dialect so the fast gate's SQLite
        # builds the same partial index the deployed Postgres does — otherwise
        # a plain unique index there would forbid re-Apply and the two gates
        # would be testing different rules.
        Index(
            "uq_application_live_pair",
            "job_id",
            "consultant_id",
            unique=True,
            postgresql_where=text(LIVE_STATUS_SQL),
            sqlite_where=text(LIVE_STATUS_SQL),
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    consultant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ApplicationStatus.applied.value,
        index=True,
    )
    # Who applied — the Consultant themselves, or their related TA. SET NULL so
    # erasing the actor does not delete another Consultant's Application.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Who last moved the status, and when. `updated_at` alone cannot answer
    # this: any write touches it, so it would report an edit as a stage change.
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    status_changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_APPLICATION_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_APPLICATION_STATUSES

    def to_dict(self) -> dict:
        """The API projection. Application-owned facts only.

        No Job body and no other Consultant: a caller who may read this
        Application may not thereby read the shared Job's other relationships.
        The route adds a small Job reference for display.
        """
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "consultant_id": str(self.consultant_id),
            "status": self.status,
            "is_live": self.is_live,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "status_changed_at": (
                self.status_changed_at.isoformat()
                if self.status_changed_at
                else None
            ),
        }
