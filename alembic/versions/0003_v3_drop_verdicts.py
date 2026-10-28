"""V3 S2: drop the verdicts table

Revision ID: 0003_v3_drop_verdicts
Revises: 0001_v2_pilot_baseline
Create Date: 2026-09-10

`docs/plans/v3/decisions.md` V3-P-008 decision 5 orders the removal of the
verdict-fed learning path: verdict writing/storage, verdict aggregation, the
config-update LLM call that read them, the proposed-change diff display, and
this table. Nothing has written a `verdicts` row since V2 Slice 3 retired the
last writer, and this pilot is disposable-data / downtime-tolerant, so this is
a forward schema change with no data migration.

Filename note: this repository's packet proposal named this file assuming S1
(Slice 1's Site-locked authorization work) would land first and claim
`0002_...`. The owner's actual delivery order put S2 first
(`docs/plans/v3/decisions.md` V3-P-008, "Delivery order: S2 first, then S1"),
so `0002` is still free when this file is created. Rather than rename this
file to match the true next-free number — which would fight the write-scope
path fixed in the S2 packet proposal — this revision keeps the `0003` filename
but chains directly onto the actual current head, `0001_v2_pilot_baseline`.
Whichever revision id S1 lands with must set `down_revision =
"0003_v3_drop_verdicts"` (this file's revision id), regardless of what number
prefix that file uses.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_v3_drop_verdicts"
down_revision = "0001_v2_pilot_baseline"
branch_labels = None
depends_on = None

_json = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.drop_index(op.f("ix_verdicts_job_id"), table_name="verdicts")
    op.drop_index(op.f("ix_verdicts_consultant_username"), table_name="verdicts")
    op.drop_table("verdicts")


def downgrade() -> None:
    op.create_table(
        "verdicts",
        sa.Column("consultant_username", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=100), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=True),
        sa.Column("cycle_id", sa.String(length=100), nullable=True),
        sa.Column("application_status", sa.String(length=50), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data", _json, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("consultant_username", "job_id", name="uq_verdict_consultant_job"),
    )
    op.create_index(op.f("ix_verdicts_consultant_username"), "verdicts", ["consultant_username"], unique=False)
    op.create_index(op.f("ix_verdicts_job_id"), "verdicts", ["job_id"], unique=False)
