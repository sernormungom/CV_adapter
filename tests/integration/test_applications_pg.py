"""The Application domain against real Postgres 16 (V2 Slice 2).

What needs the real engine, and why in-memory SQLite cannot answer it:

- **Concurrent Apply.** The partial unique index exists precisely because a
  check-then-insert is not enough: a double-clicked button, or a retry of a
  request whose first attempt is still running past the 60-second edge, are two
  transactions racing the same pair. Only two connections racing prove it, and
  only Postgres gives the loser a real integrity error inside a transaction that
  then has to be recovered.
- **Concurrent Apply *versus* Reject.** These write different tables, so no
  index can exclude them; the rule rests on the `job_decision_pairs` row lock, and
  a row lock is exactly what SQLite's single-writer model cannot demonstrate.
- **Index shape after `alembic upgrade`.** The SQLite suite builds its schema
  with `create_all`; production runs the migration. A *partial* unique index
  that ended up total in the migration would still pass every fast test and
  would then forbid re-applying to a pair whose Application had ended.
- **The closed status vocabulary.** `status IN (...)` is only a guarantee if the
  deployed database enforces it — including against V1's `application_status`
  words.
- **Cascade behaviour.** GDPR erasure deletes a consultant's Applications
  explicitly; the deployed engine must agree.

Every database here is created and dropped by the fixtures in `conftest.py`.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.applications import service
from backend.db.models import Application, ApplicationStatus, Job, JobReview, User
from backend.jobs import service as jobs_service

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"

DESCRIPTION = (
    "Own an automotive control unit end to end: C, AUTOSAR, CAN, and "
    "ISO 26262 functional-safety work alongside a verification team."
)


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


@pytest.fixture
def pg_pair(migrated_db):
    """A sessionmaker on the migrated database plus one open Job whose
    consultant is linked to it.

    Returns `(Session, job_id, consultant_id, ta_id)`. Accounts are provisioned
    through raw SQL so this module tests the Application schema, not account
    creation; the Job goes through the real service so the `job_decision_pairs`
    link the pair rules depend on exists exactly as production makes it.
    """
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
            (ta_id, "pg-app-ta", "ta"),
            (consultant_id, "pg-app-consultant", "consultant"),
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
        job_id = created.job.id
    finally:
        setup.close()

    try:
        yield Session, job_id, consultant_id, ta_id
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
        t.join(timeout=30)
    return results, errors


# ── Concurrent Apply (acceptance case 1) ───────────────────────────────────


def test_two_concurrent_applies_yield_exactly_one_live_application(pg_pair):
    """The real race, on real Postgres, with a barrier so both transactions are
    genuinely in flight before either commits.

    Both threads pass their own check-then-insert; the partial unique index is
    what decides. Whichever loses must report `created=False` and return the
    winner's row — not raise, and not insert a second Application.
    """
    Session, job_id, consultant_id, ta_id = pg_pair

    def attempt(barrier, results, errors) -> None:
        db = Session()
        try:
            job = db.get(Job, job_id)
            consultant = db.get(User, consultant_id)
            actor = db.get(User, ta_id)
            barrier.wait(timeout=20)
            application, created = service.apply(
                db, job, consultant, actor=actor
            )
            # Read the identity before the session closes — the instance is
            # detached afterwards and a lazy refresh would raise.
            results.append((created, str(application.id)))
        except Exception as exc:  # noqa: BLE001 — reported below, not swallowed
            errors.append(exc)
        finally:
            db.close()

    results, errors = _race(attempt)

    assert not errors, f"a racing apply raised instead of resolving: {errors}"
    assert len(results) == 2
    assert sorted(created for created, _ in results) == [False, True]
    assert len({app_id for _, app_id in results}) == 1, (
        "the losing caller returned a different Application than the winner"
    )

    verify = Session()
    try:
        assert verify.query(Application).count() == 1
    finally:
        verify.close()


def test_the_live_pair_index_is_unique_and_partial_in_the_migrated_database(
    pg_engine,
):
    """Not merely `create_all` in the fast gate — and *partial*, not total. A
    total index here would pass every other test and then refuse a legitimate
    re-Apply after an Application ended."""
    with pg_engine.connect() as conn:
        definition = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = "
                "'applications' AND indexname = 'uq_application_live_pair'"
            )
        ).scalar_one()
    upper = definition.upper()
    assert "UNIQUE" in upper
    assert "WHERE" in upper
    assert "'applied'" in definition and "'submitted'" in definition
    assert "'closed'" not in definition and "'withdrawn'" not in definition


def test_a_raw_second_live_row_is_rejected(pg_pair):
    Session, job_id, consultant_id, ta_id = pg_pair
    db = Session()
    try:
        service.apply(
            db, db.get(Job, job_id), db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        db.add(
            Application(
                job_id=job_id,
                consultant_id=consultant_id,
                status=ApplicationStatus.submitted.value,
            )
        )
        with pytest.raises(IntegrityError) as exc:
            db.commit()
        assert _sqlstate(exc.value) == UNIQUE_VIOLATION
    finally:
        db.rollback()
        db.close()


def test_terminal_rows_are_excluded_from_the_live_uniqueness(pg_pair):
    """Many terminal Applications may coexist with one live one — that is what
    makes the index partial rather than total."""
    Session, job_id, consultant_id, ta_id = pg_pair
    db = Session()
    try:
        consultant, actor = db.get(User, consultant_id), db.get(User, ta_id)
        for terminal in ("closed", "withdrawn"):
            application, created = service.apply(
                db, db.get(Job, job_id), consultant, actor=actor
            )
            assert created is True
            service.transition(db, application, status=terminal, actor=actor)
        service.apply(db, db.get(Job, job_id), consultant, actor=actor)

        assert db.query(Application).count() == 3
        assert (
            db.query(Application)
            .filter(Application.status.in_(("applied", "submitted")))
            .count()
            == 1
        )
    finally:
        db.close()


# ── The closed status vocabulary ───────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    # An invented value, V1's removed verdict, and three V1 application_status
    # words. None of them may reach a row even by bypassing the service.
    ["archived", "maybe", "to_apply", "cv_ready", "link_opened"],
)
def test_the_database_itself_refuses_a_status_outside_the_pilot_set(
    pg_pair, value
):
    Session, job_id, consultant_id, _ta_id = pg_pair
    db = Session()
    try:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text(
                    "INSERT INTO applications (id, job_id, consultant_id, "
                    "status, created_at, updated_at) "
                    "VALUES (:id, :job, :c, :status, now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "job": job_id,
                    "c": consultant_id,
                    "status": value,
                },
            )
        assert _sqlstate(exc.value) == CHECK_VIOLATION
    finally:
        db.rollback()
        db.close()


# ── Apply versus Reject, concurrently ──────────────────────────────────────


def test_a_concurrent_apply_and_reject_cannot_both_land(pg_pair):
    """The pair holds one live decision, and that has to survive a real race.

    Apply and Reject write different tables, so no unique index excludes them:
    without the shared `job_decision_pairs` row lock both transactions read "no
    conflict" and both commit, leaving a pair that is simultaneously rejected
    and live. Exactly one must win; the loser must fail with the documented 409
    rather than raise or corrupt anything.
    """
    Session, job_id, consultant_id, ta_id = pg_pair

    def attempt(barrier, results, errors) -> None:
        # Thread 0 applies, thread 1 rejects — decided by which reaches the
        # barrier first, which is the point: either order must be safe.
        db = Session()
        try:
            job = db.get(Job, job_id)
            consultant = db.get(User, consultant_id)
            actor = db.get(User, ta_id)
            index = barrier.wait(timeout=20)
            if index == 0:
                service.apply(db, job, consultant, actor=actor)
                results.append("applied")
            else:
                jobs_service.record_review(
                    db, job, consultant,
                    decision="reject", actor=actor,
                )
                results.append("rejected")
        except (service.ApplicationServiceError, jobs_service.JobServiceError) as exc:
            assert exc.status == 409, exc
            results.append("refused")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    results, errors = _race(attempt)

    assert not errors, f"a racing apply/reject raised: {errors}"
    assert len(results) == 2, results
    assert "refused" in results, (
        f"both writes landed instead of one being refused: {results}"
    )

    verify = Session()
    try:
        live = (
            verify.query(Application)
            .filter(Application.status.in_(("applied", "submitted")))
            .count()
        )
        rejected = verify.query(JobReview).count()
        assert live + rejected == 1, (
            f"the pair holds {live} live application(s) and {rejected} "
            "reject marker(s); exactly one decision may exist"
        )
    finally:
        verify.close()


# ── Overlapping requests, not just concurrent threads (S2-P1-R1) ───────────
#
# The first round's tests were all sequential-and-fresh: every one re-read the
# world before acting, so none could observe a *stale* in-memory object. That is
# not how HTTP works. Two overlapping requests each load their own copy of a
# Job or an Application, and whichever commits second was validated against a
# copy that is already wrong. These four reproduce exactly that shape.


def _live_count(db) -> int:
    return (
        db.query(Application)
        .filter(Application.status.in_(("applied", "submitted")))
        .count()
    )


def test_a_stale_transition_cannot_revive_a_terminal_application(pg_pair):
    """Two sessions load the same `applied` row; one closes it. The other must
    not then commit `submitted` off its stale copy.

    Without the row lock this ended at `submitted, is_live=True` — the monotonic
    lifecycle and the terminal guarantee both simply false under ordinary
    overlapping requests.
    """
    Session, job_id, consultant_id, ta_id = pg_pair
    setup = Session()
    try:
        application, _ = service.apply(
            setup, setup.get(Job, job_id), setup.get(User, consultant_id),
            actor=setup.get(User, ta_id),
        )
        application_id = application.id
    finally:
        setup.close()

    first, stale = Session(), Session()
    try:
        a1 = first.get(Application, application_id)
        a2 = stale.get(Application, application_id)      # loaded before the close
        assert a1.status == a2.status == "applied"

        service.transition(
            first, a1, status="closed", actor=first.get(User, ta_id)
        )
        with pytest.raises(service.ApplicationServiceError) as exc:
            service.transition(
                stale, a2, status="submitted", actor=stale.get(User, ta_id)
            )
        assert exc.value.status == 409
    finally:
        first.close()
        stale.close()

    verify = Session()
    try:
        assert verify.get(Application, application_id).status == "closed"
        assert _live_count(verify) == 0
    finally:
        verify.close()


def test_a_stale_transition_cannot_make_a_pair_both_live_and_rejected(pg_pair):
    """The consequence that makes the previous test a High, not a nit.

    Once the Application goes terminal a Reject legitimately becomes possible.
    If a stale transition can then revive the Application, the pair holds both
    decisions — the invariant this slice exists to keep.
    """
    Session, job_id, consultant_id, ta_id = pg_pair
    setup = Session()
    try:
        application, _ = service.apply(
            setup, setup.get(Job, job_id), setup.get(User, consultant_id),
            actor=setup.get(User, ta_id),
        )
        application_id = application.id
    finally:
        setup.close()

    first, stale, rejecter = Session(), Session(), Session()
    try:
        a1 = first.get(Application, application_id)
        a2 = stale.get(Application, application_id)      # stale from here on

        service.transition(
            first, a1, status="withdrawn", actor=first.get(User, ta_id)
        )
        jobs_service.record_review(
            rejecter, rejecter.get(Job, job_id),
            rejecter.get(User, consultant_id),
            decision="reject",
            actor=rejecter.get(User, ta_id),
        )
        with pytest.raises(service.ApplicationServiceError) as exc:
            service.transition(
                stale, a2, status="submitted", actor=stale.get(User, ta_id)
            )
        assert exc.value.status == 409
    finally:
        for s in (first, stale, rejecter):
            s.close()

    verify = Session()
    try:
        live, rejected = _live_count(verify), verify.query(JobReview).count()
        assert (live, rejected) == (0, 1), (
            f"the pair holds {live} live application(s) and {rejected} reject "
            "marker(s); exactly one decision may exist"
        )
    finally:
        verify.close()


def test_apply_cannot_land_after_a_concurrent_close(pg_pair):
    """A request that loaded an open Job must not create an Application after
    another transaction has committed the Job closed.

    Distinct from the allowed case where Apply wins first and the Job closes
    afterwards — that Application stays and can still be finished.
    """
    Session, job_id, consultant_id, ta_id = pg_pair
    stale, closer = Session(), Session()
    try:
        job = stale.get(Job, job_id)                     # loaded while open
        assert job.status == "open"
        jobs_service.close_job(closer, closer.get(Job, job_id))

        with pytest.raises(jobs_service.JobServiceError) as exc:
            service.apply(
                stale, job, stale.get(User, consultant_id),
                actor=stale.get(User, ta_id),
            )
        assert exc.value.status == 409
        assert str(exc.value) == jobs_service.JOB_CLOSED_MESSAGE
    finally:
        stale.close()
        closer.close()

    verify = Session()
    try:
        assert verify.query(Application).count() == 0
    finally:
        verify.close()


def test_reject_cannot_land_after_a_concurrent_close(pg_pair):
    """The mirror image, on the Slice 1 write that shares the same guard."""
    Session, job_id, consultant_id, ta_id = pg_pair
    stale, closer = Session(), Session()
    try:
        job = stale.get(Job, job_id)
        jobs_service.close_job(closer, closer.get(Job, job_id))

        with pytest.raises(jobs_service.JobServiceError) as exc:
            jobs_service.record_review(
                stale, job, stale.get(User, consultant_id),
                decision="reject",
                actor=stale.get(User, ta_id),
            )
        assert exc.value.status == 409
    finally:
        stale.close()
        closer.close()

    verify = Session()
    try:
        assert verify.query(JobReview).count() == 0
    finally:
        verify.close()


def test_a_transition_on_a_concurrently_erased_application_is_404_not_500(pg_pair):
    """The rights-path race, on the engine that actually takes the row lock.

    `Session.refresh` raises `InvalidRequestError` on a vanished row — not
    `ObjectDeletedError` — so the earlier handler never fired and a GDPR
    erasure overlapping a status request escaped as a 500. Reloading by select
    yields `None` instead, which is answerable.
    """
    Session, job_id, consultant_id, ta_id = pg_pair
    setup = Session()
    try:
        application, _ = service.apply(
            setup, setup.get(Job, job_id), setup.get(User, consultant_id),
            actor=setup.get(User, ta_id),
        )
        application_id = application.id
    finally:
        setup.close()

    holder, remover = Session(), Session()
    try:
        stale = holder.get(Application, application_id)
        remover.execute(
            text("DELETE FROM applications WHERE id = :id"),
            {"id": application_id},
        )
        remover.commit()

        with pytest.raises(service.ApplicationServiceError) as exc:
            service.transition(
                holder, stale, status="closed", actor=holder.get(User, ta_id)
            )
        assert exc.value.status == 404
        assert isinstance(exc.value, service.SERVICE_ERRORS), (
            "the route maps SERVICE_ERRORS; anything else reaches the client "
            "as a 500"
        )
    finally:
        holder.close()
        remover.close()


def test_apply_on_a_concurrently_deleted_job_is_404_not_500(pg_pair):
    """`lock_job` carried the same latent defect as the transition path — it
    used `Session.refresh` too. There is no Job-delete route today, so this is
    only reachable by direct database work; it is fixed and pinned rather than
    left as a 500 waiting for the first one.
    """
    Session, job_id, consultant_id, ta_id = pg_pair
    holder, remover = Session(), Session()
    try:
        job = holder.get(Job, job_id)
        consultant = holder.get(User, consultant_id)
        actor = holder.get(User, ta_id)
        remover.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        remover.commit()

        with pytest.raises(jobs_service.JobServiceError) as exc:
            service.apply(holder, job, consultant, actor=actor)
        assert exc.value.status == 404
        assert isinstance(exc.value, service.SERVICE_ERRORS)
    finally:
        holder.close()
        remover.close()


def test_an_application_in_flight_when_the_job_closes_can_still_be_finished(
    pg_pair,
):
    """The boundary the two tests above must not have over-tightened: closing a
    posting refuses *new* evaluations, it does not strand a live Application."""
    Session, job_id, consultant_id, ta_id = pg_pair
    db = Session()
    try:
        application, _ = service.apply(
            db, db.get(Job, job_id), db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        jobs_service.close_job(db, db.get(Job, job_id))
        application, changed = service.transition(
            db, application, status="closed", actor=db.get(User, ta_id)
        )
        assert changed is True
        assert application.status == "closed"
    finally:
        db.close()


# ── Cascades ───────────────────────────────────────────────────────────────


def test_deleting_a_job_removes_its_applications(pg_pair):
    Session, job_id, consultant_id, ta_id = pg_pair
    db = Session()
    try:
        service.apply(
            db, db.get(Job, job_id), db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        db.commit()
        assert db.query(Application).count() == 0
    finally:
        db.close()


def test_deleting_a_consultant_removes_their_applications_not_the_job(pg_pair):
    """`backend/privacy/service.py` deletes them explicitly; this proves the
    deployed database would too, so the two agree."""
    Session, job_id, consultant_id, ta_id = pg_pair
    db = Session()
    try:
        service.apply(
            db, db.get(Job, job_id), db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        db.execute(
            text("DELETE FROM users WHERE id = :id"), {"id": consultant_id}
        )
        db.commit()
        assert db.query(Application).count() == 0
        assert db.get(Job, job_id) is not None
    finally:
        db.close()


def test_deleting_the_acting_ta_keeps_the_application_and_nulls_authorship(
    pg_pair,
):
    """A TA leaving must not delete the Applications they made for a consultant
    who is still here — `ON DELETE SET NULL`, not CASCADE."""
    Session, job_id, consultant_id, ta_id = pg_pair
    db = Session()
    try:
        application, _ = service.apply(
            db, db.get(Job, job_id), db.get(User, consultant_id),
            actor=db.get(User, ta_id),
        )
        application_id = application.id
        db.execute(
            text("DELETE FROM ta_consultants WHERE ta_id = :id"), {"id": ta_id}
        )
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": ta_id})
        db.commit()

        surviving = db.get(Application, application_id)
        assert surviving is not None
        assert surviving.created_by_id is None
        assert surviving.status_changed_by_id is None
    finally:
        db.close()
