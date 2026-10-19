"""Apply lifecycle validity to derived evidence and stale compaction summaries."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_

from app.db.models import MemoryEntry, MemoryRevision
from app.services.memory_lifecycle import entry_score, entry_status


def filter_linked_memories(db, repo_id, items, *, decay=False):
    repo_id = uuid.UUID(str(repo_id))
    ids = set()
    for item in items:
        try:
            raw = (item.get('metadata') or {}).get('memory_entry_id')
            if raw: ids.add(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            pass
    entries = {str(row.id): row for row in db.query(MemoryEntry).filter(MemoryEntry.repo_id == repo_id, MemoryEntry.id.in_(ids)).all()} if ids else {}
    result = []
    for item in items:
        meta = item.get('metadata') or {}
        raw = meta.get('memory_entry_id')
        if not raw:
            result.append(item)
            continue
        entry = entries.get(str(raw))
        try:
            valid = entry is not None and entry_status(entry) == 'active' and int(meta.get('memory_entry_version', 0)) == entry.version
        except (ValueError, TypeError):
            valid = False
        if not valid:
            continue
        if decay:
            weight = entry_score(entry)
            item = {**item, 'score': float(item.get('score') or 0) * weight,
                    'retrieval': {**(item.get('retrieval') or {}), 'memory_decay': weight}}
        result.append(item)
    return sorted(result, key=lambda item: float(item.get('score') or 0), reverse=True) if decay else result


def boundary_memory_is_current(db, repo_id, conversation_id, boundary):
    if not boundary or not boundary.created_at:
        return True
    # Boundaries have no per-fact provenance. After a correction, or while any
    # fact is retired, even a newly generated summary of old transcript could
    # repeat it. Prefer current canonical memory and exact transcript recovery.
    changed = db.query(MemoryRevision.id).join(MemoryEntry, MemoryRevision.entry_id == MemoryEntry.id).filter(
        MemoryEntry.repo_id == repo_id, MemoryEntry.conversation_id == conversation_id,
        MemoryRevision.operation.notin_(['extract', 'import', 'create', 'alias']),
    ).first()
    expired = db.query(MemoryEntry.id).filter(
        MemoryEntry.repo_id == repo_id, MemoryEntry.conversation_id == conversation_id,
        or_(MemoryEntry.status != 'active', MemoryEntry.expires_at <= datetime.now(timezone.utc)),
    ).first()
    return not changed and not expired
