"""Persistent job state, active-task deduplication, retries and outbox dispatch."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import BackgroundJob, ChatSession, Conversation, KnowledgeSourceDocument, ProjectIndexState, Repository
from app.db.session import SessionLocal
from app.services.job_locks import JobResourceBusy, resource_lock

TERMINAL = {"completed", "failed", "cancelled"}
KINDS = {"document.ingest", "repo.sync", "memory.enrich", "project.index"}


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def safe_error(exc: Exception) -> str:
    value = str(getattr(exc, "detail", None) or str(exc) or type(exc).__name__)
    return re.sub(r"(amqps?://)[^@\s]+@", r"\1***@", value)[:1000]


def enqueue_job(db, kind: str, repo_id: uuid.UUID, *, resource_id=None, payload=None, idempotency_key=None):
    if kind not in KINDS:
        raise ValueError(f"Unsupported background job kind: {kind}")
    payload = dict(payload or {})
    identity = str(resource_id) if kind in {"document.ingest", "memory.enrich"} else json.dumps(payload, sort_keys=True, separators=(",", ":"))
    request_key = f"{kind}:{repo_id}:{hashlib.sha256(str(idempotency_key).encode()).hexdigest()}" if idempotency_key else None
    if request_key:
        identity = request_key
    digest = hashlib.sha256(identity.encode()).hexdigest()
    active_key = f"{kind}:{repo_id}:{digest}"
    match = or_(BackgroundJob.active_key == active_key, BackgroundJob.request_key == request_key) if request_key else BackgroundJob.active_key == active_key
    existing = db.query(BackgroundJob).filter(match).first()
    if existing:
        return existing
    job = BackgroundJob(
        kind=kind, repo_id=repo_id, resource_id=resource_id, payload=payload,
        active_key=active_key, request_key=request_key, status="queued", attempts=0,
        max_attempts=max(1, settings.task_max_attempts), available_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = db.query(BackgroundJob).filter(match).first()
        if existing is None:
            raise
        return existing
    return job


def latest_job(db, repo_id, *, kind=None, resource_id=None):
    query = db.query(BackgroundJob).filter(BackgroundJob.repo_id == repo_id)
    if kind:
        query = query.filter(BackgroundJob.kind == kind)
    if resource_id:
        query = query.filter(BackgroundJob.resource_id == resource_id)
    return query.order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc()).first()


def job_payload(job):
    return {
        "id": str(job.id), "job_id": str(job.id), "kind": job.kind,
        "repo_id": str(job.repo_id), "resource_id": str(job.resource_id) if job.resource_id else None,
        "status": job.status, "attempts": job.attempts, "max_attempts": job.max_attempts,
        "result": job.result or {}, "error_message": job.error_message,
        "available_at": job.available_at, "published_at": job.published_at,
        "publish_error": job.publish_error, "started_at": job.started_at,
        "finished_at": job.finished_at, "created_at": job.created_at,
    }


def cancel_jobs(db, repo_id, resource_id=None):
    query = db.query(BackgroundJob).filter(BackgroundJob.repo_id == repo_id, BackgroundJob.status.notin_(TERMINAL))
    if resource_id:
        query = query.filter(BackgroundJob.resource_id == resource_id)
    query.update({"status": "cancelled", "active_key": None, "finished_at": utcnow()}, synchronize_session=False)


def retry_delay(exc, attempt):
    """None means permanent; inspect wrapped GitHub HTTP errors before generic errors."""
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            response = current.response
            code = response.status_code
            limited = code == 429 or (code == 403 and (response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers))
            if limited:
                try:
                    delay = float(response.headers.get("retry-after", 0))
                    reset = float(response.headers.get("x-ratelimit-reset", 0)) - utcnow().timestamp()
                    return min(3600, max(settings.task_retry_seconds, delay, reset))
                except ValueError:
                    return 60
            return max(1, settings.task_retry_seconds) * min(2 ** (attempt - 1), 32) if code >= 500 or code == 408 else None
        current = current.__cause__ or current.__context__
    if isinstance(exc, (ValueError, FileNotFoundError, PermissionError)):
        return None
    if isinstance(exc, HTTPException) and exc.status_code < 500:
        return None
    return max(1, settings.task_retry_seconds) * min(2 ** (attempt - 1), 32)


def _reflect_document(db, job, status):
    if job.kind == "project.index" or (job.kind == "repo.sync" and (job.payload or {}).get("index_project")):
        state = db.query(ProjectIndexState).filter_by(repo_id=job.repo_id).first()
        if state is None:
            state = ProjectIndexState(repo_id=job.repo_id)
            db.add(state)
        state.status = "building" if status == "retrying" else "failed"
        state.error_message = job.error_message
        state.updated_at = utcnow()
        return
    if job.kind == "memory.enrich":
        session = db.get(ChatSession, job.resource_id)
        if session and session.status not in {"sealed", "sealed_with_warnings"}:
            session.status = "enrichment_retrying" if status == "retrying" else "enrichment_failed"
            session.transcript_json = {**(session.transcript_json or {}), "sealing": {"phase": session.status, "errors": [job.error_message]}}
        return
    if job.kind != "document.ingest":
        return
    source = db.get(KnowledgeSourceDocument, job.resource_id)
    if source and source.repo_id == job.repo_id:
        source.status = "queued" if status == "retrying" else "failed"
        source.error_message = job.error_message
        source.updated_at = utcnow()


def process_job(job_id, *, session_factory=SessionLocal, handlers=None):
    job_id = uuid.UUID(str(job_id))
    with session_factory() as db:
        initial = db.get(BackgroundJob, job_id)
        if initial is None:
            return "missing"
        repo_id, bind = initial.repo_id, db.get_bind()
    try:
        with resource_lock(bind, f"repo:{repo_id}"):
            return _process_locked(job_id, session_factory, handlers)
    except JobResourceBusy:
        with session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            if job and job.status not in TERMINAL and job.status != "running":
                job.available_at = utcnow() + timedelta(seconds=2)
                job.published_at = None
                db.commit()
        return "busy"


def _process_locked(job_id, session_factory, handlers):
    token = str(uuid.uuid4())
    with session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        if job is None:
            return "missing"
        if job.status in TERMINAL:
            return job.status
        if db.get(Repository, job.repo_id) is None:
            job.status, job.active_key, job.finished_at = "cancelled", None, utcnow()
            db.commit()
            return "cancelled"
        if aware(job.available_at) > utcnow():
            return "deferred"
        if job.kind == "memory.enrich":
            session = db.get(ChatSession, job.resource_id)
            conv = db.get(Conversation, session.conversation_id) if session else None
            if not session or session.repo_id != job.repo_id or not conv or conv.status != "active":
                job.status, job.active_key, job.finished_at = "cancelled", None, utcnow()
                db.commit()
                return "cancelled"
            if session.status in {"sealed", "sealed_with_warnings"}:
                job.status, job.active_key, job.finished_at = "completed", None, utcnow()
                job.result = {"session_id": str(session.id), "status": session.status}
                job.error_message = None
                db.commit()
                return "completed"
        if job.kind == "document.ingest":
            source = db.get(KnowledgeSourceDocument, job.resource_id)
            if source and source.repo_id == job.repo_id and source.status == "completed":
                # Business commit may survive a crash before the task/ACK commit,
                # including on the final permitted attempt.
                job.status = "completed"
                job.result = {"document_id": str(source.id), "status": "completed", "chunk_count": source.chunk_count}
                job.active_key, job.error_message, job.finished_at = None, None, utcnow()
                db.commit()
                return "completed"
        if job.attempts >= job.max_attempts:
            job.status, job.active_key, job.finished_at = "failed", None, utcnow()
            job.error_message = "任务在处理过程中反复中断，已达到最大尝试次数。"
            _reflect_document(db, job, "failed")
            db.commit()
            return "failed"
        job.status, job.worker_token = "running", token
        job.published_at = utcnow()
        job.attempts += 1
        job.started_at, job.error_message = utcnow(), None
        snapshot = {"id": job.id, "repo_id": job.repo_id, "resource_id": job.resource_id, "kind": job.kind, "payload": dict(job.payload or {})}
        attempt = job.attempts
        db.commit()
    try:
        if handlers is None:
            from app.workers.job_handlers import HANDLERS
            handlers = HANDLERS
        if snapshot["kind"] not in handlers:
            raise ValueError(f"No handler for job kind {snapshot['kind']}")
        result = handlers[snapshot["kind"]](snapshot) or {}
    except Exception as exc:
        delay = retry_delay(exc, attempt)
        with session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            if job is None:
                return "missing"
            if job.worker_token != token or job.status in TERMINAL:
                return job.status
            job.error_message = safe_error(exc)
            if delay is not None and job.attempts < job.max_attempts:
                job.status = "retrying"
                job.available_at = utcnow() + timedelta(seconds=delay)
                job.published_at = None
            else:
                job.status, job.active_key, job.finished_at = "failed", None, utcnow()
            _reflect_document(db, job, job.status)
            db.commit()
            return job.status
    with session_factory() as db:
        job = db.get(BackgroundJob, job_id)
        if job is None:
            return "missing"
        if job.worker_token != token or job.status in TERMINAL:
            return job.status
        job.status, job.result = "completed", result
        job.active_key, job.error_message, job.finished_at = None, None, utcnow()
        db.commit()
    return "completed"


async def publish_pending(publisher, *, session_factory=SessionLocal, limit=50):
    now = utcnow()
    cutoff = now - timedelta(seconds=max(5, settings.task_republish_seconds))
    with session_factory() as db:
        ids = [row.id for row in db.query(BackgroundJob).filter(
            BackgroundJob.status.notin_(TERMINAL), BackgroundJob.available_at <= now,
            or_(BackgroundJob.published_at.is_(None), BackgroundJob.published_at <= cutoff),
        ).order_by(BackgroundJob.created_at).limit(limit)]
        bind = db.get_bind()
    sent = 0
    for job_id in ids:
        try:
            with resource_lock(bind, f"publish:{job_id}"):
                with session_factory() as db:
                    job = db.get(BackgroundJob, job_id)
                    if job is None or job.status in TERMINAL or aware(job.available_at) > now:
                        continue
                    if job.published_at and aware(job.published_at) > cutoff:
                        continue
                    kind, dispatch_attempt = job.kind, job.attempts
                try:
                    await publisher(job_id, kind)
                except Exception as exc:
                    with session_factory() as db:
                        job = db.get(BackgroundJob, job_id)
                        if job:
                            job.publish_error, job.publish_attempts = safe_error(exc), job.publish_attempts + 1
                            db.commit()
                    continue
                with session_factory() as db:
                    job = db.get(BackgroundJob, job_id)
                    if job:
                        if job.attempts == dispatch_attempt and job.status not in TERMINAL:
                            job.published_at = utcnow()
                        job.publish_error = None
                        job.publish_attempts += 1
                        db.commit()
                sent += 1
        except JobResourceBusy:
            continue
    return sent
