"""S1: Site ownership, decision pairs, and lazy advisory scores."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_v3_sites_and_cached_scores"
down_revision = "0003_v3_drop_verdicts"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if conn.scalar(sa.text('SELECT count(*) FROM jobs')):
        sites = conn.execute(sa.text('SELECT id FROM sites WHERE is_active = true')).scalars().all()
        if len(sites) != 1:
            raise RuntimeError('Existing Jobs need one unambiguous configured pilot Site. Use an empty disposable database or resolve Site configuration before upgrading.')
    else:
        sites = []
    op.add_column('jobs', sa.Column('site_id', sa.Uuid(), nullable=True))
    if sites:
        conn.execute(sa.text('UPDATE jobs SET site_id = :site'), {'site': sites[0]})
    op.alter_column('jobs', 'site_id', nullable=False)
    op.create_foreign_key('fk_jobs_site_id_sites', 'jobs', 'sites', ['site_id'], ['id'], ondelete='RESTRICT')
    op.create_index('ix_jobs_site_id', 'jobs', ['site_id'])
    op.drop_index('ix_jobs_content_hash', table_name='jobs')
    op.create_index('uq_jobs_open_site_content', 'jobs', ['site_id', 'content_hash'], unique=True, postgresql_where=sa.text("status = 'open'"))
    op.rename_table('job_consultants', 'job_decision_pairs')
    op.execute('ALTER TABLE job_decision_pairs RENAME CONSTRAINT uq_job_consultant TO uq_job_decision_pair')
    for column in ('job_id', 'consultant_id'):
        op.execute(f'ALTER INDEX ix_job_consultants_{column} RENAME TO ix_job_decision_pairs_{column}')
    op.create_table('job_consultant_matches',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('job_id', sa.Uuid(), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('consultant_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('match_score', sa.Float(), nullable=True),
        sa.Column('result', postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint('job_id', 'consultant_id', name='uq_job_consultant_match'))
    op.create_index('ix_job_consultant_matches_job_id', 'job_consultant_matches', ['job_id'])
    op.create_index('ix_job_consultant_matches_consultant_id', 'job_consultant_matches', ['consultant_id'])


def downgrade():
    # V2 cannot represent duplicate content across Sites; refuse rather than
    # deleting Jobs or choosing a winner during a schema downgrade.
    conn = op.get_bind()
    if conn.scalar(sa.text('SELECT count(*) FROM (SELECT content_hash FROM jobs GROUP BY content_hash HAVING count(*) > 1) d')):
        raise RuntimeError('V2 cannot represent these Jobs; use an empty disposable database for downgrade verification.')
    op.drop_table('job_consultant_matches')
    for column in ('job_id', 'consultant_id'):
        op.execute(f'ALTER INDEX ix_job_decision_pairs_{column} RENAME TO ix_job_consultants_{column}')
    op.execute('ALTER TABLE job_decision_pairs RENAME CONSTRAINT uq_job_decision_pair TO uq_job_consultant')
    op.rename_table('job_decision_pairs', 'job_consultants')
    op.drop_index('uq_jobs_open_site_content', table_name='jobs')
    op.create_index('ix_jobs_content_hash', 'jobs', ['content_hash'], unique=True)
    op.drop_index('ix_jobs_site_id', table_name='jobs')
    op.drop_constraint('fk_jobs_site_id_sites', 'jobs', type_='foreignkey')
    op.drop_column('jobs', 'site_id')
