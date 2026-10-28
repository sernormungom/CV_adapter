"""Production database semantics that in-memory SQLite cannot express (V2 M1).

Each of these protects a behaviour the application already depends on, or that
an approved V2 design commits to:

- **JSONB** — `cv_publications.cv_state` today; `job_consultant_matches`
  projections in M8 (ADR-012).
- **Partial unique index** — the one-active-publication-per-pair rule (ADR-009).
- **Transactions** — Postgres aborts a whole transaction after a failed
  statement, where SQLite lets you carry on. Apply (M9) must create all of its
  records or none.
- **Concurrent writes** — two processes racing a unique key; the loser must get
  an integrity error, not a duplicate.
- **`FOR UPDATE SKIP LOCKED`** — the mechanism the workflow queue hands work out
  with. These tests prove the *engine* provides the guarantee, independently of
  the application table; the runner built on it is covered by
  `test_workflow_runner_pg.py`.

Every database here is created and dropped by the fixtures in `conftest.py`.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

# Postgres SQLSTATEs, asserted directly rather than matching on message text or
# on which SQLAlchemy wrapper class a driver version happens to choose.
INVALID_TEXT_REPRESENTATION = "22P02"
IN_FAILED_SQL_TRANSACTION = "25P02"
LOCK_NOT_AVAILABLE = "55P03"


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _seed_application(conn) -> uuid.UUID:
    """The minimal FK chain a `cv_publications` row now requires: `application_id`
    is NOT NULL and FK'd to `applications` (S5-P1-R1 — a publication is bound to
    an Application-owned tailored CV, not a free-standing `(consultant, job)`
    pair). Random-suffixed so two calls in one test (a "different application"
    case) never collide on a unique column, regardless of the per-test
    disposable database also making that true across tests."""
    suffix = uuid.uuid4().hex
    site_id, user_id, job_id, app_id = (
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    )
    conn.execute(
        text(
            "INSERT INTO sites (id, code, name, is_active) "
            "VALUES (:id, :code, 'Fixture Site', true)"
        ),
        {"id": site_id, "code": f"fx-{suffix[:20]}"},
    )
    conn.execute(
        text(
            "INSERT INTO users (id, email, username, is_active, "
            " actively_looking, site_id) "
            "VALUES (:id, :email, :username, true, false, :site_id)"
        ),
        {
            "id": user_id,
            "email": f"fx-{suffix}@example.invalid",
            "username": f"fx-{suffix[:28]}",
            "site_id": site_id,
        },
    )
    conn.execute(
        text(
            "INSERT INTO jobs (id, site_id, title, description, content_hash, status) "
            "VALUES (:id, :site_id, 'Integration Role', "
            " 'Integration description text, long enough to clear the "
            " service minimum-length floor.', :hash, 'open')"
        ),
        {"id": job_id, "site_id": site_id, "hash": suffix},
    )
    conn.execute(
        text(
            "INSERT INTO applications (id, job_id, consultant_id, status) "
            "VALUES (:id, :job_id, :consultant_id, 'applied')"
        ),
        {"id": app_id, "job_id": job_id, "consultant_id": user_id},
    )
    return app_id


def _publication_values(application_id: uuid.UUID, **overrides) -> dict:
    """Minimum viable cv_publications row. `created_by_id` stays NULL, so a
    caller need only seed the Application chain (`_seed_application`), not a
    second, separate user for authorship."""
    values = {
        "id": uuid.uuid4(),
        "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,  # 64 chars, sha256-shaped
        "application_id": application_id,
        "consultant_username": "fixture-consultant",
        "job_id": "job_integration",
        "title": "Integration Role",
        "cv_state": '{"name": "Fixture Person", "skills": ["python", "sql"], '
                    '"years": 7, "remote": true, "notes": null}',
        "photo_path": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=28),
        "view_count": 0,
        "revoked": False,
    }
    values.update(overrides)
    return values


_INSERT_PUBLICATION = text(
    "INSERT INTO cv_publications "
    "(id, token_hash, application_id, consultant_username, job_id, title, "
    " cv_state, photo_path, expires_at, view_count, revoked, created_at, updated_at) "
    "VALUES (:id, :token_hash, :application_id, :consultant_username, :job_id, :title, "
    " CAST(:cv_state AS jsonb), :photo_path, :expires_at, :view_count, "
    " :revoked, now(), now())"
)


# ── JSONB ───────────────────────────────────────────────────────────────────

def test_jsonb_round_trips_types_not_just_text(pg_engine):
    """jsonb preserves the JSON type of each value. SQLite's JSON variant
    stores a string, so this distinction is invisible in the fast gate."""
    with pg_engine.begin() as conn:
        app_id = _seed_application(conn)
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id))
        row = conn.execute(
            text(
                "SELECT jsonb_typeof(cv_state->'skills') AS skills, "
                "       jsonb_typeof(cv_state->'years')  AS years, "
                "       jsonb_typeof(cv_state->'remote') AS remote, "
                "       jsonb_typeof(cv_state->'notes')  AS notes, "
                "       cv_state->>'name'                AS name "
                "FROM cv_publications"
            )
        ).one()
    assert row.skills == "array"
    assert row.years == "number"
    assert row.remote == "boolean"
    assert row.notes == "null"
    assert row.name == "Fixture Person"


def test_jsonb_containment_and_path_queries_work(pg_engine):
    """`@>` and `->>` are how a stored match projection will be filtered
    without deserialising every row in Python."""
    with pg_engine.begin() as conn:
        app_id = _seed_application(conn)
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id))

        contained = conn.execute(
            text(
                "SELECT count(*) FROM cv_publications "
                "WHERE cv_state @> CAST(:probe AS jsonb)"
            ),
            {"probe": '{"remote": true}'},
        ).scalar_one()
        assert contained == 1

        not_contained = conn.execute(
            text(
                "SELECT count(*) FROM cv_publications "
                "WHERE cv_state @> CAST(:probe AS jsonb)"
            ),
            {"probe": '{"remote": false}'},
        ).scalar_one()
        assert not_contained == 0

        # jsonb_exists() is the function form of the `?` operator; spelled this
        # way so a literal `?` never collides with a driver's placeholder style.
        in_array = conn.execute(
            text(
                "SELECT count(*) FROM cv_publications "
                "WHERE jsonb_exists(cv_state->'skills', :skill)"
            ),
            {"skill": "python"},
        ).scalar_one()
        assert in_array == 1


def test_jsonb_rejects_malformed_documents(pg_engine):
    """A jsonb column will not accept a non-document, so a corrupt snapshot
    fails at write time rather than surfacing on read."""
    with pg_engine.begin() as conn:
        app_id = _seed_application(conn)
    with pg_engine.connect() as conn:
        with pytest.raises(DBAPIError) as rejected:
            conn.execute(
                _INSERT_PUBLICATION,
                _publication_values(app_id, cv_state="{not valid json"),
            )
        assert _sqlstate(rejected.value) == INVALID_TEXT_REPRESENTATION
        conn.rollback()


# ── Partial unique index ────────────────────────────────────────────────────

def test_partial_index_blocks_a_second_active_publication(pg_engine):
    """ADR-009: exactly one active publication per Application."""
    with pg_engine.begin() as conn:
        app_id = _seed_application(conn)
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id))

    with pg_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(_INSERT_PUBLICATION, _publication_values(app_id))


def test_partial_index_allows_a_republish_after_revoke(pg_engine):
    """The index is partial precisely so a revoked row does not permanently
    block the Application. This is the half a plain unique constraint would
    break."""
    with pg_engine.begin() as conn:
        app_id = _seed_application(conn)
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id, revoked=True))
        # Same Application, new active row — allowed because the old one is revoked.
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id, revoked=False))

    with pg_engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM cv_publications WHERE NOT revoked")
        ).scalar_one()
        total = conn.execute(
            text("SELECT count(*) FROM cv_publications")
        ).scalar_one()
    assert (active, total) == (1, 2)


def test_many_revoked_rows_are_allowed_for_one_pair(pg_engine):
    """Revoked history accumulates; only the active row is constrained."""
    with pg_engine.begin() as conn:
        app_id = _seed_application(conn)
        for _ in range(3):
            conn.execute(_INSERT_PUBLICATION, _publication_values(app_id, revoked=True))
    with pg_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM cv_publications")
        ).scalar_one() == 3


# ── Transactions ────────────────────────────────────────────────────────────

def test_rollback_discards_every_write_in_the_transaction(pg_engine):
    with pg_engine.connect() as conn:
        trans = conn.begin()
        app_id_1 = _seed_application(conn)
        app_id_2 = _seed_application(conn)
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id_1))
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id_2))
        assert conn.execute(
            text("SELECT count(*) FROM cv_publications")
        ).scalar_one() == 2
        trans.rollback()

    with pg_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM cv_publications")
        ).scalar_one() == 0


def test_a_failed_statement_aborts_the_whole_transaction(pg_engine):
    """Postgres-specific and important for M9's Apply: after an error, further
    statements in the same transaction are refused until rollback. SQLite lets
    the transaction continue, so a partial-write bug can hide in the fast gate.

    "Apply creates exactly one usable Application ... failure creates none of
    those live records" (implementation plan §1) depends on this.
    """
    with pg_engine.connect() as conn:
        trans = conn.begin()
        app_id = _seed_application(conn)
        conn.execute(_INSERT_PUBLICATION, _publication_values(app_id))
        with pytest.raises(IntegrityError):
            # Duplicate active pair.
            conn.execute(_INSERT_PUBLICATION, _publication_values(app_id))

        # The transaction is now poisoned: even a trivially valid statement
        # fails until rollback.
        with pytest.raises(DBAPIError) as aborted:
            conn.execute(text("SELECT 1"))
        assert _sqlstate(aborted.value) == IN_FAILED_SQL_TRANSACTION
        trans.rollback()

        # After rollback the connection is usable and nothing was committed.
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
        assert conn.execute(
            text("SELECT count(*) FROM cv_publications")
        ).scalar_one() == 0


# ── Concurrent writes ───────────────────────────────────────────────────────

def test_concurrent_inserts_of_the_same_pair_produce_one_winner(migrated_db):
    """B's insert races A's while A is still uncommitted — B must genuinely
    block on A's row lock, not merely fail after A has already committed.

    An earlier version of this test committed A *before* issuing B's insert,
    which made it serial: two sequential inserts into an already-populated
    unique index will raise IntegrityError with no working lock behaviour
    involved at all, so it proved nothing about the actual race that matters
    when a TA and a Consultant both click Publish at the same time. B's insert
    runs on a background thread (a blocking DB call cannot run inline while
    the main thread still needs to drive A), and a third, observer connection
    polls `pg_stat_activity` to confirm B is really lock-blocked — not just
    slow to start — before A is allowed to commit and release it.
    """
    engine_a = create_engine(migrated_db)
    engine_b = create_engine(migrated_db)
    engine_observer = create_engine(migrated_db)
    try:
        with engine_a.begin() as seed_conn:
            app_id = _seed_application(seed_conn)

        with engine_a.connect() as conn_a:
            trans_a = conn_a.begin()
            conn_a.execute(_INSERT_PUBLICATION, _publication_values(app_id))
            # A now holds the unique-index lock, uncommitted.

            b_outcome: dict[str, object] = {}
            b_began = threading.Event()

            def _attempt_b() -> None:
                with engine_b.connect() as conn_b:
                    trans_b = conn_b.begin()
                    b_began.set()
                    try:
                        conn_b.execute(_INSERT_PUBLICATION, _publication_values(app_id))
                        b_outcome["result"] = "succeeded"
                    except IntegrityError:
                        b_outcome["result"] = "integrity_error"
                    finally:
                        trans_b.rollback()

            thread = threading.Thread(target=_attempt_b)
            thread.start()
            assert b_began.wait(timeout=5), "B's thread never started its transaction"

            # Confirm B is actually lock-blocked on A's uncommitted row before
            # letting A commit — otherwise a slow-but-not-yet-issued B insert
            # could race past this point without ever having contended at all.
            blocked = False
            deadline = time.monotonic() + 5
            with engine_observer.connect() as observer:
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE wait_event_type = 'Lock' "
                            "AND query ILIKE '%cv_publications%'"
                        )
                    ).scalar_one()
                    if waiting:
                        blocked = True
                        break
                    time.sleep(0.05)
            assert blocked, (
                "B's insert was never observed lock-blocked in pg_stat_activity "
                "-- this test is not exercising real contention"
            )

            trans_a.commit()  # releases the lock; B can now proceed and fail
            thread.join(timeout=5)
            assert not thread.is_alive(), "B's thread did not finish after A committed"

        assert b_outcome.get("result") == "integrity_error", b_outcome

        with engine_a.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM cv_publications")
            ).scalar_one() == 1
    finally:
        engine_a.dispose()
        engine_b.dispose()
        engine_observer.dispose()


def test_row_locks_are_real(migrated_db):
    """`SELECT ... FOR UPDATE NOWAIT` on a row another transaction holds must
    fail rather than silently proceed."""
    engine_a = create_engine(migrated_db)
    engine_b = create_engine(migrated_db)
    try:
        with engine_a.connect() as conn:
            with conn.begin():
                app_id = _seed_application(conn)
                conn.execute(_INSERT_PUBLICATION, _publication_values(app_id))

        with engine_a.connect() as conn_a, engine_b.connect() as conn_b:
            trans_a = conn_a.begin()
            conn_a.execute(
                text("SELECT id FROM cv_publications FOR UPDATE")
            ).all()

            trans_b = conn_b.begin()
            with pytest.raises(DBAPIError) as locked:
                conn_b.execute(
                    text("SELECT id FROM cv_publications FOR UPDATE NOWAIT")
                ).all()
            assert _sqlstate(locked.value) == LOCK_NOT_AVAILABLE
            trans_b.rollback()
            trans_a.rollback()
    finally:
        engine_a.dispose()
        engine_b.dispose()


# ── FOR UPDATE SKIP LOCKED — the queue claim mechanism ──────────────────────
#
# The real queue is `workflow_runs` (V2 Step 1.3), and `test_workflow_runner_pg.py`
# exercises the runner on top of it. These tests deliberately use a scratch
# table instead, to prove the *engine* provides the claim guarantee independently
# of the application schema — if this stops holding, the runner is unsafe.
#
# The scratch table carries `lease_owner`/`lease_expiry` columns only as a
# generic claim shape. P-006 narrowed ADR-011 for the pilot and **defers leases
# and heartbeats**: `workflow_runs` has no lease columns, and leadership plus
# startup recovery replaces them. Do not read lease semantics into the
# implemented design from these fixtures.

_QUEUE_DDL = text(
    "CREATE TABLE m1_queue_probe ("
    "  id           serial PRIMARY KEY,"
    "  state        text        NOT NULL DEFAULT 'queued',"
    "  lease_owner  text,"
    "  lease_expiry timestamptz"
    ")"
)

_CLAIM_ONE = text(
    "UPDATE m1_queue_probe SET state = 'running', lease_owner = :owner, "
    "  lease_expiry = now() + interval '30 seconds' "
    "WHERE id = ("
    "  SELECT id FROM m1_queue_probe WHERE state = 'queued' "
    "  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
    ") RETURNING id"
)


@pytest.fixture
def queue_db(migrated_db):
    engine = create_engine(migrated_db)
    try:
        with engine.begin() as conn:
            conn.execute(_QUEUE_DDL)
            conn.execute(
                text(
                    "INSERT INTO m1_queue_probe (state) "
                    "SELECT 'queued' FROM generate_series(1, 4)"
                )
            )
    finally:
        engine.dispose()
    return migrated_db


def test_skip_locked_never_hands_the_same_row_to_two_workers(queue_db):
    """Two open transactions claiming concurrently must get different rows —
    the whole basis of running one bounded runner safely alongside a
    deployment's overlapping old task."""
    engine_a = create_engine(queue_db)
    engine_b = create_engine(queue_db)
    try:
        with engine_a.connect() as conn_a, engine_b.connect() as conn_b:
            trans_a = conn_a.begin()
            trans_b = conn_b.begin()

            claimed_a = conn_a.execute(_CLAIM_ONE, {"owner": "a"}).scalar_one()
            # A has not committed. Without SKIP LOCKED this would block.
            claimed_b = conn_b.execute(_CLAIM_ONE, {"owner": "b"}).scalar_one()

            assert claimed_a != claimed_b

            trans_a.commit()
            trans_b.commit()

        with engine_a.connect() as conn:
            owners = conn.execute(
                text(
                    "SELECT lease_owner, count(*) FROM m1_queue_probe "
                    "WHERE state = 'running' GROUP BY lease_owner "
                    "ORDER BY lease_owner"
                )
            ).all()
        assert owners == [("a", 1), ("b", 1)]
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_skip_locked_drains_the_queue_without_duplicates(queue_db):
    """Claim until empty across two workers: every row claimed exactly once,
    and an exhausted queue returns nothing rather than blocking."""
    engine_a = create_engine(queue_db)
    engine_b = create_engine(queue_db)
    try:
        claimed: list[int] = []
        for owner, engine in [("a", engine_a), ("b", engine_b)] * 4:
            with engine.connect() as conn:
                with conn.begin():
                    row = conn.execute(_CLAIM_ONE, {"owner": owner}).scalar_one_or_none()
            if row is not None:
                claimed.append(row)

        assert sorted(claimed) == [1, 2, 3, 4]
        assert len(set(claimed)) == len(claimed), "a row was claimed twice"

        with engine_a.connect() as conn:
            remaining = conn.execute(
                text("SELECT count(*) FROM m1_queue_probe WHERE state = 'queued'")
            ).scalar_one()
        assert remaining == 0
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_skip_locked_ignores_a_row_another_worker_holds_uncommitted(queue_db):
    """A worker that crashes mid-claim must not strand the row for others: its
    transaction is rolled back and the row becomes claimable again."""
    engine_a = create_engine(queue_db)
    engine_b = create_engine(queue_db)
    try:
        with engine_a.connect() as conn_a:
            trans_a = conn_a.begin()
            held = conn_a.execute(_CLAIM_ONE, {"owner": "a"}).scalar_one()

            with engine_b.connect() as conn_b:
                with conn_b.begin():
                    other = conn_b.execute(_CLAIM_ONE, {"owner": "b"}).scalar_one()
                assert other != held

            # A "crashes".
            trans_a.rollback()

        with engine_b.connect() as conn:
            with conn.begin():
                reclaimed = conn.execute(_CLAIM_ONE, {"owner": "c"}).scalar_one()
        assert reclaimed == held, "the abandoned row was not reclaimable"
    finally:
        engine_a.dispose()
        engine_b.dispose()
