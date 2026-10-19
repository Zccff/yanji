"""Versioned, scoped memories. All writes join the caller's transaction."""
import hashlib
import math
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Conversation, ConversationMemory, MemoryConflict, MemoryEntry, MemoryKeyClaim, MemoryRevision, Repository
from app.schemas.memory_manage import EntryCreate, EntryPatch

TTL_DAYS = {'fact': 180, 'decision': 180, 'task': 14, 'preference': 365, 'repo_context': 90, 'summary': 30}
FIELDS = {'facts': 'fact', 'decisions': 'decision', 'tasks': 'task', 'user_preferences': 'preference', 'repo_context': 'repo_context'}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _savepoint(db: Session):
    # sqlite3's legacy transaction mode doesn't BEGIN for SELECT/SAVEPOINT.
    # Explicitly start its outer transaction so releasing a savepoint cannot
    # commit a memory independently of the caller's transcript transaction.
    connection = db.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    with db.begin_nested():
        yield


def ensure_scope(db: Session, repo_id: uuid.UUID, conversation_id: uuid.UUID | None = None):
    if db.get(Repository, repo_id) is None:
        raise HTTPException(404, 'repository_not_found')
    if conversation_id is not None:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.repo_id != repo_id or conversation.status == 'deleted':
            raise HTTPException(404, 'conversation_not_found')


def semantic_key(content: str, key: str | None = None) -> str:
    if key and key.strip():
        return ' '.join(key.strip().lower().split())[:240]
    return 'content:' + hashlib.sha256(' '.join(content.lower().split()).encode()).hexdigest()


def entry_status(entry: MemoryEntry, now: datetime | None = None) -> str:
    if entry.status != 'active':
        return entry.status
    return 'expired' if entry.expires_at and utc(entry.expires_at) <= utc(now or now_utc()) else 'active'


def entry_score(entry: MemoryEntry, now: datetime | None = None) -> float:
    at = utc(now or now_utc())
    if entry_status(entry, at) != 'active':
        return 0.
    age = max(0., (at - utc(entry.last_confirmed_at or entry.created_at)).total_seconds() / 86400)
    decay = 1. if entry.pinned else 2 ** (-age / max(entry.half_life_days, .001))
    return entry.importance * entry.confidence * decay


def entry_payload(entry: MemoryEntry, now: datetime | None = None) -> dict[str, Any]:
    return {
        'id': str(entry.id), 'repo_id': str(entry.repo_id), 'conversation_id': str(entry.conversation_id),
        'kind': entry.kind, 'key': entry.key, 'title': entry.title, 'content': entry.content,
        'status': entry_status(entry, now), 'version': entry.version, 'importance': entry.importance,
        'confidence': entry.confidence, 'pinned': entry.pinned, 'half_life_days': entry.half_life_days,
        'expires_at': utc(entry.expires_at).isoformat() if entry.expires_at else None,
        'created_at': utc(entry.created_at).isoformat(), 'updated_at': utc(entry.updated_at).isoformat(),
        'last_confirmed_at': utc(entry.last_confirmed_at).isoformat(), 'score': entry_score(entry, now),
        'source': entry.source, 'metadata': entry.meta or {},
    }


def _revision(db: Session, entry: MemoryEntry, operation: str):
    db.add(MemoryRevision(repo_id=entry.repo_id, conversation_id=entry.conversation_id, entry_id=entry.id,
                          version=entry.version, operation=operation, snapshot=entry_payload(entry)))
    db.flush()


def get_entry(db: Session, repo_id: uuid.UUID, entry_id: uuid.UUID) -> MemoryEntry:
    entry = db.query(MemoryEntry).filter_by(id=entry_id, repo_id=repo_id).populate_existing().one_or_none()
    if entry is None:
        raise HTTPException(404, 'memory_entry_not_found')
    ensure_scope(db, repo_id, entry.conversation_id)
    return entry


def _validate(schema, payload):
    try:
        return schema.model_validate(payload).model_dump(exclude_unset=True)
    except ValidationError:
        # Avoid echoing input values (which can include accidentally pasted credentials).
        raise HTTPException(422, 'invalid_memory_fields') from None


def create_entry(db: Session, repo_id: uuid.UUID, payload: dict, *, source: str = 'manual', metadata: dict | None = None,
                 confirmed_at: datetime | None = None) -> MemoryEntry:
    values = _validate(EntryCreate, payload)
    ensure_scope(db, repo_id, values['conversation_id'])
    if source == 'manual':
        # A first write must not turn the existence guard into a migration skip.
        _import_legacy(db, repo_id, values['conversation_id'])
    at = confirmed_at or now_utc()
    kind = values['kind']
    ttl = values.pop('ttl_days', TTL_DAYS[kind])
    values.update(key=semantic_key(values['content'], values.get('key')), title=values.get('title') or values['content'][:100])
    entry = MemoryEntry(repo_id=repo_id, source=source, meta=metadata or {}, created_at=at, updated_at=at,
                        last_confirmed_at=at, expires_at=at + timedelta(days=ttl) if ttl is not None else None,
                        **values)
    try:
        with _savepoint(db):
            db.add(entry)
            db.flush()
            _claim_key(db, entry, kind, entry.key)
            _revision(db, entry, 'create' if source == 'manual' else 'import' if source == 'legacy' else 'extract')
    except IntegrityError:
        raise HTTPException(409, 'memory_semantic_key_exists') from None
    return entry


def _change(db: Session, entry: MemoryEntry, expected: int, values: dict, operation: str) -> MemoryEntry:
    """Compare-and-swap is authoritative even if this ORM identity is stale."""
    values = {**values, 'version': expected + 1, 'updated_at': now_utc()}
    try:
        with _savepoint(db):
            _claim_key(db, entry, values.get('kind', entry.kind), values.get('key', entry.key))
            changed = db.execute(update(MemoryEntry).where(MemoryEntry.id == entry.id,
                MemoryEntry.repo_id == entry.repo_id, MemoryEntry.version == expected).values(**values),
                execution_options={'synchronize_session': False}).rowcount
            if changed != 1:
                raise HTTPException(409, 'memory_version_conflict')
            db.refresh(entry)
            _revision(db, entry, operation)
    except IntegrityError:
        raise HTTPException(409, 'memory_semantic_key_exists') from None
    return entry


def _claim_key(db, entry, kind, key):
    # Legacy JSON aliases are checked while transitioning to normalized claims.
    # Every identity writer then inserts into the same unique database index,
    # which arbitrates competing transactions without a check-then-write race.
    other = _entry_query(db, entry.repo_id, entry.conversation_id).filter(
        MemoryEntry.id != entry.id, MemoryEntry.kind == kind,
        or_(MemoryEntry.key == key, MemoryEntry.meta['semantic_keys'][key].as_boolean().is_(True))).first()
    if other:
        raise HTTPException(409, 'memory_semantic_key_exists')
    claim = db.query(MemoryKeyClaim).filter_by(conversation_id=entry.conversation_id, kind=kind, key=key).first()
    if claim:
        if claim.entry_id != entry.id:
            raise HTTPException(409, 'memory_semantic_key_exists')
        return
    db.add(MemoryKeyClaim(repo_id=entry.repo_id, conversation_id=entry.conversation_id,
                          entry_id=entry.id, kind=kind, key=key))
    db.flush()


def _close_conflicts(db, entry, resolution='superseded'):
    db.query(MemoryConflict).filter_by(entry_id=entry.id, status='pending').update(
        {'status': 'resolved', 'resolution': resolution, 'resolved_at': now_utc()}, synchronize_session='fetch')


def update_entry(db: Session, repo_id: uuid.UUID, entry_id: uuid.UUID, payload: dict) -> MemoryEntry:
    values = _validate(EntryPatch, payload)
    expected = values.pop('expected_version')
    entry = get_entry(db, repo_id, entry_id)
    if 'ttl_days' in values:
        ttl = values.pop('ttl_days')
        values['expires_at'] = now_utc() + timedelta(days=ttl) if ttl is not None else None
    if 'key' in values:
        values['key'] = semantic_key(values.get('content', entry.content), values['key'])
    if 'title' in values and values['title'] is None:
        values['title'] = ''
    values.update(source='manual', last_confirmed_at=now_utc())
    if entry.status == 'conflict':
        values['status'] = 'active'
    entry = _change(db, entry, expected, values, 'edit')
    _close_conflicts(db, entry)
    return entry


def delete_entry(db: Session, repo_id: uuid.UUID, entry_id: uuid.UUID, expected_version: int) -> MemoryEntry:
    entry = _change(db, get_entry(db, repo_id, entry_id), expected_version, {'status': 'deleted', 'source': 'manual'}, 'delete')
    _close_conflicts(db, entry)
    return entry


def restore_entry(db: Session, repo_id: uuid.UUID, entry_id: uuid.UUID, expected_version: int) -> MemoryEntry:
    entry = get_entry(db, repo_id, entry_id)
    if entry.status != 'deleted':
        raise HTTPException(409, 'memory_entry_not_deleted')
    return _change(db, entry, expected_version, {'status': 'active', 'source': 'manual', 'last_confirmed_at': now_utc()}, 'restore')


def entry_revisions(db: Session, repo_id: uuid.UUID, entry_id: uuid.UUID) -> list[dict]:
    get_entry(db, repo_id, entry_id)
    rows = db.query(MemoryRevision).filter_by(repo_id=repo_id, entry_id=entry_id).order_by(MemoryRevision.version).all()
    return [{'version': row.version, 'operation': row.operation, 'snapshot': row.snapshot, 'created_at': row.created_at} for row in rows]


def revert_entry(db: Session, repo_id: uuid.UUID, entry_id: uuid.UUID, expected_version: int, target_version: int) -> MemoryEntry:
    entry = get_entry(db, repo_id, entry_id)
    revision = db.query(MemoryRevision).filter_by(repo_id=repo_id, entry_id=entry_id, version=target_version).one_or_none()
    if revision is None:
        raise HTTPException(404, 'memory_revision_not_found')
    snap = revision.snapshot
    values = {key: snap[key] for key in ('kind', 'key', 'title', 'content', 'importance', 'confidence', 'pinned', 'half_life_days')}
    values.update(status='deleted' if snap['status'] == 'deleted' else 'active', source='manual', last_confirmed_at=now_utc(),
                  expires_at=datetime.fromisoformat(snap['expires_at']) if snap.get('expires_at') else None)
    entry = _change(db, entry, expected_version, values, 'revert')
    _close_conflicts(db, entry)
    return entry


def list_conflicts(db: Session, repo_id: uuid.UUID, conversation_id: uuid.UUID | None = None) -> list[dict]:
    ensure_scope(db, repo_id, conversation_id)
    query = db.query(MemoryConflict).filter_by(repo_id=repo_id, status='pending')
    if conversation_id:
        query = query.filter_by(conversation_id=conversation_id)
    rows = query.order_by(MemoryConflict.created_at.desc()).limit(500).all()
    ids = {row.entry_id for row in rows}
    versions = dict(db.query(MemoryEntry.id, MemoryEntry.version).filter(MemoryEntry.repo_id == repo_id, MemoryEntry.id.in_(ids)).all()) if ids else {}
    return [{'id': row.id, 'entry_id': row.entry_id, 'conversation_id': row.conversation_id,
             'entry_version': versions.get(row.entry_id, row.entry_version), 'current_content': row.current_content, 'proposed_content': row.proposed_content,
             'status': row.status, 'created_at': row.created_at, 'metadata': row.meta or {}}
            for row in rows]


def resolve_conflict(db: Session, repo_id: uuid.UUID, conflict_id: uuid.UUID, resolution: str, expected_version: int) -> MemoryEntry:
    row = db.query(MemoryConflict).filter_by(repo_id=repo_id, id=conflict_id).populate_existing().with_for_update().one_or_none()
    if row is None:
        raise HTTPException(404, 'memory_conflict_not_found')
    if resolution not in {'keep', 'replace'}:
        raise HTTPException(422, 'invalid_resolution')
    if row.status != 'pending':
        raise HTTPException(409, 'memory_conflict_already_resolved')
    entry = get_entry(db, repo_id, row.entry_id)
    entry = _change(db, entry, expected_version,
        {'content': row.proposed_content if resolution == 'replace' else entry.content, 'status': 'active',
         'source': 'manual', 'last_confirmed_at': now_utc()}, 'resolve_' + resolution)
    _close_conflicts(db, entry, resolution)
    return entry


def _entry_query(db, repo_id, conversation_id=None):
    query = db.query(MemoryEntry).filter_by(repo_id=repo_id)
    return query.filter_by(conversation_id=conversation_id) if conversation_id else query


def memory_stats(db, repo_id, conversation_id=None, now=None):
    at = now or now_utc()
    query = _entry_query(db, repo_id, conversation_id)
    counts = dict(query.with_entities(MemoryEntry.status, func.count(MemoryEntry.id)).group_by(MemoryEntry.status).all())
    expired = query.filter(MemoryEntry.status == 'active', MemoryEntry.expires_at <= at).count()
    return {'active': counts.get('active', 0) - expired, 'expired': expired, 'deleted': counts.get('deleted', 0),
            'conflicts': counts.get('conflict', 0), 'total': sum(counts.values())}


def list_entries(db, repo_id, conversation_id=None, q='', status='active', kind=None, limit=50, offset=0):
    ensure_scope(db, repo_id, conversation_id)
    if conversation_id:
        _import_legacy(db, repo_id, conversation_id)
    query = _entry_query(db, repo_id, conversation_id)
    at = now_utc()
    if status == 'active':
        query = query.filter(MemoryEntry.status == 'active', or_(MemoryEntry.expires_at.is_(None), MemoryEntry.expires_at > at))
    elif status == 'expired':
        query = query.filter(MemoryEntry.status == 'active', MemoryEntry.expires_at <= at)
    elif status in {'deleted', 'conflict'}:
        query = query.filter_by(status=status)
    elif status != 'all':
        raise HTTPException(422, 'invalid_memory_status')
    if kind:
        if kind not in TTL_DAYS:
            raise HTTPException(422, 'invalid_memory_kind')
        query = query.filter_by(kind=kind)
    if q.strip():
        escaped = q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        query = query.filter(or_(MemoryEntry.content.ilike(f'%{escaped}%', escape='\\'), MemoryEntry.title.ilike(f'%{escaped}%', escape='\\')))
    total = query.count()
    rows = query.order_by(MemoryEntry.updated_at.desc(), MemoryEntry.id).offset(max(offset, 0)).limit(min(max(limit, 1), 200)).all()
    return {'items': [entry_payload(row, at) for row in rows], 'total': total, 'stats': memory_stats(db, repo_id, conversation_id, at)}


def extraction_items(memory_update: dict) -> list[dict]:
    if not isinstance(memory_update, dict):
        return []
    structured = memory_update.get('_memory_items')
    if isinstance(structured, list):
        values = [item for item in structured[:200] if isinstance(item, dict)]
    else:
        values = [{'kind': kind, 'content': value, 'citations': memory_update.get('citations') or []}
                  for field, kind in FIELDS.items() if isinstance(memory_update.get(field), list)
                  for value in memory_update[field][:40] if isinstance(value, str)]
    output = []
    for value in values:
        kind = value.get('kind')
        content = value.get('content')
        content = content.strip()[:4000] if isinstance(content, str) else ''
        if not isinstance(kind, str) or kind not in TTL_DAYS or kind == 'summary' or not content:
            continue
        try:
            confidence = float(value.get('confidence', .7))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            continue
        citations = value.get('citations')
        citations = [item for item in citations[:20] if isinstance(item, dict)] if isinstance(citations, list) else []
        output.append({'kind': kind, 'key': semantic_key(content, str(value.get('key') or '')),
                       'content': content, 'title': str(value.get('title') or content[:100])[:500],
                       'confidence': confidence, 'citations': citations})
    return output


def _matching_entry(db, repo_id, conversation_id, kind, content, key=None, *, lock=False):
    """Shared semantic/current/historical lookup for sync and promotion links."""
    query = _entry_query(db, repo_id, conversation_id)
    if lock:
        query = query.with_for_update(of=MemoryEntry)
    identity = semantic_key(content, key)
    entry = query.join(MemoryKeyClaim, MemoryKeyClaim.entry_id == MemoryEntry.id).filter(
        MemoryKeyClaim.conversation_id == conversation_id, MemoryKeyClaim.kind == kind, MemoryKeyClaim.key == identity).first()
    scoped_kind = query.filter_by(kind=kind)
    if entry is None:
        entry = scoped_kind.filter(MemoryEntry.key == identity).first()
    if entry is None:
        entry = scoped_kind.filter(MemoryEntry.meta['semantic_keys'][identity].as_boolean().is_(True)).first()
    if entry is None:
        entry = scoped_kind.filter(MemoryEntry.content == content).first()
    if entry is None:
        entry = (query.join(MemoryRevision, MemoryRevision.entry_id == MemoryEntry.id)
                 .filter(MemoryRevision.snapshot['content'].as_string() == content,
                         MemoryRevision.snapshot['kind'].as_string() == kind)
                 .order_by(MemoryRevision.version.desc()).first())
    return entry


def _remember_semantic_key(db, entry, key):
    # Keep the primary key chosen by a user. A JSON object allows a portable
    # parameterized lookup for aliases on both SQLite and PostgreSQL.
    try:
        with _savepoint(db):
            _claim_key(db, entry, entry.kind, key)
            aliases = dict((entry.meta or {}).get('semantic_keys') or {})
            if key != entry.key and not aliases.get(key):
                aliases[key] = True
                _change(db, entry, entry.version, {'meta': {**(entry.meta or {}), 'semantic_keys': aliases}}, 'alias')
    except IntegrityError:
        raise HTTPException(409, 'memory_semantic_key_exists') from None


def sync_memory_entries(db: Session, repo_id, conversation_id, memory_update, session_id=None) -> list[MemoryEntry]:
    if repo_id is None or conversation_id is None:
        return []
    ensure_scope(db, repo_id, conversation_id)
    _import_legacy(db, repo_id, conversation_id)
    touched = []
    for item in extraction_items(memory_update):
        entry = _matching_entry(db, repo_id, conversation_id, item['kind'], item['content'], item['key'], lock=True)
        metadata = {'citations': item['citations'][:20], 'session_id': str(session_id) if session_id else None}
        if entry is None:
            try:
                entry = create_entry(db, repo_id, {key: value for key, value in item.items() if key != 'citations'} |
                                     {'conversation_id': conversation_id}, source='extracted', metadata=metadata)
            except HTTPException as exc:
                if exc.status_code == 409:
                    continue
                raise
            touched.append(entry)
            continue
        _remember_semantic_key(db, entry, item['key'])
        if entry.status == 'deleted' or entry.content == item['content']:
            # Repeated model extraction is not a new explicit confirmation.
            continue
        pending = db.query(MemoryConflict.id).filter_by(entry_id=entry.id, status='pending', proposed_content=item['content']).first()
        if pending:
            continue
        entry = _change(db, entry, entry.version, {'status': 'conflict'}, 'conflict')
        db.add(MemoryConflict(repo_id=repo_id, conversation_id=conversation_id, entry_id=entry.id, entry_version=entry.version,
                              current_content=entry.content, proposed_content=item['content'], meta=metadata))
        db.flush()
        touched.append(entry)
    return touched


def _import_legacy(db, repo_id, conversation_id):
    # Existence includes deleted rows: absence is the only migration trigger.
    if _entry_query(db, repo_id, conversation_id).first():
        return
    legacy = db.query(ConversationMemory).filter_by(repo_id=repo_id, conversation_id=conversation_id).first()
    if legacy is None:
        return
    update = {field: getattr(legacy, field) for field in FIELDS}
    update['citations'] = legacy.citations or []
    for item in extraction_items(update):
        try:
            create_entry(db, repo_id, {key: value for key, value in item.items() if key != 'citations'} |
                         {'conversation_id': conversation_id}, source='legacy', metadata={'citations': item['citations'][:20]},
                         confirmed_at=utc(legacy.updated_at))
        except HTTPException as exc:
            if exc.status_code != 409:
                raise


def managed_memory_context(db: Session, repo_id, conversation_id, query='', max_tokens=700) -> dict:
    if repo_id is None or conversation_id is None:
        return {'text': '', 'items': [], 'stats': {}, 'managed': False}
    ensure_scope(db, repo_id, conversation_id)
    _import_legacy(db, repo_id, conversation_id)
    at = now_utc()
    stats = memory_stats(db, repo_id, conversation_id, at)
    active = _entry_query(db, repo_id, conversation_id).filter(MemoryEntry.status == 'active',
        or_(MemoryEntry.expires_at.is_(None), MemoryEntry.expires_at > at))
    terms = set(re.findall(r'\w+', query.lower()))
    # Bound ORM materialization, while letting a matching query find older entries.
    if terms:
        predicates = [MemoryEntry.content.ilike('%' + term.replace('%', '').replace('_', '')[:80] + '%') for term in list(terms)[:12]]
        matches = active.filter(or_(*predicates)).order_by(MemoryEntry.pinned.desc(), MemoryEntry.updated_at.desc()).limit(100).all()
    else:
        matches = []
    recent = active.order_by(MemoryEntry.pinned.desc(), MemoryEntry.importance.desc(), MemoryEntry.last_confirmed_at.desc()).limit(250).all()
    rows = {row.id: row for row in matches + recent}.values()
    def rank(row):
        text = (row.title + ' ' + row.content).lower()
        relevance = sum(term in text for term in terms) / max(len(terms), 1)
        return (entry_score(row, at) * (1 + 2 * relevance), str(row.id))
    # One Unicode code point per token is conservative for mixed Chinese/English.
    budget = max(0, min(int(max_tokens), 4000))
    lines, items = [], []
    for row in sorted(rows, key=rank, reverse=True):
        reference = f'[memory:{row.id}@v{row.version}]'
        line = f'{reference} {row.kind}: {row.content}'
        remaining = budget - sum(len(part) + 1 for part in lines)
        if remaining <= len(reference) + len(row.kind) + 5:
            continue
        if len(line) > remaining:
            line = line[:remaining - 1] + '…'
        lines.append(line)
        items.append(entry_payload(row, at))
        if len(items) >= 30:
            break
    return {'text': '\n'.join(lines), 'items': items, 'stats': stats, 'managed': bool(stats['total'])}


def candidate_memory_link(db: Session, repo_id, conversation_id, kind: str, content: str, key: str | None = None) -> dict:
    if conversation_id is None:
        return {}
    entry = _matching_entry(db, repo_id, conversation_id, kind, content, key)
    if entry is None:
        return {}
    version = entry.version
    if entry.content != content:
        historical = (db.query(MemoryRevision).filter_by(repo_id=repo_id, entry_id=entry.id)
                      .filter(MemoryRevision.snapshot['content'].as_string() == content)
                      .order_by(MemoryRevision.version.desc()).first())
        # An old claim stays tied to its historical version; a new unaccepted
        # proposal gets an invalid version rather than the unrelated current one.
        version = historical.version if historical else 0
    return {'memory_entry_id': str(entry.id), 'memory_entry_version': version}


def memory_link_active(db: Session, repo_id, metadata: dict, now: datetime | None = None) -> bool:
    """Shared candidate/RAG guard; unlinked legacy documents remain eligible."""
    raw_id = metadata.get('memory_entry_id')
    if not raw_id:
        return True
    try:
        entry = db.get(MemoryEntry, uuid.UUID(str(raw_id)))
        version = int(metadata.get('memory_entry_version', 0))
    except (ValueError, TypeError):
        return False
    return bool(entry and entry.repo_id == repo_id and entry.version == version and entry_status(entry, now) == 'active')
