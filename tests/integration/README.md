# Postgres 16 integration gate

**Established by:** the superseded V2 M1 planning cycle and retained as an active
V2 Pilot gate. General testing guidance is in `docs/testing.md`.

```bash
make test-integration
```

## Why this exists

The rest of the suite runs on in-memory SQLite. That is fast and right for
application logic, but it cannot express the production semantics this codebase
already relies on. Before M1, **no automated check ran a migration at all** —
the SQLite suite builds its schema with `Base.metadata.create_all`, so the
Alembic chain was only ever exercised by a human running `make migrate`, and a
model change shipped without a migration stayed invisible until deploy.

## Where the database comes from

Resolved in this order by `conftest.py::pg_url`:

1. **`CV_ADAPTER_TEST_DATABASE_URL`** — a local test service, accepted only if
   it satisfies the allowed-target contract below. CI uses this with a GitHub
   Actions `services: postgres:16` container.
2. **A Docker container** — `postgres:16` on a random loopback port, started and
   removed by the fixture. Runs with `fsync=off` because the data is discarded.
3. **Skip.** So `make test-complete` still works on a machine without Docker.

That skip is a local convenience. CI sets
**`CV_ADAPTER_REQUIRE_INTEGRATION=1`**, which turns an unavailable database into
a hard failure — otherwise this gate could silently contribute nothing to a
green build, which is the exact class of problem M1 exists to remove.

### The allowed-target contract

These fixtures `CREATE DATABASE`, `DROP DATABASE`, and `pg_terminate_backend`.
So an externally supplied URL is not trusted — it is checked against a positive
allowlist **before any connection is opened**:

- **Host must be loopback.** An IP literal that is loopback, or `localhost`
  confirmed to resolve only to loopback addresses. Every other hostname is
  refused without a lookup. **Remote and external URLs are unsupported, with no
  opt-out.**
- **Database must be one this project provisions**: exactly `cvtest` (the CI
  service container) or an `itest_<12 hex>` database created by this gate.

A target passing both is then checked **live** for existing rows in every
business table (`users`, `verdicts`, `cv_publications`, `audit_logs`, …), and
refused if any holds data. `alembic_version` is excluded, since a migrated empty
database legitimately has a row there.

A self-started container skips these checks — we chose its name, port, and
database ourselves seconds earlier.

> Deliberately **not** a "does the name look test-ish" heuristic. An earlier
> revision accepted any database whose name merely contained "test" with an
> empty `users` table, which
> `postgresql://user:pw@db-host:5432/itest_db` satisfied while naming an
> arbitrary reachable remote server. Widening the data check past `users` alone
> matters too: a database can hold Jobs, publications, or audit history with no
> user rows at all and still obviously not be disposable.

If you want a throwaway database of your own, unset the variable and let the
Docker path create one.

## Fixtures

| Fixture | Gives you |
| --- | --- |
| `pg_url` | session-scoped URL of the disposable server |
| `pg_server_version` | major version that actually answered |
| `blank_db` | a fresh, empty database (no schema, no `alembic_version`), dropped after |
| `migrated_db` | `blank_db` after a real `alembic upgrade head` |
| `pg_engine` | an engine on `migrated_db` |
| `alembic(*args, url=...)` | runs the real `alembic` CLI in a subprocess |

`alembic()` shells out deliberately. `alembic/env.py` reads `DATABASE_URL`
through `backend.config` at *import* time, and the test session has already
imported that module with the isolated test settings — so calling the library
in-process would migrate the wrong database. The subprocess also exercises the
same command production deploys run.

## What is covered

### `test_migrations.py`

| Test | Protects |
| --- | --- |
| `test_repository_has_exactly_one_head` | two heads make `upgrade head` ambiguous |
| `test_upgrade_from_base_to_head_succeeds` | the whole chain applies to empty Postgres and stamps the expected revision |
| `test_migrated_schema_has_the_expected_tables` | the chain built real tables, not just a version row |
| `test_no_model_schema_drift` | `alembic check` — a model edit shipped without a migration |
| `test_head_revision_is_reversible` | the head revision's `downgrade()` works, and no drift after a round trip |
| `test_partial_unique_index_survives_migration` | the migration produced a *partial* index, not a plain unique constraint |
| `test_jsonb_columns_are_really_jsonb` | `JSONB().with_variant(JSON(), "sqlite")` lands as `jsonb`, not `json` |
| `test_server_is_postgres_16` | records the engine version the gate actually ran against |

`PRODUCTION_HEAD` in that module pins the revision deployed to the pilot. Update
it when a migration ships, and re-verify it against the deployed task
definition rather than assuming.

### `test_postgres_semantics.py`

Each group protects a behaviour the code depends on today or that an approved
V2 design commits to:

- **JSONB** — type-preserving round trip, `@>` containment, `jsonb_exists`,
  and rejection of a malformed document at write time. `cv_publications.cv_state`
  today; `job_consultant_matches` projections in M8 (ADR-012).
- **Partial unique index** — one active publication per `(consultant, job)`
  (ADR-009), a republish allowed after revoke, and revoked history
  accumulating. A plain unique constraint would break the middle one.
- **Transactions** — rollback discards everything, and a failed statement
  aborts the whole transaction (SQLSTATE `25P02`). SQLite lets a transaction
  continue after an error, so a partial-write bug can hide in the fast gate.
  M9's "Apply creates all of its records or none" depends on this.
- **Concurrent writes** — two connections racing the same unique pair, with the
  interleaving that actually matters when a TA and a Consultant both click
  Publish; and `FOR UPDATE NOWAIT` failing with `55P03` on a held row. The race
  test runs B's insert on a background thread while A is still uncommitted and
  polls `pg_stat_activity` to confirm B is genuinely lock-blocked before
  letting A commit — an earlier version committed A first, which made it two
  serial inserts that proved nothing about locking. Do not "simplify" it back.
- **`FOR UPDATE SKIP LOCKED`** — the mechanism the workflow queue hands work
  out with: never the same row to two claimers, a clean drain with no
  duplicates, and an abandoned claim becoming reclaimable after rollback.

That last group runs against a scratch `m1_queue_probe` table: it proves the
*engine* gives the guarantee, independently of the application table. The real
table now exists — `workflow_runs`, built in V2 Step 1.3 — and
`test_workflow_runner_pg.py` exercises the runner on top of it, including
advisory-lock leadership, automatic standby takeover, graceful shutdown while a
handler is in flight, and recovery after a real process kill. Note that P-006
narrowed ADR-011 for the pilot: the durable row, one advisory-lock leader,
idempotency, and startup recovery are implemented; **per-job leases, heartbeats,
retry/backoff classification, and scheduler occurrences are deliberately
deferred**, so do not read lease semantics into these tests. If the engine-level
guarantees here ever start failing, the runner built on them is unsafe — do not
work around them.

Errors are asserted by SQLSTATE, not by message text or by which SQLAlchemy
wrapper class a driver version happens to pick.

### `test_disposability_guard.py`

The live half of the allowed-target contract above: a freshly migrated database
is accepted, one holding rows in any business table is refused (including the
no-users-but-real-publications case an earlier revision missed), and every
table the guard names still exists after migration — because a table name that
has been renamed away would be silently skipped rather than checked.

The no-I/O half (loopback host, provisioned database name) is in
`tests/test_integration_gate_safety.py`, which needs no Postgres and so runs in
the **fast** gate.
