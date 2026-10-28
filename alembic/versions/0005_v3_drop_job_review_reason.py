"""S20: drop job_reviews.reason — Reject no longer collects a free-text reason."""
from alembic import op
import sqlalchemy as sa

revision = "0005_v3_drop_job_review_reason"
down_revision = "0004_v3_sites_and_cached_scores"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('job_reviews', 'reason')


def downgrade():
    op.add_column('job_reviews', sa.Column('reason', sa.Text(), nullable=True))
