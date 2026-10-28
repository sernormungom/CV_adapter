"""The one in-process runner.

Shape (P-006): a single asyncio loop in the web process, gated by a Postgres
advisory lock so overlapping ECS deployments cannot run two leaders. It claims
one run at a time, executes its handler, and records a terminal state.

`run_once` is deliberately synchronous and takes a session: the whole state
machine is testable without starting a loop, an event loop, or a container.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.workflows import registry, service
from backend.workflows.leader import LeaderLock

logger = logging.getLogger(__name__)


def run_once(db: Session) -> bool:
    """Claim and execute at most one run. True if something was executed.

    Every failure path is terminal-and-recorded rather than raised: a bad
    handler must not kill the loop that would process the next run. Retry
    classification/backoff is deferred (P-006), so a failure stays `failed`
    until someone enqueues fresh work.
    """
    run = service.claim_next(db)
    if run is None:
        return False

    handler = registry.get_handler(run.workflow_type)
    if handler is None:
        logger.error(
            "workflow %s: no handler registered for type %r",
            run.id,
            run.workflow_type,
        )
        service.mark_failed(
            db,
            run,
            service.WorkflowError(
                "no_handler", f"no handler for workflow type {run.workflow_type!r}"
            ),
        )
        return True

    try:
        result_ref = handler(db, run)
    except Exception as exc:  # noqa: BLE001 - recorded, never propagated
        db.rollback()
        # mark_failed refuses to downgrade an already-succeeded row: the
        # lock-loss contract permits a sibling invocation of this same run to
        # have already committed the effect and finished. applied tells us
        # which outcome the row actually holds.
        applied = service.mark_failed(db, run, exc)
        if applied:
            logger.warning(
                "workflow %s (%s) failed: %s",
                run.id,
                run.workflow_type,
                type(exc).__name__,  # never str(exc) — see service._safe_error
            )
        else:
            logger.info(
                "workflow %s (%s): this invocation failed (%s), but a "
                "concurrent invocation already succeeded — the row stays "
                "succeeded",
                run.id,
                run.workflow_type,
                type(exc).__name__,
            )
        return True

    service.mark_succeeded(db, run, result_ref)
    logger.info("workflow %s (%s) succeeded", run.id, run.workflow_type)
    return True


def drain(db: Session, max_runs: int = 100) -> int:
    """Execute queued runs until the queue is empty. Returns how many ran.

    `max_runs` bounds a single drain so a handler that re-enqueues its own work
    cannot spin forever.
    """
    executed = 0
    while executed < max_runs and run_once(db):
        executed += 1
    return executed


class WorkflowRunner:
    """Owns the background loop and this process's leadership.

    Leadership is *supervised*, not decided once at startup. Two failure modes
    make that necessary, and both are real during an ECS rolling deployment:

    - **Losing the initial race is temporary.** The new task starts while the
      old one still holds the lock, loses, and must keep trying — ECS stops the
      old task moments later, and if the new one had given up permanently the
      queue would stall until the next deployment.
    - **Holding the lock is not permanent.** If the lock session dies the lock
      is released without telling us, so leadership is re-verified every cycle
      and the runner stands down when it no longer holds it.

    Shutdown is graceful: leadership is kept until the in-flight handler
    finishes, because `asyncio` cancellation cannot stop a thread. Releasing the
    lock while a handler is still running would let a replacement leader recover
    the same run and execute it concurrently.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        leader: LeaderLock,
        poll_seconds: float = 2.0,
        leader_retry_seconds: float = 15.0,
        shutdown_grace_seconds: float = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._leader = leader
        self._poll_seconds = poll_seconds
        self._leader_retry_seconds = leader_retry_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._task: asyncio.Task | None = None
        self._stopping: asyncio.Event | None = None
        # Gate on consuming work: set only after recovery succeeds under the
        # current leadership, cleared whenever leadership changes.
        self._recovered = False

    @property
    def is_leader(self) -> bool:
        return self._leader.is_leader

    async def start(self) -> None:
        """Start supervising leadership and, while leading, executing work.

        Always returns promptly: a process that is not (yet) the leader serves
        HTTP normally and keeps trying in the background. Losing the lock, or
        failing to reach the database to ask for it, is not a startup error —
        queued work is durable and waits for a runner.
        """
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Stop after the in-flight handler finishes, then release the lock.

        Deliberately *awaits* the loop instead of cancelling it: cancelling the
        `await asyncio.to_thread(...)` returns control here while the handler
        thread keeps running, and releasing leadership at that moment lets a
        replacement leader recover and repeat a run that is still executing.

        **The grace period bounds how long `stop()` blocks — not whether the
        lock is held.** If it elapses, leadership is *retained*: a handler that
        outlives the grace is the expected case, not an edge, because exceeding
        60 seconds is the entire reason this runner exists. Releasing then would
        reintroduce exactly the double-execution this lock prevents. Instead the
        lock is handed to a completion callback, so it is released if the
        handler eventually finishes, and otherwise dies with the process — a
        session advisory lock is released by Postgres when the backend goes
        away, so a SIGKILL costs nothing but the wait.
        """
        if self._stopping is not None:
            self._stopping.set()

        task, self._task = self._task, None
        if task is not None:
            try:
                # shield: a timeout must not cancel the loop, or we would be
                # back to "handler still running, nobody waiting for it".
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self._shutdown_grace_seconds
                )
            except asyncio.TimeoutError:
                logger.error(
                    "workflow runner: in-flight handler still running after "
                    "%.0fs; RETAINING leadership so no replacement leader can "
                    "repeat it. The lock is released when the handler finishes "
                    "or when this process exits.",
                    self._shutdown_grace_seconds,
                )
                task.add_done_callback(self._release_after_handler)
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("workflow runner: loop failed during shutdown")
        self._leader.release()

    def _release_after_handler(self, task: asyncio.Task) -> None:
        """Release leadership once a grace-exceeding handler actually returns.

        A cancelled asyncio task is not evidence that its `to_thread` worker
        stopped: cancellation detaches the await while the handler thread can
        continue executing. In that case retain the dedicated session lock
        until process exit, where Postgres releases it with the session. This
        deliberately favours a paused queue over concurrent re-execution.
        """
        if task.cancelled():
            logger.error(
                "workflow runner: supervised task was cancelled while a late "
                "handler may still be running; retaining leadership until "
                "process exit"
            )
            return
        try:
            logger.info(
                "workflow runner: late handler finished; releasing leadership"
            )
            self._leader.release()
        except Exception:  # noqa: BLE001 - best effort during shutdown
            logger.exception("workflow runner: late leadership release failed")

    def _recover(self) -> bool:
        """Re-queue anything a previous process left mid-flight.

        Returns whether recovery succeeded. A failure is not cosmetic: rows left
        `running` are not claimable, so consuming new work while they are
        unrecovered strands them until some later restart happens to succeed.
        The caller therefore refuses to consume until this returns True.
        """
        db = self._session_factory()
        try:
            requeued = service.recover_interrupted(db)
            if requeued:
                logger.info(
                    "workflow recovery: re-queued %d interrupted run(s)", requeued
                )
            return True
        except Exception:  # noqa: BLE001 - retried, never fatal
            logger.exception("workflow recovery failed")
            return False
        finally:
            db.close()

    def _ensure_leadership(self) -> bool:
        """True if this process may execute work right now.

        Three conditions, in order: hold the lock, still hold it, and have
        successfully recovered. The last one gates consumption because a
        recovery that silently failed leaves `running` rows unclaimable —
        consuming new work on top of that strands them until some later restart
        happens to succeed. Leadership is *kept* while recovery is retried:
        handing the lock to another task would only move the same database
        problem, and holding it keeps a second runner from starting behind us.
        """
        if self._leader.is_leader:
            if not self._leader.verify():
                logger.error(
                    "workflow runner: leadership lost; standing down and will "
                    "try to re-acquire"
                )
                self._leader.release()
                self._recovered = False
                return False
        else:
            try:
                acquired = self._leader.acquire()
            except SQLAlchemyError:
                logger.error(
                    "workflow runner: could not reach the database to acquire "
                    "leadership; retrying (queued work is durable and waits)",
                    exc_info=True,
                )
                return False
            if not acquired:
                return False
            logger.info("workflow runner: leadership acquired")
            self._recovered = False

        # Recovery belongs to the moment leadership starts — including a
        # takeover, which is exactly when another task's interrupted runs need
        # re-queuing — and must succeed before anything new is consumed.
        if not self._recovered:
            if not self._recover():
                logger.error(
                    "workflow runner: refusing to consume work until startup "
                    "recovery succeeds (interrupted runs would be stranded)"
                )
                return False
            self._recovered = True

        return True

    async def _supervise(self) -> None:
        while not self._stopping.is_set():
            if not self._ensure_leadership():
                await self._idle(self._leader_retry_seconds)
                continue
            try:
                # Executed in a worker thread: handlers use blocking SQLAlchemy
                # (and later blocking LLM/document work), which must not stall
                # the API event loop this shares a process with.
                did_work = await asyncio.to_thread(self._run_once_in_session)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad cycle must not kill the loop
                logger.exception("workflow runner cycle failed")
                did_work = False
            # Only idle when there was nothing to do, so a backlog drains at
            # full speed instead of one run per poll interval.
            if not did_work:
                await self._idle(self._poll_seconds)

    async def _idle(self, seconds: float) -> None:
        """Sleep, but wake immediately when shutdown is requested."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def _run_once_in_session(self) -> bool:
        db = self._session_factory()
        try:
            return run_once(db)
        finally:
            db.close()
