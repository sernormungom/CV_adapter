"""Durable workflow state — the queue and progress store.

Why this exists (P-006, `docs/plans/v2-pilot.md` §5.2): CloudFront and the ALB
both time out at a **verified 60 seconds** (`docs/evidence/m1-timeout-inventory.md`),
and a timed-out request keeps running at the origin. Retrying a synchronous
import or generation therefore duplicates work. The fix is not a longer timeout:
it is a durable row plus an idempotency key, so acceptance is a database commit
rather than an HTTP connection staying open.

**This row holds references, never content.** No CV text, email body, job
description, LLM prompt, or capability token goes in a `workflow_runs` column —
those stay in their authorized records or private storage, and the row points at
them (`subject_type`/`subject_id`, `input_ref`, `result_ref`). The safety floor
in §1 of the plan requires it, and `tests/test_workflow_runner.py`'s column
allowlist fails if a content-shaped column is added here.

Deliberately absent (deferred by P-006, do not add without changing it):
per-job leases, heartbeats, automated retry/backoff classification, priority,
`available_at`, scheduler occurrences, and an Admin retry console. Startup
recovery plus one advisory-lock-protected leader replaces the lease model.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.models.mixins import Timestamps, UUIDPrimaryKey


class WorkflowStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


#: Statuses no runner may claim again.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {WorkflowStatus.succeeded.value, WorkflowStatus.failed.value}
)

STATUS_VALUES: frozenset[str] = frozenset(s.value for s in WorkflowStatus)

#: Hard cap on a persisted error message. A handler failure must never write an
#: unbounded upstream payload into the row (see service._safe_error).
MAX_ERROR_MESSAGE = 500

_STATUS_CHECK = (
    "status IN ('queued', 'running', 'succeeded', 'failed')"
)


class WorkflowRun(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="ck_workflow_runs_status"),
        CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_workflow_runs_progress",
        ),
    )

    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WorkflowStatus.queued.value,
        index=True,
    )

    # Human-readable progress, for the polling client. A label, not a payload.
    stage: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The idempotency contract: one row per business intent. A duplicate
    # enqueue (double-click, retried POST after a 60s timeout) collides here
    # and returns the original run instead of starting a second one.
    idempotency_key: Mapped[str] = mapped_column(
        String(200), unique=True, index=True, nullable=False
    )

    # Typed reference to what this run is about — e.g. ("consultant", username).
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[str | None] = mapped_column(String(200))

    # References to input/result records or storage keys. NOT the data itself.
    input_ref: Mapped[str | None] = mapped_column(String(500))
    result_ref: Mapped[str | None] = mapped_column(String(500))

    # Bounded, safe failure description. `error_code` is a stable slug for
    # operators; `error_message` is truncated and never carries upstream
    # exception text (which can contain prompts or personal data).
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(MAX_ERROR_MESSAGE))

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_status_dict(self) -> dict:
        """The polling projection. Deliberately excludes nothing sensitive
        because the row holds nothing sensitive — see the module docstring."""
        return {
            "id": str(self.id),
            "type": self.workflow_type,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "result_ref": self.result_ref,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attempts": self.attempts,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
        }
