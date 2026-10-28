"""The live half of the allowed-target contract (M1-H-001).

`tests/test_integration_gate_safety.py` covers the no-I/O half (loopback host
plus a provisioned database name). This covers
`_assert_target_has_no_existing_application_data`, which needs a real
connection: it must accept a freshly migrated, empty database and refuse one
that already holds application data.

The second revision of this guard widened it from `users` alone to every
business table. `users` on its own was not enough — a database can hold Jobs,
publications, or audit history with no user rows at all and still obviously not
be disposable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from .conftest import (
    _APPLICATION_TABLES,
    NotDisposableError,
    _assert_target_has_no_existing_application_data,
)


def test_accepts_a_freshly_migrated_empty_database(migrated_db):
    """A fresh `alembic upgrade head` creates every business table but seeds no
    rows — this must pass, or every legitimate use of this gate would fail."""
    _assert_target_has_no_existing_application_data(migrated_db)


def test_every_guarded_table_exists_after_migration(pg_engine):
    """The guard is only as good as its table list: a name that no longer
    exists is silently skipped, so a typo or a renamed table would quietly
    stop being checked."""
    with pg_engine.connect() as conn:
        present = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        }
    missing = set(_APPLICATION_TABLES) - present
    assert not missing, (
        f"the guard names tables that do not exist after migration: {sorted(missing)}"
    )


def _insert_probe_user(conn, *, username: str = "probe") -> None:
    """Insert one raw `users` row.

    Raw SQL on purpose — this guard protects a database the ORM may not match.
    Since V2 Step 1 that means supplying `site_id` (NOT NULL, FK to `sites`)
    and *no* `role` column: role membership lives in `user_roles`.
    """
    site_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO sites (id, code, name, is_active, created_at, updated_at) "
            "VALUES (:id, :code, 'Probe Site', true, now(), now())"
        ),
        {"id": site_id, "code": f"PROBE-{uuid.uuid4().hex[:6]}"},
    )
    conn.execute(
        text(
            "INSERT INTO users "
            "(id, email, username, site_id, is_active, actively_looking, "
            " created_at, updated_at) "
            "VALUES (:id, :email, :username, :site_id, true, true, now(), now())"
        ),
        {
            "id": uuid.uuid4(),
            "email": f"{username}@example.invalid",
            "username": username,
            "site_id": site_id,
        },
    )


def test_rejects_a_database_with_a_populated_users_table(migrated_db, pg_engine):
    with pg_engine.begin() as conn:
        _insert_probe_user(conn)

    with pytest.raises(NotDisposableError, match="users=1"):
        _assert_target_has_no_existing_application_data(migrated_db)


def test_rejects_a_database_holding_only_site_configuration(
    migrated_db, pg_engine
):
    """A Site row alone means somebody provisioned this database deliberately.

    `sites` and `user_roles` were absent from the guarded list when they were
    introduced, so a database holding only Site configuration was accepted as
    disposable and dropped. Nothing else catches this case: no user rows exist,
    so the `users` check passes.
    """
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sites (id, code, name, is_active, created_at, updated_at) "
                "VALUES (:id, 'GBG', 'Göteborg', true, now(), now())"
            ),
            {"id": uuid.uuid4()},
        )

    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 0

    with pytest.raises(NotDisposableError, match="sites=1"):
        _assert_target_has_no_existing_application_data(migrated_db)


def test_rejects_a_database_holding_only_non_user_application_data(
    migrated_db, pg_engine
):
    """The case the first revision of this guard missed entirely: a database
    with no *directly seeded* user row, but real application data in it, was
    accepted as disposable.

    A `cv_publications` row is Application-owned since S5-P1-R1 (`application_id`
    is NOT NULL, FK'd through `applications` to a consultant `User`), so
    inserting one now transitively creates that whole chain rather than sitting
    as a free-standing row the way it once did. The guard must still name
    `cv_publications` specifically in its refusal, not merely notice `users`
    incidentally — asserted by matching on `cv_publications=1` rather than
    asserting `users` stayed empty, which is no longer true."""
    with pg_engine.begin() as conn:
        site_id, user_id, job_id, app_id = (
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        )
        conn.execute(
            text(
                "INSERT INTO sites (id, code, name, is_active, created_at, updated_at) "
                "VALUES (:id, 'GBG', 'Göteborg', true, now(), now())"
            ),
            {"id": site_id},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, email, username, is_active, "
                " actively_looking, site_id, created_at, updated_at) "
                "VALUES (:id, 'someone@example.invalid', 'someone', true, "
                " false, :site_id, now(), now())"
            ),
            {"id": user_id, "site_id": site_id},
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, site_id, title, description, content_hash, status, "
                " created_at, updated_at) "
                "VALUES (:id, :site_id, 'Real Job', 'A real posting, long enough to clear "
                " the minimum length floor.', :hash, 'open', now(), now())"
            ),
            {"id": job_id, "site_id": site_id, "hash": uuid.uuid4().hex + uuid.uuid4().hex},
        )
        conn.execute(
            text(
                "INSERT INTO applications (id, job_id, consultant_id, status, "
                " created_at, updated_at) "
                "VALUES (:id, :job_id, :consultant_id, 'applied', now(), now())"
            ),
            {"id": app_id, "job_id": job_id, "consultant_id": user_id},
        )
        conn.execute(
            text(
                "INSERT INTO cv_publications "
                "(id, token_hash, application_id, consultant_username, job_id, "
                " title, cv_state, expires_at, view_count, revoked, created_at, updated_at) "
                "VALUES (:id, :token_hash, :application_id, :username, :job_id, :title, "
                " CAST(:state AS jsonb), :expires_at, 0, false, now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,
                "application_id": app_id,
                "username": "someone",
                "job_id": "job_real",
                "title": "A real publication",
                "state": '{"name": "Real Person"}',
                "expires_at": datetime.now(timezone.utc) + timedelta(days=28),
            },
        )

    with pytest.raises(NotDisposableError, match="cv_publications=1"):
        _assert_target_has_no_existing_application_data(migrated_db)


def test_error_names_every_populated_table(migrated_db, pg_engine):
    """The refusal has to be diagnosable: it should say what it found, so the
    operator can tell at a glance which environment they nearly wiped."""
    with pg_engine.begin() as conn:
        _insert_probe_user(conn)
    with pytest.raises(NotDisposableError) as refused:
        _assert_target_has_no_existing_application_data(migrated_db)
    message = str(refused.value)
    assert "application data" in message
    assert "users=1" in message
    assert "Refusing" in message
