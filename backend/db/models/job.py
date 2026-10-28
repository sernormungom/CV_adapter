"""Site-owned Jobs, explicit decision pairs, and Reject markers.

Scores live separately in job_consultant_matches. Decision pairs serialize
Apply/Reject; they never grant opportunity or consultant-record access.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    text,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey


class JobStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


JOB_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in JobStatus)

_JOB_STATUS_CHECK = "status IN ('open', 'closed')"


class ReviewDecision(str, enum.Enum):
    """What a Consultant–Job review marker can say.

    Deliberately one value. `maybe` is removed for good (P-008), and `apply`
    arrives with Applications in Slice 2 — adding it here early would create a
    marker the rest of the system cannot honour.
    """

    reject = "reject"


REVIEW_DECISION_VALUES: frozenset[str] = frozenset(d.value for d in ReviewDecision)

_REVIEW_DECISION_CHECK = "decision IN ('reject')"


class Job(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_JOB_STATUS_CHECK, name="ck_jobs_status"),
        Index("uq_jobs_open_site_content", "site_id", "content_hash", unique=True,
              postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),
    )

    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # A reference the operator pasted alongside the posting. Recorded only —
    # nothing fetches it. URL pre-fill is deferred (SSRF surface, DEFERRED.md).
    source_url: Mapped[str | None] = mapped_column(String(1000))
    close_date: Mapped[date | None] = mapped_column(Date)

    # sha256 of the normalized (title, company, description) triple. Exact
    # duplicates are blocked per Site while a Job is open, by the partial
    # unique index above (`uq_jobs_open_site_content`) — not by a prior
    # SELECT, so two concurrent identical submissions still can't both pass.
    # A closed Job's hash is free to repeat, and the same posting may recur
    # at a different Site.
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobStatus.open.value, index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_open(self) -> bool:
        return self.status == JobStatus.open.value

    def to_dict(self) -> dict:
        """The API projection. Carries no score: a score is per-Consultant and
        computed on demand (`backend/matching/service.py`)."""
        return {
            "id": str(self.id),
            "site_id": str(self.site_id),
            "title": self.title,
            "company": self.company or "",
            "location": self.location or "",
            "description": self.description,
            "source_url": self.source_url or "",
            "close_date": self.close_date.isoformat() if self.close_date else None,
            "status": self.status,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_summary(self) -> dict:
        """The list projection — everything but the full posting body."""
        summary = self.to_dict()
        summary.pop("description")
        return summary


class JobDecisionPair(UUIDPrimaryKey, Base):
    """A pair on which a human decision was started; not an access grant."""

    __tablename__ = "job_decision_pairs"
    __table_args__ = (
        UniqueConstraint("job_id", "consultant_id", name="uq_job_decision_pair"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    consultant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )


class JobReview(UUIDPrimaryKey, Timestamps, Base):
    """One Consultant's review marker for one Job. At most one row per pair."""

    __tablename__ = "job_reviews"
    __table_args__ = (
        UniqueConstraint("job_id", "consultant_id", name="uq_job_review_pair"),
        CheckConstraint(_REVIEW_DECISION_CHECK, name="ck_job_reviews_decision"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    consultant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    # Who recorded it — a Consultant themselves, or their related TA.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "consultant_id": str(self.consultant_id),
            "decision": self.decision,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
