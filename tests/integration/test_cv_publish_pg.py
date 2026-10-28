"""Publish vs. the confirmed-terminal-Application transition, against real
Postgres row locks (S5-P2, `S5-P1-R2` finding 2).

The fast gate's SQLite cannot express this: the defect is a genuine race
between two transactions each taking `SELECT ... FOR UPDATE` on the same
Application row, and only two real connections plus a real lock prove which
orderings are possible. The reviewer's reproduction — pause Publish right
after its tailored-CV read, commit `applied -> closed` in another session,
resume Publish — must be unreachable: `publish_for_application` now takes the
Application row lock (`backend.applications.service.lock_application`)
*before* reading the tailored CV, and holds it through its own commit, so the
two orderings below are the only two that can happen.

Modeled directly on
`tests/integration/test_tailored_cv_pg.py::test_a_generation_serializes_with_the_applications_own_row_lock`
and `::test_a_close_cannot_land_between_the_artifact_and_the_enqueue` — same
fixture shape, same barrier/event pattern.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.applications import service as applications_service
from backend.cv_publish import service as cv_publish_service
from backend.db.models import Application, CVPublication, TailoredCV, User
from backend.jobs import service as jobs_service

DESCRIPTION = (
    "Own an automotive control unit end to end: C, AUTOSAR, CAN, and "
    "ISO 26262 functional-safety work alongside a verification team."
)


@pytest.fixture
def pg_application_with_ready_cv(migrated_db):
    """A sessionmaker on the migrated database, one live Application, and a
    `ready` tailored CV on it — everything `publish_for_application` needs.
    Returns `(Session, application_id, ta_id)`."""
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
            (ta_id, "pg-pub-ta", "ta"),
            (consultant_id, "pg-pub-consultant", "consultant"),
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
                text("INSERT INTO user_roles (id, user_id, role) VALUES (:id, :uid, :role)"),
                {"id": uuid.uuid4(), "uid": uid, "role": role},
            )
        conn.execute(
            text("INSERT INTO ta_consultants (id, ta_id, consultant_id) VALUES (:id, :ta, :c)"),
            {"id": uuid.uuid4(), "ta": ta_id, "c": consultant_id},
        )

    setup = Session()
    try:
        created = jobs_service.create_job(
            setup,
            jobs_service.build_input(
                title="Senior Embedded Engineer", company="Client AB", description=DESCRIPTION
            ),
            consultant=setup.get(User, consultant_id),
            actor=setup.get(User, ta_id),
        )
        application, _ = applications_service.apply(
            setup, created.job, setup.get(User, consultant_id), actor=setup.get(User, ta_id)
        )
        application_id = application.id
        setup.add(
            TailoredCV(
                application_id=application_id,
                status="ready",
                revision=1,
                content_hash="a" * 64,
                cv_state={"iName": "Nora"},
                input_snapshot={},
                input_hash="b" * 64,
            )
        )
        setup.commit()
    finally:
        setup.close()

    try:
        yield Session, application_id, ta_id
    finally:
        engine.dispose()


def test_terminal_transition_wins_first_and_publish_is_refused(
    pg_application_with_ready_cv, monkeypatch
):
    """One safe ordering: a real terminal `transition()` is mid-flight — past
    its row lock, not yet committed — when Publish is attempted. Publish must
    block, then be refused once the transition (hook included) completes."""
    Session, application_id, ta_id = pg_application_with_ready_cv
    holder = Session()
    paused = threading.Event()
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    errors: list = []

    real_disable = cv_publish_service.disable_for_application

    def pausing_disable(db, app_id):
        result = real_disable(db, app_id)
        # transition() has the row lock, has set the new status, and has run
        # the disable half of its hook — paused here, right before it deletes
        # the tailored CV and commits, so the lock is provably still held.
        paused.set()
        assert release.wait(timeout=30)
        return result

    monkeypatch.setattr(cv_publish_service, "disable_for_application", pausing_disable)

    def run_transition() -> None:
        try:
            applications_service.transition(
                holder, holder.get(Application, application_id),
                status="closed", actor=holder.get(User, ta_id),
            )
        finally:
            paused.set()  # in case transition raised before reaching the pause

    transition_thread = threading.Thread(target=run_transition)
    try:
        transition_thread.start()
        assert paused.wait(timeout=20), "the transition never reached the pause"

        def publish() -> None:
            db = Session()
            try:
                started.set()
                cv_publish_service.publish_for_application(
                    db, application=db.get(Application, application_id),
                    actor=db.get(User, ta_id),
                )
                errors.append(AssertionError("publish succeeded for a closed application"))
            except cv_publish_service.CVPublishError as exc:
                errors.append(exc)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()
                finished.set()

        thread = threading.Thread(target=publish)
        thread.start()
        assert started.wait(timeout=10)
        assert not finished.wait(timeout=2), (
            "publish decided the application's state without taking its row lock"
        )
        release.set()
        assert finished.wait(timeout=30)
        transition_thread.join(timeout=30)
        assert not transition_thread.is_alive()
    finally:
        holder.close()

    assert len(errors) == 1
    assert isinstance(errors[0], cv_publish_service.CVPublishError)
    assert errors[0].status == 409
    assert "ended" in str(errors[0])

    verify = Session()
    try:
        # The reviewer's reproduction must be unreachable: not this shape.
        assert verify.scalar(
            select(CVPublication).where(CVPublication.application_id == application_id)
        ) is None
        assert verify.get(Application, application_id).status == "closed"
        assert verify.query(TailoredCV).filter_by(application_id=application_id).count() == 0
    finally:
        verify.close()


def test_publish_wins_first_and_terminal_cleanup_then_revokes_it(
    pg_application_with_ready_cv, monkeypatch
):
    """The other safe ordering: Publish is already past its terminal check and
    committing when the terminal transition is attempted — the transition must
    block on the same row lock, then find and revoke the publication Publish
    just committed (and still delete the tailored CV)."""
    Session, application_id, ta_id = pg_application_with_ready_cv
    db = Session()
    paused = threading.Event()
    release = threading.Event()
    closed_after: list = []

    real_snapshot_photo = cv_publish_service._snapshot_photo

    def pausing_snapshot_photo(*args, **kwargs):
        result = real_snapshot_photo(*args, **kwargs)
        # Right before publish_for_application builds/commits the row — the
        # same "exactly where the commit used to be" seam
        # test_tailored_cv_pg.py's equivalent test uses.
        paused.set()
        assert release.wait(timeout=30)
        return result

    closer_finished = threading.Event()

    def racing_close() -> None:
        other = Session()
        try:
            applications_service.transition(
                other, other.get(Application, application_id),
                status="closed", actor=other.get(User, ta_id),
            )
            closed_after.append("closed")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            closed_after.append(exc)
        finally:
            other.close()
            closer_finished.set()

    monkeypatch.setattr(cv_publish_service, "_snapshot_photo", pausing_snapshot_photo)
    try:
        thread = threading.Thread(
            target=lambda: cv_publish_service.publish_for_application(
                db, application=db.get(Application, application_id),
                actor=db.get(User, ta_id),
            )
        )
        thread.start()
        assert paused.wait(timeout=20), "publish never reached the pause"

        closer = threading.Thread(target=racing_close)
        closer.start()
        # The terminal transition must genuinely block on Publish's still-open
        # row lock, not race past it — no lock_timeout here, unlike the sibling
        # window test in test_tailored_cv_pg.py, because this assertion is
        # about the safe ordering (it must eventually succeed once the lock is
        # released), not about contention alone.
        assert not closer_finished.wait(timeout=2), (
            "the terminal transition acquired the Application lock while "
            "publish still held it — the two are not serialized"
        )

        release.set()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert closer_finished.wait(timeout=30)
    finally:
        db.close()

    assert closed_after == ["closed"]

    verify = Session()
    try:
        assert verify.get(Application, application_id).status == "closed"
        pub = verify.scalar(
            select(CVPublication).where(CVPublication.application_id == application_id)
        )
        assert pub is not None, "publish's own commit did not land"
        assert pub.revoked is True, "the terminal cleanup did not revoke the link publish created"
        assert verify.query(TailoredCV).filter_by(application_id=application_id).count() == 0
    finally:
        verify.close()
