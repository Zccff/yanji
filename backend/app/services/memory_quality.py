"""Conservative evidence checks; a model's confidence never substitutes for a source."""
import math
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import ChatMessage, ChatSession, Document, EvidenceItem, Issue, MemoryCandidate, MemoryConflict, MemoryEntry, PullRequest, WorkflowRun
from app.services.memory_lifecycle import entry_status, memory_link_active, now_utc

_SENSITIVE = re.compile(
    r'(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
    r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b|'
    r'\b(?:api[_ -]?key|access[_ -]?token|secret|password|authorization)\s*[:=]\s*[^\s,;]{6,})', re.I)
_SOURCE_KINDS = {'chat_message', 'message', 'chat_session', 'document', 'issue', 'pull_request', 'pr',
                 'workflow_run', 'memory_entry', 'evidence_item', 'memory_note', 'knowledge_file',
                 'project_doc', 'project_manifest', 'project_overview', 'weekly_report', 'team_member'}


def _normalized(text: Any) -> str:
    return ' '.join(str(text or '').casefold().split())


def _source(db: Session, repo_id, citation: dict) -> tuple[str, bool]:
    kind = str(citation.get('type') or citation.get('source_type') or '')
    try:
        identifier = uuid.UUID(str(citation.get('id') or citation.get('source_id') or ''))
    except (ValueError, TypeError):
        return '', False
    model = {'chat_message': ChatMessage, 'message': ChatMessage, 'chat_session': ChatSession,
             'document': Document, 'issue': Issue, 'pull_request': PullRequest, 'pr': PullRequest,
             'workflow_run': WorkflowRun, 'memory_entry': MemoryEntry, 'evidence_item': EvidenceItem}.get(kind)
    if model is None:
        # RAG citations usually identify a stable source group rather than a chunk UUID.
        if kind in {'memory_note', 'knowledge_file', 'project_doc', 'project_manifest', 'project_overview', 'weekly_report', 'team_member'}:
            docs = db.query(Document).filter_by(repo_id=repo_id, source_type=kind, source_id=identifier).limit(30).all()
            docs = [row for row in docs if memory_link_active(db, repo_id, row.meta or {})]
            return '\n'.join(row.content for row in docs)[:100000], bool(docs)
        return '', False
    row = db.get(model, identifier)
    if row is None or row.repo_id != repo_id:
        return '', False
    if isinstance(row, MemoryEntry):
        return row.content, entry_status(row) == 'active' and row.source == 'manual'
    if isinstance(row, Document):
        return row.content, memory_link_active(db, repo_id, row.meta or {})
    if isinstance(row, ChatMessage):
        return (row.content, True) if row.role == 'user' else ('', False)
    if isinstance(row, ChatSession):
        # A generated assistant answer/digest cannot confirm its own extraction.
        message = db.get(ChatMessage, row.user_message_id) if row.user_message_id else None
        valid = bool(message and message.repo_id == repo_id and message.role == 'user')
        return message.content if valid else '', valid
    if isinstance(row, EvidenceItem):
        # Evidence summaries alone are not authoritative: resolve their original source.
        if row.source_type in {'evidence_item', 'thread_memory', 'conversation_memory'}:
            return '', False
        return _source(db, repo_id, {'type': row.source_type, 'id': row.source_id})
    if isinstance(row, WorkflowRun):
        return '\n'.join([row.name or '', row.logs_text or '', row.conclusion or '']), True
    if isinstance(row, (Issue, PullRequest)):
        return '\n'.join([row.title or '', row.body or '']), True
    return row.content, True


def assess_candidate(db: Session, candidate: MemoryCandidate) -> dict[str, Any]:
    meta = candidate.meta or {}
    content = _normalized(candidate.content)
    refs = meta.get('citations') or meta.get('source_refs') or []
    if not isinstance(refs, list):
        refs = []
    evidence = []
    sources_valid = bool(refs)
    quote_matches = bool(refs)
    supported = False
    for citation in refs[:20]:
        if not isinstance(citation, dict):
            sources_valid = quote_matches = False
            continue
        source, valid = _source(db, candidate.repo_id, citation)
        normalized = _normalized(source)
        quote = _normalized(citation.get('quote') or citation.get('excerpt'))
        matches = bool(valid and quote and quote in normalized)
        backs_content = bool(matches and content and content in quote)
        sources_valid = sources_valid and valid
        quote_matches = quote_matches and matches
        supported = supported or backs_content
        kind = str(citation.get('type') or citation.get('source_type') or '')
        try:
            source_id = str(uuid.UUID(str(citation.get('id') or citation.get('source_id') or '')))
        except (TypeError, ValueError):
            source_id = None
        evidence.append({'type': kind if kind in _SOURCE_KINDS else 'unknown', 'id': source_id,
                         'source_exists': valid, 'quote_matches': matches, 'supports_content': backs_content})
    try:
        confidence = float(meta.get('confidence', .7))
        confident = math.isfinite(confidence) and .7 <= confidence <= 1
    except (TypeError, ValueError):
        confident = False
    linked = memory_link_active(db, candidate.repo_id, meta)
    conflict = False
    raw_id = meta.get('memory_entry_id')
    if raw_id:
        try:
            conflict = db.query(MemoryConflict.id).filter_by(repo_id=candidate.repo_id,
                entry_id=uuid.UUID(str(raw_id)), status='pending').first() is not None
        except (TypeError, ValueError):
            conflict = True
    checks = {'nonempty': bool(content), 'not_sensitive': not bool(_SENSITIVE.search((candidate.title or '') + '\n' + (candidate.content or ''))),
              'sources_valid': bool(sources_valid), 'quote_matches': bool(quote_matches),
              'content_supported': supported, 'confidence_sufficient': confident,
              'no_conflict': not conflict, 'active_memory_version': linked}
    rejected = not checks['nonempty'] or not checks['not_sensitive']
    verified = all(checks.values()) and not rejected
    result = {'verdict': 'rejected' if rejected else 'verified' if verified else 'needs_review',
              'score': round(sum(checks.values()) / len(checks), 3) if not rejected else 0.,
              'checks': checks, 'evidence': evidence, 'can_auto_approve': verified}
    candidate.meta = {**meta, 'quality': result, 'quality_assessed_at': now_utc().isoformat()}
    db.flush()
    return result
