"""Write-through advisory scores, filled only when the Jobs board reads a pair."""
from sqlalchemy import select
from backend.db.models import Job, User
from backend.db.models.matching import JobConsultantMatch
from backend.jobs.service import lock_job
from backend.matching.service import score_job_for_consultant


def read_score(db, job, consultant):
    # Serialize with close and opt-out, re-reading eligibility before profile I/O.
    current = db.scalar(select(User).where(User.id == consultant.id).with_for_update().execution_options(populate_existing=True))
    lock_job(db, job)
    if current is None or not current.is_active or not current.actively_looking or not job.is_open or current.site_id != job.site_id:
        db.commit()
        return None
    row = db.scalar(select(JobConsultantMatch).where(JobConsultantMatch.job_id == job.id, JobConsultantMatch.consultant_id == current.id))
    if row is not None:
        result = row.result
        db.commit()
        return result
    result = score_job_for_consultant(job, current)
    # Missing inputs are not cached: the explicit configurator can clear them
    # on the next read without refreshing any existing indicator.
    if result['scored']:
        if db.bind.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        stmt = insert(JobConsultantMatch).values(job_id=job.id, consultant_id=current.id, match_score=result['match_score'], result=result)
        db.execute(stmt.on_conflict_do_update(index_elements=['job_id', 'consultant_id'], set_={'match_score': stmt.excluded.match_score, 'result': stmt.excluded.result}))
    db.commit()
    return result
