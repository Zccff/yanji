"""Real PostgreSQL/RabbitMQ/Worker/checkout race check, using only local test Git."""
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
os.chdir(ROOT/'backend')
import httpx
from app.db.models import BackgroundJob, CodeSymbol, Document, Repository
from app.db.session import SessionLocal, engine
from app.services.job_locks import resource_lock
from app.services.task_queue import process_job

marker = 'Project index pipeline acceptance ' + uuid.uuid4().hex
folder = ROOT/'.data/runtime'/('index-fixture-'+uuid.uuid4().hex)
upstream, checkout = folder/'upstream', folder/'checkout'
upstream.mkdir(parents=True)
def git(*args):
    return subprocess.run(['git',*args],cwd=upstream,check=True,capture_output=True,encoding='utf-8',errors='replace')
git('init','-b','main')
git('config','user.email','fixture@example.invalid')
git('config','user.name','Index acceptance')
(upstream/'README.md').write_text('# Index acceptance\n\nCode checkout must finish before indexing.\n',encoding='utf-8')
(upstream/'sample.py').write_text('def ready():\n    return True\n',encoding='utf-8')
git('add','README.md','sample.py')
git('commit','-m','local fixture')
with SessionLocal() as db:
    name=uuid.uuid4().hex
    repo=Repository(owner='index-acceptance',name=name,full_name=f'index-acceptance/{name}',provider='github',
                    description=marker,local_path=str(checkout),checkout_mode='managed',
                    clone_url=upstream.as_uri(),default_branch='main')
    db.add(repo);db.commit();rid=repo.id
report={'started_at':datetime.now(timezone.utc).isoformat(),'fixture_repo_id':str(rid),'checks':{}}
with httpx.Client(base_url='http://127.0.0.1:8000',timeout=30) as client:
    try:
        with resource_lock(engine,f'repo:{rid}'):
            response=client.post(f'/api/repos/{rid}/sync',json={'sync_issues':False,'sync_pull_requests':False,'sync_workflow_runs':False,'limit':1})
            response.raise_for_status();sync_id=response.json()['job_id']
            def request_scan(_):
                response=httpx.post(f'http://127.0.0.1:8000/api/repos/{rid}/memory-index/scan',json={},timeout=30)
                response.raise_for_status();return response.json()['job_id']
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                index_ids=list(pool.map(request_scan,range(3)))
            assert len(set(index_ids))==1,index_ids
            assert not checkout.exists()
            state=client.get(f'/api/repos/{rid}/memory-index/state').json()
            assert state['status']=='building' and state['error_message'] is None,state
            report['checks']['early_and_concurrent_scan_queued_without_missing_path_failure']=True
        job_ids=[sync_id,index_ids[0]]
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            jobs=[]
            for jid in job_ids:
                response=client.get(f'/api/repos/{rid}/jobs/{jid}');response.raise_for_status();jobs.append(response.json())
            assert not any(job['status'] in {'failed','cancelled'} for job in jobs),jobs
            if all(job['status']=='completed' for job in jobs):break
            time.sleep(1)
        else:raise AssertionError('Pipeline deadline exceeded')
        assert jobs[0]['result']['project_index']['status']=='ready'
        response=client.get(f'/api/repos/{rid}/memory-index/state');response.raise_for_status();state=response.json()
        assert state['status']=='ready' and state['docs_indexed']>=2,state
        assert (checkout/'.git').exists()
        report['checks']['sync_checkout_automatic_index_and_manual_index_complete']=True
        with SessionLocal() as db:
            first_ids={row.id for row in db.query(Document).filter_by(repo_id=rid)}
            assert db.query(CodeSymbol).filter_by(repo_id=rid).count()>0
        assert process_job(index_ids[0])=='completed'
        with SessionLocal() as db:
            assert {row.id for row in db.query(Document).filter_by(repo_id=rid)}==first_ids
            report['checks']['terminal_redelivery_does_not_reindex']=True
        report['jobs']=[{key:job.get(key) for key in ['id','kind','status','attempts','result']} for job in jobs]
        report['index']={key:state.get(key) for key in ['status','docs_indexed','docs_total','error_message']}
        report['passed']=True
    except Exception as exc:
        report['passed']=False;report['error']=type(exc).__name__+': '+str(exc)[:1000]
        raise
    finally:
        with SessionLocal() as db:
            stored=db.get(Repository,rid)
            assert stored and stored.description==marker and stored.owner=='index-acceptance'
            active=db.query(BackgroundJob).filter(BackgroundJob.repo_id==rid,BackgroundJob.status.in_(['queued','running','retrying'])).count()
        if not active:
            response=client.delete(f'/api/repos/{rid}')
            report['cleanup_status']=response.status_code
            response.raise_for_status()
        else:
            report['cleanup_status']='fixture retained for failed active-job diagnosis'
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        (ROOT/'.data/runtime/project-index-pipeline-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2,default=str),flush=True)
