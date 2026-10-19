import uuid

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import project_index, repos
from app.db.models import BackgroundJob, Base, Document, ProjectIndexState, Repository
from app.schemas.repos import RepoConnectRequest
from app.services.job_locks import resource_lock
from app.services.task_queue import enqueue_job, process_job
from app.workers import job_handlers


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(job_handlers, 'SessionLocal', sessions)
    with sessions() as db:
        repo = Repository(owner='tests', name='index-pipeline', full_name=f'tests/{uuid.uuid4().hex}',
                          local_path=str(tmp_path/'checkout'), checkout_mode='managed')
        db.add(repo)
        db.commit()
        rid = repo.id
    yield sessions, rid, tmp_path/'checkout'
    engine.dispose()


def write_checkout(path):
    path.mkdir(exist_ok=True)
    (path/'README.md').write_text('# Pipeline fixture\n\nCheckout must precede project indexing.', encoding='utf-8')


@pytest.mark.asyncio
async def test_early_manual_scan_is_a_durable_deduplicated_job(pipeline):
    sessions, rid, path = pipeline
    with sessions() as db:
        background = BackgroundTasks()
        first = await project_index.start_memory_index_scan(rid, background, db)
        second = await project_index.start_memory_index_scan(rid, background, db)
        assert first.job_id == second.job_id
        assert first.job_id is not None
        assert background.tasks == []
        assert not path.exists()
        assert db.query(BackgroundJob).count() == 1
        assert db.get(BackgroundJob, first.job_id).kind == 'project.index'
        assert db.query(ProjectIndexState).filter_by(repo_id=rid).one().status == 'building'


@pytest.mark.asyncio
async def test_connect_requests_automatic_indexing_after_checkout(pipeline, monkeypatch):
    sessions, _, _ = pipeline
    async def metadata(*args, **kwargs):
        return {'id':9871, 'default_branch':'main', 'clone_url':'https://github.com/tests/pipeline.git'}
    monkeypatch.setattr(repos.GitHubClient, 'get_repo', metadata)
    with sessions() as db:
        result = await repos.connect_repo(RepoConnectRequest(owner='tests',repo='pipeline',demo_mode=False), BackgroundTasks(), db)
        job = db.get(BackgroundJob, result.job_id)
        assert job.payload['analyze_code'] is True
        assert job.payload['index_project'] is True
        assert db.query(ProjectIndexState).filter_by(repo_id=result.repo_id).one().status == 'building'


def test_worker_prepares_missing_checkout_then_indexes(pipeline, monkeypatch):
    sessions, rid, path = pipeline
    async def checkout(identifier):
        assert identifier == rid
        write_checkout(path)
        return 1
    monkeypatch.setattr('app.services.code_analysis.sync_repository_code_analysis_by_id', checkout)
    with sessions() as db:
        job = enqueue_job(db, 'project.index', rid)
        db.commit()
        jid = job.id
    assert process_job(jid, session_factory=sessions) == 'completed'
    with sessions() as db:
        assert db.query(ProjectIndexState).filter_by(repo_id=rid).one().status == 'ready'
        assert any('Checkout must precede' in doc.content for doc in db.query(Document).filter_by(repo_id=rid))
        assert db.get(BackgroundJob,jid).result['docs_indexed'] >= 2


def test_sync_automatically_indexes_after_code_analysis(pipeline, monkeypatch):
    sessions, rid, path = pipeline
    async def checkout(identifier):
        write_checkout(path)
        return 3
    monkeypatch.setattr('app.services.code_analysis.sync_repository_code_analysis_by_id', checkout)
    with sessions() as db:
        job = enqueue_job(db, 'repo.sync', rid, payload={'code_only':True,'analyze_code':True,'index_project':True})
        db.commit()
        jid = job.id
    assert process_job(jid, session_factory=sessions) == 'completed'
    with sessions() as db:
        assert db.query(ProjectIndexState).filter_by(repo_id=rid).one().status == 'ready'
        assert db.get(BackgroundJob,jid).result['project_index']['status'] == 'ready'


def test_checkout_failure_is_visible_and_does_not_scan_missing_path(pipeline, monkeypatch):
    sessions, rid, path = pipeline
    async def failed_checkout(identifier):
        return None
    monkeypatch.setattr('app.services.code_analysis.sync_repository_code_analysis_by_id', failed_checkout)
    with sessions() as db:
        job = enqueue_job(db, 'project.index', rid)
        job.max_attempts = 1
        db.commit()
        jid = job.id
    assert process_job(jid, session_factory=sessions) == 'failed'
    with sessions() as db:
        state = db.query(ProjectIndexState).filter_by(repo_id=rid).one()
        assert state.status == 'failed' and state.error_message
        assert db.query(Document).filter_by(repo_id=rid).count() == 0
        assert not path.exists()


def test_duplicate_index_delivery_preserves_document_ids(pipeline, monkeypatch):
    sessions, rid, path = pipeline
    write_checkout(path)
    async def must_not_fetch(identifier):
        pytest.fail('A manual rescan of an available checkout must not require network access')
    monkeypatch.setattr('app.services.code_analysis.sync_repository_code_analysis_by_id', must_not_fetch)
    with sessions() as db:
        job = enqueue_job(db, 'project.index', rid)
        db.commit()
        jid = job.id
    assert process_job(jid, session_factory=sessions) == 'completed'
    with sessions() as db:
        first_ids = {row.id for row in db.query(Document).filter_by(repo_id=rid)}
    assert process_job(jid, session_factory=sessions) == 'completed'
    with sessions() as db:
        assert {row.id for row in db.query(Document).filter_by(repo_id=rid)} == first_ids
        assert db.get(BackgroundJob,jid).attempts == 1


def test_index_waits_for_repository_lock_without_spending_attempt(pipeline):
    sessions, rid, path = pipeline
    write_checkout(path)
    with sessions() as db:
        job = enqueue_job(db, 'project.index', rid)
        db.commit()
        jid = job.id
    with resource_lock(sessions.kw['bind'],f'repo:{rid}'):
        assert process_job(jid, session_factory=sessions) == 'busy'
    with sessions() as db:
        assert db.get(BackgroundJob,jid).attempts == 0
        assert db.query(Document).filter_by(repo_id=rid).count() == 0


@pytest.mark.asyncio
async def test_failed_enqueue_does_not_commit_building_state(pipeline, monkeypatch):
    sessions, rid, _ = pipeline
    def fail(*args, **kwargs):
        raise RuntimeError('outbox unavailable')
    monkeypatch.setattr(project_index, 'enqueue_job', fail, raising=False)
    with sessions() as db:
        with pytest.raises(RuntimeError,match='outbox unavailable'):
            await project_index.start_memory_index_scan(rid, BackgroundTasks(), db)
        db.rollback()
        assert db.query(ProjectIndexState).filter_by(repo_id=rid).count() == 0


def test_invalid_index_policy_fails_sync_without_replacing_old_evidence(pipeline, monkeypatch):
    from app.services.project_indexing import scan_project_index
    sessions, rid, path = pipeline
    write_checkout(path)
    with sessions() as db:
        assert scan_project_index(db,db.get(Repository,rid))['status'] == 'ready'
        old_ids = {row.id for row in db.query(Document).filter_by(repo_id=rid)}
    (path/'.devflow').mkdir()
    (path/'.devflow/index.yml').write_text('project_docs:\n  mode: invalid\n',encoding='utf-8')
    async def checkout(identifier):return 1
    monkeypatch.setattr('app.services.code_analysis.sync_repository_code_analysis_by_id', checkout)
    with sessions() as db:
        job = enqueue_job(db,'repo.sync',rid,payload={'code_only':True,'analyze_code':True,'index_project':True})
        job.max_attempts=1
        db.commit()
        jid=job.id
    assert process_job(jid,session_factory=sessions)=='failed'
    with sessions() as db:
        assert db.query(ProjectIndexState).filter_by(repo_id=rid).one().status=='failed'
        assert {row.id for row in db.query(Document).filter_by(repo_id=rid)} == old_ids
