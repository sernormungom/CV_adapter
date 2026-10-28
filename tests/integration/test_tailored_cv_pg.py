"""The tailored CV against real Postgres 16 (V2 Slice 3).

What needs the real engine, and why the fast gate cannot answer it:

- **Concurrent Generate.** The unique `application_id` index exists because a
  check-then-insert is not enough: a double-clicked button, or a POST retried
  after the 60-second edge, are two transactions racing the same Application.
  Only two connections prove it, and only Postgres gives the loser a real
  integrity error inside a transaction that then has to be recovered.
- **Concurrent handler invocations.** The runner's contract is at-least-once
  *invocation*, exactly-once *effect*: a dead lock session can leave one
  invocation running while a standby recovers the run and starts another. The
  effect boundary is a conditional `UPDATE`, and whether it really admits one
  writer is a question about row locking under two connections.
- **Schema shape after `alembic upgrade`.** The fast gate builds its schema with
  `create_all`; production runs the migration. A missing unique index or a
  check constraint that ended up absent would pass every fast test and then
  allow two artifacts for one Application, or a status the code cannot handle.
- **Cascades.** GDPR erasure deletes tailored CVs explicitly, but the deployed
  engine must also agree that deleting an Application takes its CV with it —
  otherwise an erasure that ever relies on the cascade leaves CV documents
  behind.
- **The row-lock ordering** the services claim (Job → pair → Application →
  tailored CV). SQLite's single-writer model cannot demonstrate a lock at all.

Every database here is created and dropped by the fixtures in `conftest.py`.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.applications import service as applications_service
from backend.cv_state import service as cv_state_service
from backend.db.models import (
    Application,
    Job,
    TailoredCV,
    User,
    WorkflowRun,
)
from backend.jobs import service as jobs_service
from backend.tailored_cv import service

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"

DESCRIPTION = (
    "Own an automotive control unit end to end: C, AUTOSAR, CAN, and "
    "ISO 26262 functional-safety work alongside a verification team."
)


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


@pytest.fixture
def pg_application(migrated_db, tmp_path, monkeypatch):
    """A sessionmaker on the migrated database plus one live Application.

    Also points the profile root at a throwaway directory and installs a
    profile, because `request_generation` snapshots one. Returns
    `(Session, application_id, consultant_id, ta_id)`.
    """
    import yaml

    from backend import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.profile_reader.DATA_DIR", tmp_path)
    monkeypatch.setattr("backend.cv_pipeline.context_assembler.DATA_DIR", tmp_path)

    engine = create_engine(migrated_db)
    Session = sessionmaker(bind=engine)
    site_id, ta_id, consultant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sites (id, code, name, is_active, created_at, "
                "updated_at) VALUES (:id, 'GBG', 'Test', true, now(), now())"
            ),
            {"id": site_id},
        )
        for uid, username, role in (
            (ta_id, "pg-cv-ta", "ta"),
            (consultant_id, "pg-cv-consultant", "consultant"),
        ):
            conn.execute(
                text(
                    "INSERT INTO users (id, email, username, is_active, "
                    "actively_looking, site_id, created_at, updated_at) VALUES "
                    "(:id, :email, :username, true, true, :site, now(), now())"
                ),
                {
                    "id": uid,
                    "email": f"{username}@example.invalid",
                    "username": username,
                    "site": site_id,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO user_roles (id, user_id, role) "
                    "VALUES (:id, :uid, :role)"
                ),
                {"id": uuid.uuid4(), "uid": uid, "role": role},
            )
        conn.execute(
            text(
                "INSERT INTO ta_consultants (id, ta_id, consultant_id) "
                "VALUES (:id, :ta, :c)"
            ),
            {"id": uuid.uuid4(), "ta": ta_id, "c": consultant_id},
        )

    root = tmp_path / "pg-cv-consultant"
    root.mkdir(parents=True, exist_ok=True)
    (root / "profile.md").write_text(
        "# Profile\n\n```yaml\n"
        + yaml.dump({"summary": "Embedded work", "career_history": {}})
        + "```\n",
        encoding="utf-8",
    )
    (root / "style.md").write_text("## Rules\n- Terse.\n", encoding="utf-8")
    # Generation refuses a consultant with no photo (V3-P-023).
    photo_src = "/api/profiles/pg-cv-consultant/photo"
    base = cv_state_service.load_base("pg-cv-consultant")
    saved = cv_state_service.save_base(
        "pg-cv-consultant",
        {**base.state, "photoSrc": photo_src},
        expected_revision=base.revision,
    )
    assert saved.state.get("photoSrc") == photo_src

    setup = Session()
    try:
        created = jobs_service.create_job(
            setup,
            jobs_service.build_input(
                title="Senior Embedded Engineer",
                company="Client AB",
                description=DESCRIPTION,
            ),
            consultant=setup.get(User, consultant_id),
            actor=setup.get(User, ta_id),
        )
        application, _ = applications_service.apply(
            setup,
            created.job,
            setup.get(User, consultant_id),
            actor=setup.get(User, ta_id),
        )
        application_id = application.id
    finally:
        setup.close()

    try:
        yield Session, application_id, consultant_id, ta_id
    finally:
        engine.dispose()


def _race(target, count: int = 2) -> tuple[list, list]:
    """Run `target(barrier, results, errors)` in `count` threads."""
    barrier = threading.Barrier(count)
    results: list = []
    errors: list = []
    threads = [
        threading.Thread(target=target, args=(barrier, results, errors))
        for _ in range(count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


# ── Concurrent Generate ───────────────────────────────────────────────────


def test_two_concurrent_generate_requests_yield_one_artifact(pg_application):
    """The real race, with a barrier so both transactions are genuinely in
    flight before either commits.

    Both threads pass their own check-then-insert; the unique `application_id`
    index is what decides. Neither may raise, and there must be exactly one
    artifact afterwards.
    """
    Session, application_id, _consultant_id, ta_id = pg_application

    def attempt(barrier, results, errors) -> None:
        db = Session()
        try:
            application = db.get(Application, application_id)
            actor = db.get(User, ta_id)
            barrier.wait(timeout=30)
            tailored, run, started = service.request_generation(
                db, application, actor=actor
            )
            results.append((started, str(tailored.id), str(run.id)))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)
        finally:
            db.close()

    results, errors = _race(attempt)

    assert not errors, f"a racing generate raised instead of resolving: {errors}"
    assert len(results) == 2
    assert len({artifact for _, artifact, _ in results}) == 1, (
        "two concurrent requests produced two tailored CVs for one application"
    )

    verify = Session()
    try:
        assert verify.query(TailoredCV).count() == 1
        # And exactly one generation was started. This is the assertion that
        # caught the real defect: with a stored `generation_run_id` — written
        # only *after* the enqueue committed — the second request took the
        # artifact's lock inside that window, read a null reference, concluded
        # nothing was in flight, and enqueued a duplicate generation.
        assert verify.query(WorkflowRun).count() == 1
        # …and the loser was told it joined rather than started one.
        assert sorted(started for started, _, _ in results) == [False, True]
        assert len({run_id for _, _, run_id in results}) == 1
    finally:
        verify.close()


def test_a_raw_second_artifact_for_one_application_is_rejected(pg_application):
    Session, application_id, _consultant_id, ta_id = pg_application
    db = Session()
    try:
        service.request_generation(
            db, db.get(Application, application_id), actor=db.get(User, ta_id)
        )
        db.add(
            TailoredCV(
                application_id=application_id,
                status="generating",
                revision=0,
                input_snapshot={},
                input_hash="x" * 64,
                generation_attempt=0,
            )
        )
        with pytest.raises(IntegrityError) as exc:
            db.commit()
        assert _sqlstate(exc.value) == UNIQUE_VIOLATION
    finally:
        db.rollback()
        db.close()


# ── The exactly-once effect ───────────────────────────────────────────────


def test_two_concurrent_invocations_commit_the_effect_exactly_once(
    pg_application,
):
    """The runner's contract, at this handler's own boundary.

    A dead advisory-lock session can leave one invocation executing while a
    standby recovers the run and starts another (P-006 defers the leases that
    would prevent it). Both reach `commit_generated_state_once`; exactly one may
    change the row, and the revision must move exactly once.
    """
    Session, application_id, _consultant_id, ta_id = pg_application
    setup = Session()
    try:
        tailored, _run, _started = service.request_generation(
            setup, setup.get(Application, application_id), actor=setup.get(User, ta_id)
        )
        key = service.GenerationKey(
            tailored_cv_id=tailored.id,
            input_hash=tailored.input_hash,
            attempt=tailored.generation_attempt,
        )
    finally:
        setup.close()

    def attempt(barrier, results, errors) -> None:
        db = Session()
        try:
            barrier.wait(timeout=30)
            _artifact, performed = service.commit_generated_state_once(
                db, key, {"iName": "Generated once"}
            )
            results.append(performed)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    results, errors = _race(attempt)

    assert not errors, f"a concurrent invocation raised: {errors}"
    assert sorted(results) == [False, True], (
        "the effect boundary admitted more than one writer"
    )

    verify = Session()
    try:
        row = verify.query(TailoredCV).one()
        assert row.revision == 1
        assert row.status == "ready"
        assert row.cv_state == {"iName": "Generated once"}
    finally:
        verify.close()


def test_a_superseded_attempt_changes_nothing_and_reports_it(pg_application):
    """A slow attempt still executing beside a newer one. Its predicate no
    longer matches, so it performs no effect — and must say so rather than
    reporting success."""
    Session, application_id, _consultant_id, ta_id = pg_application
    db = Session()
    try:
        tailored, _run, _ = service.request_generation(
            db, db.get(Application, application_id), actor=db.get(User, ta_id)
        )
        current = service.GenerationKey(
            tailored_cv_id=tailored.id,
            input_hash=tailored.input_hash,
            attempt=tailored.generation_attempt,
        )
        service.commit_generated_state_once(db, current, {"iName": "Current"})

        superseded = service.GenerationKey(
            tailored_cv_id=tailored.id,
            input_hash="a-superseded-hash",
            attempt=tailored.generation_attempt,
        )
        artifact, performed = service.commit_generated_state_once(
            db, superseded, {"iName": "Superseded"}
        )
        assert performed is False
        assert artifact.revision == 1
        assert artifact.cv_state == {"iName": "Current"}

        # The same guard on the failure path: a superseded attempt raising
        # afterwards must not downgrade a good artifact.
        assert service.record_generation_failure(
            db, superseded, code="late", message="from a superseded attempt"
        ) is False
        db.expire_all()
        assert db.query(TailoredCV).one().status == "ready"
    finally:
        db.close()


def test_an_identical_save_writes_nothing_and_releases_the_row_lock(
    pg_application,
):
    """S22 (V3-P-033): a content-identical save is a no-op on the real engine.

    Two things SQLite cannot show. `save_state` takes the row's `FOR UPDATE`
    lock and the no-op path has nothing to commit, so the lock must be released
    by ending the transaction — proven here by a second connection taking it
    with a short `lock_timeout` while the saving Session is still open. And
    `content_changed_at` is a microsecond `timestamptz`, so an unchanged value
    is evidence that nothing wrote it.
    """
    Session, application_id, _consultant_id, ta_id = pg_application
    db = Session()
    other = Session()
    try:
        tailored, _run, _ = service.request_generation(
            db, db.get(Application, application_id), actor=db.get(User, ta_id)
        )
        key = service.current_key(tailored)
        generated, _ = service.commit_generated_state_once(
            db, key, {"iName": "Generated"}
        )
        before = (generated.revision, generated.content_hash,
                  generated.content_changed_at, generated.updated_by_id)

        returned = service.save_state(
            db, generated, state={"iName": "Generated"},
            expected_revision=generated.revision,
            expected_id=str(generated.id), actor=db.get(User, ta_id),
        )
        assert returned.revision == before[0]

        # `db` is still open. If its transaction still held the row lock this
        # would time out after two seconds.
        other.execute(text("SET LOCAL lock_timeout = '2s'"))
        row = other.scalars(
            select(TailoredCV).where(TailoredCV.id == generated.id).with_for_update()
        ).one()
        assert (row.revision, row.content_hash, row.content_changed_at,
                row.updated_by_id) == before
        other.rollback()
    finally:
        other.close()
        db.close()


def test_a_generation_serializes_with_the_applications_own_row_lock(
    pg_application,
):
    """The lock order this slice extends — Job → pair → Application → tailored.

    One session holds the Application row lock; the other's `request_generation`
    must wait for it rather than deciding "is this Application still live" from
    a copy loaded before the first session committed.
    """
    Session, application_id, _consultant_id, ta_id = pg_application
    holder = Session()
    started = threading.Event()
    finished = threading.Event()
    errors: list = []

    try:
        # Close the Application under a row lock, and hold the transaction open.
        locked = applications_service.lock_application(
            holder, holder.get(Application, application_id)
        )
        locked.status = "closed"

        def generate() -> None:
            db = Session()
            try:
                started.set()
                service.request_generation(
                    db, db.get(Application, application_id),
                    actor=db.get(User, ta_id),
                )
                errors.append(AssertionError("generation ran on a closed application"))
            except service.TailoredCVServiceError as exc:
                errors.append(exc)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()
                finished.set()

        thread = threading.Thread(target=generate)
        thread.start()
        assert started.wait(timeout=10)
        # It must still be blocked on the row lock: nothing has committed yet.
        assert not finished.wait(timeout=2), (
            "the generation decided the application's state without the lock"
        )
        holder.commit()
        assert finished.wait(timeout=30)
    finally:
        holder.close()

    assert len(errors) == 1
    assert isinstance(errors[0], service.TailoredCVServiceError)
    assert errors[0].status == 409
    assert "has ended" in str(errors[0])


def test_a_close_cannot_land_between_the_artifact_and_the_enqueue(
    pg_application, monkeypatch
):
    """The *other* ordering: the generation is already past its terminal check.

    `S3-P1-R1` finding 4. The test above covers a close that already holds the
    lock. This covers the window the implementation actually had: `_get_or_create`
    used to commit, which released the Application row lock the terminal check
    had just been made under. A close committing in that window meant a run was
    enqueued for an Application that was terminal by the time the run existed —
    and the artifact and the run no longer became visible together.

    Proving the window is closed means proving the lock is *still held* at the
    point it used to be released, so the racing session is given a short
    `lock_timeout` and must fail to close. Without the fix it closes immediately
    and the assertion below fails.
    """
    Session, application_id, _consultant_id, ta_id = pg_application
    db = Session()
    paused = threading.Event()
    release = threading.Event()
    closed_early: list = []

    real_get_or_create = service._get_or_create

    def pausing_get_or_create(*args, **kwargs):
        result = real_get_or_create(*args, **kwargs)
        # Exactly where the commit used to be.
        paused.set()
        assert release.wait(timeout=30)
        return result

    def racing_close() -> None:
        other = Session()
        try:
            # Short timeout: this asserts on *contention*, so it must not sit
            # and wait for the generation to finish and then succeed.
            other.execute(text("SET LOCAL lock_timeout = '2s'"))
            locked = applications_service.lock_application(
                other, other.get(Application, application_id)
            )
            locked.status = "closed"
            other.commit()
            closed_early.append("closed")
        except Exception as exc:  # noqa: BLE001 - a lock timeout is the pass
            closed_early.append(exc)
        finally:
            other.close()

    monkeypatch.setattr(service, "_get_or_create", pausing_get_or_create)
    try:
        thread = threading.Thread(
            target=lambda: service.request_generation(
                db, db.get(Application, application_id),
                actor=db.get(User, ta_id),
            )
        )
        thread.start()
        assert paused.wait(timeout=20), "the generation never reached the pause"

        closer = threading.Thread(target=racing_close)
        closer.start()
        closer.join(timeout=30)
        assert not closer.is_alive()

        release.set()
        thread.join(timeout=60)
        assert not thread.is_alive()
    finally:
        db.close()

    assert len(closed_early) == 1
    assert not isinstance(closed_early[0], str), (
        "a concurrent close committed between the artifact insert and the "
        "enqueue, so work was accepted for an application that had ended"
    )

    # And the outcome is coherent: the application the run belongs to is still
    # live, and the artifact and its run are both there.
    verify = Session()
    try:
        application = verify.get(Application, application_id)
        assert application.status == "applied"
        tailored = service.get_for_application(verify, application)
        assert tailored is not None
        assert service.current_run(verify, tailored) is not None
    finally:
        verify.close()


# ── Schema shape after the migration ──────────────────────────────────────


def test_one_tailored_cv_per_application_is_enforced_by_the_migration(pg_engine):
    """Not merely `create_all` in the fast gate: production runs the migration,
    and a non-unique index here would allow two CV documents for one
    Application with every fast test still green."""
    with pg_engine.connect() as conn:
        definition = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = "
                "'tailored_cvs' AND indexname = 'ix_tailored_cvs_application_id'"
            )
        ).scalar_one()
    assert "UNIQUE" in definition.upper()


def test_the_status_vocabulary_is_closed_in_the_migrated_database(pg_application):
    """A status the code cannot handle must be unrepresentable, not merely
    unaccepted — including V1's `cv_ready`, which named this very artifact."""
    Session, application_id, _consultant_id, _ta_id = pg_application
    for bad in ("cv_ready", "published", "pending", ""):
        db = Session()
        try:
            db.add(
                TailoredCV(
                    application_id=application_id,
                    status=bad,
                    revision=0,
                    input_snapshot={},
                    input_hash="y" * 64,
                    generation_attempt=0,
                )
            )
            with pytest.raises(IntegrityError) as exc:
                db.commit()
            assert _sqlstate(exc.value) == CHECK_VIOLATION, bad
        finally:
            db.rollback()
            db.close()


def test_the_snapshot_and_state_are_real_jsonb(pg_application):
    """The fast gate stores plain JSON. Production stores JSONB, and a column
    that silently became text would still round-trip through the ORM."""
    Session, _application_id, _consultant_id, _ta_id = pg_application
    db = Session()
    try:
        types = dict(
            db.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'tailored_cvs' AND column_name IN "
                    "('cv_state', 'input_snapshot')"
                )
            ).all()
        )
        assert types == {"cv_state": "jsonb", "input_snapshot": "jsonb"}
    finally:
        db.close()


# ── Cascades the rights paths lean on ─────────────────────────────────────


def test_deleting_an_application_takes_its_tailored_cv_with_it(pg_application):
    """GDPR erasure deletes tailored CVs explicitly — but the deployed engine
    must agree too, or a code path that ever relies on the cascade would leave
    CV documents behind after the Application is gone."""
    Session, application_id, _consultant_id, ta_id = pg_application
    db = Session()
    try:
        service.request_generation(
            db, db.get(Application, application_id), actor=db.get(User, ta_id)
        )
        assert db.query(TailoredCV).count() == 1
        db.execute(
            text("DELETE FROM applications WHERE id = :id"), {"id": application_id}
        )
        db.commit()
        assert db.query(TailoredCV).count() == 0
    finally:
        db.close()


def test_deleting_the_acting_ta_keeps_the_tailored_cv_and_nulls_authorship(
    pg_application,
):
    Session, application_id, _consultant_id, ta_id = pg_application
    db = Session()
    try:
        service.request_generation(
            db, db.get(Application, application_id), actor=db.get(User, ta_id)
        )
        db.execute(text("DELETE FROM ta_consultants WHERE ta_id = :id"), {"id": ta_id})
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": ta_id})
        db.commit()
        row = db.scalar(select(TailoredCV))
        assert row is not None
        assert row.created_by_id is None
    finally:
        db.close()


def test_the_artifact_stores_no_reference_to_its_workflow_run(pg_application):
    """The run is found by the idempotency key, never by a stored column.

    That is what closed the duplicate-generation race above, so it is worth
    holding structurally: a column re-added here would reintroduce a second
    source of truth and the window in which it is not yet written. Losing the
    run row must therefore cost the polling link and nothing else.
    """
    assert not any(
        "run" in column.name for column in TailoredCV.__table__.columns
    ), {c.name for c in TailoredCV.__table__.columns}

    Session, application_id, _consultant_id, ta_id = pg_application
    db = Session()
    try:
        tailored, run, _ = service.request_generation(
            db, db.get(Application, application_id), actor=db.get(User, ta_id)
        )
        assert service.current_run(db, tailored).id == run.id
        service.commit_generated_state_once(
            db, service.current_key(tailored), {"iName": "Generated"}
        )
        db.execute(text("DELETE FROM workflow_runs WHERE id = :id"), {"id": run.id})
        db.commit()
        db.expire_all()
        row = db.query(TailoredCV).one()
        assert service.current_run(db, row) is None
        assert row.revision == 1
        assert row.cv_state == {"iName": "Generated"}
    finally:
        db.close()
