import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models import ChatSession, Conversation, Repository
from app.db.session import get_db
from app.services.message_search import search_messages
from app.services.job_locks import JobResourceBusy, resource_lock
from app.services.runtime_state import runtime_projection
from app.services.task_queue import enqueue_job, job_payload, latest_job

router = APIRouter()


def scope(db, repo_id, conversation_id):
    if db.get(Repository, repo_id) is None:
        raise HTTPException(404, '未找到项目')
    conversation = db.query(Conversation).filter_by(id=conversation_id, repo_id=repo_id, status='active').one_or_none()
    if conversation is None:
        raise HTTPException(404, '未找到当前项目中的会话')
    return conversation


@router.get('/{repo_id}/memory/messages')
async def messages(repo_id: uuid.UUID, conversation_id: uuid.UUID, q: str = Query(default='', max_length=300), limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    scope(db, repo_id, conversation_id)
    result = search_messages(db, repo_id, conversation_id, q, limit=limit, offset=offset)
    return {'items': [{'id': str(item.id), 'role': item.role, 'content': item.content, 'created_at': item.created_at} for item in result['rows']], 'total': result['total'], 'search_mode': result['search_mode']}


@router.get('/{repo_id}/memory/runtime')
async def runtime(repo_id: uuid.UUID, conversation_id: uuid.UUID, db: Session = Depends(get_db)):
    scope(db, repo_id, conversation_id)
    return runtime_projection(db, repo_id, conversation_id)


@router.post('/{repo_id}/memory/sessions/{session_id}/retry', status_code=202)
async def retry_session(repo_id: uuid.UUID, session_id: uuid.UUID, db: Session = Depends(get_db)):
    try:
        with resource_lock(db.get_bind(), f'repo:{repo_id}'):
            return _retry_session_locked(repo_id, session_id, db)
    except JobResourceBusy as exc:
        raise HTTPException(409, '项目任务正在处理，请稍后刷新状态。') from exc


def _retry_session_locked(repo_id, session_id, db):
    session = db.get(ChatSession, session_id)
    if session is None or session.repo_id != repo_id:
        raise HTTPException(404, '未找到封存会话')
    scope(db, repo_id, session.conversation_id)
    current = latest_job(db, repo_id, kind='memory.enrich', resource_id=session_id)
    if current and (current.status in {'queued', 'running', 'retrying'} or session.status in {'sealed', 'sealed_with_warnings'}):
        return job_payload(current)
    job = enqueue_job(db, 'memory.enrich', repo_id, resource_id=session_id,
                      payload={'session_id': str(session_id), 'conversation_id': str(session.conversation_id)})
    session.status = 'queued'
    session.transcript_json = {**(session.transcript_json or {}), 'sealing': {'phase': 'queued', 'errors': []}}
    db.commit()
    return job_payload(job)
