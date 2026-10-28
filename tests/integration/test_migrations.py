"""Alembic against real Postgres 16 (V2 M1: "alembic upgrade plus drift checks").

Before M1 nothing ran a migration in CI. The SQLite unit suite builds its schema
with `Base.metadata.create_all`, which means the migrations were never executed
by any automated check and a model change with no migration was invisible until
deploy.

Every database here is created and dropped by the fixtures.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from .conftest import alembic

# Pre-first-deployment V2 baseline, `0001_v2_pilot_baseline`, was the only
# editable migration until the first V2 cloud deployment (P-001/P-004/P-005).
# `0003_v3_drop_verdicts` chains onto it — dropping the `verdicts` table with
# no writer left, `docs/plans/v3/decisions.md` V3-P-008 decision 5 (V3 packet
# S2). Its filename number assumes S1's own revision would have claimed
# `0002` first; see that file's own docstring for why the number is not
# contiguous. `0004_v3_sites_and_cached_scores` (V3 packet S1) chains onto
# `0003` in turn: Jobs gain `site_id`, `job_consultants` becomes
# `job_decision_pairs`, and `job_consultant_matches` is added.
# `0005_v3_drop_job_review_reason` (V3 packet S20) drops `job_reviews.reason`.
PREVIOUS_HEAD = "0004_v3_sites_and_cached_scores"
PRODUCTION_HEAD = "0005_v3_drop_job_review_reason"
BASELINE_REVISION = "0001_v2_pilot_baseline"


def test_repository_has_exactly_one_head(pg_url):
    """Two heads mean a merge was missed and `upgrade head` becomes ambiguous."""
    result = alembic("heads", url=pg_url)
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected one head, got:\n{result.stdout}"
    assert heads[0].startswith(PRODUCTION_HEAD)


def test_upgrade_from_base_to_head_succeeds(blank_db):
    """The whole chain applies to an empty Postgres database."""
    result = alembic("upgrade", "head", url=blank_db)
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    engine = create_engine(blank_db)
    try:
        with engine.connect() as conn:
            stamped = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert stamped == PRODUCTION_HEAD
    finally:
        engine.dispose()


def test_migrated_schema_has_the_expected_tables(pg_engine):
    """A smoke check that the chain built real tables, not just a version row."""
    tables = set(inspect(pg_engine).get_table_names())
    expected = {
        "alembic_version",
        "sites",
        "users",
        "user_roles",
        "ta_consultants",
        "user_sessions",
        "invites",
        "jobs",
        "job_decision_pairs",
        "job_consultant_matches",
        "job_reviews",
        "applications",
        "notifications",
        "audit_logs",
        "cv_publications",
        "workflow_runs",
    }
    missing = expected - tables
    assert not missing, f"missing after upgrade head: {sorted(missing)}"
    assert "verdicts" not in tables, (
        "the verdicts table should be dropped at head (V3 packet S2)"
    )


def test_no_model_schema_drift(migrated_db):
    """`alembic check` compares Base.metadata against the migrated database.

    This is the gate that catches a model edit shipped without a migration —
    the failure mode that reaches production as a runtime error on first query.
    """
    result = alembic("check", url=migrated_db)
    assert result.returncode == 0, (
        "models have drifted from the migrations; generate a revision.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_head_revision_is_reversible(migrated_db):
    """Downgrade one step and re-upgrade.

    Production rollback deliberately does *not* run destructive downgrades
    (implementation plan §4.3) — application code rolls forward or back over
    additive schema. This proves the head revision's `downgrade()` is
    nonetheless implemented and consistent, which is what makes a local
    recovery or a pre-deploy dry run possible.

    The chain is now four revisions (`0001_v2_pilot_baseline` ->
    `0003_v3_drop_verdicts` -> `0004_v3_sites_and_cached_scores` ->
    `0005_v3_drop_job_review_reason`), so "one step down" from head lands on
    `0004`, not on the baseline: `0005`'s `downgrade()` only re-adds
    `job_reviews.reason`, so every table stays in place throughout.
    """
    down = alembic("downgrade", "-1", url=migrated_db)
    assert down.returncode == 0, f"{down.stdout}\n{down.stderr}"

    engine = create_engine(migrated_db)
    try:
        with engine.connect() as conn:
            stamped = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
            remaining = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            }
    finally:
        engine.dispose()

    assert stamped == [PREVIOUS_HEAD]
    assert PRODUCTION_HEAD not in stamped
    # 0005's downgrade() touches one column; 0004 and 0003 never ran their
    # downgrades, so the Site/decision-pair tables stay and verdicts stays
    # absent.
    assert "verdicts" not in remaining
    assert "users" in remaining
    assert "user_roles" in remaining
    assert "sites" in remaining
    assert "jobs" in remaining
    assert "job_decision_pairs" in remaining
    assert "job_consultants" not in remaining
    assert "job_consultant_matches" in remaining

    up = alembic("upgrade", "head", url=migrated_db)
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"

    # Back at head, the S1 tables are restored and verdicts stays dropped.
    engine = create_engine(migrated_db)
    try:
        with engine.connect() as conn:
            at_head = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            }
    finally:
        engine.dispose()
    assert "verdicts" not in at_head
    assert "job_decision_pairs" in at_head
    assert "job_consultant_matches" in at_head
    assert "job_consultants" not in at_head

    # And no drift after the round trip.
    assert alembic("check", url=migrated_db).returncode == 0


def test_partial_unique_index_survives_migration(pg_engine):
    """The one-active-publication-per-Application rule (S5-P1-R1) is enforced
    by a *partial* unique index. `create_all` in the SQLite suite builds it
    too, but only the migration path is what production actually ran — so
    assert the migration produced a partial index, not a plain unique
    constraint."""
    with pg_engine.connect() as conn:
        definition = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'cv_publications' "
                "AND indexname = 'uq_cv_publications_active_application'"
            )
        ).scalar_one()
    assert "UNIQUE" in definition.upper()
    assert "WHERE" in definition.upper(), (
        f"index is not partial, so a revoked row would still block a "
        f"republish: {definition}"
    )


def test_jsonb_columns_are_really_jsonb(pg_engine):
    """`JSONB().with_variant(JSON(), "sqlite")` must land as jsonb on Postgres.

    If it degraded to `json` (or `text`) the containment/index behaviour the
    V2 match projections rely on would silently disappear.
    """
    with pg_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name, udt_name "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND udt_name IN ('json', 'jsonb')"
            )
        ).all()
    by_column = {(t, c): udt for t, c, udt in rows}
    assert by_column, "no JSON-ish columns found at all — schema is wrong"
    assert by_column.get(("cv_publications", "cv_state")) == "jsonb"
    plain_json = [key for key, udt in by_column.items() if udt == "json"]
    assert not plain_json, f"these should be jsonb, not json: {plain_json}"


def test_server_is_postgres_16(pg_server_version):
    """The gate exists to test the deployed engine's semantics; record which
    major version actually answered."""
    assert pg_server_version == 16, (
        f"integration gate ran against Postgres {pg_server_version}, not 16"
    )
