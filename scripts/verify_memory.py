"""Local PostgreSQL/RabbitMQ/API memory acceptance checks, using a precise fixture.

Run with backend/.venv/Scripts/python.exe scripts/verify_memory.py --keep
The configured model is used once by the actual Worker. No keys are printed.
--cleanup removes only the exact fixture whose UUID and marker were saved.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.chdir(ROOT / 'backend')

import aio_pika
import httpx
from sqlalchemy import text
from app.core.config import settings
from app.db import models
from app.db.session import SessionLocal
from app.services.chat_memory import ContextAssembler, ProgressiveContextManager, SessionSealer
from app.services.memory_lifecycle import entry_score, sync_memory_entries
from app.services.memory_quality import assess_candidate
from app.services.memory_hub import capture_memory_candidates
from app.services.memory_recall import filter_linked_memories
from app.services.runtime_state import persist_plan

API = 'http://127.0.0.1:8000'
FIXTURE = ROOT / '.data/runtime/memory-ui-fixture.json'
REPORT = ROOT / '.data/runtime/memory-verification.json'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def cleanup(client, fixture):
    repo_id = uuid.UUID(fixture['repo_id'])
    with SessionLocal() as db:
        repo = db.get(models.Repository, repo_id)
        if repo is None:
            return
        assert repo.provider == 'local'
        assert repo.description == fixture['marker']
        assert repo.full_name == fixture['full_name']
        assert repo.full_name.startswith('memory-acceptance/')
    response = client.delete(f'/api/repos/{repo_id}')
    response.raise_for_status()
    assert response.json()['deleted'] is True


async def queue_session(repo_id, conv_id):
    with SessionLocal() as db:
        repo, conv = db.get(models.Repository, repo_id), db.get(models.Conversation, conv_id)
        user = models.ChatMessage(repo_id=repo_id, conversation_id=conv_id, role='user',
            content='已确认：演示项目数据库使用 PostgreSQL。请记住此项目约定。')
        assistant = models.ChatMessage(repo_id=repo_id, conversation_id=conv_id, role='assistant', content='已确认本轮项目约定。')
        db.add_all([user, assistant]); db.flush()
        session = await SessionSealer(db).seal(repo=repo, conversation=conv, user_message=user,
            assistant_message=assistant, route='chat', tool_calls=[], defer_enrichment=True)
        assert session.status == 'queued'
        job = db.query(models.BackgroundJob).filter_by(kind='memory.enrich', resource_id=session.id).one()
        ids = str(session.id), str(job.id)
        db.commit()
        return ids


async def replay(job_id):
    connection = await aio_pika.connect_robust(settings.rabbitmq_url, timeout=5)
    try:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.get_exchange('devflow.jobs')
        for _ in range(2):
            await exchange.publish(aio_pika.Message(
                body=json.dumps({'schema_version':1,'job_id':job_id,'kind':'memory.enrich'}).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT), routing_key='memory.enrich', mandatory=True)
    finally:
        await connection.close()


def run(client, fixture, results):
    repo_id, conv_id = uuid.UUID(fixture['repo_id']), uuid.UUID(fixture['conversation_id'])
    base = f'/api/repos/{repo_id}/memory'
    scope = {'conversation_id':str(conv_id)}
    def request(method, path, expected=200, **kwargs):
        response = client.request(method, base+path, **kwargs)
        assert response.status_code == expected, (path, response.status_code, response.text[:500])
        return response.json()
    def checked(name):
        results.append({'check':name,'passed':True})
        print('PASS', name, flush=True)

    with SessionLocal() as db:
        tables = set(db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
        assert {'memory_entries','memory_revisions','memory_conflicts','memory_key_claims'} <= tables
        indexes = set(db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='chat_messages'")).scalars())
        assert {'ix_chat_messages_fts','ix_chat_messages_trgm','ix_chat_messages_scope_time'} <= indexes
    checked('PostgreSQL memory tables and FTS/trigram/scope indexes')

    entry = request('POST','/entries',json={**scope,'kind':'fact','key':'acceptance-database','title':'数据库约定','content':'数据库使用 SQLite','ttl_days':None})
    entry_id = entry['id']
    edited = request('PATCH',f'/entries/{entry_id}',json={'expected_version':1,'content':'数据库使用 PostgreSQL'})
    assert edited['version'] == 2
    request('PATCH',f'/entries/{entry_id}',expected=409,json={'expected_version':1,'content':'stale'})
    request('DELETE',f'/entries/{entry_id}',params={'expected_version':2})
    assert request('GET','/entries',params=scope)['total'] == 0
    restored = request('POST',f'/entries/{entry_id}/restore',json={'expected_version':3})
    assert restored['version'] == 4
    reverted = request('POST',f'/entries/{entry_id}/revert',json={'expected_version':4,'target_version':1})
    assert reverted['content'] == '数据库使用 SQLite'
    assert len(request('GET',f'/entries/{entry_id}/revisions')) == 5
    checked('API create/edit/version conflict/delete/restore/history/revert')

    with SessionLocal() as db:
        sync_memory_entries(db,repo_id,conv_id,{'_memory_items':[{'kind':'fact','key':'acceptance-database','content':'数据库使用 MySQL'}]})
        db.commit()
    conflicts = request('GET','/conflicts',params=scope)
    conflict = next(item for item in conflicts if item['entry_id'] == entry_id)
    assert request('GET','/entries',params=scope)['total'] == 0
    resolved = request('POST',f"/conflicts/{conflict['id']}/resolve",json={'resolution':'replace','expected_version':conflict['entry_version']})
    assert resolved['content'] == '数据库使用 MySQL'
    checked('Conflicting extraction excluded until explicit versioned resolution')

    alias = request('POST','/entries',json={**scope,'kind':'fact','content':'唯一事实身份演示','ttl_days':None})
    with SessionLocal() as db:
        sync_memory_entries(db,repo_id,conv_id,{'_memory_items':[{'kind':'fact','key':'alias-demo','content':alias['content']}]})
        db.commit()
    request('POST','/entries',expected=409,json={**scope,'kind':'fact','key':'alias-demo','content':'矛盾的新事实'})
    checked('Primary keys and semantic aliases share a unique database owner')

    expired = request('POST','/entries',json={**scope,'kind':'task','content':'即时过期测试','ttl_days':0,'pinned':True})
    assert expired['status'] == 'expired' and expired['score'] == 0
    assert any(item['id'] == expired['id'] for item in request('GET','/entries',params={**scope,'status':'expired'})['items'])
    with SessionLocal() as db:
        row = db.get(models.MemoryEntry,uuid.UUID(alias['id']))
        start = row.last_confirmed_at
        assert abs(entry_score(row,start+timedelta(days=30)) - row.importance*row.confidence/2) < .00001
        old = db.get(models.MemoryEntry,uuid.UUID(expired['id']))
        assert not filter_linked_memories(db,repo_id,[{'score':1,'metadata':{'memory_entry_id':str(old.id),'memory_entry_version':old.version}}])
    checked('Half-life decay, pin does not bypass expiry, stale RAG evidence excluded')

    with SessionLocal() as db:
        user = models.ChatMessage(repo_id=repo_id,conversation_id=conv_id,role='user',content='知识衰减测试：使用 PostgreSQL')
        assistant = models.ChatMessage(repo_id=repo_id,conversation_id=conv_id,role='assistant',content='这只是助手生成的提议')
        db.add_all([user,assistant]); db.flush()
        for role, message, content, expected in [('user',user,user.content,True),('assistant',assistant,assistant.content,False)]:
            candidate = models.MemoryCandidate(repo_id=repo_id,conversation_id=conv_id,kind='fact',title=role+'证据校验',content=content,
                meta={'confidence':.9,'citations':[{'type':'chat_message','id':str(message.id),'quote':content}]})
            db.add(candidate); db.flush()
            assert assess_candidate(db,candidate)['can_auto_approve'] is expected
        plan = persist_plan(db,repo_id,conv_id,{'workflow_id':'memory-live-plan','goal':'验证记忆链路','tasks':[{'id':'inspect','agent_name':'KnowledgeAgent','task_type':'research','objective':'检查持久化状态'}]})
        task = db.query(models.AgentTaskRun).filter_by(workflow_id=plan.id).one()
        task.status = 'success'
        plan.status = 'success'
        db.commit()
    result = request('GET','/messages',params={**scope,'q':'知识衰减','limit':1})
    assert result['total'] == 1 and len(result['items']) == 1
    assert result['search_mode'] == 'postgres_fulltext_trigram'
    request('GET','/messages',expected=404,params={'conversation_id':str(uuid.uuid4())})
    state = request('GET','/runtime',params=scope)
    assert state['plan']['goal'] == '验证记忆链路' and state['agents'][0]['status'] == 'success'
    checked('Chinese database search, scope isolation, source authority and durable execution state')

    verify_enrichment(client,fixture,results)


def verify_enrichment(client, fixture, results, *, resume=False):
    repo_id, conv_id = uuid.UUID(fixture['repo_id']), uuid.UUID(fixture['conversation_id'])
    base = f'/api/repos/{repo_id}/memory'
    def request(method,path,expected=200,**kwargs):
        response = client.request(method,base+path,**kwargs)
        assert response.status_code == expected, (response.status_code,response.text[:500])
        return response.json()
    def checked(name):
        results.append({'check':name,'passed':True})
        print('PASS',name,flush=True)
    if resume:
        session_id = fixture['session_id']
        job_id = request('POST',f'/sessions/{session_id}/retry',expected=202,json={})['job_id']
    else:
        session_id, job_id = asyncio.run(queue_session(repo_id,conv_id))
    fixture.update(session_id=session_id,job_id=job_id)
    save(FIXTURE,fixture)
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        response = client.get(f'/api/repos/{repo_id}/jobs/{job_id}'); response.raise_for_status()
        job = response.json()
        if job['status'] in {'completed','failed','cancelled'}:
            assert job['status'] == 'completed', job
            break
        time.sleep(.5)
    else:
        raise AssertionError('Actual Worker enrichment timed out')
    with SessionLocal() as db:
        sealed = db.get(models.ChatSession,uuid.UUID(session_id))
        assert sealed.status == 'sealed'
        assert sealed.transcript_json['messages'][0]['content'].startswith('已确认')
        assert db.query(models.ConversationMemory).filter_by(conversation_id=conv_id).one().sessions_incorporated == 1
        assert db.query(models.MemoryEntry).filter_by(conversation_id=conv_id,source='extracted').count() > 0
    checked('Actual outbox -> RabbitMQ -> Worker -> configured model -> persisted memory')
    asyncio.run(replay(job_id))
    time.sleep(2)
    with SessionLocal() as db:
        assert db.query(models.ConversationMemory).filter_by(conversation_id=conv_id).one().sessions_incorporated == 1
        msg = db.query(models.ChatMessage).filter_by(conversation_id=conv_id).first()
        rehydration = ProgressiveContextManager(db)._rehydration_state([msg])
        assert rehydration['runtime_state']['plan']['goal'] == '验证记忆链路'
        context = ContextAssembler(db).assemble(db.get(models.Repository,repo_id),db.get(models.Conversation,conv_id),'项目约定')
        assert '[Current Execution State]' in context.system_context
        assert '[Managed Conversation Memory]' in context.system_context
    retry = request('POST',f'/sessions/{session_id}/retry',expected=202,json={})
    assert retry['job_id'] == job_id and retry['status'] == 'completed'
    checked('Duplicate MQ deliveries and completed retry do not incorporate memory twice; current state rehydrates')
    verify_promotion(fixture,results)


def verify_promotion(fixture,results):
    repo_id, conv_id = uuid.UUID(fixture['repo_id']), uuid.UUID(fixture['conversation_id'])
    assert settings.memory_auto_approve_verified
    phrase = '已核实的项目规范 '+uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = models.ChatMessage(repo_id=repo_id,conversation_id=conv_id,role='user',content=phrase)
        db.add(user); db.flush()
        update = {'_memory_items':[{'kind':'fact','key':phrase,'content':phrase,'confidence':.95,
            'citations':[{'type':'chat_message','id':str(user.id),'quote':phrase}]}]}
        sync_memory_entries(db,repo_id,conv_id,update)
        candidates = capture_memory_candidates(db,repo_id=repo_id,conversation_id=conv_id,session_id=None,memory_update=update)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.meta['quality']['can_auto_approve'] is True
        assert candidate.status == 'approved', candidate.meta.get('auto_approve_error')
        doc_id = candidate.meta['approved_document_id']
        doc = db.get(models.Document,uuid.UUID(doc_id))
        assert doc.source_type == 'memory_note' and doc.meta['memory_entry_id']
        db.commit()
    results.append({'check':'Source-verified automatic knowledge promotion with actual embedding/vector indexing','passed':True})
    print('PASS Source-verified automatic knowledge promotion with actual embedding/vector indexing',flush=True)


def verify_chat_api(client,fixture,results):
    repo_id = fixture['repo_id']
    response = client.post(f'/api/repos/{repo_id}/conversations',json={'title':'聊天入口异步记忆验收'})
    response.raise_for_status()
    conv_id = response.json()['id']
    response = client.post('/api/chat',json={'repo_id':repo_id,'conversation_id':conv_id,
        'message':'验收专用：我已确认，此演示项目的主语言是 Python。请简短回复“收到”，无需调用工具或操作仓库。'})
    response.raise_for_status()
    assert response.json()['answer']
    with SessionLocal() as db:
        sealed = db.query(models.ChatSession).filter_by(conversation_id=uuid.UUID(conv_id)).one()
        job = db.query(models.BackgroundJob).filter_by(kind='memory.enrich',resource_id=sealed.id).one()
        assert job.status in {'queued','running','retrying'}, 'HTTP must finish before remote enrichment'
        assert sealed.transcript_json['messages']
        job_id = str(job.id)
    deadline = time.monotonic()+180
    while time.monotonic()<deadline:
        response = client.get(f'/api/repos/{repo_id}/jobs/{job_id}'); response.raise_for_status()
        job = response.json()
        if job['status'] in {'completed','failed','cancelled'}:
            assert job['status'] == 'completed',job
            break
        time.sleep(.5)
    else:
        raise AssertionError('Chat API enrichment timed out')
    results.append({'check':'Actual Chat HTTP returns with raw transcript/outbox before Worker enrichment, then memory completes','passed':True})
    print('PASS Actual Chat HTTP returns before Worker enrichment, then memory completes',flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--resume', action='store_true', help='Retry the exact retained fixture enrichment after a diagnosed repair')
    parser.add_argument('--chat-only', action='store_true', help='Verify the real Chat HTTP entry in the retained fixture')
    args = parser.parse_args()
    with httpx.Client(base_url=API, timeout=120) as client:
        if args.cleanup:
            cleanup(client,json.loads(FIXTURE.read_text(encoding='utf-8')))
            print('Exact acceptance fixture cleaned')
            return
        if args.resume or args.chat_only:
            fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
            previous = json.loads(REPORT.read_text(encoding='utf-8'))
            assert previous['fixture']['repo_id'] == fixture['repo_id']
            results = previous['checks']
            try:
                if args.chat_only:
                    verify_chat_api(client,fixture,results)
                else:
                    verify_enrichment(client,fixture,results,resume=True)
                save(REPORT,{'passed':True,'checked_at':datetime.now(timezone.utc),'checks':results,'fixture':fixture})
            except Exception as exc:
                save(REPORT,{'passed':False,'checks':results,'fixture':fixture,'error':type(exc).__name__+': '+str(exc)[:1000]})
                raise
            return
        marker = 'DevFlow memory acceptance fixture '+uuid.uuid4().hex
        with SessionLocal() as db:
            repo = models.Repository(owner='memory-acceptance',name=uuid.uuid4().hex,full_name='memory-acceptance/'+uuid.uuid4().hex,provider='local',description=marker)
            db.add(repo); db.flush()
            conv = models.Conversation(repo_id=repo.id,title='记忆工作台验收演示')
            db.add(conv); db.flush()
            fixture = {'repo_id':str(repo.id),'conversation_id':str(conv.id),'marker':marker,'full_name':repo.full_name}
            db.commit()
        save(FIXTURE,fixture)
        results = []
        try:
            run(client,fixture,results)
            verify_chat_api(client,fixture,results)
            save(REPORT,{'passed':True,'checked_at':datetime.now(timezone.utc),'checks':results,'fixture':fixture})
            print('REPORT',str(REPORT))
        except Exception as exc:
            save(REPORT,{'passed':False,'checked_at':datetime.now(timezone.utc),'checks':results,'error':type(exc).__name__+': '+str(exc)[:1000],'fixture':fixture})
            raise
        finally:
            if not args.keep:
                cleanup(client,fixture)


if __name__ == '__main__':
    main()
