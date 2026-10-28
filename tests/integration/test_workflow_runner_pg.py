"""The workflow runner against real Postgres (V2 Step 1.3, P-006).

Three things cannot be proved on SQLite and are the whole reason this file
exists:

1. **Advisory-lock leadership.** The mechanism that stops two overlapping ECS
   deployment tasks both running a runner. SQLite has no advisory locks, so the
   fast gate's `LeaderLock` short-circuits to "leader".
2. **`FOR UPDATE SKIP LOCKED` claiming.** SQLite ignores it, so the fast gate
   cannot show that a concurrent claimer skips a locked row rather than
   double-claiming it.
3. **The database-level idempotency constraint.** The unique index is what makes
   two concurrent enqueues collapse to one run; a prior SELECT cannot.

Every database here is created and dropped by the fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.db.models.workflow import WorkflowRun, WorkflowStatus
from backend.workflows import registry, service
from backend.workflows.handlers import probe
from backend.workflows.leader import LeaderLock, WORKFLOW_LEADER_LOCK_KEY
from backend.workflows.runner import WorkflowRunner, drain, run_once


def _test_lock_key(url: str) -> int:
    """Use the production key: advisory locks are database-local.

    Every integration test receives its own database, so it cannot contend
    with another test merely because they share the same Postgres server.
    """
    del url
    return WORKFLOW_LEADER_LOCK_KEY


@pytest.fixture
def sessions(migrated_db):
    """A session factory on a migrated, disposable Postgres database."""
    engine = create_engine(migrated_db, future=True)
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_probe_counts():
    probe.reset_call_counts()
    yield
    probe.reset_call_counts()


# ── 1. Advisory-lock leadership ───────────────────────────────────────────

def test_only_one_process_can_hold_leadership(migrated_db):
    """The rolling-deployment case: two tasks, one leader.

    Two engines stand in for the two ECS tasks — separate connection pools
    against the same database, which is exactly the real topology.
    """
    old_task = create_engine(migrated_db, future=True)
    new_task = create_engine(migrated_db, future=True)
    old_leader = LeaderLock(old_task, lock_key=_test_lock_key(migrated_db))
    new_leader = LeaderLock(new_task, lock_key=_test_lock_key(migrated_db))
    try:
        assert old_leader.requires_lock is True, "expected the Postgres path"

        assert old_leader.acquire() is True
        # The second task must not get leadership while the first holds it.
        assert new_leader.acquire() is False
        assert new_leader.is_leader is False

        # ...and must take over once the first releases (deployment completes).
        old_leader.release()
        assert new_leader.acquire() is True
        assert new_leader.is_leader is True
    finally:
        old_leader.release()
        new_leader.release()
        old_task.dispose()
        new_task.dispose()


def test_leadership_is_released_when_the_leader_process_dies(migrated_db):
    """A SIGKILLed leader must not wedge the lock — no operator unsticking.

    `pg_terminate_backend` is the faithful analogue: what actually releases a
    session advisory lock is the *server-side session ending*, which is what
    happens when a killed process's socket closes. (`engine.dispose()` is not
    an analogue — it returns pooled connections, but the leader's connection is
    checked out, so the session and its lock survive. That distinction is
    exactly why this is tested against a real server.)
    """
    dead_task = create_engine(migrated_db, future=True)
    dead_leader = LeaderLock(dead_task, lock_key=_test_lock_key(migrated_db))
    assert dead_leader.acquire() is True

    # Kill the leader's session from outside, as the OS would on process death.
    executioner = create_engine(migrated_db, future=True, isolation_level="AUTOCOMMIT")
    try:
        with executioner.connect() as conn:
            killed = conn.exec_driver_sql(
                """
                SELECT pg_terminate_backend(pid) FROM pg_locks
                WHERE locktype = 'advisory' AND granted
                  AND database = (
                      SELECT oid FROM pg_database WHERE datname = current_database()
                  )
                """
            ).fetchall()
        assert killed, "expected to find the leader's advisory lock to terminate"
    finally:
        executioner.dispose()

    survivor_engine = create_engine(migrated_db, future=True)
    survivor = LeaderLock(survivor_engine, lock_key=_test_lock_key(migrated_db))
    try:
        assert survivor.acquire() is True, (
            "a dead leader's advisory lock was not released"
        )
    finally:
        survivor.release()
        survivor_engine.dispose()
        dead_task.dispose()


def test_acquire_is_idempotent_for_the_holder(migrated_db):
    engine = create_engine(migrated_db, future=True)
    leader = LeaderLock(engine, lock_key=_test_lock_key(migrated_db))
    try:
        assert leader.acquire() is True
        assert leader.acquire() is True  # still leader, no second lock taken
    finally:
        leader.release()
        engine.dispose()


# ── 2. SKIP LOCKED claiming ───────────────────────────────────────────────

def test_a_committed_claim_is_not_visible_to_the_next_claimer(sessions):
    """After a claim commits, the row is `running` and no longer claimable.

    Narrow on purpose: this proves the *status transition*, not SKIP LOCKED —
    `claim_next` commits before returning, so no row lock is still held here.
    The genuinely concurrent case is the next test.
    """
    setup = sessions()
    service.enqueue(
        setup, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key="solo"
    )
    setup.close()

    first, second = sessions(), sessions()
    try:
        assert service.claim_next(first) is not None
        assert service.claim_next(second) is None
    finally:
        first.close()
        second.close()


def test_skip_locked_lets_a_second_claimer_pass_a_locked_row(migrated_db):
    """The real SKIP LOCKED property, with a row lock actually held.

    Two live connections: the first selects the only queued row `FOR UPDATE
    SKIP LOCKED` and *keeps its transaction open*; the second runs the same
    statement while that lock is held. It must come back empty rather than
    blocking or returning the same row. Without SKIP LOCKED the second
    statement would block on the lock until the first transaction ended.

    This uses the same statement `claim_next` issues rather than calling it,
    because `claim_next` commits — which is exactly the flaw in the earlier
    version of this test.
    """
    setup_engine = create_engine(migrated_db, future=True)
    Setup = sessionmaker(bind=setup_engine, expire_on_commit=False)
    setup = Setup()
    service.enqueue(
        setup, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key="locked-row"
    )
    setup.close()
    setup_engine.dispose()

    claim_sql = (
        "SELECT id FROM workflow_runs WHERE status = 'queued' "
        "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    )

    holder_engine = create_engine(migrated_db, future=True)
    rival_engine = create_engine(migrated_db, future=True)
    try:
        holder = holder_engine.connect()
        rival = rival_engine.connect()
        try:
            # Explicit transaction, deliberately left open.
            holder.begin()
            locked = holder.exec_driver_sql(claim_sql).fetchall()
            assert len(locked) == 1, "expected the holder to lock the queued row"

            rival.begin()
            # A plain statement here would block; SKIP LOCKED makes it empty.
            skipped = rival.exec_driver_sql(claim_sql).fetchall()
            assert skipped == [], (
                "second claimer saw a row that was already locked — "
                "SKIP LOCKED is not in effect"
            )
            rival.rollback()
            holder.rollback()
        finally:
            rival.close()
            holder.close()
    finally:
        holder_engine.dispose()
        rival_engine.dispose()


def test_claims_are_fifo_by_creation(sessions):
    db = sessions()
    try:
        for key in ("a", "b", "c"):
            service.enqueue(
                db, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key=key
            )
        assert service.claim_next(db).idempotency_key == "a"
        assert service.claim_next(db).idempotency_key == "b"
    finally:
        db.close()


# ── 3. Database-enforced idempotency ──────────────────────────────────────

def test_the_unique_index_is_what_enforces_one_run_per_key(sessions):
    """Insert a colliding row directly, bypassing `enqueue`'s SELECT.

    This is the concurrent-enqueue race: both callers pass the pre-check, and
    the database is what refuses the second.
    """
    db = sessions()
    try:
        service.enqueue(
            db, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key="race"
        )
        db.add(
            WorkflowRun(
                workflow_type=probe.PROBE_WORKFLOW_TYPE,
                status=WorkflowStatus.queued.value,
                idempotency_key="race",
                progress=0,
                attempts=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_enqueue_takes_the_integrity_error_path_and_returns_the_winner(migrated_db):
    """Forces the loser branch, rather than hoping to hit it.

    A barrier before `enqueue` is not enough: `enqueue` does its own "does it
    exist?" SELECT, and if the winner commits first that SELECT simply finds
    the row — the IntegrityError path never runs and the test passes without
    proving anything.

    So the interleaving is controlled instead, using the same technique this
    suite already uses for the publication race (see
    `tests/integration/README.md`):

      A: INSERT the key, do NOT commit.
      B: `enqueue(...)` — its SELECT sees nothing (A is uncommitted), so it
         INSERTs and *blocks* on the unique index.
      -- confirm via pg_stat_activity that B really is lock-blocked --
      A: commit  →  B's insert raises IntegrityError  →  B recovers and must
         return A's run rather than propagating the error.
    """
    engine = create_engine(migrated_db, future=True, pool_size=6, max_overflow=4)
    Sessions = sessionmaker(bind=engine, expire_on_commit=False)

    key = "hot-key"
    outcome: dict[str, object] = {}

    a = Sessions()
    a.add(
        WorkflowRun(
            workflow_type=probe.PROBE_WORKFLOW_TYPE,
            status=WorkflowStatus.queued.value,
            idempotency_key=key,
            progress=0,
            attempts=0,
        )
    )
    a.flush()  # sends the INSERT, still uncommitted
    winner_id = a.scalar(
        select(WorkflowRun.id).where(WorkflowRun.idempotency_key == key)
    )

    def loser() -> None:
        db = Sessions()
        try:
            run = service.enqueue(
                db, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key=key
            )
            outcome["id"] = run.id
        except BaseException as exc:  # noqa: BLE001 - reported below
            outcome["error"] = exc
        finally:
            db.close()

    thread = threading.Thread(target=loser, daemon=True)
    watcher_engine = create_engine(
        migrated_db, future=True, isolation_level="AUTOCOMMIT"
    )
    try:
        thread.start()

        # Confirm B is genuinely blocked on A's uncommitted insert before
        # letting A commit — otherwise this degrades into two serial inserts.
        blocked = False
        deadline = time.monotonic() + 30
        with watcher_engine.connect() as conn:
            while time.monotonic() < deadline:
                waiting = conn.exec_driver_sql(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                ).scalar()
                if waiting:
                    blocked = True
                    break
                time.sleep(0.1)
        assert blocked, "the second enqueue never blocked on the unique index"

        a.commit()  # releases B, whose INSERT now violates the unique index
        thread.join(timeout=60)
        assert not thread.is_alive(), "the losing enqueue never completed"
    finally:
        a.close()
        watcher_engine.dispose()

    assert "error" not in outcome, (
        f"enqueue propagated an error instead of returning the winner: "
        f"{outcome.get('error')!r}"
    )
    assert outcome["id"] == winner_id, "the loser did not receive the winner's run"

    check = Sessions()
    try:
        rows = check.scalars(
            select(WorkflowRun).where(WorkflowRun.idempotency_key == key)
        ).all()
        assert len(rows) == 1
    finally:
        check.close()
        engine.dispose()


# ── End to end on the real schema ─────────────────────────────────────────

def test_enqueue_run_and_poll_to_completion(sessions):
    db = sessions()
    try:
        run = service.enqueue(
            db,
            workflow_type=probe.PROBE_WORKFLOW_TYPE,
            idempotency_key="e2e",
            subject_type="consultant",
            subject_id="mo.chen",
        )
        run_id = run.id
        assert run.status == WorkflowStatus.queued.value

        assert run_once(db) is True

        finished = service.get_run(db, run_id)
        assert finished.status == WorkflowStatus.succeeded.value
        assert finished.result_ref == "probe-result:e2e"
        assert finished.progress == 100
        assert probe.call_count("e2e") == 1
    finally:
        db.close()


def test_recovery_then_drain_completes_interrupted_work(sessions):
    """Startup recovery on the real schema, including the crash-after-commit
    case where the handler must not repeat its effect."""
    db = sessions()
    try:
        interrupted = service.enqueue(
            db, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key="crash-pg"
        )
        half_done = service.enqueue(
            db, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key="half-pg"
        )
        # Two crashed runs: one that had not started its effect, one that had.
        interrupted.status = WorkflowStatus.running.value
        half_done.status = WorkflowStatus.running.value
        half_done.result_ref = "probe-result:half-pg"
        db.commit()

        assert service.recover_interrupted(db) == 2
        assert drain(db) == 2

        assert service.get_run(db, interrupted.id).status == "succeeded"
        assert service.get_run(db, half_done.id).status == "succeeded"
        # The first did its work; the second found it already committed.
        assert probe.call_count("crash-pg") == 1
        assert probe.call_count("half-pg") == 0
    finally:
        db.close()


# A child process that claims a run, commits the handler's business effect,
# and then dies abruptly *before* the runner can mark it succeeded — the exact
# window that makes handler idempotency necessary. `os._exit` skips atexit
# handlers, `finally` blocks, and connection cleanup, so this is a genuine
# abrupt termination rather than an orderly shutdown.
_CRASHING_CHILD = """
import os, sys
from backend.db.session import SessionLocal
from backend.workflows import service
from backend.workflows.handlers import probe

db = SessionLocal()
service.enqueue(
    db, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key=sys.argv[1]
)
claimed = service.claim_next(db)
assert claimed is not None
probe.handle_probe(db, claimed)   # business effect committed here
sys.stderr.write("child: effect committed, dying now\\n")
sys.stderr.flush()
os._exit(9)                        # die before mark_succeeded
"""


def test_a_killed_runner_is_recovered_without_repeating_the_work(
    migrated_db, sessions
):
    """End-to-end: real process, real kill, real recovery.

    The other recovery tests set up the crashed row state directly. This one
    lets a separate OS process actually create that state and then die, so the
    durability claim does not rest on the test's idea of what a crash leaves
    behind.
    """
    import subprocess
    import sys as _sys

    env = os.environ.copy()
    env["DATABASE_URL"] = migrated_db
    key = f"killed-{uuid.uuid4().hex[:8]}"

    child = subprocess.run(
        [_sys.executable, "-c", _CRASHING_CHILD, key],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # -9 from os._exit(9); the point is it did not exit cleanly through 0.
    assert child.returncode == 9, f"stdout:{child.stdout}\nstderr:{child.stderr}"
    assert "effect committed" in child.stderr

    db = sessions()
    try:
        # The dead process left a durable, claimable-looking `running` row whose
        # business effect is already committed.
        orphan = db.scalar(
            select(WorkflowRun).where(WorkflowRun.idempotency_key == key)
        )
        assert orphan is not None
        assert orphan.status == WorkflowStatus.running.value
        assert orphan.result_ref == f"probe-result:{key}"

        # Startup recovery in the surviving/restarted process.
        assert service.recover_interrupted(db) >= 1
        assert drain(db) >= 1

        db.refresh(orphan)
        assert orphan.status == WorkflowStatus.succeeded.value
        assert orphan.result_ref == f"probe-result:{key}"
        # The work was done once, by the process that died — not repeated here.
        assert probe.call_count(key) == 0
    finally:
        db.close()


# ── Leadership lifecycle (the rolling-deployment cases) ───────────────────

def _lock_is_free(url: str) -> bool:
    """Whether the workflow leadership lock can be taken right now."""
    engine = create_engine(url, future=True)
    try:
        probe_lock = LeaderLock(engine, lock_key=_test_lock_key(url))
        if probe_lock.acquire():
            probe_lock.release()
            return True
        return False
    finally:
        engine.dispose()


def test_a_standby_runner_takes_over_when_the_incumbent_releases(migrated_db):
    """The deployment sequence that previously stalled the queue forever.

    The new task loses the initial race to the old task. When ECS then stops the
    old task, the standby must notice and take over — previously `start()`
    returned permanently on a lost race, leaving no runner at all.
    """
    incumbent_engine = create_engine(migrated_db, future=True)
    standby_engine = create_engine(migrated_db, future=True)
    incumbent = LeaderLock(incumbent_engine, lock_key=_test_lock_key(migrated_db))
    Sessions = sessionmaker(bind=standby_engine, expire_on_commit=False)

    setup = Sessions()
    service.enqueue(
        setup, workflow_type=probe.PROBE_WORKFLOW_TYPE, idempotency_key="takeover"
    )
    setup.close()

    assert incumbent.acquire() is True, "the incumbent should hold the lock"

    runner = WorkflowRunner(
        session_factory=Sessions,
        leader=LeaderLock(standby_engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=0.05,
    )

    async def _scenario() -> None:
        await runner.start()
        # While the incumbent holds the lock the standby must not lead or work.
        await asyncio.sleep(0.4)
        assert runner.is_leader is False
        check = Sessions()
        try:
            waiting = check.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.idempotency_key == "takeover"
                )
            )
            assert waiting.status == WorkflowStatus.queued.value
        finally:
            check.close()

        # The old task goes away.
        incumbent.release()

        # The standby should take over and drain the queue without a restart.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            check = Sessions()
            try:
                run = check.scalar(
                    select(WorkflowRun).where(
                        WorkflowRun.idempotency_key == "takeover"
                    )
                )
                if run.status == WorkflowStatus.succeeded.value:
                    break
            finally:
                check.close()
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("standby never took over and drained the queue")

        assert runner.is_leader is True
        await runner.stop()

    try:
        asyncio.run(_scenario())
    finally:
        incumbent.release()
        incumbent_engine.dispose()
        standby_engine.dispose()


def test_shutdown_keeps_leadership_until_the_handler_finishes(migrated_db):
    """Graceful shutdown, the second blocking defect.

    `asyncio` cancellation cannot stop a thread, so cancelling the await around
    `asyncio.to_thread` and releasing the lock would let a replacement leader
    recover and re-execute a run whose handler is still going. The lock must
    stay held until the handler returns.
    """
    engine = create_engine(migrated_db, future=True, pool_size=5, max_overflow=5)
    Sessions = sessionmaker(bind=engine, expire_on_commit=False)

    handler_started = threading.Event()
    may_finish = threading.Event()
    handler_returned = threading.Event()

    def slow_handler(db, run):  # noqa: ARG001
        handler_started.set()
        # Block inside the worker thread, as a real LLM/document call would.
        assert may_finish.wait(timeout=30), "test did not release the handler"
        handler_returned.set()
        return "slow-result"

    registry.register("slow_probe", slow_handler)

    setup = Sessions()
    service.enqueue(
        setup, workflow_type="slow_probe", idempotency_key="slow-1"
    )
    setup.close()

    runner = WorkflowRunner(
        session_factory=Sessions,
        leader=LeaderLock(engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=0.05,
        shutdown_grace_seconds=30.0,
    )

    observations: dict[str, bool] = {}

    async def _scenario() -> None:
        await runner.start()
        assert await asyncio.to_thread(handler_started.wait, 15), (
            "handler never started"
        )

        # Ask the runner to stop while the handler is mid-flight.
        stopping = asyncio.create_task(runner.stop())
        await asyncio.sleep(0.5)

        # THE ASSERTION: leadership is still held, so no replacement leader can
        # recover and repeat this run.
        observations["lock_free_while_handler_running"] = await asyncio.to_thread(
            _lock_is_free, migrated_db
        )
        observations["handler_returned_yet"] = handler_returned.is_set()
        observations["stop_completed_early"] = stopping.done()

        # Let the handler finish; stop() should then complete and release.
        may_finish.set()
        await asyncio.wait_for(stopping, timeout=30)
        observations["handler_returned_after_stop"] = handler_returned.is_set()

    try:
        asyncio.run(_scenario())
    finally:
        may_finish.set()
        engine.dispose()

    assert observations["lock_free_while_handler_running"] is False, (
        "leadership was released while the handler was still running — a "
        "replacement leader could recover and repeat the run"
    )
    assert observations["handler_returned_yet"] is False
    assert observations["stop_completed_early"] is False, (
        "stop() returned before the in-flight handler finished"
    )
    assert observations["handler_returned_after_stop"] is True

    # The run finished normally, and leadership is now free for the next task.
    check = sessionmaker(
        bind=create_engine(migrated_db, future=True), expire_on_commit=False
    )()
    try:
        run = check.scalar(
            select(WorkflowRun).where(WorkflowRun.idempotency_key == "slow-1")
        )
        assert run.status == WorkflowStatus.succeeded.value
        assert run.result_ref == "slow-result"
    finally:
        check.close()
    assert _lock_is_free(migrated_db) is True


def test_grace_expiry_retains_leadership_while_the_handler_runs(migrated_db):
    """The reported defect: timing out must not unlock mid-handler.

    Handlers are *expected* to exceed 60 seconds — that is the whole reason this
    runner exists — so grace expiry is a normal path, not a rare edge. On
    timeout `stop()` must return (so shutdown is not blocked forever) while
    still holding the lock, and release only once the handler actually finishes.
    """
    engine = create_engine(migrated_db, future=True, pool_size=5, max_overflow=5)
    Sessions = sessionmaker(bind=engine, expire_on_commit=False)

    handler_started = threading.Event()
    may_finish = threading.Event()
    handler_returned = threading.Event()

    def very_slow_handler(db, run):  # noqa: ARG001
        handler_started.set()
        assert may_finish.wait(timeout=60), "test did not release the handler"
        handler_returned.set()
        return "late-result"

    registry.register("very_slow_probe", very_slow_handler)

    setup = Sessions()
    service.enqueue(
        setup, workflow_type="very_slow_probe", idempotency_key="late-1"
    )
    setup.close()

    runner = WorkflowRunner(
        session_factory=Sessions,
        leader=LeaderLock(engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=0.05,
        # Deliberately tiny: force the grace to expire while work is in flight.
        shutdown_grace_seconds=0.5,
    )

    seen: dict[str, bool] = {}

    async def _scenario() -> None:
        await runner.start()
        assert await asyncio.to_thread(handler_started.wait, 20), (
            "handler never started"
        )

        # stop() must return once the grace expires rather than hanging...
        await asyncio.wait_for(runner.stop(), timeout=20)
        seen["handler_done_at_stop"] = handler_returned.is_set()
        # ...but the lock must still be held, because the handler is still going.
        seen["lock_free_after_grace_expiry"] = await asyncio.to_thread(
            _lock_is_free, migrated_db
        )

        # When the handler finally finishes, leadership is released.
        may_finish.set()
        assert await asyncio.to_thread(handler_returned.wait, 30)
        for _ in range(100):
            if await asyncio.to_thread(_lock_is_free, migrated_db):
                seen["lock_free_after_handler"] = True
                break
            await asyncio.sleep(0.1)

    try:
        asyncio.run(_scenario())
    finally:
        may_finish.set()
        engine.dispose()

    assert seen["handler_done_at_stop"] is False, "handler finished too early to test"
    assert seen["lock_free_after_grace_expiry"] is False, (
        "leadership was released after the grace expired while the handler was "
        "still running — a replacement leader could repeat the run"
    )
    assert seen.get("lock_free_after_handler") is True, (
        "leadership was never released after the late handler finished"
    )


def test_cancelling_the_supervisor_after_grace_does_not_unlock_a_live_thread(
    migrated_db,
):
    """Cancellation is not handler completion.

    Application shutdown may cancel pending asyncio tasks after `stop()` has
    returned from its grace timeout. `asyncio.to_thread` keeps its worker alive,
    so the done callback must retain the session lock rather than hand a live
    run to a replacement leader.
    """
    engine = create_engine(migrated_db, future=True, pool_size=5, max_overflow=5)
    Sessions = sessionmaker(bind=engine, expire_on_commit=False)
    handler_started = threading.Event()
    may_finish = threading.Event()
    handler_finished = threading.Event()

    def blocking_handler(db, run):  # noqa: ARG001
        handler_started.set()
        assert may_finish.wait(timeout=60), "test did not release the handler"
        handler_finished.set()
        return "late-after-cancel"

    registry.register("cancelled_late_probe", blocking_handler)
    setup = Sessions()
    service.enqueue(
        setup, workflow_type="cancelled_late_probe", idempotency_key="cancelled-late"
    )
    setup.close()
    runner = WorkflowRunner(
        session_factory=Sessions,
        leader=LeaderLock(engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=0.05,
        shutdown_grace_seconds=0.05,
    )
    seen: dict[str, bool] = {}

    async def _scenario() -> None:
        await runner.start()
        supervisor = runner._task
        assert supervisor is not None
        assert await asyncio.to_thread(handler_started.wait, 20)

        await runner.stop()  # grace expires; completion callback is registered
        supervisor.cancel()  # simulates lifespan/app task cancellation
        with contextlib.suppress(asyncio.CancelledError):
            await supervisor

        seen["handler_finished_when_cancelled"] = handler_finished.is_set()
        seen["lock_free_after_cancel"] = await asyncio.to_thread(
            _lock_is_free, migrated_db
        )
        may_finish.set()
        assert await asyncio.to_thread(handler_finished.wait, 30)
        # A cancelled supervisor deliberately leaves leadership to process exit.
        seen["lock_free_after_thread_finished"] = await asyncio.to_thread(
            _lock_is_free, migrated_db
        )

    try:
        asyncio.run(_scenario())
    finally:
        may_finish.set()
        # The real process exit releases this session lock. Tests stay in one
        # process, so release after the thread has definitely returned.
        runner._leader.release()
        engine.dispose()

    assert seen["handler_finished_when_cancelled"] is False
    assert seen["lock_free_after_cancel"] is False
    assert seen["lock_free_after_thread_finished"] is False


def test_a_second_runner_takes_over_after_the_leader_loses_its_lock(migrated_db):
    """Two live runners, not just `verify()` in isolation.

    The leader's lock session is terminated (a failover, or an operator
    terminating a backend). The former leader must stop consuming, and a
    replacement runner must take over and drain the queue.
    """
    leader_engine = create_engine(migrated_db, future=True, pool_size=5)
    standby_engine = create_engine(migrated_db, future=True, pool_size=5)
    LeaderSessions = sessionmaker(bind=leader_engine, expire_on_commit=False)
    StandbySessions = sessionmaker(bind=standby_engine, expire_on_commit=False)

    first = WorkflowRunner(
        session_factory=LeaderSessions,
        leader=LeaderLock(leader_engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=60.0,  # will not re-acquire during this test
    )
    second = WorkflowRunner(
        session_factory=StandbySessions,
        leader=LeaderLock(standby_engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=0.05,
    )

    async def _scenario() -> None:
        await first.start()
        for _ in range(100):
            if first.is_leader:
                break
            await asyncio.sleep(0.05)
        assert first.is_leader, "the first runner never became leader"

        await second.start()
        await asyncio.sleep(0.3)
        assert second.is_leader is False, "two leaders at once"

        # Kill the leader's lock session.
        executioner = create_engine(
            migrated_db, future=True, isolation_level="AUTOCOMMIT"
        )
        try:
            with executioner.connect() as conn:
                conn.exec_driver_sql(
                    """
                    SELECT pg_terminate_backend(pid) FROM pg_locks
                    WHERE locktype = 'advisory' AND granted
                      AND database = (
                          SELECT oid FROM pg_database
                          WHERE datname = current_database()
                      )
                    """
                ).fetchall()
        finally:
            executioner.dispose()

        # Enqueue only now, so draining it proves who is actually working.
        setup = StandbySessions()
        service.enqueue(
            setup,
            workflow_type=probe.PROBE_WORKFLOW_TYPE,
            idempotency_key="after-loss",
        )
        setup.close()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            check = StandbySessions()
            try:
                run = check.scalar(
                    select(WorkflowRun).where(
                        WorkflowRun.idempotency_key == "after-loss"
                    )
                )
                if run is not None and run.status == WorkflowStatus.succeeded.value:
                    break
            finally:
                check.close()
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("the replacement runner never drained the queue")

        # The former leader stood down; the replacement leads.
        assert first.is_leader is False, "the former leader still believes it leads"
        assert second.is_leader is True

        await first.stop()
        await second.stop()

    try:
        asyncio.run(_scenario())
    finally:
        leader_engine.dispose()
        standby_engine.dispose()


def test_losing_the_lock_while_a_handler_runs_stops_further_consumption(migrated_db):
    """Lock loss *during* execution, which the direct verify() test did not cover.

    The in-flight handler cannot be stopped (it is a thread), but the runner
    must not claim anything further once it no longer holds leadership.
    """
    engine = create_engine(migrated_db, future=True, pool_size=5, max_overflow=5)
    Sessions = sessionmaker(bind=engine, expire_on_commit=False)

    handler_started = threading.Event()
    may_finish = threading.Event()

    def blocking_handler(db, run):  # noqa: ARG001
        handler_started.set()
        assert may_finish.wait(timeout=60)
        return "done-despite-loss"

    registry.register("loss_probe", blocking_handler)

    setup = Sessions()
    service.enqueue(setup, workflow_type="loss_probe", idempotency_key="in-flight")
    setup.close()

    runner = WorkflowRunner(
        session_factory=Sessions,
        leader=LeaderLock(engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=60.0,  # do not silently re-acquire
        shutdown_grace_seconds=30.0,
    )

    async def _scenario() -> None:
        await runner.start()
        assert await asyncio.to_thread(handler_started.wait, 20)

        # Lock session dies while the handler is mid-flight.
        executioner = create_engine(
            migrated_db, future=True, isolation_level="AUTOCOMMIT"
        )
        try:
            with executioner.connect() as conn:
                conn.exec_driver_sql(
                    """
                    SELECT pg_terminate_backend(pid) FROM pg_locks
                    WHERE locktype = 'advisory' AND granted
                      AND database = (
                          SELECT oid FROM pg_database
                          WHERE datname = current_database()
                      )
                    """
                ).fetchall()
        finally:
            executioner.dispose()

        # A second unit of work appears; the stood-down runner must not take it.
        setup2 = Sessions()
        service.enqueue(
            setup2,
            workflow_type=probe.PROBE_WORKFLOW_TYPE,
            idempotency_key="must-not-run",
        )
        setup2.close()

        may_finish.set()
        await asyncio.sleep(1.0)

        check = Sessions()
        try:
            untouched = check.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.idempotency_key == "must-not-run"
                )
            )
            assert untouched.status == WorkflowStatus.queued.value, (
                "a runner that lost its lock kept consuming work"
            )
        finally:
            check.close()

        assert runner.is_leader is False
        await runner.stop()

    try:
        asyncio.run(_scenario())
    finally:
        may_finish.set()
        engine.dispose()


def test_lock_loss_during_execution_allows_at_least_once_invocation_not_duplicate_effect(
    migrated_db,
):
    """The bounded P-006 contract with two live runners.

    A lost lock session cannot cancel a live worker thread. The standby may
    therefore recover and invoke the same run before the former leader returns.
    That is explicitly *at-least-once invocation*, not exactly-once execution.
    The required safety boundary is the handler's atomic business commit: only
    one invocation may commit the durable effect.
    """
    first_engine = create_engine(migrated_db, future=True, pool_size=5)
    second_engine = create_engine(migrated_db, future=True, pool_size=5)
    FirstSessions = sessionmaker(bind=first_engine, expire_on_commit=False)
    SecondSessions = sessionmaker(bind=second_engine, expire_on_commit=False)

    effect_committed = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()
    second_invoked = threading.Event()
    counts = {"invocations": 0, "effects": 0}
    count_lock = threading.Lock()

    def atomic_blocking_handler(db, run):
        result_ref, did_effect = service.commit_result_ref_once(
            db, run, f"atomic-effect:{run.idempotency_key}"
        )
        with count_lock:
            counts["invocations"] += 1
            if did_effect:
                counts["effects"] += 1
        if did_effect:
            effect_committed.set()
            assert release_first.wait(timeout=60), "test did not release first handler"
            first_finished.set()
        else:
            second_invoked.set()
        return result_ref

    registry.register("atomic_loss_probe", atomic_blocking_handler)
    setup = FirstSessions()
    service.enqueue(
        setup, workflow_type="atomic_loss_probe", idempotency_key="atomic-loss"
    )
    setup.close()

    first = WorkflowRunner(
        session_factory=FirstSessions,
        leader=LeaderLock(first_engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        # Do not let the former leader retake the lock during this proof.
        leader_retry_seconds=60.0,
    )
    second = WorkflowRunner(
        session_factory=SecondSessions,
        leader=LeaderLock(second_engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=0.05,
    )

    async def _scenario() -> None:
        await first.start()
        assert await asyncio.to_thread(effect_committed.wait, 20)
        await second.start()
        await asyncio.sleep(0.2)
        assert second.is_leader is False

        # The first runner still has a live handler, but its dedicated lock
        # session is gone. The standby may take over and recover that `running`
        # row before the first worker thread returns.
        executioner = create_engine(
            migrated_db, future=True, isolation_level="AUTOCOMMIT"
        )
        try:
            with executioner.connect() as conn:
                killed = conn.exec_driver_sql(
                    """
                    SELECT pg_terminate_backend(pid) FROM pg_locks
                    WHERE locktype = 'advisory' AND granted
                      AND database = (
                          SELECT oid FROM pg_database
                          WHERE datname = current_database()
                      )
                    """
                ).fetchall()
            assert killed
        finally:
            executioner.dispose()

        assert await asyncio.to_thread(second_invoked.wait, 30), (
            "standby did not recover and invoke the interrupted run"
        )
        assert second.is_leader is True
        assert counts == {"invocations": 2, "effects": 1}

        # Finish the stale first invocation. It may mark the same run terminal,
        # but on the next cycle it must notice its lost lock and stand down.
        release_first.set()
        assert await asyncio.to_thread(first_finished.wait, 30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and first.is_leader:
            await asyncio.sleep(0.05)
        assert first.is_leader is False

        check = SecondSessions()
        try:
            run = check.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.idempotency_key == "atomic-loss"
                )
            )
            assert run.status == WorkflowStatus.succeeded.value
            assert run.result_ref == "atomic-effect:atomic-loss"
        finally:
            check.close()
        await first.stop()
        await second.stop()

    try:
        asyncio.run(_scenario())
    finally:
        release_first.set()
        first._leader.release()
        second._leader.release()
        first_engine.dispose()
        second_engine.dispose()

    assert counts == {"invocations": 2, "effects": 1}


def test_a_late_failing_invocation_cannot_overwrite_a_succeeded_row(migrated_db):
    """A stale duplicate invocation must not corrupt a completed run's row.

    Reproduces the exact defect a review found: with two live invocations of
    one run, if the winner commits the atomic effect and is marked
    `succeeded`, a *stale* invocation that later raises (for a reason unrelated
    to the effect — its own late-stage processing, say) must not flip the row
    back to `failed`. `commit_result_ref_once` alone does not guarantee this:
    it only makes the *effect* exactly-once. The terminal-state write
    (`mark_succeeded`/`mark_failed`) needed its own guard, which is what this
    proves.

    Sequenced with events rather than the earlier tests' pattern (block one
    handler on the winner path) precisely because the ordering under test is
    "succeeded is written, THEN a late failure is attempted" — the opposite
    order would not exercise the guard at all.
    """
    first_engine = create_engine(migrated_db, future=True, pool_size=5)
    second_engine = create_engine(migrated_db, future=True, pool_size=5)
    FirstSessions = sessionmaker(bind=first_engine, expire_on_commit=False)
    SecondSessions = sessionmaker(bind=second_engine, expire_on_commit=False)

    effect_committed = threading.Event()
    release_winner = threading.Event()
    loser_invoked = threading.Event()
    may_raise = threading.Event()

    def racing_handler(db, run):
        result_ref, performed_effect = service.commit_result_ref_once(
            db, run, f"race-effect:{run.idempotency_key}"
        )
        if performed_effect:
            # The winner: hold here so the test controls exactly when this
            # invocation returns (and run_once marks the row succeeded).
            effect_committed.set()
            assert release_winner.wait(timeout=60), "test did not release winner"
            return result_ref
        # The loser: wait until the test has confirmed the row is already
        # succeeded, THEN raise — reproducing "a stale invocation that later
        # raises" in the exact order the finding describes.
        loser_invoked.set()
        assert may_raise.wait(timeout=60), "test did not release the late failure"
        raise service.WorkflowError(
            "late_failure", "stale duplicate invocation failed after the winner"
        )

    registry.register("terminal_race_probe", racing_handler)
    setup = FirstSessions()
    service.enqueue(
        setup, workflow_type="terminal_race_probe", idempotency_key="terminal-race"
    )
    setup.close()

    first = WorkflowRunner(
        session_factory=FirstSessions,
        leader=LeaderLock(first_engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=60.0,  # do not let the former leader retake it
    )
    second = WorkflowRunner(
        session_factory=SecondSessions,
        leader=LeaderLock(second_engine, lock_key=_test_lock_key(migrated_db)),
        poll_seconds=0.05,
        leader_retry_seconds=0.05,
    )

    async def _wait_for_status(expected: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            check = SecondSessions()
            try:
                run = check.scalar(
                    select(WorkflowRun).where(
                        WorkflowRun.idempotency_key == "terminal-race"
                    )
                )
                if run is not None and run.status == expected:
                    return
            finally:
                check.close()
            await asyncio.sleep(0.05)
        raise AssertionError(f"row never reached status={expected!r}")

    async def _scenario() -> None:
        await first.start()
        assert await asyncio.to_thread(effect_committed.wait, 20)

        await second.start()
        await asyncio.sleep(0.2)
        assert second.is_leader is False

        # Kill the first runner's lock session (a failover, or the OS closing
        # a dead process's socket) so the standby can recover the row while
        # the winner's handler is still blocked, live, mid-invocation.
        executioner = create_engine(
            migrated_db, future=True, isolation_level="AUTOCOMMIT"
        )
        try:
            with executioner.connect() as conn:
                killed = conn.exec_driver_sql(
                    """
                    SELECT pg_terminate_backend(pid) FROM pg_locks
                    WHERE locktype = 'advisory' AND granted
                      AND database = (
                          SELECT oid FROM pg_database
                          WHERE datname = current_database()
                      )
                    """
                ).fetchall()
            assert killed
        finally:
            executioner.dispose()

        assert await asyncio.to_thread(loser_invoked.wait, 30), (
            "standby never recovered and invoked the second, losing invocation"
        )

        # THE ORDERING UNDER TEST: let the winner finish and be marked
        # succeeded FIRST, confirmed by reading the row back, before the loser
        # is allowed to raise.
        release_winner.set()
        await _wait_for_status(WorkflowStatus.succeeded.value, timeout=20)

        may_raise.set()
        # Give the second runner's loop a beat to process the raised exception
        # and attempt (and, if the fix holds, fail to apply) its mark_failed.
        await asyncio.sleep(1.0)

        check = SecondSessions()
        try:
            run = check.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.idempotency_key == "terminal-race"
                )
            )
            assert run.status == WorkflowStatus.succeeded.value, (
                "a late failing invocation corrupted an already-succeeded row"
            )
            assert run.result_ref == "race-effect:terminal-race"
            assert run.error_code is None
            assert run.error_message is None
        finally:
            check.close()

        await first.stop()
        await second.stop()

    try:
        asyncio.run(_scenario())
    finally:
        release_winner.set()
        may_raise.set()
        first._leader.release()
        second._leader.release()
        first_engine.dispose()
        second_engine.dispose()


def test_a_runner_stands_down_when_its_lock_session_is_killed(migrated_db):
    """Finding 3: a dead lock session must not leave a false leader.

    After the lock session is terminated Postgres has released the advisory
    lock, so another task can take it. This process must notice and stop
    executing rather than keep claiming work through its pooled connections.
    """
    engine = create_engine(migrated_db, future=True)
    leader = LeaderLock(engine, lock_key=_test_lock_key(migrated_db))
    try:
        assert leader.acquire() is True
        assert leader.verify() is True

        # Kill the lock session from outside, as a failover would.
        executioner = create_engine(
            migrated_db, future=True, isolation_level="AUTOCOMMIT"
        )
        try:
            with executioner.connect() as conn:
                conn.exec_driver_sql(
                    """
                    SELECT pg_terminate_backend(pid) FROM pg_locks
                    WHERE locktype = 'advisory' AND granted
                      AND database = (
                          SELECT oid FROM pg_database
                          WHERE datname = current_database()
                      )
                    """
                ).fetchall()
        finally:
            executioner.dispose()

        # The claim is no longer true, and verify() must say so.
        assert leader.verify() is False
        assert leader.is_leader is False
        # The freed lock is available to the next task.
        assert _lock_is_free(migrated_db) is True
    finally:
        leader.release()
        engine.dispose()


def test_status_check_constraint_rejects_an_unknown_status(sessions):
    db = sessions()
    try:
        db.add(
            WorkflowRun(
                workflow_type=probe.PROBE_WORKFLOW_TYPE,
                status="not_a_status",
                idempotency_key=f"bad-{uuid.uuid4().hex}",
                progress=0,
                attempts=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()
