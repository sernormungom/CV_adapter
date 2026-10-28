"""clean editable V2 Pilot baseline

Revision ID: 0001_v2_pilot_baseline
Revises:
Create Date: 2026-08-27

The Pilot has not had a V2 deployment.  Per P-001 and the V2 delivery plan,
this is intentionally the one editable baseline rather than a migration from
the disposable V1 schema.  It creates no users or other business data; the
controlled `scripts.seed_pilot` bootstrap owns the approved fabricated cast.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_v2_pilot_baseline"
down_revision = None
branch_labels = None
depends_on = None

_json = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _pk() -> sa.Column:
    """The UUIDPrimaryKey mixin's column. Every table has one."""
    return sa.Column("id", sa.Uuid(), nullable=False)


def _timestamps() -> list[sa.Column]:
    """The Timestamps mixin's columns.

    Only for tables whose model actually mixes in `Timestamps`. The pure
    association tables (`user_roles`, `ta_consultants`) do not, so adding these
    there would drift from the models — which is exactly what `alembic check`
    caught the first time this helper also emitted `id`.
    """
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        _pk(), *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sites_code"), "sites", ["code"], unique=True)

    op.create_table(
        "users",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        # Login throttling (STAB-2): consecutive failed logins, and the end of
        # an active lockout.
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("actively_looking", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        _pk(), *_timestamps(),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
    op.create_index(op.f("ix_users_site_id"), "users", ["site_id"], unique=False)

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        _pk(),  # no Timestamps mixin on UserRoleAssignment
        sa.CheckConstraint("role IN ('admin', 'ta', 'consultant')", name="ck_user_roles_role"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "role", name="uq_user_role"),
    )
    op.create_index(op.f("ix_user_roles_user_id"), "user_roles", ["user_id"], unique=False)

    op.create_table(
        "ta_consultants",
        sa.Column("ta_id", sa.Uuid(), nullable=False),
        sa.Column("consultant_id", sa.Uuid(), nullable=False),
        _pk(),  # no Timestamps mixin on TAConsultant
        sa.ForeignKeyConstraint(["ta_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consultant_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ta_id", "consultant_id", name="uq_ta_consultant"),
    )
    op.create_index(op.f("ix_ta_consultants_ta_id"), "ta_consultants", ["ta_id"], unique=False)
    op.create_index(op.f("ix_ta_consultants_consultant_id"), "ta_consultants", ["consultant_id"], unique=False)

    op.create_table(
        "user_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _pk(), *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_sessions_token_hash"), "user_sessions", ["token_hash"], unique=True)
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False)

    op.create_table(
        "invites",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        _pk(), *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invites_token_hash"), "invites", ["token_hash"], unique=True)
    op.create_index(op.f("ix_invites_user_id"), "invites", ["user_id"], unique=False)

    # The relational Job domain (Slice 1). Postgres is the only authoritative
    # Job store; the V1 JSON job files are gone. See backend/db/models/job.py.
    op.create_table(
        "jobs",
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("company", sa.String(length=255), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("close_date", sa.Date(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        _pk(), *_timestamps(),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_jobs_status"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique: the exact-duplicate contract is enforced by the database, not by a
    # prior SELECT — two concurrent identical submissions both pass a
    # check-then-insert.
    op.create_index(
        op.f("ix_jobs_content_hash"), "jobs", ["content_hash"], unique=True
    )
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)

    op.create_table(
        "job_consultants",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("consultant_id", sa.Uuid(), nullable=False),
        _pk(),  # no Timestamps mixin on JobConsultant
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consultant_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "consultant_id", name="uq_job_consultant"),
    )
    op.create_index(op.f("ix_job_consultants_job_id"), "job_consultants", ["job_id"], unique=False)
    op.create_index(op.f("ix_job_consultants_consultant_id"), "job_consultants", ["consultant_id"], unique=False)

    op.create_table(
        "job_reviews",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("consultant_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        _pk(), *_timestamps(),
        # There is no Maybe (P-008), and Apply is Slice 2. A schema that cannot
        # spell the removed value is a stronger guarantee than a route check.
        sa.CheckConstraint("decision IN ('reject')", name="ck_job_reviews_decision"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consultant_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "consultant_id", name="uq_job_review_pair"),
    )
    op.create_index(op.f("ix_job_reviews_job_id"), "job_reviews", ["job_id"], unique=False)
    op.create_index(op.f("ix_job_reviews_consultant_id"), "job_reviews", ["consultant_id"], unique=False)

    # Applications (Slice 2). Apply is a first-class row rather than a field
    # inside a verdict blob; see backend/db/models/application.py.
    op.create_table(
        "applications",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("consultant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_by_id", sa.Uuid(), nullable=True),
        _pk(), *_timestamps(),
        sa.CheckConstraint(
            "status IN ('applied', 'submitted', 'closed', 'withdrawn')",
            name="ck_applications_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consultant_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["status_changed_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_applications_job_id"), "applications", ["job_id"], unique=False)
    op.create_index(op.f("ix_applications_consultant_id"), "applications", ["consultant_id"], unique=False)
    op.create_index(op.f("ix_applications_status"), "applications", ["status"], unique=False)
    # One *live* Application per (Job, Consultant), enforced by the database:
    # a check-then-insert lets two concurrent Apply requests both through.
    # Terminal rows are excluded so a pair whose Application ended can be
    # applied to again without deleting the historical row.
    op.create_index(
        "uq_application_live_pair", "applications",
        ["job_id", "consultant_id"], unique=True,
        postgresql_where=sa.text("status IN ('applied', 'submitted')"),
    )

    op.create_table(
        "verdicts",
        sa.Column("consultant_username", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=100), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=True),
        sa.Column("cycle_id", sa.String(length=100), nullable=True),
        sa.Column("application_status", sa.String(length=50), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data", _json, nullable=False),
        _pk(), *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("consultant_username", "job_id", name="uq_verdict_consultant_job"),
    )
    op.create_index(op.f("ix_verdicts_consultant_username"), "verdicts", ["consultant_username"], unique=False)
    op.create_index(op.f("ix_verdicts_job_id"), "verdicts", ["job_id"], unique=False)

    op.create_table(
        "notifications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("consultant_username", sa.String(length=100), nullable=True),
        sa.Column("job_id", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("payload", _json, nullable=False),
        sa.Column("channels_sent", _json, nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        _pk(), *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)
    op.create_index("ix_notifications_user_dedupe", "notifications", ["user_id", "dedupe_key"], unique=False)

    op.create_table(
        "audit_logs",
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("target_username_hash", sa.String(length=16), nullable=False),
        _pk(), *_timestamps(),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_actor_id"), "audit_logs", ["actor_id"], unique=False)
    op.create_index(op.f("ix_audit_logs_target_username_hash"), "audit_logs", ["target_username_hash"], unique=False)

    op.create_table(
        "cv_publications",
        # SHA-256 hash only (ADR-009/D-013/P-010) — the raw token is never
        # persisted, only minted per publish/republish and returned once.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("consultant_username", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("cv_state", _json, nullable=False),
        sa.Column("photo_path", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        _pk(), *_timestamps(),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cv_publications_token_hash"), "cv_publications", ["token_hash"], unique=True)
    op.create_index(op.f("ix_cv_publications_application_id"), "cv_publications", ["application_id"], unique=False)
    op.create_index(
        "uq_cv_publications_active_application", "cv_publications",
        ["application_id"], unique=True,
        postgresql_where=sa.text("NOT revoked"),
    )

    # Durable workflow state (P-006). References only — never CV/email/prompt
    # content; see backend/db/models/workflow.py.
    op.create_table(
        "workflow_runs",
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=True),
        sa.Column("subject_id", sa.String(length=200), nullable=True),
        sa.Column("input_ref", sa.String(length=500), nullable=True),
        sa.Column("result_ref", sa.String(length=500), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _pk(), *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_workflow_runs_status",
        ),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_workflow_runs_progress",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique: the idempotency contract is enforced by the database, not by a
    # prior SELECT — two concurrent enqueues both pass a check-then-insert.
    op.create_index(
        op.f("ix_workflow_runs_idempotency_key"),
        "workflow_runs", ["idempotency_key"], unique=True,
    )
    op.create_index(
        op.f("ix_workflow_runs_status"), "workflow_runs", ["status"], unique=False
    )

    # The Application-owned tailored CV (Slice 3, P-009). Unlike `workflow_runs`
    # this one *is* a domain record and legitimately holds CV content; the
    # references-only rule applies to the generic queue row.
    #
    # Deliberately **no** column pointing at a workflow run: the run is found by
    # the idempotency key built from `(id, input_hash, generation_attempt)`, so
    # there is no second source of truth and no window in which a stored
    # reference is not yet written. See backend/db/models/tailored_cv.py.
    op.create_table(
        "tailored_cvs",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("cv_state", _json, nullable=True),
        sa.Column("input_snapshot", _json, nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        _pk(), *_timestamps(),
        sa.CheckConstraint(
            "status IN ('generating', 'ready', 'failed')",
            name="ck_tailored_cvs_status",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_tailored_cvs_revision"),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique: one tailored CV per Application, decided by the database rather
    # than by a prior SELECT — two concurrent Generate requests both pass a
    # check-then-insert.
    op.create_index(
        op.f("ix_tailored_cvs_application_id"),
        "tailored_cvs", ["application_id"], unique=True,
    )
    op.create_index(
        op.f("ix_tailored_cvs_status"), "tailored_cvs", ["status"], unique=False
    )

    # V2 Slice 4: durable async CV import. One row per consultant, unique on
    # `consultant_username` like the base CV is a singleton — see
    # backend/db/models/cv_import.py for why there is no stored workflow-run
    # reference either, mirroring tailored_cvs.
    op.create_table(
        "cv_imports",
        sa.Column("consultant_username", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("source_ref", sa.String(length=500), nullable=True),
        sa.Column("result_roles_count", sa.Integer(), nullable=True),
        sa.Column("result_skills_count", sa.Integer(), nullable=True),
        sa.Column("result_open_gaps", sa.Integer(), nullable=True),
        sa.Column("result_blocking_gaps", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        _pk(), *_timestamps(),
        sa.CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed')",
            name="ck_cv_imports_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_cv_imports_attempt"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_cv_imports_consultant_username"),
        "cv_imports", ["consultant_username"], unique=True,
    )
    op.create_index(
        op.f("ix_cv_imports_status"), "cv_imports", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_table("cv_imports")
    op.drop_table("tailored_cvs")
    op.drop_table("workflow_runs")
    op.drop_table("cv_publications")
    op.drop_table("audit_logs")
    op.drop_table("notifications")
    op.drop_table("verdicts")
    op.drop_table("applications")
    op.drop_table("job_reviews")
    op.drop_table("job_consultants")
    op.drop_table("jobs")
    op.drop_table("invites")
    op.drop_table("user_sessions")
    op.drop_table("ta_consultants")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("sites")
