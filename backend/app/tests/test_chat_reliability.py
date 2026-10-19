import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models
from app.services.chat_memory import SessionSealer


@pytest.fixture
def sessions():
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def conversation(db):
    repo = models.Repository(owner='memory-tests', name='scope', full_name=f'memory-tests/{uuid.uuid4().hex}')
    db.add(repo)
    db.flush()
    conv = models.Conversation(repo_id=repo.id, title='memory fixture', status='active')
    db.add(conv)
    db.flush()
    return repo, conv


@pytest.mark.asyncio
async def test_deferred_seal_commits_raw_session_and_outbox_without_calling_model(sessions):
    class NeverModel:
        async def chat_json(self, *args, **kwargs):
            pytest.fail('HTTP completion must not wait for enrichment')
    with sessions() as db:
        repo, conv = conversation(db)
        user = models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='记住开发环境使用 Python')
        assistant = models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='assistant', content='已记录本轮讨论。')
        db.add_all([user, assistant])
        db.flush()
        sealed = await SessionSealer(db, NeverModel()).seal(repo=repo, conversation=conv, user_message=user, assistant_message=assistant, route='chat', tool_calls=[], defer_enrichment=True)
        assert sealed.status == 'queued'
        db.commit()
        task = db.query(models.BackgroundJob).filter_by(kind='memory.enrich').one()
        assert task.resource_id == sealed.id
        assert task.status == 'queued'
        assert sealed.transcript_json['messages'][0]['content'] == user.content
        assert db.query(models.ConversationMemory).count() == 0


@pytest.mark.asyncio
async def test_deferred_raw_session_rolls_back_with_its_job(sessions):
    with sessions() as db:
        repo, conv = conversation(db)
        db.commit()
        messages = [models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role=role, content=role) for role in ('user', 'assistant')]
        db.add_all(messages)
        db.flush()
        await SessionSealer(db).seal(repo=repo, conversation=conv, user_message=messages[0], assistant_message=messages[1], route='chat', tool_calls=[], defer_enrichment=True)
        db.rollback()
        assert db.query(models.ChatSession).count() == 0
        assert db.query(models.BackgroundJob).count() == 0


def test_database_message_search_is_scoped_paginated_and_excludes_compatibility_copies(sessions):
    from app.services.message_search import search_messages
    with sessions() as db:
        repo, conv = conversation(db)
        _, other = conversation(db)
        now = datetime.now(timezone.utc)
        for index in range(7):
            db.add(models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content=f'Python 知识衰减规则 {index}', created_at=now+timedelta(seconds=index)))
        db.add(models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Python 知识衰减 duplicate', meta={'subtype':'preserved_after_compact'}))
        db.add(models.ChatMessage(repo_id=repo.id, conversation_id=other.id, role='user', content='Python 知识衰减 other conversation'))
        db.commit()
        page = search_messages(db, repo.id, conv.id, '知识衰减', limit=2, offset=2)
        assert page['total'] == 7
        assert [item.content for item in page['rows']] == ['Python 知识衰减规则 3', 'Python 知识衰减规则 4']


def test_message_search_rejects_cursor_from_another_scope(sessions):
    from app.services.message_search import search_messages
    with sessions() as db:
        repo, conv = conversation(db)
        other_repo, other = conversation(db)
        msg = models.ChatMessage(repo_id=other_repo.id, conversation_id=other.id, role='user', content='private cursor')
        db.add(msg)
        db.commit()
        with pytest.raises(ValueError, match='cursor'):
            search_messages(db, repo.id, conv.id, '', before_message_id=msg.id)


@pytest.mark.parametrize('with_boundary', [False, True])
def test_active_context_history_is_limited_in_database(sessions, with_boundary):
    from sqlalchemy import event
    from app.services.chat_memory import MessageStore
    from app.services.context_compression import COMPACT_BOUNDARY_SUBTYPE
    with sessions() as db:
        repo, conv = conversation(db)
        start = datetime.now(timezone.utc)-timedelta(days=1)
        for index in range(100):
            db.add(models.ChatMessage(repo_id=repo.id,conversation_id=conv.id,role='user',content=str(index),created_at=start+timedelta(seconds=index)))
        if with_boundary:
            db.add(models.ChatMessage(repo_id=repo.id,conversation_id=conv.id,role='system',content='boundary',meta={'subtype':COMPACT_BOUNDARY_SUBTYPE},created_at=start+timedelta(seconds=50)))
        db.commit()
        statements = []
        def capture(_conn,_cursor,sql,*args):
            if sql.lstrip().upper().startswith('SELECT') and 'FROM chat_messages' in sql:
                statements.append(sql)
        event.listen(db.get_bind(),'before_cursor_execute',capture)
        try:
            rows = MessageStore(db).recent(repo.id,conv.id,limit=5)
        finally:
            event.remove(db.get_bind(),'before_cursor_execute',capture)
        assert [row.content for row in rows] == ['95','96','97','98','99']
        assert statements and all('LIMIT' in sql.upper() for sql in statements)


def test_runtime_projection_reloads_persisted_plan_and_current_agent_status(sessions):
    from app.services.runtime_state import persist_plan, runtime_projection
    with sessions() as db:
        repo, conv = conversation(db)
        plan = {'workflow_id':'workflow_fixture', 'goal':'检查 PR', 'tasks':[{'id':'review','agent_name':'PRReviewAgent','task_type':'pr_review','objective':'审查代码'}]}
        workflow = persist_plan(db, repo.id, conv.id, plan)
        db.commit()
        assert runtime_projection(db, repo.id, conv.id)['plan']['status'] == 'pending_confirmation'
        task = db.query(models.AgentTaskRun).filter_by(workflow_id=workflow.id).one()
        workflow.status, task.status = 'running', 'running'
        db.commit()
        projection = runtime_projection(db, repo.id, conv.id)
        assert projection['plan']['goal'] == '检查 PR'
        assert projection['agents'][0]['status'] == 'running'


def test_runtime_enrichment_projects_latest_attempt_per_session(sessions):
    from app.services.runtime_state import runtime_projection
    with sessions() as db:
        repo, conv = conversation(db)
        session_id = uuid.uuid4()
        at = datetime.now(timezone.utc)
        old = models.BackgroundJob(repo_id=repo.id, kind='memory.enrich', resource_id=session_id,
            payload={'conversation_id':str(conv.id)}, status='failed', created_at=at)
        current = models.BackgroundJob(repo_id=repo.id, kind='memory.enrich', resource_id=session_id,
            payload={'conversation_id':str(conv.id)}, status='completed', created_at=at+timedelta(seconds=1))
        db.add_all([old, current]); db.commit()
        state = runtime_projection(db, repo.id, conv.id)['enrichment']
        assert state['failed'] == 0
        assert [job['job_id'] for job in state['jobs']] == [str(current.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [{'error':'upstream unavailable'}, {'summary':42}, {'facts':'not a list'}])
async def test_invalid_extraction_contract_cannot_seal_as_success(sessions, response):
    from app.services.session_enrichment import enrich_session_job
    class InvalidModel:
        async def chat_json(self, prompt, payload):
            return response
    with sessions() as db:
        repo, conv = conversation(db)
        messages = [models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role=role, content=role) for role in ('user','assistant')]
        db.add_all(messages); db.flush()
        sealed = await SessionSealer(db).seal(repo=repo, conversation=conv, user_message=messages[0], assistant_message=messages[1], route='chat', tool_calls=[], defer_enrichment=True)
        db.commit()
        snapshot = {'repo_id':repo.id, 'resource_id':sealed.id}
    with pytest.raises(RuntimeError, match='model_output_invalid'):
        await enrich_session_job(snapshot, session_factory=sessions, llm=InvalidModel())
    with sessions() as db:
        assert db.get(models.ChatSession, sealed.id).status == 'queued'
        assert db.query(models.ConversationMemory).count() == 0


def test_compaction_rehydration_reloads_live_plan_instead_of_only_old_tools(sessions):
    from app.services.runtime_state import persist_plan
    from app.services.chat_memory import ProgressiveContextManager
    with sessions() as db:
        repo, conv = conversation(db)
        persist_plan(db, repo.id, conv.id, {'workflow_id':'workflow_current','goal':'检查最新计划','tasks':[]})
        message = models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='continue')
        db.add(message)
        db.commit()
        state = ProgressiveContextManager(db)._rehydration_state([message])
        assert state['runtime_state']['plan']['goal'] == '检查最新计划'


@pytest.mark.asyncio
async def test_strict_extraction_surfaces_http_failure_for_durable_retry(monkeypatch):
    import httpx
    from app.services.llm.client import LLMClient
    actual_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(503, request=request))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: actual_client(transport=transport, **kwargs))
    llm = LLMClient()
    llm.api_key = 'test-only'
    with pytest.raises(httpx.HTTPStatusError):
        await llm.chat_json_strict('Extract facts', {'message': 'test'})


@pytest.mark.asyncio
async def test_strict_malformed_model_json_is_retryable(monkeypatch):
    import httpx
    from app.services.llm.client import LLMClient
    from app.services.task_queue import retry_delay
    actual_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200,request=request,json={'choices':[{'message':{'content':'invalid JSON'}}]}))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:actual_client(transport=transport,**kwargs))
    llm = LLMClient(); llm.api_key = 'test-only'
    with pytest.raises(RuntimeError,match='model_output_invalid') as failure:
        await llm.chat_json_strict('Extract facts',{'message':'test'})
    assert retry_delay(failure.value,1) is not None


@pytest.mark.asyncio
async def test_old_memory_extraction_input_is_bounded(sessions):
    from app.services.context_compression import dumps_compact, estimate_tokens
    class CaptureModel:
        payload = None
        async def chat_json(self, prompt, payload):
            self.payload = payload
            return {'summary': 'current summary', 'facts': []}
    with sessions() as db:
        repo, conv = conversation(db)
        db.add(models.ConversationMemory(repo_id=repo.id, conversation_id=conv.id, summary='summary '*5000, facts=['fact '+str(i)+' x '*120 for i in range(300)]))
        db.commit()
        llm = CaptureModel()
        await SessionSealer(db, llm)._build_memory_update('hello', 'answer', 'chat', [], 'digest', repo_id=repo.id, conversation_id=conv.id)
        assert estimate_tokens(dumps_compact(llm.payload['existing_memory'])) <= 1200


def test_context_uses_only_live_managed_memory_not_stale_aggregate(sessions, monkeypatch):
    from app.services.chat_memory import ContextAssembler, EvidenceStore
    from app.services.memory_lifecycle import create_entry, delete_entry
    monkeypatch.setattr(EvidenceStore, 'search', lambda *args, **kwargs: [])
    with sessions() as db:
        repo, conv = conversation(db)
        entry = create_entry(db, repo.id, {'conversation_id':conv.id,'kind':'fact','content':'obsolete-remembered-fact','ttl_days':None})
        db.add(models.ConversationMemory(repo_id=repo.id, conversation_id=conv.id, summary='obsolete-remembered-fact', facts=['obsolete-remembered-fact']))
        db.commit()
        delete_entry(db, repo.id, entry.id, entry.version)
        db.commit()
        assembled = ContextAssembler(db).assemble(repo, conv, 'hello')
        assert 'obsolete-remembered-fact' not in assembled.system_context


def test_linked_rag_note_is_hidden_after_memory_version_changes(sessions):
    from app.services.memory_lifecycle import create_entry, update_entry
    from app.services.rag.retrieval import search_similar_documents
    with sessions() as db:
        repo, conv = conversation(db)
        entry = create_entry(db, repo.id, {'conversation_id':conv.id,'kind':'fact','content':'database uses SQLite','ttl_days':None})
        db.add(models.Document(repo_id=repo.id, source_type='memory_note', source_id=uuid.uuid4(), title='database', content='database uses SQLite', meta={'memory_entry_id':str(entry.id),'memory_entry_version':1}))
        db.commit()
        update_entry(db, repo.id, entry.id, {'expected_version':entry.version,'content':'database uses PostgreSQL'})
        db.commit()
        results = search_similar_documents(db, repo.id, 'database', retrieval_method='fulltext', apply_rerank=False, apply_compression=False)
        assert results == []


def test_boundary_cannot_restore_retired_facts_even_when_created_after_expiry(sessions):
    from app.services.memory_lifecycle import create_entry
    from app.services.memory_recall import boundary_memory_is_current
    with sessions() as db:
        repo, conv = conversation(db)
        entry = create_entry(db, repo.id, {'conversation_id':conv.id,'kind':'fact','content':'expired remembered fact','ttl_days':None})
        entry.expires_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.query(models.MemoryRevision).filter_by(entry_id=entry.id).update({'created_at':datetime.now(timezone.utc)-timedelta(days=3)})
        boundary = models.ChatMessage(repo_id=repo.id,conversation_id=conv.id,role='system',content=entry.content,
            created_at=datetime.now(timezone.utc) - timedelta(days=1))
        db.add(boundary); db.commit()
        assert boundary_memory_is_current(db, repo.id, conv.id, boundary) is False


@pytest.mark.asyncio
async def test_enrichment_is_idempotent_and_preserves_raw_session_on_model_outage(sessions):
    from app.services.session_enrichment import enrich_session_job
    class Extractor:
        calls = 0
        async def chat_json(self, prompt, payload):
            self.calls += 1
            return {'summary':'current state', 'facts':['Python is the development language']}
    class Offline:
        async def chat_json(self, prompt, payload):
            raise ConnectionError('model temporarily offline')
    with sessions() as db:
        repo, conv = conversation(db)
        user = models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Python is the development language')
        assistant = models.ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='assistant', content='confirmed')
        db.add_all([user,assistant]); db.flush()
        session = await SessionSealer(db).seal(repo=repo, conversation=conv, user_message=user, assistant_message=assistant, route='chat', tool_calls=[], defer_enrichment=True)
        db.commit()
        snapshot = {'repo_id':repo.id,'resource_id':session.id}
    with pytest.raises(ConnectionError):
        await enrich_session_job(snapshot, session_factory=sessions, llm=Offline())
    with sessions() as db:
        raw = db.get(models.ChatSession,snapshot['resource_id'])
        assert raw.transcript_json['messages'][0]['content'] == user.content
        assert raw.status == 'queued'
    llm = Extractor()
    await enrich_session_job(snapshot, session_factory=sessions, llm=llm)
    await enrich_session_job(snapshot, session_factory=sessions, llm=llm)
    assert llm.calls == 1
    with sessions() as db:
        assert db.query(models.ConversationMemory).one().sessions_incorporated == 1


def test_conversation_removal_retires_managed_memory_and_pending_enrichment(sessions):
    from app.services.conversations import delete_conversation
    from app.services.memory_lifecycle import create_entry
    from app.services.task_queue import enqueue_job
    with sessions() as db:
        repo, conv = conversation(db)
        entry = create_entry(db,repo.id,{'conversation_id':conv.id,'kind':'fact','content':'retire with conversation'})
        job = enqueue_job(db,'memory.enrich',repo.id,resource_id=uuid.uuid4(),payload={'conversation_id':str(conv.id)})
        db.commit()
        delete_conversation(db,repo.id,conv.id)
        db.commit()
        assert db.get(models.MemoryEntry,entry.id).status == 'deleted'
        assert db.get(models.BackgroundJob,job.id).status == 'cancelled'
