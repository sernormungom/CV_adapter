"""The relational Job domain against real Postgres 16 (V2 Slice 1).

What needs the real engine, and why in-memory SQLite cannot answer it:

- **Concurrent duplicate creation.** The whole point of the unique
  `content_hash` index is that a check-then-insert is not enough. Only two
  connections racing the same key prove it, and only Postgres gives the loser a
  real integrity error inside a transaction that then has to be recovered.
- **Constraint shape after `alembic upgrade`.** The SQLite suite builds its
  schema with `create_all`; production runs the migration. A check constraint or
  unique index that exists in the models but not in the migration would pass
  every fast test and fail on deploy.
- **The Maybe guarantee.** `decision IN ('reject')` is only a guarantee if the
  deployed database actually enforces it.
- **Cascade behaviour.** Deleting a user or a job must not leave orphaned links
  or review markers in the deployed engine.

Every database here is created and dropped by the fixtures in `conftest.py`.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.db.models import Job, JobDecisionPair, JobReview, JobStatus, User
from backend.jobs import service

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


DESCRIPTION = (
    "Own an automotive control unit end to end: C, AUTOSAR, CAN, and "
    "ISO 26262 functional-safety work alongside a verification team."
)


@pytest.fixture
def pg_sessions(migrated_db):
    """A sessionmaker on the migrated database, plus a seeded Site/TA/consultant.

    Returns `(Session, consultant_id, ta_id)`. Provisioning goes through raw
    SQL rather than the auth service so this module tests the Job schema, not
    account creation.
    """
    engine = create_engine(migrated_db)
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
            (ta_id, "pg-ta", "ta"),
            (consultant_id, "pg-consultant", "consultant"),
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
    try:
        yield sessionmaker(bind=engine), consultant_id, ta_id
    finally:
        engine.dispose()


def _job_input(**over) -> service.JobInput:
    fields = {
        "title": "Senior Embedded Engineer",
        "description": DESCRIPTION,
        "company": "Client AB",
    }
    fields.update(over)
    return service.build_input(**fields)


# ── Concurrent duplicate creation (acceptance case 2) ──────────────────────


def test_two_concurrent_identical_creates_yield_exactly_one_job(pg_sessions):
    """The real race, on real Postgres, with a barrier so both transactions are
    genuinely in flight before either commits.

    Both threads pass their own check-then-insert; the unique index is what
    decides. Whichever loses must report `duplicate` and return the winner's
    row, not raise and not insert a second Job.
    """
    Session, consultant_id, ta_id = pg_sessions
    data = _job_input()
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def attempt() -> None:
        db = Session()
        try:
            consultant = db.get(User, consultant_id)
            actor = db.get(User, ta_id)
            # Force the INSERT to be attempted only once both threads are here.
            barrier.wait(timeout=20)
            result = service.create_job(
                db, data, consultant=consultant, actor=actor
            )
            # Read the identity *before* the session closes — the ORM instance
            # is detached afterwards and a lazy refresh would raise.
            results.append((result.status, str(result.job.id)))
        except Exception as exc:  # noqa: BLE001 — reported below, not swallowed
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a racing create raised instead of resolving: {errors}"
    assert len(results) == 2
    assert sorted(status for status, _ in results) == ["created", "duplicate"]
    assert len({job_id for _, job_id in results}) == 1, (
        "the duplicate returned a different Job than the winner created"
    )

    verify = Session()
    try:
        assert verify.query(Job).count() == 1
        # Creation no longer materializes a decision pair — that happens lazily
        # at the first decision (acceptance 7) — so neither racing create did.
        assert verify.query(JobDecisionPair).count() == 0
    finally:
        verify.close()


def test_the_unique_content_hash_is_enforced_by_the_migrated_database(pg_engine):
    """Not merely by `create_all` in the fast gate.

    S1 scoped the exact-duplicate constraint to open Jobs within one Site
    (brief acceptance 7): a plain unique index on `content_hash` became a
    partial unique index on `(site_id, content_hash)` where `status = 'open'`.
    """
    with pg_engine.connect() as conn:
        definition = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'jobs' AND indexname = 'uq_jobs_open_site_content'"
            )
        ).scalar_one()
    assert "UNIQUE" in definition.upper()
    assert "WHERE" in definition.upper(), (
        f"index is not partial, so a closed Job would still block a same-hash "
        f"repost: {definition}"
    )
    assert "site_id" in definition and "content_hash" in definition


def test_a_raw_duplicate_insert_is_rejected(pg_sessions):
    Session, consultant_id, ta_id = pg_sessions
    db = Session()
    try:
        actor = db.get(User, ta_id)
        service.create_job(db, _job_input(), consultant=db.get(User, consultant_id), actor=actor)
        db.add(
            Job(
                site_id=actor.site_id,
                title="Anything",
                description="Another posting entirely, but the same hash.",
                content_hash=_job_input().hash,
                status=JobStatus.open.value,
            )
        )
        with pytest.raises(IntegrityError) as exc:
            db.commit()
        assert _sqlstate(exc.value) == UNIQUE_VIOLATION
    finally:
        db.rollback()
        db.close()


# ── Reject: idempotent, and Maybe is unrepresentable ───────────────────────


def test_concurrent_rejects_leave_exactly_one_review_marker(pg_sessions):
    Session, consultant_id, ta_id = pg_sessions
    setup = Session()
    try:
        result = service.create_job(
            setup, _job_input(), consultant=setup.get(User, consultant_id),
            actor=setup.get(User, ta_id),
        )
        job_id = result.job.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    outcomes: list = []
    errors: list = []

    def attempt() -> None:
        db = Session()
        try:
            job = db.get(Job, job_id)
            consultant = db.get(User, consultant_id)
            actor = db.get(User, ta_id)
            barrier.wait(timeout=20)
            outcomes.append(
                service.record_review(
                    db, job, consultant,
                    decision="reject", actor=actor,
                )[1]
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a racing reject raised: {errors}"
    assert sorted(outcomes) == [False, True], (
        "exactly one caller must have created the marker"
    )
    verify = Session()
    try:
        assert verify.query(JobReview).count() == 1
    finally:
        verify.close()


def test_the_database_itself_refuses_a_maybe_decision(pg_sessions):
    """P-008 as a schema guarantee, not merely a route check. Bypassing the
    service entirely must still fail."""
    Session, consultant_id, ta_id = pg_sessions
    db = Session()
    try:
        result = service.create_job(
            db, _job_input(), consultant=db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text(
                    "INSERT INTO job_reviews (id, job_id, consultant_id, "
                    "decision, created_at, updated_at) "
                    "VALUES (:id, :job, :c, 'maybe', now(), now())"
                ),
                {"id": uuid.uuid4(), "job": result.job.id, "c": consultant_id},
            )
        assert _sqlstate(exc.value) == CHECK_VIOLATION
    finally:
        db.rollback()
        db.close()


def test_job_status_is_check_constrained(pg_sessions):
    Session, _consultant_id, ta_id = pg_sessions
    db = Session()
    try:
        site_id = db.get(User, ta_id).site_id
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text(
                    "INSERT INTO jobs (id, site_id, title, description, content_hash, "
                    "status, created_at, updated_at) VALUES "
                    "(:id, :site_id, 'x', 'y', :h, 'archived', now(), now())"
                ),
                {"id": uuid.uuid4(), "site_id": site_id, "h": "a" * 64},
            )
        assert _sqlstate(exc.value) == CHECK_VIOLATION
    finally:
        db.rollback()
        db.close()


# ── Cascades ───────────────────────────────────────────────────────────────


def test_deleting_a_job_removes_its_links_and_reviews(pg_sessions):
    Session, consultant_id, ta_id = pg_sessions
    db = Session()
    try:
        result = service.create_job(
            db, _job_input(), consultant=db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        service.record_review(
            db, result.job, db.get(User, consultant_id),
            decision="reject", actor=db.get(User, ta_id),
        )
        db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": result.job.id})
        db.commit()
        assert db.query(JobDecisionPair).count() == 0
        assert db.query(JobReview).count() == 0
    finally:
        db.close()


def test_deleting_a_consultant_leaves_the_shared_job_but_no_orphans(pg_sessions):
    """A Job is business content and survives; the erased consultant's link and
    review markers do not. `backend/privacy/service.py` deletes both explicitly
    — this proves the deployed database would too, so the two agree."""
    Session, consultant_id, ta_id = pg_sessions
    db = Session()
    try:
        result = service.create_job(
            db, _job_input(), consultant=db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        service.record_review(
            db, result.job, db.get(User, consultant_id),
            decision="reject", actor=db.get(User, ta_id),
        )
        db.execute(
            text("DELETE FROM users WHERE id = :id"), {"id": consultant_id}
        )
        db.commit()
        assert db.query(Job).count() == 1
        assert db.query(JobDecisionPair).count() == 0
        assert db.query(JobReview).count() == 0
        # created_by_id was SET NULL, not cascaded — authorship goes, Job stays.
        assert db.get(Job, result.job.id) is not None
    finally:
        db.close()


# ── Listing and close, on the deployed engine ──────────────────────────────


def test_close_job_transitions_status_and_clears_cached_scores_on_postgres(pg_sessions):
    """`close_job` (acceptance 4: "Closing a Job deletes its rows") on the
    deployed engine: the status/`closed_at` transition and the
    `job_consultant_matches` cascade both need a real commit to prove, not
    just an in-memory session.

    Listing is exercised at the HTTP layer now (`jobs/routes.py::list_jobs`
    builds its own Site/score-aware query) — `jobs.service.list_jobs` has no
    caller left and was removed as dead code.
    """
    from backend.db.models.matching import JobConsultantMatch

    Session, consultant_id, ta_id = pg_sessions
    db = Session()
    try:
        consultant, actor = db.get(User, consultant_id), db.get(User, ta_id)
        first = service.create_job(db, _job_input(), consultant=consultant, actor=actor)
        db.commit()
        # Seed the cache row directly — the fixture's fabricated consultant has
        # no profile.md, so the real scorer would report `scored: false` and
        # `read_score` would never insert one. What this test proves is the
        # cascade on close, not the scorer.
        db.add(JobConsultantMatch(
            job_id=first.job.id, consultant_id=consultant_id,
            match_score=42.0, result={"scored": True},
        ))
        db.commit()
        assert db.query(JobConsultantMatch).filter_by(job_id=first.job.id).count() == 1

        job = db.get(Job, first.job.id)
        service.close_job(db, job)

        assert job.status == JobStatus.closed.value
        assert job.closed_at is not None
        assert db.query(JobConsultantMatch).filter_by(job_id=first.job.id).count() == 0
    finally:
        db.close()
