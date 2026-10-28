"""The durable workflow runner (V2 Step 1.3, P-006 / plan §5.2).

What this must prove, per the plan: enqueue, poll to completion, a duplicate
enqueue that does not double-run, and clean recovery of a run interrupted
mid-flight. Plus the two invariants that make the row safe to keep and show:
no content-bearing columns, and no upstream exception text in `error_message`.

The advisory-lock half of leadership is a Postgres mechanism and is proved in
`tests/integration/test_workflow_runner_pg.py`; SQLite has no advisory locks.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SQLAlchemySession, sessionmaker

from backend.db.models.site import Site
from backend.db.models.workflow import (
    MAX_ERROR_MESSAGE,
    WorkflowRun,
    WorkflowStatus,
)
from backend.workflows import registry, service
from backend.workflows.handlers import probe
from backend.workflows.runner import WorkflowRunner, drain, run_once


@pytest.fixture(autouse=True)
def _reset_probe_counts():
    probe.reset_call_counts()
    yield
    probe.reset_call_counts()


def _enqueue(db, key="k1", workflow_type=probe.PROBE_WORKFLOW_TYPE):
    return service.enqueue(
        db,
        workflow_type=workflow_type,
        idempotency_key=key,
        subject_type="consultant",
        subject_id="norberto.munoz",
    )


# ── Enqueue is the acceptance boundary ────────────────────────────────────

def test_enqueue_creates_a_queued_run(db):
    run = _enqueue(db)
    assert run.status == WorkflowStatus.queued.value
    assert run.progress == 0
    assert run.attempts == 0
    assert run.result_ref is None
    assert run.is_terminal is False
    # Typed reference to the subject, not a copy of the subject's data.
    assert (run.subject_type, run.subject_id) == ("consultant", "norberto.munoz")


def test_duplicate_enqueue_returns_the_same_run(db):
    first = _enqueue(db, key="same-key")
    second = _enqueue(db, key="same-key")
    assert second.id == first.id
    assert db.query(WorkflowRun).count() == 1


def test_duplicate_enqueue_does_not_double_run_the_handler(db):
    """The 60-second-timeout case: a client retries the POST after giving up.

    Status alone cannot distinguish "ran once" from "ran twice", hence the
    handler's call counter.
    """
    _enqueue(db, key="dup")
    _enqueue(db, key="dup")

    assert drain(db) == 1
    assert probe.call_count("dup") == 1


# ── Enqueue's transaction contract: every return path commits ─────────────
#
# `enqueue` commits the caller's transaction, because
# `tailored_cv.service.request_generation` relies on the domain row and the run
# becoming visible together while it still holds the Application's row lock. It
# has **three** return paths, and `S3-P1-R2` found one of them (the key already
# exists) returning without a commit: the caller got a successful run back and
# silently lost the row the run was for.
#
# The invariant is about the commit, not about flushing. `Session.close()`
# discards whatever was not committed — pending objects if they were never
# flushed, an open transaction if they were — so a missing commit loses the
# caller's work under either flush policy. An earlier version of this comment
# blamed the lookup `SELECT` for autoflushing, which is SQLAlchemy's default but
# **not** what the deployed application does (`backend/db/session.py` sets
# `autoflush=False`): `S3-P1-R3` finding 1.
#
# So these run under **both** policies, deliberately: the deployed one, and the
# repository fixture's default. That is what makes the claim "durable only if
# enqueue commits" a property of the code rather than of a session setting.
#
# One test per path, because a contract that holds on two of three paths is the
# shape of defect that gets missed. Each asserts from a **fresh Session**: the
# writing Session would happily show its own uncommitted work, so asserting
# there is exactly the check that passed while the defect was live.


@pytest.fixture(
    params=[False, True],
    ids=["autoflush-off-as-deployed", "autoflush-on-fixture-default"],
)
def CallerSession(request, Session):
    """A session factory whose flush policy is stated rather than inherited.

    `backend/db/session.py` builds `SessionLocal` with `autoflush=False`; the
    repository `Session` fixture takes SQLAlchemy's `autoflush=True` default. For
    tests about *when work becomes durable* that difference is the subject, so it
    is parametrized here instead of being whichever the fixture happened to pick.
    Built on the fixture's own engine, so the seeded Site and schema still apply.
    """
    probe = Session()
    try:
        bind = probe.get_bind()
    finally:
        probe.close()
    return sessionmaker(
        bind=bind,
        autocommit=False,
        autoflush=request.param,
        class_=SQLAlchemySession,
    )


def _pending_caller_work(db, code: str) -> None:
    """Work the caller owns and expects `enqueue` to commit for it.

    A `Site` because it is unrelated to workflows, so nothing in `enqueue` can
    special-case it. `is_active=False` keeps the single-active-Site invariant
    (P-005) out of the picture.
    """
    db.add(Site(code=code, name=f"Pending {code}", is_active=False))


def _committed(Session, code: str) -> int:
    fresh = Session()
    try:
        return (
            fresh.query(Site).filter(Site.code == code).count()
        )
    finally:
        fresh.close()


def test_enqueue_commits_caller_owned_work_when_it_creates_the_run(CallerSession):
    """Path 3, the ordinary case — the baseline the other two must match."""
    db = CallerSession()
    try:
        _pending_caller_work(db, "PATH3")
        _enqueue(db, key="path-3")
    finally:
        db.close()

    assert _committed(CallerSession, "PATH3") == 1


def test_enqueue_commits_caller_owned_work_when_the_key_already_exists(
    CallerSession,
):
    """Path 1 — the `S3-P1-R2` finding.

    The run is seeded and committed first, so the lookup inside `enqueue` finds
    it and takes the early return. Before the fix the caller's `Site` never
    reached the database: `enqueue` returned the run as if everything had landed,
    and `close()` discarded the pending row.
    """
    seed = CallerSession()
    try:
        _enqueue(seed, key="already-there")
    finally:
        seed.close()

    db = CallerSession()
    try:
        _pending_caller_work(db, "PATH1")
        returned = _enqueue(db, key="already-there")
        assert returned is not None
        # It really is the early return, not a second insert.
        assert db.query(WorkflowRun).count() == 1
    finally:
        db.close()

    assert _committed(CallerSession, "PATH1") == 1, (
        "enqueue returned an existing run but discarded the caller's pending "
        "work: the domain row the run is for is gone"
    )


def test_enqueue_commits_caller_owned_work_when_it_loses_the_index_race(
    CallerSession, monkeypatch
):
    """Path 2 — the savepoint path added for `S3-P1-R1` finding 4.

    The race is simulated at the only point that matters: the row exists in the
    database, and our lookup does not see it. That is precisely what a concurrent
    winner committing between our `SELECT` and our flush produces, and it is
    deterministic here, unlike two real sessions over the fast gate's single
    shared SQLite connection. The genuine lock-wait version of this race lives in
    `tests/integration/test_workflow_runner_pg.py`.
    """
    seed = CallerSession()
    try:
        _enqueue(seed, key="racy")
    finally:
        seed.close()

    db = CallerSession()
    calls = {"n": 0}
    real_scalar = db.scalar

    def blind_on_the_first_lookup(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_scalar(*args, **kwargs)

    monkeypatch.setattr(db, "scalar", blind_on_the_first_lookup)
    try:
        _pending_caller_work(db, "PATH2")
        returned = _enqueue(db, key="racy")
        assert returned is not None
        # The winner's row, and no duplicate.
        assert db.query(WorkflowRun).count() == 1
        assert calls["n"] == 2, "the IntegrityError path was not the one taken"
    finally:
        db.close()

    assert _committed(CallerSession, "PATH2") == 1


# ── Poll to completion ────────────────────────────────────────────────────

def test_run_once_executes_and_completes(db):
    run = _enqueue(db, key="run-me")
    assert run_once(db) is True

    db.refresh(run)
    assert run.status == WorkflowStatus.succeeded.value
    assert run.is_terminal is True
    assert run.result_ref == "probe-result:run-me"
    assert run.progress == 100
    assert run.attempts == 1
    assert run.started_at is not None and run.finished_at is not None
    assert run.error_code is None


def test_run_once_is_a_noop_on_an_empty_queue(db):
    assert run_once(db) is False


def test_terminal_runs_are_not_claimed_again(db):
    _enqueue(db, key="once")
    assert run_once(db) is True
    # Nothing left to claim, and the handler is not re-entered.
    assert run_once(db) is False
    assert probe.call_count("once") == 1


def test_status_projection_is_pollable(db):
    run = _enqueue(db, key="poll")
    queued = run.to_status_dict()
    assert queued["status"] == "queued"
    assert queued["progress"] == 0

    run_once(db)
    db.refresh(run)
    done = run.to_status_dict()
    assert done["status"] == "succeeded"
    assert done["progress"] == 100
    assert done["result_ref"] == "probe-result:poll"


# ── Crash mid-run → startup recovery ──────────────────────────────────────

def test_recovery_requeues_a_run_interrupted_mid_flight(db):
    """Simulates the state a killed process leaves behind: `running`.

    A literal SIGKILL is not reproduced here — a killed process leaves exactly
    this row state, and asserting on the state is deterministic where killing a
    process mid-test is not. The integration gate runs the same recovery against
    real Postgres.
    """
    run = _enqueue(db, key="crashed")
    run.status = WorkflowStatus.running.value
    run.attempts = 1
    db.commit()

    assert service.recover_interrupted(db) == 1
    db.refresh(run)
    assert run.status == WorkflowStatus.queued.value
    assert run.stage == "requeued_after_restart"

    # And the re-queued run then completes normally.
    assert run_once(db) is True
    db.refresh(run)
    assert run.status == WorkflowStatus.succeeded.value


def test_recovery_after_business_commit_does_not_redo_the_work(db):
    """The crash-after-business-commit case the handler contract exists for.

    The handler committed `result_ref` but the process died before
    `mark_succeeded`. Recovery re-queues it; the handler must observe the
    committed effect and not perform the work twice.
    """
    run = _enqueue(db, key="half-done")
    # Exactly the durable state of that crash: effect committed, still running.
    run.status = WorkflowStatus.running.value
    run.result_ref = "probe-result:half-done"
    run.attempts = 1
    db.commit()

    service.recover_interrupted(db)
    assert run_once(db) is True

    db.refresh(run)
    assert run.status == WorkflowStatus.succeeded.value
    assert run.result_ref == "probe-result:half-done"
    # The work itself never ran in this process — the effect was already there.
    assert probe.call_count("half-done") == 0


def test_recovery_leaves_terminal_runs_alone(db):
    _enqueue(db, key="finished")
    run_once(db)
    assert service.recover_interrupted(db) == 0


# ── Failure is bounded and safe ───────────────────────────────────────────

def test_handler_failure_is_recorded_not_raised(db):
    _enqueue(db, key="boom", workflow_type=probe.PROBE_FAILURE_WORKFLOW_TYPE)
    assert run_once(db) is True

    run = db.query(WorkflowRun).filter_by(idempotency_key="boom").one()
    assert run.status == WorkflowStatus.failed.value
    assert run.error_code == "probe_failed"
    assert run.finished_at is not None


def test_unknown_workflow_type_fails_the_run_without_killing_the_loop(db):
    _enqueue(db, key="mystery", workflow_type="not_registered")
    assert run_once(db) is True

    run = db.query(WorkflowRun).filter_by(idempotency_key="mystery").one()
    assert run.status == WorkflowStatus.failed.value
    assert run.error_code == "no_handler"
    # The loop is still usable afterwards.
    _enqueue(db, key="after")
    assert run_once(db) is True


def test_unexpected_exception_text_is_never_persisted(db):
    """An arbitrary exception's message can embed a prompt, a CV excerpt, or a
    token. Only a deliberately raised WorkflowError contributes text."""
    secret = "PROMPT-WITH-CV-CONTENT-alice-personnummer"

    def leaky_handler(_db, _run):
        raise RuntimeError(secret)

    registry.register("leaky", leaky_handler)
    _enqueue(db, key="leak", workflow_type="leaky")
    run_once(db)

    run = db.query(WorkflowRun).filter_by(idempotency_key="leak").one()
    assert run.status == WorkflowStatus.failed.value
    assert run.error_code == "unexpected_error"
    assert run.error_message == "RuntimeError"
    assert secret not in (run.error_message or "")


def test_error_message_is_bounded(db):
    def verbose_handler(_db, _run):
        raise service.WorkflowError("too_long", "x" * (MAX_ERROR_MESSAGE * 3))

    registry.register("verbose", verbose_handler)
    _enqueue(db, key="verbose", workflow_type="verbose")
    run_once(db)

    run = db.query(WorkflowRun).filter_by(idempotency_key="verbose").one()
    assert len(run.error_message) <= MAX_ERROR_MESSAGE


# ── Terminal-state writes are monotonic, not just the effect ──────────────
#
# commit_result_ref_once makes the *business effect* exactly-once, but the
# lock-loss contract permits two live invocations of one run, and each one
# independently tries to write a terminal status. These prove the row itself
# — not just the effect — cannot be corrupted by whichever invocation writes
# second. Real concurrency (a genuinely stale invocation racing a completed
# one) is proved on Postgres in
# test_workflow_runner_pg.py::test_a_late_failing_invocation_cannot_overwrite_a_succeeded_row;
# these exercise the same guards directly, without needing two live runners.

def test_mark_failed_does_not_downgrade_an_already_succeeded_run(db):
    run = _enqueue(db, key="already-won")
    service.mark_succeeded(db, run, result_ref="the-real-effect")
    db.refresh(run)
    assert run.status == WorkflowStatus.succeeded.value

    applied = service.mark_failed(db, run, service.WorkflowError("late", "too late"))

    assert applied is False
    db.refresh(run)
    assert run.status == WorkflowStatus.succeeded.value
    assert run.result_ref == "the-real-effect"
    assert run.error_code is None
    assert run.error_message is None


def test_mark_failed_applies_normally_when_not_already_succeeded(db):
    run = _enqueue(db, key="genuine-failure")
    applied = service.mark_failed(db, run, service.WorkflowError("boom", "real"))

    assert applied is True
    db.refresh(run)
    assert run.status == WorkflowStatus.failed.value
    assert run.error_code == "boom"


def test_mark_succeeded_can_upgrade_a_stale_failed_row(db):
    """A late genuine success is authoritative over an earlier stale failure.

    Unlike mark_failed, mark_succeeded is not guarded against overwriting
    `failed`: if the effect commits and this invocation completes, that is the
    true outcome regardless of what an earlier, ultimately-wrong invocation
    wrote.
    """
    run = _enqueue(db, key="recovers")
    service.mark_failed(db, run, service.WorkflowError("premature", "too early"))
    db.refresh(run)
    assert run.status == WorkflowStatus.failed.value

    service.mark_succeeded(db, run, result_ref="the-real-effect")

    db.refresh(run)
    assert run.status == WorkflowStatus.succeeded.value
    assert run.result_ref == "the-real-effect"
    assert run.error_code is None
    assert run.error_message is None


def test_mark_succeeded_is_idempotent_and_does_not_rewrite_finished_at(db):
    run = _enqueue(db, key="already-done")
    service.mark_succeeded(db, run, result_ref="ref-1")
    db.refresh(run)
    first_finished_at = run.finished_at

    # A second, redundant success write (e.g. a benign duplicate invocation
    # reaching the same conclusion) must not disturb the existing record.
    service.mark_succeeded(db, run, result_ref="ref-1")

    db.refresh(run)
    assert run.finished_at == first_finished_at


def test_record_stage_is_a_noop_once_the_run_is_terminal(db):
    """A stale invocation's progress update must not make a completed run
    look like it regressed to mid-flight."""
    run = _enqueue(db, key="finished-already")
    service.mark_succeeded(db, run, result_ref="done-ref")
    db.refresh(run)

    service.record_stage(db, run, "stale-stage", progress=17)

    db.refresh(run)
    assert run.stage == "done"
    assert run.progress == 100


# ── Leadership and startup robustness ─────────────────────────────────────

class _StubLeader:
    """Stands in for LeaderLock without needing a database.

    `acquire_results` is consumed one call at a time, so a test can script
    "lose, lose, then win" — the rolling-deployment takeover sequence.
    """

    def __init__(
        self,
        *,
        error: BaseException | None = None,
        acquired: bool = False,
        acquire_results: list[bool] | None = None,
        verify_results: list[bool] | None = None,
    ):
        self._error = error
        self._acquired = acquired
        self._acquire_results = list(acquire_results or [])
        self._verify_results = list(verify_results or [])
        self.is_leader = False
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        if self._error is not None:
            raise self._error
        if self._acquire_results:
            got = self._acquire_results.pop(0)
        else:
            got = self._acquired
        self.is_leader = got
        return got

    def verify(self) -> bool:
        if self._verify_results:
            ok = self._verify_results.pop(0)
            if not ok:
                self.is_leader = False
            return ok
        return self.is_leader

    def release(self) -> None:
        self.release_calls += 1
        self.is_leader = False


def _run_briefly(runner, seconds: float = 0.3) -> None:
    """Start the runner, let it supervise for a moment, then stop it.

    The repo configures no async pytest plugin, so the async lifecycle is driven
    through one `asyncio.run` rather than adding test infrastructure.
    """

    async def _cycle():
        await runner.start()
        await asyncio.sleep(seconds)
        await runner.stop()

    asyncio.run(_cycle())


def test_runner_start_is_not_fatal_when_leadership_errors():
    """An unreachable database must not stop the API from serving.

    Queued work is durable, so "no runner right now" is recoverable; a web
    process that refuses to boot is not. The single-Site startup guard remains
    the deliberate hard gate on an unreachable database.
    """
    leader = _StubLeader(
        error=OperationalError("SELECT 1", {}, Exception("refused"))
    )
    runner = WorkflowRunner(
        session_factory=None,
        leader=leader,
        poll_seconds=0.01,
        leader_retry_seconds=0.01,
    )
    _run_briefly(runner)  # must not raise
    assert runner.is_leader is False
    # And it kept trying rather than giving up after the first failure.
    assert leader.acquire_calls > 1


def test_runner_does_not_consume_work_when_it_is_not_leader(db):
    """The losing task in a rolling deployment: no work consumed."""
    _enqueue(db, key="untouched")
    leader = _StubLeader(acquired=False)
    runner = WorkflowRunner(
        session_factory=lambda: db,
        leader=leader,
        poll_seconds=0.01,
        leader_retry_seconds=0.01,
    )
    _run_briefly(runner)

    run = db.query(WorkflowRun).filter_by(idempotency_key="untouched").one()
    assert run.status == WorkflowStatus.queued.value
    assert probe.call_count("untouched") == 0


def test_a_standby_keeps_trying_and_takes_over(db):
    """The blocking defect this covers: a task that lost the initial race must
    not stay runnerless once the incumbent goes away.

    Scripted as "lose, lose, then win" — which is what an ECS rolling
    deployment looks like from the new task's point of view.
    """
    _enqueue(db, key="handover")
    leader = _StubLeader(acquire_results=[False, False, True])
    runner = WorkflowRunner(
        session_factory=lambda: db,
        leader=leader,
        poll_seconds=0.01,
        leader_retry_seconds=0.01,
    )
    _run_briefly(runner)

    # Took over, then did the work that was waiting.
    assert leader.acquire_calls >= 3
    run = db.query(WorkflowRun).filter_by(idempotency_key="handover").one()
    assert run.status == WorkflowStatus.succeeded.value
    assert probe.call_count("handover") == 1


def test_recovery_failure_blocks_consumption_until_it_succeeds(db):
    """A silently-failed recovery must not be followed by business as usual.

    `running` rows are not claimable, so consuming new work while recovery is
    broken strands them until some later restart happens to succeed. The runner
    keeps leadership (handing it over would only move the same database
    problem) and retries recovery before touching the queue.
    """
    _enqueue(db, key="gated")
    calls = {"recover": 0}
    real_recover = service.recover_interrupted
    # Recovery keeps failing until the test allows it — deterministic, rather
    # than racing a retry interval.
    recovery_allowed = threading.Event()

    def flaky_recover(session):
        calls["recover"] += 1
        if not recovery_allowed.is_set():
            raise OperationalError("UPDATE", {}, Exception("transient"))
        return real_recover(session)

    leader = _StubLeader(acquire_results=[True])
    runner = WorkflowRunner(
        session_factory=lambda: db,
        leader=leader,
        poll_seconds=0.01,
        leader_retry_seconds=0.01,
    )

    with patch.object(service, "recover_interrupted", flaky_recover):
        async def _cycle():
            await runner.start()
            # While recovery keeps failing, nothing may be consumed no matter
            # how many cycles pass.
            await asyncio.sleep(0.3)
            first_pass = (
                db.query(WorkflowRun).filter_by(idempotency_key="gated").one().status
            )
            failed_attempts = calls["recover"]

            # Once it succeeds, the queue drains normally.
            recovery_allowed.set()
            await asyncio.sleep(0.3)
            await runner.stop()
            return first_pass, failed_attempts

        first_pass_status, failed_attempts = asyncio.run(_cycle())

    assert first_pass_status == WorkflowStatus.queued.value, (
        "work was consumed before recovery succeeded"
    )
    assert failed_attempts >= 2, "recovery was not retried while it kept failing"
    # Leadership was retained across the failure rather than handed away.
    assert leader.acquire_calls == 1
    run = db.query(WorkflowRun).filter_by(idempotency_key="gated").one()
    assert run.status == WorkflowStatus.succeeded.value


def test_runner_stands_down_when_leadership_can_no_longer_be_verified(db):
    """A lock session that died must not leave a false leader executing work."""
    leader = _StubLeader(acquire_results=[True], verify_results=[False])
    runner = WorkflowRunner(
        session_factory=lambda: db,
        leader=leader,
        poll_seconds=0.01,
        leader_retry_seconds=0.01,
    )

    async def _cycle():
        await runner.start()
        await asyncio.sleep(0.2)
        await runner.stop()

    asyncio.run(_cycle())

    # Standing down releases the (dead) lock so it can be re-acquired cleanly.
    assert leader.release_calls >= 1


# ── Application wiring ────────────────────────────────────────────────────
#
# The failure these catch is "everything works in tests, nothing runs in the
# app": handlers registered only because a test imported them, or a lifespan
# that composes a runner against the wrong session factory. Neither shows up in
# a state-machine test.

def test_the_app_process_has_the_built_in_handlers_registered():
    import backend.main  # noqa: F401  (the real app module, as uvicorn imports it)

    assert probe.PROBE_WORKFLOW_TYPE in registry.registered_types()


def test_the_lifespan_composes_a_runner_on_the_app_session_factory():
    """Composition only — this must not open a connection."""
    from backend.db.session import SessionLocal, engine
    from backend.main import _build_workflow_runner

    runner = _build_workflow_runner()
    assert isinstance(runner, WorkflowRunner)
    assert runner._session_factory is SessionLocal
    assert runner._leader._engine is engine
    # Composed but not yet leading: leadership is acquired in start().
    assert runner.is_leader is False


# ── The row holds references, never content ───────────────────────────────

def test_workflow_run_has_no_content_bearing_columns():
    """Guards the plan §5.2 invariant structurally, not by reviewer vigilance.

    Adding e.g. `cv_text`, `prompt`, or `email_body` to this table fails here.
    Widening the allowlist is a deliberate act that has to be justified.
    """
    allowed = {
        "id", "created_at", "updated_at",
        "workflow_type", "status", "stage", "progress",
        "idempotency_key",
        "subject_type", "subject_id",
        "input_ref", "result_ref",
        "error_code", "error_message",
        "attempts", "started_at", "finished_at",
    }
    actual = {c.name for c in WorkflowRun.__table__.columns}
    assert actual == allowed, (
        "workflow_runs columns changed; this table must hold references only "
        f"(unexpected: {sorted(actual - allowed)})"
    )


def test_reference_columns_cannot_hold_a_large_payload():
    """`input_ref`/`result_ref` are sized for keys and ids, not documents."""
    columns = {c.name: c for c in WorkflowRun.__table__.columns}
    for name in ("input_ref", "result_ref"):
        assert columns[name].type.length <= 500
