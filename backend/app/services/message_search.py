"""Bounded, scope-checked transcript queries; no Python full-history filtering."""

import logging
import re

from sqlalchemy import and_, func, or_, text

from app.db.models import ChatMessage

logger = logging.getLogger(__name__)


def scoped_message_query(db, repo_id, conversation_id, *, include_system=False, before_message_id=None, after_message_id=None):
    query = db.query(ChatMessage).filter(ChatMessage.repo_id == repo_id, ChatMessage.conversation_id == conversation_id)
    if not include_system:
        query = query.filter(ChatMessage.role != 'system')
    subtype = ChatMessage.meta['subtype'].as_string()
    query = query.filter(or_(subtype.is_(None), subtype != 'preserved_after_compact'))
    for cursor_id, before in [(before_message_id, True), (after_message_id, False)]:
        if not cursor_id:
            continue
        cursor = db.query(ChatMessage).filter(ChatMessage.id == cursor_id, ChatMessage.repo_id == repo_id, ChatMessage.conversation_id == conversation_id).one_or_none()
        if cursor is None:
            raise ValueError('cursor_not_found_in_conversation')
        if before:
            query = query.filter(or_(ChatMessage.created_at < cursor.created_at, and_(ChatMessage.created_at == cursor.created_at, ChatMessage.id < cursor.id)))
        else:
            query = query.filter(or_(ChatMessage.created_at > cursor.created_at, and_(ChatMessage.created_at == cursor.created_at, ChatMessage.id > cursor.id)))
    return query


def search_messages(db, repo_id, conversation_id, query='', *, limit=30, offset=0, before_message_id=None, after_message_id=None, include_system=False):
    limit, offset = min(max(int(limit), 1), 100), max(int(offset), 0)
    q = scoped_message_query(db, repo_id, conversation_id, include_system=include_system, before_message_id=before_message_id, after_message_id=after_message_id)
    terms = list(dict.fromkeys(re.findall(r'[a-zA-Z0-9_\-\u4e00-\u9fff]+', query.lower())))[:12]
    if not terms and query.strip():
        terms = [query.strip()[:200]]
    postgres = db.get_bind().dialect.name == 'postgresql'
    if terms:
        clauses = []
        vector = func.to_tsvector('simple', func.coalesce(ChatMessage.content, ''))
        for term in terms:
            literal = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            clauses.append(ChatMessage.content.ilike(f'%{literal}%', escape='\\'))
            if postgres and re.search('[a-zA-Z0-9]', term):
                clauses.append(vector.op('@@')(func.plainto_tsquery('simple', term)))
        q = q.filter(or_(*clauses))
    total = q.count()
    if after_message_id and not before_message_id:
        rows = q.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc()).offset(offset).limit(limit).all()
    else:
        rows = list(reversed(q.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).offset(offset).limit(limit).all()))
    return {'rows': rows, 'total': total, 'search_mode': 'postgres_fulltext_trigram' if postgres else 'sqlite_like'}


def ensure_message_search_indexes(engine):
    if engine.dialect.name != 'postgresql':
        return {'mode': 'sqlite_like'}
    result = {'fulltext': False, 'trigram': False}
    statements = [
        ('fulltext', "CREATE INDEX IF NOT EXISTS ix_chat_messages_fts ON chat_messages USING GIN (to_tsvector('simple', coalesce(content, '')))"),
        ('scope', 'CREATE INDEX IF NOT EXISTS ix_chat_messages_scope_time ON chat_messages (repo_id, conversation_id, created_at, id)'),
        ('extension', 'CREATE EXTENSION IF NOT EXISTS pg_trgm'),
        ('trigram', 'CREATE INDEX IF NOT EXISTS ix_chat_messages_trgm ON chat_messages USING GIN (content gin_trgm_ops)'),
    ]
    for name, statement in statements:
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
            result[name] = True
        except Exception:
            # Managed PostgreSQL may disallow extension installation; SQL LIKE
            # remains correct and the fulltext index is installed independently.
            logger.warning('Could not install transcript search index: %s', name)
    return result
