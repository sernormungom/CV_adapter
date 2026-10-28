"""Lazy advisory score cache. Presence conveys no authorization."""
import uuid
from sqlalchemy import ForeignKey, Float, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from backend.db.base import Base
from backend.db.models.mixins import UUIDPrimaryKey


class JobConsultantMatch(UUIDPrimaryKey, Base):
    __tablename__ = "job_consultant_matches"
    __table_args__ = (UniqueConstraint("job_id", "consultant_id", name="uq_job_consultant_match"),)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    consultant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    match_score: Mapped[float | None] = mapped_column(Float)
    result: Mapped[dict] = mapped_column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
