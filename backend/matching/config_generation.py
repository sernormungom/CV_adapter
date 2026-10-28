"""Human-requested profile-only configuration, through the durable runner.

A user row lock serializes request selection, publication and erasure. A marker
in the atomic config document recognizes a crash after file replacement but
before database commit. Generation itself can repeat; publication cannot.
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from backend.auth.dependencies import get_current_user
from backend.auth.service import normalize_username
from backend.authz import policy
from backend.db.models import User, WorkflowRun
from backend.db.session import get_db
from sqlalchemy.orm import Session
from backend.matching.config_reader import load_config, save_config
from backend.opportunity_pipeline.learning_module.config_bootstrapper import bootstrap_config

WORKFLOW_TYPE = 'matching_config'
router = APIRouter(prefix='/api/matching', tags=['matching'])


def _latest(db, user_id):
    return db.scalar(select(WorkflowRun).where(WorkflowRun.workflow_type == WORKFLOW_TYPE, WorkflowRun.subject_id == str(user_id)).order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()).limit(1))


def _projection(run):
    return {k: getattr(run, k) for k in ('status', 'stage', 'progress', 'error_code', 'error_message')} | {'id': str(run.id)}


class ConfigRequest(BaseModel):
    request_id: uuid.UUID


@router.post('/{username}/config', status_code=202)
def request_config(username: str, payload: ConfigRequest, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from backend.workflows import service
    user = policy.ensure_consultant_access(db, actor, normalize_username(username))
    db.refresh(user, with_for_update=True)
    key = f'{WORKFLOW_TYPE}:{user.id}:{payload.request_id}'
    existing = db.scalar(select(WorkflowRun).where(WorkflowRun.idempotency_key == key))
    if existing is not None:
        db.commit()
        return _projection(existing)
    latest = _latest(db, user.id)
    if latest is not None and latest.status in ('queued', 'running'):
        db.commit()
        return _projection(latest)
    run = service.enqueue(db, workflow_type=WORKFLOW_TYPE, idempotency_key=key, subject_type='consultant', subject_id=str(user.id), input_ref=f'profile:{user.id}')
    return _projection(run)


@router.get('/{username}/config')
def config_status(username: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user = policy.ensure_consultant_access(db, actor, normalize_username(username))
    latest = _latest(db, user.id)
    return _projection(latest) if latest is not None else {'status': 'idle'}


def handle_config(db, run):
    from backend.workflows import service
    user_id = uuid.UUID(run.subject_id)
    user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        raise service.WorkflowError('consultant_deleted', 'Consultant no longer exists')
    username = user.username
    latest = _latest(db, user_id)
    if latest is None or latest.id != run.id:
        db.rollback()
        return f'matching_config:{run.id}:superseded'
    previous = load_config(username)
    if previous and previous.get('metadata', {}).get('_config_run') == str(run.id):
        service.mark_succeeded(db, run, result_ref=f'matching_config:{run.id}')
        return f'matching_config:{run.id}'
    db.commit()
    service.record_stage(db, run, 'generating_config', progress=25)
    cfg = bootstrap_config(username, persist=False)
    # No lock or open transaction during the LLM call. Recheck the source
    # identity and newest intent at the publication boundary.
    user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        raise service.WorkflowError('consultant_deleted', 'Consultant no longer exists')
    latest = _latest(db, user_id)
    if latest is None or latest.id != run.id:
        db.rollback()
        return f'matching_config:{run.id}:superseded'
    from backend.cv_state.service import exclusive_profile_write
    from backend.cv_importer.keys import PROFILE_LOCK_FILENAME
    with exclusive_profile_write(username, PROFILE_LOCK_FILENAME):
        previous = load_config(username)
        if not previous or previous.get('metadata', {}).get('_config_run') != str(run.id):
            cfg['metadata']['_config_run'] = str(run.id)
            save_config(username, cfg)
        service.mark_succeeded(db, run, result_ref=f'matching_config:{run.id}')
    return f'matching_config:{run.id}'
