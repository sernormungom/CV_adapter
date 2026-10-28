"""A disposable Postgres 16 for the integration gate (V2 M1, gap G-037).

The rest of the suite runs on in-memory SQLite, which cannot express the
production semantics this project actually depends on: JSONB operators, partial
unique indexes, aborted-transaction behaviour, real row locks, and
`FOR UPDATE SKIP LOCKED` (the mechanism the `workflow_runs` queue is built on —
see `test_workflow_runner_pg.py`). This gate runs those against the real engine.

Where the database comes from, in order:

1. `CV_ADAPTER_TEST_DATABASE_URL` — a local test service. This is what CI uses
   (a GitHub Actions `services: postgres:16` container). It is accepted only if
   it satisfies the allowed-target contract below.
2. Otherwise a `postgres:16` Docker container that this module starts on a
   random loopback port and removes at session end.
3. Otherwise the gate skips — so `make test-complete` still works on a machine
   without Docker.

That skip is a local convenience only. Set `CV_ADAPTER_REQUIRE_INTEGRATION=1`
(CI does) and an unavailable database is a hard error instead, so the gate can
never quietly contribute nothing to a green build.

**Every database this touches must be disposable, and that is enforced, not
assumed.** These fixtures `CREATE DATABASE`, `DROP DATABASE`, and
`pg_terminate_backend`, so an externally supplied URL is checked against a
positive allowlist *before* a connection is opened:

- the host must be **loopback** (an IP literal that is loopback, or
  `localhost` confirmed to resolve only to loopback) — remote and external
  URLs are unsupported, with no opt-out; and
- the database must be one this project provisions for testing: exactly
  `cvtest` (the CI service container) or an `itest_<12 hex>` database created
  by this gate.

A target that passes those is then checked *live* for existing application
data across every business table, and refused if any holds rows. A
self-started container skips these checks: we chose its name, port, and
database ourselves, seconds earlier.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]

PG_IMAGE = "postgres:16"
PG_USER = "cvtest"
PG_PASSWORD = "cvtest"
PG_DB = "cvtest"

_REQUIRE = os.environ.get("CV_ADAPTER_REQUIRE_INTEGRATION", "").lower() in {
    "1",
    "true",
    "yes",
}


def _unavailable(reason: str):
    """Skip locally, fail in CI."""
    if _REQUIRE:
        pytest.fail(
            f"integration gate is required but unavailable: {reason}. "
            "CV_ADAPTER_REQUIRE_INTEGRATION is set, so this is an error rather "
            "than a skip."
        )
    pytest.skip(reason, allow_module_level=True)


class NotDisposableError(RuntimeError):
    """`CV_ADAPTER_TEST_DATABASE_URL` names a target this gate is not allowed to
    mutate, so no destructive integration test may run against it. See
    `_assert_target_is_an_allowed_test_service` and
    `_assert_target_has_no_existing_application_data`."""


def _url_identity(url: str) -> tuple[str, int | None, str]:
    """(host, port, dbname) — the target a URL names, ignoring credentials and
    query string."""
    parsed = urlsplit(url)
    return (parsed.hostname or "", parsed.port, (parsed.path or "").lstrip("/"))


# ── The allowed-target contract ─────────────────────────────────────────────
#
# This is an allowlist of positively-controlled test services, not a heuristic.
# An earlier version accepted any URL whose database name merely *contained*
# "test" and whose `users` table happened to be empty. That was not a guarantee
# of anything: `postgresql://user:pw@db-host:5432/itest_db` satisfied it while
# pointing at an arbitrary reachable remote server, which this gate would then
# CREATE DATABASE / DROP DATABASE / pg_terminate_backend against.
#
# The contract now is: the target must be on loopback, and its database must be
# one of the two identities this project actually provisions for testing.
# Remote and external URLs are unsupported — there is no opt-out, because the
# only reason to point this at a remote server is a mistake. If you want a
# throwaway database of your own, let the Docker path in `pg_url` create one.

_LOOPBACK_HOSTNAME = "localhost"

# The exact database name ci.yml's integration job provisions as a
# `services: postgres:16` container. Pinned by a test in
# tests/test_integration_gate_safety.py so a change to that workflow cannot
# silently drift away from this allowlist.
CI_TEST_DB_NAME = "cvtest"

# The per-test databases `blank_db` creates on the server itself, named
# `itest_<uuid4 hex[:12]>`. Anchored and length-bounded so an arbitrary
# `itest_db` does not match.
_OWN_GENERATED_DB = re.compile(r"^itest_[0-9a-f]{12}$")


def _host_is_loopback(host: str) -> bool:
    """True only for loopback, decided without a DNS lookup wherever possible.

    An IP literal is checked directly. The single hostname accepted is
    `localhost`, and even that is resolved to confirm every address it maps to
    is loopback (a hosts-file entry could point it elsewhere). Any other
    hostname is refused outright with no lookup at all: resolving it would be
    both slow and pointless, since a name that resolves to 127.0.0.1 today is
    still not a target this gate controls.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass  # not an IP literal — fall through to the hostname rule

    if host.lower() != _LOOPBACK_HOSTNAME:
        return False
    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not resolved:
        return False
    return all(
        ipaddress.ip_address(info[4][0]).is_loopback for info in resolved
    )


def _assert_target_is_an_allowed_test_service(url: str) -> None:
    """Refuse anything but a positively-allowed local test target. No I/O
    beyond at most one `localhost` resolution, and always **before** a
    connection is opened."""
    host, _, dbname = _url_identity(url)

    if not _host_is_loopback(host):
        raise NotDisposableError(
            f"CV_ADAPTER_TEST_DATABASE_URL points at host {host!r}, which is "
            "not loopback. Destructive integration tests only run against a "
            "local test service; remote and external URLs are unsupported. "
            "Unset the variable to let the Docker path create a disposable "
            "database instead."
        )

    if dbname != CI_TEST_DB_NAME and not _OWN_GENERATED_DB.match(dbname):
        raise NotDisposableError(
            f"CV_ADAPTER_TEST_DATABASE_URL names database {dbname!r}, which is "
            f"not one this project provisions for testing (expected "
            f"{CI_TEST_DB_NAME!r} or an itest_<12 hex> database created by this "
            "gate). Refusing to run destructive integration tests against it."
        )


# Every table the application writes business, personal, or configuration data
# to. `users` alone was not enough: a database could hold Jobs, publications, or
# audit history with no user rows at all and still obviously not be disposable.
# `alembic_version` is deliberately excluded — a migrated empty database has a
# row there and that is expected.
#
# The list is deliberately *complete* rather than minimal. `user_roles` cannot
# currently be non-empty while `users` is empty (NOT NULL FK, ON DELETE
# CASCADE), and a lone `sites` row is cheap to recreate — but "some other check
# already covers it" is exactly the reasoning that left this guard checking only
# `users` the first time. A table missing from this tuple is silently
# unguarded, so every table the baseline creates is named here and
# `test_every_guarded_table_exists_after_migration` keeps the names honest.
# `sites` is included because a populated Site means somebody provisioned this
# database on purpose — which is the signal this guard exists to notice.
_APPLICATION_TABLES = (
    "sites",
    "users",
    "user_roles",
    "ta_consultants",
    "user_sessions",
    "invites",
    # Relational Jobs (Slice 1). A database holding jobs, their consultant
    # links, or review markers is somebody's working state, not a blank target.
    "jobs",
    "job_decision_pairs",
    "job_reviews",
    # Cached advisory scores (S1). A populated cache implies scored Jobs and
    # Consultants, not a blank target.
    "job_consultant_matches",
    # Applications (Slice 2). A database holding these has real consultants
    # placed against real postings — the clearest possible "not a blank target".
    "applications",
    "notifications",
    "audit_logs",
    "cv_publications",
    # Accepted-but-unfinished background work. A database holding workflow rows
    # has live state somebody is polling for; dropping it loses accepted work.
    "workflow_runs",
)


def _assert_target_has_no_existing_application_data(url: str) -> None:
    """Live signal: refuse a target that already holds application data.

    A freshly created disposable Postgres — our own container, or a CI service
    container — has these tables either absent or empty even after migrations
    run, because nothing seeds them. A real staging/production/personal-dev
    database does not. This is the check that is *observed* on the target
    rather than inferred from its name.
    """
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            present = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            }
            populated: dict[str, int] = {}
            for table in _APPLICATION_TABLES:
                if table not in present:
                    continue
                # Table names come from the module-level tuple above, never
                # from the URL or any other caller input.
                count = conn.execute(
                    text(f'SELECT count(*) FROM "{table}"')
                ).scalar_one()
                if count:
                    populated[table] = count
            if populated:
                detail = ", ".join(
                    f"{name}={count}" for name, count in sorted(populated.items())
                )
                raise NotDisposableError(
                    "CV_ADAPTER_TEST_DATABASE_URL's target already holds "
                    f"application data ({detail}) — this looks like a real "
                    "environment, not a disposable test database. Refusing to "
                    "run destructive integration tests against it."
                )
    finally:
        engine.dispose()


def _run(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_works() -> bool:
    try:
        return _run(["docker", "info"], timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _start_container() -> tuple[str, str]:
    """Start a throwaway Postgres 16 and return (container_name, url)."""
    name = f"cv-adapter-itest-{uuid.uuid4().hex[:10]}"
    started = _run(
        [
            "docker", "run", "--detach", "--rm", "--name", name,
            "--env", f"POSTGRES_USER={PG_USER}",
            "--env", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "--env", f"POSTGRES_DB={PG_DB}",
            # Random free loopback port, so parallel runs never collide and
            # nothing is exposed off-host.
            "--publish", "127.0.0.1::5432",
            # This database is thrown away, so durability is wasted time.
            PG_IMAGE, "-c", "fsync=off", "-c", "full_page_writes=off",
        ],
        timeout=300,
    )
    if started.returncode != 0:
        _unavailable(f"could not start {PG_IMAGE}: {started.stderr.strip()}")

    published = _run(["docker", "port", name, "5432/tcp"])
    if published.returncode != 0 or not published.stdout.strip():
        _run(["docker", "rm", "--force", name])
        _unavailable(f"could not read the published port: {published.stderr.strip()}")
    # e.g. "127.0.0.1:49158" (possibly several lines for v4/v6).
    port = published.stdout.strip().splitlines()[0].rsplit(":", 1)[1]

    url = f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@127.0.0.1:{port}/{PG_DB}"

    # The image first accepts local socket checks in a temporary init server,
    # then restarts for its normal TCP listener. `docker exec pg_isready` can
    # therefore say ready while the *published client path* still drops its
    # first connection. Require two consecutive fresh TCP connections and a
    # real SELECT instead of trusting the container-local socket probe.
    deadline = time.monotonic() + 120
    consecutive_ready = 0
    last_error = "no connection attempted"
    while time.monotonic() < deadline:
        engine = None
        try:
            engine = create_engine(url, connect_args={"connect_timeout": 3})
            with engine.connect() as conn:
                assert conn.exec_driver_sql("SELECT 1").scalar_one() == 1
            consecutive_ready += 1
            if consecutive_ready >= 2:
                return name, url
        except Exception as exc:  # noqa: BLE001 - bounded readiness retry
            consecutive_ready = 0
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if engine is not None:
                engine.dispose()
        time.sleep(0.5)

    logs = _run(["docker", "logs", name]).stdout[-3000:]
    _run(["docker", "rm", "--force", name])
    _unavailable(
        f"{PG_IMAGE} never became stably ready on its published TCP port. "
        f"Last error: {last_error}. Container logs:\n{logs}"
    )


@pytest.fixture(scope="session")
def pg_url() -> str:
    """A URL for a disposable Postgres 16, however it is provided.

    An externally supplied URL is checked *before* being handed to any other
    fixture: `blank_db` goes on to `CREATE DATABASE`/`DROP DATABASE` and
    `pg_terminate_backend` against whatever this returns, so this is the one
    place that guard has to live to cover every downstream fixture (M1-H-001).
    A self-started container needs no check — we chose its name and database
    ourselves, seconds ago.
    """
    external = os.environ.get("CV_ADAPTER_TEST_DATABASE_URL")
    if external:
        try:
            _assert_target_is_an_allowed_test_service(external)
            _assert_target_has_no_existing_application_data(external)
        except NotDisposableError as exc:
            pytest.fail(str(exc))
        except Exception as exc:  # connection failure, bad URL, etc.
            pytest.fail(
                "could not positively verify CV_ADAPTER_TEST_DATABASE_URL is "
                f"disposable, so refusing to use it: {exc}"
            )
        yield external
        return

    if not _docker_works():
        _unavailable(
            "no CV_ADAPTER_TEST_DATABASE_URL and Docker is not available, so no "
            "disposable Postgres 16 could be created"
        )

    name, url = _start_container()
    try:
        yield url
    finally:
        # --rm means stopping removes it; force covers a container that never
        # started cleanly.
        _run(["docker", "rm", "--force", name], timeout=120)


@pytest.fixture(scope="session")
def pg_server_version(pg_url) -> int:
    """Major version of the server under test, so a test can say what it saw."""
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            # SHOW returns text, e.g. "160009" for 16.9.
            raw = conn.execute(text("SHOW server_version_num")).scalar_one()
        return int(raw) // 10000
    finally:
        engine.dispose()


def alembic(*args: str, url: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run the real `alembic` CLI against `url` in a subprocess.

    A subprocess, not `alembic.command`, on purpose: `alembic/env.py` reads
    `DATABASE_URL` through `backend.config` at import time, and this test
    session has already imported that module with the isolated test settings.
    Calling the library in-process would migrate the wrong database. Running the
    CLI also exercises the exact command production deploys use.
    """
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        ["alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


@pytest.fixture
def blank_db(pg_url) -> str:
    """A URL for a freshly created, empty database on the same server.

    Migration tests need a database with no schema and no `alembic_version`
    row. Each gets its own so they cannot interfere, and it is dropped
    afterwards.
    """
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"itest_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()

    url = pg_url.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield url
    finally:
        admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                # Terminate stragglers first; DROP DATABASE fails while any
                # session is still connected.
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :n AND pid <> pg_backend_pid()"
                    ),
                    {"n": name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            admin.dispose()


@pytest.fixture
def migrated_db(blank_db) -> str:
    """A fresh database migrated to head with the real `alembic upgrade`."""
    result = alembic("upgrade", "head", url=blank_db)
    assert result.returncode == 0, (
        f"alembic upgrade head failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return blank_db


@pytest.fixture
def pg_engine(migrated_db):
    """An engine on a migrated, disposable database."""
    engine = create_engine(migrated_db, future=True)
    try:
        yield engine
    finally:
        engine.dispose()
