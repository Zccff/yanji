"""Real Broker/DB/Milvus verification; run using backend/.venv Python.

--faults intentionally restarts only this project's Worker and RabbitMQ.
GitHub is a local HTTP fixture; normal document ingestion uses configured embeddings.
Temporary test records are removed in finally; the JSON report remains in .data/runtime.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import aio_pika
import httpx
from app.core.config import settings
from app.db.models import BackgroundJob, Document, Issue, KnowledgeGraphEdge, KnowledgeSourceDocument, PullRequest, Repository, WorkflowRun
from app.db.session import SessionLocal
from app.services.rag.vector_store import MilvusVectorStore
from app.services.job_locks import JobResourceBusy, resource_lock
from app.services.task_queue import enqueue_job

API = 'http://127.0.0.1:8000'
DOCKER = Path('C:/Program Files/Docker/Docker/resources/bin/docker.exe')


class GitHubFixture(BaseHTTPRequestHandler):
    mode = 'normal'
    transient_remaining = 0
    slow_entered = threading.Event()
    slow_release = threading.Event()

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        status, data = 200, []
        if path.endswith('/issues'):
            if type(self).mode == 'slow':
                type(self).slow_entered.set()
                type(self).slow_release.wait(timeout=60)
            if type(self).mode == 'denied':
                status, data = 403, {'message': 'verification permission denied'}
            elif type(self).transient_remaining:
                type(self).transient_remaining -= 1
                status, data = 503, {'message': 'verification temporary unavailable'}
            else:
                data = [{'id': 9821001, 'number': 1, 'title': 'RabbitMQ fixture issue', 'body': 'durable synchronization', 'state': 'open', 'labels': [], 'user': {'login': 'fixture'}, 'assignees': []}]
        elif path.endswith('/pulls'):
            data = [{'id': 9821002, 'number': 2, 'title': 'RabbitMQ fixture PR', 'state': 'open', 'base': {'ref': 'main'}, 'head': {'ref': 'verify'}}]
        elif path.endswith('/files'):
            data = [{'filename': 'README.md', 'status': 'modified', 'patch': '@@ -1 +1 @@\n-old\n+new'}]
        elif path.endswith('/actions/runs'):
            if type(self).mode == 'ci_unavailable':
                status, data = 503, {'message': 'temporary CI endpoint outage'}
                type(self).mode = 'normal'
            else:
                data = {'workflow_runs': [{'id': 9821003, 'name': 'fixture CI', 'status': 'completed', 'conclusion': 'success'}]}
        elif path.endswith('/jobs'):
            data = {'jobs': [{'id': 9821004, 'name': 'test', 'status': 'completed', 'conclusion': 'success', 'steps': []}]}
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


def wait_job(client, repo_id, job_id, *, expected='completed', timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f'/api/repos/{repo_id}/jobs/{job_id}')
        response.raise_for_status()
        job = response.json()
        if job['status'] in {'completed', 'failed', 'cancelled'}:
            assert job['status'] == expected, job
            return job
        time.sleep(0.5)
    raise AssertionError(f'Task timeout: {job_id}')


async def replay(job_id, kind):
    connection = await aio_pika.connect_robust(settings.rabbitmq_url, timeout=5)
    try:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.get_exchange('devflow.jobs')
        for _ in range(2):
            await exchange.publish(aio_pika.Message(body=json.dumps({'schema_version': 1, 'job_id': job_id, 'kind': kind}).encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT), routing_key=kind, mandatory=True)
    finally:
        await connection.close()


def worker_control(*args):
    # Background descendants can inherit a capture PIPE on Windows and keep
    # communicate() waiting after PowerShell exits. A file avoids that EOF dependency.
    log = ROOT / '.data/runtime/worker-control.log'
    with log.open('w', encoding='utf-8') as output:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'scripts/restart_worker.ps1'), *args], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=150)
    if result.returncode:
        raise AssertionError(log.read_text(encoding='utf-8', errors='replace')[-2000:])


def docker(*args):
    log = ROOT / '.data/runtime/rabbitmq-docker-control.log'
    with log.open('a', encoding='utf-8') as output:
        subprocess.run([str(DOCKER), *args], check=True, stdout=output, stderr=subprocess.STDOUT, timeout=90)


def verify(faults):
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'tests': {}, 'faults_enabled': faults}
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), GitHubFixture)
    import faulthandler
    faulthandler.dump_traceback_later(90, repeat=True)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    repo_id = None
    broker_stopped = worker_stopped = False
    report_path = ROOT / '.data/runtime/rabbitmq-verification.json'
    with httpx.Client(base_url=API, timeout=20) as client:
        try:
            with SessionLocal() as db:
                checkout = ROOT / '.data/demo-repositories/devflow-agent-lab'
                repo = Repository(owner='mq-fixture', name='verification', full_name=f'mq-fixture/{uuid.uuid4().hex}', provider='github_compatible', api_base_url=f'http://127.0.0.1:{fixture.server_port}', local_path=str(checkout) if checkout.exists() else None, checkout_mode='local' if checkout.exists() else 'managed')
                db.add(repo)
                db.commit()
                repo_id = str(repo.id)
            report['fixture_repo_id'] = repo_id

            def sync():
                response = client.post(f'/api/repos/{repo_id}/sync', json={'limit': 5})
                assert response.status_code == 202, response.text
                task = response.json()
                assert task.get('job_id'), task
                return task['job_id']

            first = wait_job(client, repo_id, sync())
            assert first['result']['synced'] == {'issues': 1, 'pull_requests': 1, 'workflow_runs': 1}, first
            asyncio.run(replay(first['job_id'], 'repo.sync'))
            time.sleep(3)
            repeated = client.get(f"/api/repos/{repo_id}/jobs/{first['job_id']}").json()
            assert repeated['attempts'] == 1, repeated
            with SessionLocal() as db:
                rid = uuid.UUID(repo_id)
                assert db.query(Issue).filter_by(repo_id=rid).count() == 1
                assert db.query(PullRequest).filter_by(repo_id=rid).count() == 1
                assert db.query(WorkflowRun).filter_by(repo_id=rid).count() == 1
            report['tests']['real_sync_and_duplicate_delivery'] = 'passed'

            with SessionLocal() as db:
                with resource_lock(db.get_bind(), f'repo:{repo_id}'):
                    def competing_lock():
                        try:
                            with resource_lock(db.get_bind(), f'repo:{repo_id}'):
                                raise AssertionError('PostgreSQL allowed a concurrent repository lock')
                        except JobResourceBusy:
                            return True
                    with ThreadPoolExecutor(max_workers=3) as executor:
                        assert executor.submit(competing_lock).result()
                        jobs = list(executor.map(lambda _: sync(), range(3)))
                    assert len(set(jobs)) == 1, jobs
                    barrier = threading.Barrier(3)
                    def database_enqueue(_):
                        with SessionLocal() as session:
                            barrier.wait(timeout=10)
                            task = enqueue_job(session, 'repo.sync', uuid.UUID(repo_id), payload={'sync_issues': True, 'sync_pull_requests': False, 'sync_workflow_runs': False, 'limit': 3})
                            session.commit()
                            return str(task.id)
                    with ThreadPoolExecutor(max_workers=3) as executor:
                        concurrent_ids = list(executor.map(database_enqueue, range(3)))
                    assert len(set(concurrent_ids)) == 1, concurrent_ids
            wait_job(client, repo_id, jobs[0])
            wait_job(client, repo_id, concurrent_ids[0])
            report['tests']['postgres_resource_mutex_and_concurrent_enqueue_coalescing'] = 'passed'

            text = '# RabbitMQ verification\n\nThis document describes issue #1 and confirms durable queue ingestion.\n\n验证目标：重复消息不应重复生成知识切片。'
            response = client.post(f'/api/rag/repositories/{repo_id}/documents', files={'file': ('rabbitmq-verification.md', text.encode(), 'text/markdown')})
            assert response.status_code == 202, response.text
            source = response.json()
            document_job = wait_job(client, repo_id, source['job_id'])
            with SessionLocal() as db:
                chunks = db.query(Document).filter_by(repo_id=uuid.UUID(repo_id), source_id=uuid.UUID(source['id'])).count()
                assert db.query(KnowledgeGraphEdge).filter_by(repo_id=uuid.UUID(repo_id), from_id=source['id'], to_type='issue').count() == 1
            assert chunks > 0
            store = MilvusVectorStore()
            vectors = store.client.query(collection_name=store.collection_name, filter=f'repo_id == "{repo_id}" and source_id == "{source["id"]}"', output_fields=['pk'], consistency_level='Strong')
            assert len(vectors) == chunks, (len(vectors), chunks)
            asyncio.run(replay(source['job_id'], 'document.ingest'))
            time.sleep(3)
            with SessionLocal() as db:
                assert db.query(Document).filter_by(source_id=uuid.UUID(source['id'])).count() == chunks
            assert client.get(f"/api/repos/{repo_id}/jobs/{source['job_id']}").json()['attempts'] == document_job['attempts']
            report['tests']['real_document_embedding_milvus_and_duplicates'] = {'status': 'passed', 'chunks': chunks}
            report['tests']['server_side_graph_completion_without_browser'] = 'passed'

            legacy_text = b'# Project knowledge upload\nThis entry must also use RabbitMQ.'
            legacy = client.post(f'/api/repos/{repo_id}/knowledge/upload', files={'file': ('project.md', legacy_text, 'text/markdown')})
            assert legacy.status_code == 202, legacy.text
            legacy = legacy.json()
            assert legacy['job_id'] and not legacy['indexed'], legacy
            wait_job(client, repo_id, legacy['job_id'])
            duplicate = client.post(f'/api/repos/{repo_id}/knowledge/upload', files={'file': ('project.md', legacy_text, 'text/markdown')}).json()
            assert duplicate['id'] == legacy['id'] and duplicate['job_id'] == legacy['job_id'] and duplicate['duplicate'], duplicate
            report['tests']['project_knowledge_upload_and_checksum_deduplication'] = 'passed'

            invalid = client.post(f'/api/rag/repositories/{repo_id}/documents', files={'file': ('empty-text.txt', b'   \n', 'text/plain')}).json()
            failed = wait_job(client, repo_id, invalid['job_id'], expected='failed')
            assert failed['attempts'] == 1
            report['tests']['invalid_document_permanent_failure'] = 'passed'

            GitHubFixture.transient_remaining = 1
            recovered = wait_job(client, repo_id, sync())
            assert recovered['attempts'] == 2, recovered
            report['tests']['transient_dependency_retry'] = 'passed'

            GitHubFixture.mode = 'ci_unavailable'
            ci_recovered = wait_job(client, repo_id, sync())
            assert ci_recovered['attempts'] == 2, ci_recovered
            report['tests']['nested_ci_http_failure_retries_instead_of_false_success'] = 'passed'

            GitHubFixture.mode = 'denied'
            denied = wait_job(client, repo_id, sync(), expected='failed')
            assert denied['attempts'] == 1
            GitHubFixture.mode = 'normal'
            report['tests']['permission_failure_no_retry'] = 'passed'

            if faults:
                GitHubFixture.slow_entered.clear()
                GitHubFixture.slow_release.clear()
                GitHubFixture.mode = 'slow'
                job_id = sync()
                assert GitHubFixture.slow_entered.wait(timeout=30), 'Worker did not reach blocked HTTP request'
                previous_heartbeat = json.loads((ROOT / '.data/runtime/worker-heartbeat.json').read_text())
                worker_stopped = True
                worker_control('-StopOnly')
                GitHubFixture.mode = 'normal'
                GitHubFixture.slow_release.set()
                interrupted = client.get(f'/api/repos/{repo_id}/jobs/{job_id}').json()
                assert interrupted['status'] == 'running', interrupted
                removed = client.post(f'/api/repos/{repo_id}/knowledge/upload', files={'file': ('cancelled.md', b'delete before consumption', 'text/markdown')}).json()
                deletion = client.delete(f'/api/repos/{repo_id}/knowledge/{removed["id"]}?source_type=knowledge_file')
                assert deletion.status_code == 200, deletion.text
                asyncio.run(replay(removed['job_id'], 'document.ingest'))
                worker_control()
                worker_stopped = False
                next_heartbeat = json.loads((ROOT / '.data/runtime/worker-heartbeat.json').read_text())
                assert next_heartbeat['instance_id'] != previous_heartbeat['instance_id'], 'Worker instance did not change'
                resumed = wait_job(client, repo_id, job_id)
                assert resumed['attempts'] >= 2, resumed
                report['tests']['worker_process_interruption_and_recovery'] = 'passed'
                wait_job(client, repo_id, removed['job_id'], expected='cancelled')
                with SessionLocal() as db:
                    assert db.get(KnowledgeSourceDocument, uuid.UUID(removed['id'])) is None
                    assert db.query(Document).filter_by(source_id=uuid.UUID(removed['id'])).count() == 0
                report['tests']['deleted_queued_document_stale_messages_do_not_resurrect'] = 'passed'

                broker_stopped = True
                docker('stop', '--time', '2', 'devflow-ai-local-rabbitmq')
                queued_id = sync()
                time.sleep(3)
                persisted = client.get(f'/api/repos/{repo_id}/jobs/{queued_id}').json()
                assert persisted['status'] == 'queued' and persisted['attempts'] == 0, persisted
                docker('start', 'devflow-ai-local-rabbitmq')
                broker_stopped = False
                wait_job(client, repo_id, queued_id)
                report['tests']['broker_outage_outbox_recovery'] = 'passed'

            report['status'] = 'passed'
        except Exception as exc:
            report['status'], report['error'] = 'failed', str(exc)
            raise
        finally:
            GitHubFixture.slow_release.set()
            recovery_errors = []
            if broker_stopped:
                try:
                    docker('start', 'devflow-ai-local-rabbitmq')
                except Exception as exc:
                    recovery_errors.append(f'broker recovery: {exc}')
            if worker_stopped:
                try:
                    worker_control()
                except Exception as exc:
                    recovery_errors.append(f'worker recovery: {exc}')
            GitHubFixture.mode = 'normal'
            if repo_id:
                try:
                    cleanup = client.delete(f'/api/repos/{repo_id}')
                    report['cleanup_status'] = cleanup.status_code
                    if cleanup.status_code != 200:
                        recovery_errors.append(f'fixture cleanup HTTP {cleanup.status_code}')
                except Exception as exc:
                    recovery_errors.append(f'fixture cleanup: {exc}')
            fixture.shutdown()
            if recovery_errors:
                report['status'] = 'failed'
                report['recovery_errors'] = recovery_errors
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            faulthandler.cancel_dump_traceback_later()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if recovery_errors:
                raise AssertionError('; '.join(recovery_errors))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--faults', action='store_true')
    verify(parser.parse_args().faults)
