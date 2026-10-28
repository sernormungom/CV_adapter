"""Durable workflow operations: enqueue, claim, finish, recover.

Every function takes an explicit session. The runner owns the loop
(`runner.py`); this module owns the state transitions, so a handler and its
transitions are testable without starting anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models.workflow import (
    MAX_ERROR_MESSAGE,
    TERMINAL_STATUSES,
    WorkflowRun,
    WorkflowStatus,
)


class WorkflowError(Exception):
    """A handler failure with an operator-safe code and message.

    Handlers raise this when they know what went wrong and the text is safe to
    persist. Anything else that escapes a handler is recorded as
    `unexpected_error` with no exception text at all — see `_safe_error`.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error(exc: BaseException) -> tuple[str, str]:
    """Reduce an exception to a bounded, non-sensitive (code, message).

    An arbitrary exception's `str()` is untrusted for persistence: an LLM or
    HTTP client error can embed the prompt, a CV excerpt, or a token, and this
    row is shown to users and read by operators. So only a deliberately raised
    `WorkflowError` contributes a message; everything else records its
    exception class name and nothing more.
    """
    if isinstance(exc, WorkflowError):
        return exc.code[:64], exc.message[:MAX_ERROR_MESSAGE]
    return "unexpected_error", type(exc).__name__[:MAX_ERROR_MESSAGE]


# ── Enqueue ───────────────────────────────────────────────────────────────


def enqueue(
    db: Session,
    *,
    workflow_type: str,
    idempotency_key: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    input_ref: str | None = None,
) -> WorkflowRun:
    """Accept work durably, exactly once per `idempotency_key`.

    Returns the existing run when the key is already known, so a retried POST
    (the 60-second-timeout case this exists for) observes the original run
    rather than starting a second one. The uniqueness is enforced by the
    database, not by a prior SELECT: two concurrent requests both pass a
    check-then-insert, so the loser is caught here on IntegrityError.

    **This commits the caller's transaction, and every one of its three return
    paths commits.** A caller may hold its own uncommitted work pending (the
    domain row this run is for) and rely on it landing in the same commit as the
    run — `tailored_cv.service.request_generation` does exactly that, so that the
    artifact and the run become visible together while it still holds the
    Application's row lock.

    The invariant, stated without reference to any flush policy: **a caller's
    pending work is durable only if this function commits it.** `Session.close()`
    discards whatever was not committed — pending objects if they were never
    flushed, an open transaction if they were — so a return without a commit
    loses the caller's row either way, and the caller is told the run succeeded.
    That is why each path is spelled out; getting one wrong is silent.

    1. **The key already exists** — return the known run. This commits too, and
       that is not a formality: it is the whole of `S3-P1-R2` finding 1, where
       this path returned without one and the caller received a successful run
       for a domain row that no longer existed.
    2. **Lost the unique-index race** — the insert is attempted inside a
       savepoint so the `IntegrityError` rolls back that insert *and nothing
       else*. An unqualified `rollback()` here would discard the caller's work
       as well, which is the same enqueued-but-absent outcome by a different
       route.
    3. **A new run** — the ordinary case.

    A caller that does *not* want its pending work committed must not hold it
    across this call.

    (An earlier version of this docstring blamed the lookup `SELECT` for
    autoflushing the caller's work. That happens under SQLAlchemy's default
    policy, but `backend/db/session.py` builds `SessionLocal` with
    `autoflush=False`, so it is *not* what the deployed application does —
    `S3-P1-R3` finding 1. The outcome is identical, which is why the invariant
    above is stated in terms of the commit rather than the flush.)
    """
    existing = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        # Path 1. This commit is what makes the caller's pending work durable;
        # without it, close() discards the work and the caller still gets a run.
        db.commit()
        return existing

    run = WorkflowRun(
        workflow_type=workflow_type,
        status=WorkflowStatus.queued.value,
        idempotency_key=idempotency_key,
        subject_type=subject_type,
        subject_id=subject_id,
        input_ref=input_ref,
        progress=0,
        attempts=0,
    )
    try:
        with db.begin_nested():
            db.add(run)
            # Explicit, so the unique-index violation is raised here rather than
            # at whatever later point something happens to flush.
            db.flush()
    except IntegrityError:
        # Path 2. Lost the race on the unique index; the winner's row is the
        # answer. The savepoint rolled back our insert and nothing else, so any
        # work the caller still has pending is intact and is committed below.
        run = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.idempotency_key == idempotency_key
            )
        )
        if run is None:  # pragma: no cover - only if the constraint vanished
            raise
        db.commit()
        return run
    # Path 3.
    db.commit()
    db.refresh(run)
    return run


def get_run(db: Session, run_id: uuid.UUID) -> WorkflowRun | None:
    return db.get(WorkflowRun, run_id)


# ── Claim / finish ────────────────────────────────────────────────────────


def claim_next(db: Session) -> WorkflowRun | None:
    """Claim one queued run and mark it running, or return None.

    `FOR UPDATE SKIP LOCKED` is what makes this safe if a second process ever
    does run: the loser skips the locked row instead of blocking or
    double-claiming. It is a no-op on SQLite (the fast gate), which is why
    `tests/integration/` re-proves the claim against real Postgres.
    """
    stmt = (
        select(WorkflowRun)
        .where(WorkflowRun.status == WorkflowStatus.queued.value)
        .order_by(WorkflowRun.created_at)
        .limit(1)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    run = db.scalar(stmt)
    if run is None:
        return None

    run.status = WorkflowStatus.running.value
    run.started_at = _now()
    run.attempts = (run.attempts or 0) + 1
    run.stage = "started"
    db.commit()
    db.refresh(run)
    return run


def record_stage(
    db: Session, run: WorkflowRun, stage: str, progress: int | None = None
) -> None:
    """Persist visible progress mid-run, so a poller sees movement.

    Conditional on the row still being `running`. The lock-loss contract
    permits two live invocations of one run (see `recover_interrupted`), so a
    stale invocation's progress write must not overwrite the stage/progress a
    poller is already showing for a sibling invocation that has since
    terminalized the row — that would make a completed run look like it
    regressed to mid-flight.
    """
    values: dict = {"stage": stage[:64]}
    if progress is not None:
        values["progress"] = max(0, min(100, progress))
    db.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.status == WorkflowStatus.running.value,
        )
        .values(**values)
    )
    db.commit()


def stage_succeeded(
    db: Session, run: WorkflowRun, result_ref: str | None = None
) -> None:
    """`mark_succeeded`'s write, **without its commit**.

    For a caller that must publish this transition and its own domain effect in
    one transaction, so no interruption can leave a durable state where the two
    disagree. The caller's commit publishes both; the caller's rollback
    discards both. `backend/cv_importer/service.py::_try_reconcile_stuck_attempt`
    is the reason this exists (`S4-P1-R5`): it finalizes an import row and that
    attempt's run together, and a poll between two separate commits could
    otherwise read a succeeded import beside a failed run.

    Run-state transitions stay owned by this module rather than being open-coded
    at the call site — the point of splitting the write from the commit rather
    than letting a domain service write `workflow_runs` itself.
    """
    values = {
        "status": WorkflowStatus.succeeded.value,
        "stage": "done",
        "progress": 100,
        "error_code": None,
        "error_message": None,
        "finished_at": _now(),
    }
    if result_ref is not None:
        values["result_ref"] = result_ref[:500]
    db.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.status != WorkflowStatus.succeeded.value,
        )
        .values(**values)
    )


def mark_succeeded(
    db: Session, run: WorkflowRun, result_ref: str | None = None
) -> None:
    """Terminal success for one invocation.

    Allowed to *upgrade* a stale `failed` row: if a sibling invocation wrote
    `failed` before this one committed the effect and finished, the effect
    having actually happened is the authoritative outcome. Does not re-write an
    already-`succeeded` row — every invocation of one run reaches the same
    `result_ref` via `commit_result_ref_once`, so a second write would only
    bump `finished_at` for no reason.
    """
    stage_succeeded(db, run, result_ref=result_ref)
    db.commit()


def commit_result_ref_once(
    db: Session, run: WorkflowRun, result_ref: str
) -> tuple[str, bool]:
    """Atomically commit a handler's business-effect reference once.

    A dead advisory-lock session can overlap an in-flight thread with the next
    leader's recovery. P-006 intentionally does not add leases or cancellation
    machinery, so handlers must tolerate at-least-once *invocation*. Their
    durable business effect must instead be an atomic idempotency boundary.

    This reference implementation uses the run's already-unique business
    intent: exactly one contender may fill an empty `result_ref`; a later
    invocation observes and returns the committed reference. Real handlers use
    the same pattern at their domain record/storage boundary, never a
    check-then-effect sequence.
    """
    candidate = result_ref[:500]
    stored = db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.result_ref.is_(None))
        .values(result_ref=candidate)
        .returning(WorkflowRun.result_ref)
    ).scalar_one_or_none()
    if stored is not None:
        db.commit()
        return stored, True

    # No row was changed: another invocation already committed the effect.
    # End this transaction before observing its value, so READ COMMITTED sees
    # the winner even if this Session previously loaded a stale ORM instance.
    db.rollback()
    stored = db.scalar(
        select(WorkflowRun.result_ref).where(WorkflowRun.id == run.id)
    )
    if stored is None:  # pragma: no cover - run rows are never deleted here
        raise WorkflowError("missing_result", "workflow result reference vanished")
    return stored, False


def mark_failed(db: Session, run: WorkflowRun, exc: BaseException) -> bool:
    """Terminal failure for one invocation. Returns whether the write applied.

    Never downgrades an already-`succeeded` row. The lock-loss contract permits
    two live invocations of the same run; if a sibling invocation already
    committed the atomic effect and finished, a stale invocation that later
    raises (for a reason unrelated to the effect — e.g. its own connection
    dropping) must not corrupt that terminal record. Without this guard the row
    would end up `status=failed` while `result_ref` still shows the committed
    effect, and pollers/operators could not trust `succeeded` when they see it.
    The caller uses the return value to log which outcome the row actually
    holds, rather than assuming its own failure won.
    """
    code, message = _safe_error(exc)
    result = db.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.status != WorkflowStatus.succeeded.value,
        )
        .values(
            status=WorkflowStatus.failed.value,
            error_code=code,
            error_message=message,
            finished_at=_now(),
        )
    )
    db.commit()
    return result.rowcount > 0


# ── Startup recovery ──────────────────────────────────────────────────────


def recover_interrupted(db: Session) -> int:
    """Re-queue runs left `running` when this process becomes leader.

    This replaces the deferred lease/heartbeat model (P-006), and the premise
    is narrower than "the previous owner is gone" — **only its lock session is
    known to be gone**, not necessarily its process or thread. Losing a session
    advisory lock (a network blip, `pg_terminate_backend`, a failover) does not
    stop a `to_thread` worker that is mid-handler; P-006 deliberately has no
    cancellation mechanism for it. So a `running` row recovered here can have a
    live invocation still executing concurrently with the new invocation this
    re-queue makes possible. Returns how many were re-queued.

    Safe to re-run for that reason: every handler must commit its business
    effect through an atomic idempotency boundary — `commit_result_ref_once`
    above is the reference implementation, used by `handlers/probe.py` — so a
    second, concurrent invocation observes the committed result instead of
    redoing the work, and `mark_succeeded`/`mark_failed`'s own guards keep a
    stale invocation's terminal write from corrupting a sibling's completed
    outcome. Do not read this function's existence as proof that recovery only
    ever runs after the prior invocation has stopped — it does not, and the
    contract above is what makes that safe rather than assumed away.
    """
    result = db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.status == WorkflowStatus.running.value)
        .values(
            status=WorkflowStatus.queued.value,
            stage="requeued_after_restart",
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def counts_by_status(db: Session) -> dict[str, int]:
    """Small operational read: how much work is queued/running/terminal."""
    rows = db.execute(
        select(WorkflowRun.status, WorkflowRun.id)
    ).all()
    counts: dict[str, int] = {}
    for status, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    return counts


__all__ = [
    "WorkflowError",
    "TERMINAL_STATUSES",
    "claim_next",
    "commit_result_ref_once",
    "counts_by_status",
    "enqueue",
    "get_run",
    "mark_failed",
    "mark_succeeded",
    "record_stage",
    "recover_interrupted",
]
