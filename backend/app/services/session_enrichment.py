"""Durable session enrichment handler and bounded extraction inputs."""

import asyncio

from app.db.models import ChatSession
from app.db.session import SessionLocal
from app.services.context_compression import clip_to_token_budget, dumps_compact, estimate_tokens


def bounded_memory_snapshot(state, max_tokens=1200):
    clean = {}
    for key, value in state.items():
        if isinstance(value, list):
            clean[key] = [clip_to_token_budget(str(item), 80) if not isinstance(item, dict) else {k: clip_to_token_budget(str(v), 60) for k, v in item.items()} for item in value[-16:]]
        else:
            clean[key] = clip_to_token_budget(str(value or ''), 300)
    while estimate_tokens(dumps_compact(clean)) > max_tokens:
        lists = [value for value in clean.values() if isinstance(value, list) and value]
        if not lists:
            break
        max(lists, key=lambda value: estimate_tokens(dumps_compact(value))).pop(0)
    return clean


async def enrich_session_job(job, *, session_factory=SessionLocal, llm=None):
    from app.services.chat_memory import SessionSealer
    with session_factory() as db:
        session = db.get(ChatSession, job['resource_id'])
        if session is None or session.repo_id != job['repo_id']:
            raise ValueError('封存会话已删除或不属于当前项目')
        await SessionSealer(db, llm).enrich_session(session, strict=True)
        db.commit()
        return {'session_id': str(session.id), 'status': session.status}


def enrich_session(job):
    return asyncio.run(enrich_session_job(job))
