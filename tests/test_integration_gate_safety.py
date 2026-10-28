"""The allowed-target contract for the Postgres integration gate (M1-H-001).

`tests/integration/conftest.py::pg_url` lets CI (or a developer) point the
integration gate at an externally supplied `CV_ADAPTER_TEST_DATABASE_URL`
rather than the container it starts itself. Those fixtures then `CREATE
DATABASE`, `DROP DATABASE`, and `pg_terminate_backend` against whatever that
names, so the target has to be positively allowed rather than merely
plausible-looking.

Two revisions of this guard exist, and the difference is the point:

- The first accepted any URL whose database name *contained* "test" and whose
  `users` table happened to be empty. That is a heuristic, not a guarantee —
  `postgresql://user:pw@db-host:5432/itest_db` satisfied it while naming an
  arbitrary reachable remote server.
- The current one is an allowlist: loopback host, and a database this project
  actually provisions (`cvtest`, or a gate-created `itest_<12 hex>`).

This file covers the no-I/O half of the contract, so it runs in the fast gate
and needs no Postgres. The live application-data half is covered by
`tests/integration/test_disposability_guard.py`.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    CI_TEST_DB_NAME,
    NotDisposableError,
    _assert_target_is_an_allowed_test_service,
    _host_is_loopback,
)

# ── Accepted targets ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        # The exact identity ci.yml's integration job provisions.
        f"postgresql+psycopg://cvtest:cvtest@localhost:5432/{CI_TEST_DB_NAME}",
        # Same, via an explicit loopback literal and a random published port,
        # which is what the self-started Docker container looks like.
        f"postgresql+psycopg://cvtest:cvtest@127.0.0.1:49158/{CI_TEST_DB_NAME}",
        # A per-test database this gate creates itself.
        "postgresql+psycopg://cvtest:cvtest@127.0.0.1:5432/itest_0123456789ab",
    ],
)
def test_accepts_a_provisioned_local_test_target(url):
    _assert_target_is_an_allowed_test_service(url)  # must not raise


def test_matches_this_projects_own_ci_configuration():
    """Pinned to the literal value ci.yml's integration job sets for
    CV_ADAPTER_TEST_DATABASE_URL. If that workflow's database name changes, the
    allowlist has drifted and CI would fail closed — catch it here instead."""
    _assert_target_is_an_allowed_test_service(
        "postgresql+psycopg://cvtest:cvtest@localhost:5432/cvtest"
    )


# ── Rejected: not loopback ──────────────────────────────────────────────────
# The finding that reopened M1-H-001: a remote target with a test-ish database
# name is still a remote target.


@pytest.mark.parametrize(
    "url",
    [
        # The exact example cited in review.
        "postgresql://user:pw@db-host:5432/itest_db",
        f"postgresql://user:pw@db-host:5432/{CI_TEST_DB_NAME}",
        "postgresql://user:pw@10.0.0.5:5432/cvtest",
        "postgresql://user:pw@db.internal.example.com:5432/cvtest",
        "postgresql://user:pw@rds.eu-north-1.amazonaws.com:5432/cvtest",
        # A public IP literal.
        "postgresql://user:pw@8.8.8.8:5432/cvtest",
    ],
)
def test_rejects_a_non_loopback_host(url):
    with pytest.raises(NotDisposableError, match="not loopback"):
        _assert_target_is_an_allowed_test_service(url)


def test_rejects_a_missing_host():
    with pytest.raises(NotDisposableError, match="not loopback"):
        _assert_target_is_an_allowed_test_service("postgresql:///cvtest")


# ── Rejected: loopback, but not a provisioned database ──────────────────────


@pytest.mark.parametrize(
    "dbname",
    [
        # The application's real database name — the original realistic accident.
        "cvadapter",
        # Test-ish but not provisioned by this project. Rejected precisely
        # because "looks like a test database" is not the contract any more.
        "itest_db",
        "test",
        "cvtest_staging",
        "my_test_db",
        # Right prefix, wrong shape for a gate-created database.
        "itest_",
        "itest_zzzzzzzzzzzz",
        "itest_0123456789abc",
        "itest_0123456789a",
    ],
)
def test_rejects_a_loopback_database_this_project_does_not_provision(dbname):
    with pytest.raises(NotDisposableError, match="not one this project provisions"):
        _assert_target_is_an_allowed_test_service(
            f"postgresql+psycopg://u:p@127.0.0.1:5432/{dbname}"
        )


def test_host_check_is_ordered_before_the_database_check():
    """A remote URL is refused for being remote, even when its database name
    would otherwise be allowed — so the error names the real problem."""
    with pytest.raises(NotDisposableError, match="not loopback"):
        _assert_target_is_an_allowed_test_service(
            f"postgresql://u:p@db-host:5432/{CI_TEST_DB_NAME}"
        )


# ── The loopback predicate itself ───────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts_are_recognised(host):
    assert _host_is_loopback(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "",
        "10.0.0.5",
        "8.8.8.8",
        "192.168.1.10",
        "db-host",
        "example.com",
        # Not resolved at all: any hostname other than `localhost` is refused
        # outright, so a name that happens to point at 127.0.0.1 today still
        # is not an allowed target.
        "localhost.example.com",
        "notlocalhost",
    ],
)
def test_non_loopback_hosts_are_rejected(host):
    assert _host_is_loopback(host) is False
