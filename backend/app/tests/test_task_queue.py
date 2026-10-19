import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import rag
from app.db import models
from app.db.session import Base


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(rag.settings, 'upload_dir', str(tmp_path / 'uploads'))
    yield factory
    engine.dispose()


def make_repo(db):
    repo = models.Repository(owner='tests', name='queue', full_name=f'tests/{uuid.uuid4().hex}')
    db.add(repo)
    db.commit()
    return repo


@pytest.mark.asyncio
async def test_upload_commits_recoverable_job_without_request_background_work(sessions):
    with sessions() as db:
        repo = make_repo(db)
        background = BackgroundTasks()
        result = await rag.upload_document(repo.id, background, UploadFile(io.BytesIO(b'# Queue\nPersistent document test.'), filename='queue.md'), db)
        assert getattr(result, 'job_id', None) is not None, 'accepted upload must expose its durable job id'
        job = db.get(models.BackgroundJob, result.job_id)
        assert job.status == 'queued'
        assert job.resource_id == result.id
        assert job.published_at is None
        assert background.tasks == []


def test_duplicate_active_jobs_coalesce_but_new_refresh_after_completion_is_allowed(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        first = enqueue_job(db, 'repo.sync', repo.id, payload={'limit': 5})
        db.commit()
        duplicate = enqueue_job(db, 'repo.sync', repo.id, payload={'limit': 5})
        different = enqueue_job(db, 'repo.sync', repo.id, payload={'limit': 50})
        db.commit()
        assert duplicate.id == first.id
        assert different.id != first.id
        job_id, repo_id = first.id, repo.id
    assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': lambda job: {'synced': {'issues': 2}}}) == 'completed'
    with sessions() as db:
        new = enqueue_job(db, 'repo.sync', repo_id, payload={'limit': 5})
        db.commit()
        assert new.id != job_id


def test_duplicate_delivery_does_not_repeat_committed_business_effect(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'repo.sync', repo.id, payload={})
        db.commit()
        job_id, repo_id = job.id, repo.id
    def handler(job):
        with sessions() as db:
            db.add(models.Issue(repo_id=repo_id, github_issue_id=721, number=7, title='one business effect'))
            db.commit()
        return {'synced': {'issues': 1}}
    assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': handler}) == 'completed'
    assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': handler}) == 'completed'
    with sessions() as db:
        assert db.query(models.Issue).count() == 1
        assert db.get(models.BackgroundJob, job_id).attempts == 1


def test_transient_failure_waits_before_retry_and_then_recovers(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'repo.sync', repo.id, payload={})
        db.commit()
        job_id = job.id
    def unavailable(job):
        raise ConnectionError('dependency temporarily unavailable')
    assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': unavailable}) == 'retrying'
    assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': lambda job: {'ok': True}}) == 'deferred'
    with sessions() as db:
        job = db.get(models.BackgroundJob, job_id)
        assert job.attempts == 1
        assert job.active_key is not None
        assert job.published_at is None
        job.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': lambda job: {'ok': True}}) == 'completed'


def test_permanent_failure_does_not_schedule_retries(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'document.ingest', repo.id, resource_id=uuid.uuid4(), payload={})
        db.commit()
        job_id = job.id
    def invalid_document(job):
        raise ValueError('unsupported document contents')
    assert process_job(job_id, session_factory=sessions, handlers={'document.ingest': invalid_document}) == 'failed'
    with sessions() as db:
        job = db.get(models.BackgroundJob, job_id)
        assert job.attempts == 1
        assert job.active_key is None
        assert job.finished_at is not None


@pytest.mark.asyncio
async def test_failed_publish_stays_in_outbox_and_recovers(sessions):
    from app.services.task_queue import enqueue_job, publish_pending
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'repo.sync', repo.id, payload={})
        db.commit()
        job_id = job.id
    async def offline(job_id, kind):
        raise ConnectionError('broker unavailable')
    assert await publish_pending(offline, session_factory=sessions) == 0
    with sessions() as db:
        job = db.get(models.BackgroundJob, job_id)
        assert job.published_at is None
        assert job.status == 'queued'
        assert job.publish_error
    delivered = []
    async def publish(job_id, kind):
        delivered.append((job_id, kind))
    assert await publish_pending(publish, session_factory=sessions) == 1
    assert delivered == [(job_id, 'repo.sync')]
    with sessions() as db:
        assert db.get(models.BackgroundJob, job_id).published_at is not None


def test_deleted_repository_task_is_cancelled(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'repo.sync', repo.id, payload={})
        db.commit()
        job_id = job.id
        db.delete(repo)
        db.commit()
    assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': lambda job: {'unexpected': True}}) in {'missing', 'cancelled'}


@pytest.mark.asyncio
async def test_manual_sync_is_accepted_without_calling_github_in_request(sessions):
    from app.api.routes import repos
    from app.schemas.repos import RepoSyncRequest
    with sessions() as db:
        repo = make_repo(db)
        background = BackgroundTasks()
        result = await repos.sync_repo(repo.id, RepoSyncRequest(sync_issues=False, sync_pull_requests=False, sync_workflow_runs=False), background, db)
        assert result.status == 'queued'
        assert result.job_id is not None
        assert background.tasks == []
        job = db.get(models.BackgroundJob, result.job_id)
        assert job.kind == 'repo.sync'
        assert job.attempts == 0
        assert repo.last_sync_at is None


@pytest.mark.asyncio
async def test_same_upload_and_repeated_retry_reuse_one_active_job(sessions):
    with sessions() as db:
        repo = make_repo(db)
        async def upload():
            return await rag.upload_document(repo.id, BackgroundTasks(), UploadFile(io.BytesIO(b'same checksum payload'), filename='same.txt'), db)
        first = await upload()
        second = await upload()
        retry = await rag.retry_document(repo.id, first.id, BackgroundTasks(), db)
        assert first.id == second.id == retry.id
        assert first.job_id == second.job_id == retry.job_id
        assert db.query(models.BackgroundJob).count() == 1


def test_document_processor_exposes_failure_to_worker_and_keeps_failed_state(sessions, monkeypatch):
    from app.services.rag import document_processing
    monkeypatch.setattr(document_processing, 'SessionLocal', sessions)
    with sessions() as db:
        repo = make_repo(db)
        source = models.KnowledgeSourceDocument(repo_id=repo.id, name='missing.txt', checksum='a'*64, stored_path='does-not-exist-queue-test.txt', status='queued')
        db.add(source)
        db.commit()
        source_id = source.id
    with pytest.raises(FileNotFoundError):
        document_processing.process_knowledge_document(source_id, raise_errors=True)
    with sessions() as db:
        assert db.get(models.KnowledgeSourceDocument, source_id).status == 'failed'


def test_retry_limit_stops_a_repeated_dependency_failure(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'repo.sync', repo.id, payload={})
        job.max_attempts = 3
        db.commit()
        job_id = job.id
    def unavailable(job):
        raise ConnectionError('not ready')
    for expected in ['retrying', 'retrying', 'failed']:
        assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': unavailable}) == expected
        with sessions() as db:
            job = db.get(models.BackgroundJob, job_id)
            job.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
    with sessions() as db:
        job = db.get(models.BackgroundJob, job_id)
        assert job.attempts == 3
        assert job.active_key is None
        assert job.finished_at is not None


def test_same_repo_resource_cannot_be_processed_concurrently(sessions):
    from app.services.job_locks import resource_lock
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'repo.sync', repo.id, payload={})
        db.commit()
        job_id, repo_id = job.id, repo.id
    with resource_lock(sessions.kw['bind'], f'repo:{repo_id}'):
        assert process_job(job_id, session_factory=sessions, handlers={'repo.sync': lambda job: {}}) == 'busy'
    with sessions() as db:
        job = db.get(models.BackgroundJob, job_id)
        assert job.attempts == 0
        assert job.status == 'queued'


def test_permission_failure_is_permanent_but_github_rate_limit_is_delayed():
    import httpx
    from app.services.task_queue import retry_delay
    request = httpx.Request('GET', 'https://api.github.com/repos/test/queue/issues')
    denied = httpx.HTTPStatusError('denied', request=request, response=httpx.Response(403, request=request))
    limited = httpx.HTTPStatusError('rate limited', request=request, response=httpx.Response(403, headers={'x-ratelimit-remaining': '0', 'retry-after': '60'}, request=request))
    assert retry_delay(denied, 1) is None
    assert retry_delay(limited, 1) >= 60


def test_delivery_id_remains_idempotent_after_terminal_completion(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        first = enqueue_job(db, 'repo.sync', repo.id, payload={}, idempotency_key='github-delivery-123')
        db.commit()
        job_id, repo_id = first.id, repo.id
    process_job(job_id, session_factory=sessions, handlers={'repo.sync': lambda job: {'ok': True}})
    with sessions() as db:
        duplicate = enqueue_job(db, 'repo.sync', repo_id, payload={}, idempotency_key='github-delivery-123')
        assert duplicate.id == job_id
        assert duplicate.status == 'completed'


@pytest.mark.asyncio
async def test_fast_consumer_retry_is_not_lost_when_publish_confirmation_arrives(sessions):
    from app.services.task_queue import enqueue_job, process_job, publish_pending
    with sessions() as db:
        repo = make_repo(db)
        job = enqueue_job(db, 'repo.sync', repo.id, payload={})
        db.commit()
        job_id = job.id
    def unavailable(job):
        raise ConnectionError('quick transient failure')
    async def publish(job_id, kind):
        assert process_job(job_id, session_factory=sessions, handlers={kind: unavailable}) == 'retrying'
    await publish_pending(publish, session_factory=sessions)
    with sessions() as db:
        job = db.get(models.BackgroundJob, job_id)
        assert job.status == 'retrying'
        assert job.published_at is None


@pytest.mark.asyncio
async def test_upload_reuses_source_during_failure_to_retry_transition(sessions):
    with sessions() as db:
        repo = make_repo(db)
        async def upload():
            return await rag.upload_document(repo.id, BackgroundTasks(), UploadFile(io.BytesIO(b'failure transition same file'), filename='transition.txt'), db)
        first = await upload()
        db.get(models.KnowledgeSourceDocument, first.id).status = 'failed'
        db.get(models.BackgroundJob, first.job_id).status = 'running'
        db.commit()
        duplicate = await upload()
        assert duplicate.id == first.id
        assert duplicate.job_id == first.job_id
        assert db.query(models.KnowledgeSourceDocument).count() == 1


def test_worker_heartbeat_matches_launch_instance_even_with_python_launcher(tmp_path, monkeypatch):
    import json
    from app.workers import task_worker
    path = tmp_path / 'heartbeat.json'
    monkeypatch.setattr(task_worker, 'HEARTBEAT', path)
    monkeypatch.setenv('DEVFLOW_WORKER_INSTANCE', 'launch-instance-7')
    task_worker.heartbeat('ready')
    assert json.loads(path.read_text(encoding='utf-8')).get('instance_id') == 'launch-instance-7'


@pytest.mark.parametrize('shell', ['pwsh', 'powershell'])
def test_process_record_guard_accepts_roundtripped_dates_but_rejects_reused_pid(shell):
    import shutil
    import subprocess
    from pathlib import Path
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f'{shell} unavailable')
    script = Path(__file__).resolve().parents[3] / 'scripts/process_helpers.ps1'
    command = f"$ErrorActionPreference='Stop'; . '{str(script).replace(chr(39), chr(39)*2)}'; $p=Get-Process -Id $PID; $r=@{{Pid=$PID; StartedAt=$p.StartTime.ToUniversalTime().ToString('o')}} | ConvertTo-Json | ConvertFrom-Json; if(-not(Test-DevFlowProcessRecord -Record $r)){{throw 'Valid process was rejected'}}; $r.StartedAt='2000-01-01T00:00:00Z'; if(Test-DevFlowProcessRecord -Record $r){{throw 'Reused PID was accepted'}}; Write-Output 'process-guard-passed'"
    result = subprocess.run([executable, '-NoProfile', '-Command', command], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert b'process-guard-passed' in result.stdout, result.stderr.decode(errors='replace')


@pytest.mark.parametrize('shell', ['pwsh', 'powershell'])
def test_process_records_are_flat_on_both_powershell_versions(shell, tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f'{shell} unavailable')
    records = tmp_path / 'processes.json'
    records.write_text(json.dumps([{'Role': 'backend', 'Pid': 111}, {'Role': 'worker', 'Pid': 222}]))
    helper = Path(__file__).resolve().parents[3] / 'scripts/process_helpers.ps1'
    command = f"$ErrorActionPreference='Stop'; . '{helper}'; $r=@(Read-DevFlowProcessRecords -Path '{records}'); if($r.Count -ne 2){{throw 'Nested records'}}; $w=@($r | Where-Object Role -eq 'worker'); if($w.Count -ne 1 -or $w[0].Pid -ne 222){{throw 'Worker not selected'}}; Write-Output 'records-passed'"
    result = subprocess.run([executable, '-NoProfile', '-Command', command], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert b'records-passed' in result.stdout


def test_completed_document_is_reconciled_after_crash_on_last_attempt(sessions):
    from app.services.task_queue import enqueue_job, process_job
    with sessions() as db:
        repo = make_repo(db)
        source = models.KnowledgeSourceDocument(repo_id=repo.id, name='committed.txt', checksum='b'*64, stored_path='committed.txt', status='completed', chunk_count=3)
        db.add(source)
        db.flush()
        job = enqueue_job(db, 'document.ingest', repo.id, resource_id=source.id)
        job.status, job.attempts, job.max_attempts = 'running', 1, 1
        db.commit()
        job_id, source_id = job.id, source.id
    def must_not_repeat(job):
        pytest.fail('Already committed document must not be ingested again')
    assert process_job(job_id, session_factory=sessions, handlers={'document.ingest': must_not_repeat}) == 'completed'
    with sessions() as db:
        assert db.get(models.KnowledgeSourceDocument, source_id).status == 'completed'
        job = db.get(models.BackgroundJob, job_id)
        assert job.result['chunk_count'] == 3
        assert job.active_key is None
        assert job.attempts == 1


@pytest.mark.asyncio
async def test_connect_repository_and_initial_job_commit_together(sessions, monkeypatch):
    from app.api.routes import repos
    from app.schemas.repos import RepoConnectRequest
    async def metadata(*args, **kwargs):
        return {'id': 112, 'default_branch': 'main'}
    def cannot_enqueue(*args, **kwargs):
        raise RuntimeError('outbox insert failed')
    monkeypatch.setattr(repos.GitHubClient, 'get_repo', metadata)
    monkeypatch.setattr(repos, 'enqueue_job', cannot_enqueue)
    with sessions() as db:
        with pytest.raises(RuntimeError, match='outbox insert failed'):
            await repos.connect_repo(RepoConnectRequest(owner='atomic', repo='connect'), BackgroundTasks(), db)
        db.rollback()
        assert db.query(models.Repository).filter_by(full_name='atomic/connect').count() == 0


@pytest.mark.asyncio
async def test_project_knowledge_upload_also_returns_durable_document_job(sessions, monkeypatch):
    from app.api.routes import knowledge
    def must_not_extract(*args, **kwargs):
        raise AssertionError('HTTP upload must only save the source and outbox')
    monkeypatch.setattr('app.services.rag.embeddings.embed_knowledge_texts', must_not_extract)
    with sessions() as db:
        repo = make_repo(db)
        source = await knowledge.upload_knowledge_file(repo.id, UploadFile(io.BytesIO(b'# Deferred knowledge'), filename='legacy.md'), db)
        assert source.job_id is not None
        assert source.status == 'queued'
        assert source.indexed is False
        assert db.get(models.BackgroundJob, source.job_id).resource_id == source.id


@pytest.mark.asyncio
async def test_project_knowledge_delete_cancels_queue_and_stale_delivery(sessions, monkeypatch):
    from app.api.routes import knowledge
    from app.services.task_queue import process_job
    monkeypatch.setattr(rag.settings, 'milvus_enabled', False)
    monkeypatch.setattr(knowledge, 'rebuild_knowledge_graph', lambda *args: {})
    with sessions() as db:
        repo = make_repo(db)
        source = await knowledge.upload_knowledge_file(repo.id, UploadFile(io.BytesIO(b'delete queued source'), filename='remove.md'), db)
        await knowledge.delete_knowledge_item(repo.id, source.id, 'knowledge_file', db)
        assert db.get(models.KnowledgeSourceDocument, source.id) is None
        assert db.get(models.BackgroundJob, source.job_id).status == 'cancelled'
    assert process_job(source.job_id, session_factory=sessions, handlers={}) == 'cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['runs', 'files', 'reviews', 'jobs', 'logs'])
async def test_strict_sync_propagates_nested_http_errors(sessions, monkeypatch, stage):
    import httpx
    from app.services import repos_sync
    from app.schemas.repos import RepoSyncRequest
    async def issues(*args, **kwargs): return []
    async def prs(*args, **kwargs): return [{'id': 123, 'number': 1, 'title': 'test'}]
    async def runs(*args, **kwargs): return [{'id': 456, 'name': 'CI', 'conclusion': 'failure'}]
    async def empty(*args, **kwargs): return []
    async def logs(*args, **kwargs): return 'old log'
    async def failure(*args, **kwargs):
        request = httpx.Request('GET', 'https://api.github.com/fixture')
        raise httpx.HTTPStatusError('temporary unavailable', request=request, response=httpx.Response(503, request=request))
    functions = {'list_issues': issues, 'list_pull_requests': prs, 'list_workflow_runs': runs, 'list_pull_request_files': empty, 'list_pull_request_review_comments': empty, 'list_workflow_run_jobs': empty, 'get_workflow_run_logs': logs}
    names = {'runs': 'list_workflow_runs', 'files': 'list_pull_request_files', 'reviews': 'list_pull_request_review_comments', 'jobs': 'list_workflow_run_jobs', 'logs': 'get_workflow_run_logs'}
    functions[names[stage]] = failure
    for name, function in functions.items():
        monkeypatch.setattr(repos_sync, name, function)
    with sessions() as db:
        repo = make_repo(db)
        with pytest.raises(httpx.HTTPStatusError):
            await repos_sync.sync_repository(db, repo, RepoSyncRequest(), strict=True)
        db.rollback()
        assert db.query(models.PullRequest).count() == 0
        assert db.query(models.WorkflowRun).count() == 0


def test_document_completion_also_commits_graph_without_a_browser(sessions, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.services.rag import document_processing
    monkeypatch.setattr(document_processing, 'SessionLocal', sessions)
    monkeypatch.setattr(rag.settings, 'milvus_enabled', False)
    monkeypatch.setattr(document_processing, 'get_or_create_config', lambda *args: SimpleNamespace(chunk_size=1200, chunk_overlap=100, embedding_provider='fixture', embedding_model='fixture', embedding_dimensions=3))
    monkeypatch.setattr(document_processing, '_embed_in_batches', lambda texts, **kwargs: [[0.1, 0.2, 0.3] for text in texts])
    stored = tmp_path / 'graph.md'
    stored.write_text('This document describes issue #7.', encoding='utf-8')
    with sessions() as db:
        repo = make_repo(db)
        db.add(models.Issue(repo_id=repo.id, github_issue_id=7, number=7, title='linked issue'))
        source = models.KnowledgeSourceDocument(repo_id=repo.id, name=stored.name, checksum='c'*64, stored_path=str(stored), status='queued')
        db.add(source)
        db.commit()
        source_id = source.id
    document_processing.process_knowledge_document(source_id, raise_errors=True)
    with sessions() as db:
        assert db.get(models.KnowledgeSourceDocument, source_id).status == 'completed'
        assert db.query(models.KnowledgeGraphEdge).filter_by(from_id=str(source_id), to_type='issue').count() == 1
