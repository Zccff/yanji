"""Current durable plan, agent and pending-action projection for UI and prompts."""

import functools
import uuid
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from app.db.models import ActionDraft, AgentTaskRun, AgentWorkflowRun, BackgroundJob
from app.services.context_compression import clip_to_token_budget, dumps_compact


def workflow_for_spec(db, repo_id, conversation_id, spec_id):
    return db.query(AgentWorkflowRun).filter(AgentWorkflowRun.repo_id == repo_id, AgentWorkflowRun.conversation_id == conversation_id, AgentWorkflowRun.spec_json['workflow_id'].as_string() == str(spec_id)).order_by(AgentWorkflowRun.created_at.desc()).first()


def persist_plan(db, repo_id, conversation_id, spec, *, status='pending_confirmation'):
    workflow = workflow_for_spec(db, repo_id, conversation_id, spec.get('workflow_id'))
    if workflow is None:
        workflow = AgentWorkflowRun(repo_id=repo_id, conversation_id=conversation_id, goal=str(spec.get('goal') or ''), spec_json=spec, status=status, metrics={'plan_only': True})
        db.add(workflow)
        db.flush()
    elif workflow.status in {'pending_confirmation', 'running', 'interrupted'}:
        workflow.status, workflow.spec_json = status, spec
    existing = {item.task_id for item in db.query(AgentTaskRun).filter_by(workflow_id=workflow.id)}
    for task in (spec.get('tasks') or [])[:64]:
        if not isinstance(task, dict) or not task.get('id') or str(task['id']) in existing:
            continue
        db.add(AgentTaskRun(workflow_id=workflow.id, task_id=str(task['id']), agent_name=str(task.get('agent_name') or ''), task_type=str(task.get('task_type') or ''), claim_json=task.get('claim') or {}, input_json={'objective': task.get('objective'), 'dependencies': task.get('dependencies') or []}, status='pending'))
    db.flush()
    return workflow


def runtime_projection(db, repo_id, conversation_id):
    workflows = db.query(AgentWorkflowRun).filter(AgentWorkflowRun.repo_id == repo_id, AgentWorkflowRun.conversation_id == conversation_id).order_by(AgentWorkflowRun.created_at.desc()).limit(12).all()
    workflow = next((item for item in workflows if item.status in {'running', 'pending_confirmation', 'interrupted'}), workflows[0] if workflows else None)
    tasks = db.query(AgentTaskRun).filter_by(workflow_id=workflow.id).order_by(AgentTaskRun.task_id).limit(64).all() if workflow else []
    agents = [{'task_id': item.task_id, 'agent_name': item.agent_name, 'status': item.status, 'objective': str((item.input_json or {}).get('objective') or ''), 'error': item.error, 'started_at': item.started_at, 'completed_at': item.completed_at} for item in tasks]
    pending = db.query(ActionDraft).filter(ActionDraft.repo_id == repo_id, ActionDraft.status.in_(['pending_confirmation', 'confirmed', 'executing'])).order_by(ActionDraft.created_at.desc()).limit(12).all()
    actions = []
    for item in pending:
        origin = str((item.meta or {}).get('conversation_id') or '')
        if origin and origin != str(conversation_id):
            continue
        actions.append({'id': str(item.id), 'title': item.title, 'status': item.status, 'risk_level': item.risk_level, 'scope': 'conversation' if origin else 'repository'})
    ranked = db.query(BackgroundJob.id.label('id'), func.row_number().over(
        partition_by=BackgroundJob.resource_id,
        order_by=(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())).label('position')).filter(
            BackgroundJob.repo_id == repo_id, BackgroundJob.kind == 'memory.enrich',
            BackgroundJob.payload['conversation_id'].as_string() == str(conversation_id)).subquery()
    latest = db.query(BackgroundJob).join(ranked, ranked.c.id == BackgroundJob.id).filter(ranked.c.position == 1)
    counts = dict(latest.with_entities(BackgroundJob.status, func.count(BackgroundJob.id)).group_by(BackgroundJob.status).all())
    jobs = latest.order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc()).limit(10).all()
    plan = {'id': str(workflow.id), 'workflow_id': (workflow.spec_json or {}).get('workflow_id'), 'goal': workflow.goal, 'status': workflow.status, 'tasks': agents, 'created_at': workflow.created_at} if workflow else None
    return {'plan': plan, 'agents': agents, 'pending_actions': actions, 'enrichment': {'queued': counts.get('queued', 0)+counts.get('retrying', 0), 'running': counts.get('running', 0), 'failed': counts.get('failed', 0), 'jobs': [{'job_id': str(item.id), 'session_id': str(item.resource_id), 'status': item.status, 'attempts': item.attempts, 'error_message': item.error_message} for item in jobs]}, 'updated_at': (workflow.completed_at or workflow.created_at) if workflow else None}


def runtime_context(db, repo_id, conversation_id, max_tokens=600):
    state = runtime_projection(db, repo_id, conversation_id)
    compact = {**state, 'agents': state['agents'][:12], 'pending_actions': state['pending_actions'][:6]}
    if compact['plan']:
        compact['plan'] = {**compact['plan'], 'tasks': compact['plan']['tasks'][:12]}
    return clip_to_token_budget(dumps_compact(compact), max_tokens)


def tracked_runners(runners, db):
    factory = sessionmaker(bind=db.get_bind())
    def decorate(runner):
        @functools.wraps(runner)
        async def tracked(task, spec, context):
            repo_id, conv_id = context.get('repo_id'), context.get('conversation_id')
            if not repo_id or not conv_id:
                return await runner(task, spec, context)
            def update(status, result=None, error=None):
                with factory() as session:
                    workflow = persist_plan(session, uuid.UUID(str(repo_id)), uuid.UUID(str(conv_id)), spec.model_dump(mode='json'), status='running')
                    row = session.query(AgentTaskRun).filter_by(workflow_id=workflow.id, task_id=task.id).one()
                    row.status = status
                    if status == 'running': row.started_at = datetime.now(timezone.utc)
                    else: row.completed_at = datetime.now(timezone.utc)
                    if result is not None: row.result_json = result.model_dump(mode='json') if hasattr(result, 'model_dump') else dict(result)
                    if error: row.error = str(error)[:400]
                    session.commit()
            update('running')
            try:
                result = await runner(task, spec, context)
                update(getattr(result, 'status', None) or (result.get('status') if isinstance(result, dict) else 'success'), result=result)
                return result
            except BaseException as exc:
                update('error', error=type(exc).__name__)
                raise
        return tracked
    return {name: decorate(runner) for name, runner in runners.items()}


def recover_interrupted_workflows(db):
    ids = [item.id for item in db.query(AgentWorkflowRun.id).filter(AgentWorkflowRun.status == 'running')]
    if ids:
        db.query(AgentTaskRun).filter(AgentTaskRun.workflow_id.in_(ids), AgentTaskRun.status == 'running').update({'status': 'error', 'error': 'API 进程中断，需重新执行或检查当前状态。'}, synchronize_session=False)
        db.query(AgentWorkflowRun).filter(AgentWorkflowRun.id.in_(ids)).update({'status': 'interrupted'}, synchronize_session=False)
        db.commit()
