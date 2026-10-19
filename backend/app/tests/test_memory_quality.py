import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import ChatMessage, Conversation, Document, MemoryCandidate, Repository
from app.db.session import Base


@pytest.fixture
def scope():
    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner='a', name='b', full_name='a/b')
        other = Repository(owner='a', name='c', full_name='a/c')
        db.add_all([repo, other])
        db.flush()
        conv = Conversation(repo_id=repo.id)
        db.add(conv)
        db.flush()
        yield db, repo, conv, other


def candidate(scope, content='Use SQLite', **meta):
    db, repo, conv, _ = scope
    row = MemoryCandidate(repo_id=repo.id, conversation_id=conv.id, kind='fact', title='Database', content=content, meta=meta)
    db.add(row)
    db.flush()
    return row


def assess(scope, row):
    from app.services.memory_quality import assess_candidate
    return assess_candidate(scope[0], row)


def test_missing_or_spoofed_sources_cannot_autoapprove(scope):
    row = candidate(scope, confidence=1, score=1)
    assert assess(scope, row)['verdict'] == 'needs_review'
    row.meta = {'citations': [{'type': 'chat_message', 'id': str(uuid.uuid4()), 'quote': 'Use SQLite'}], 'confidence': 1}
    result = assess(scope, row)
    assert result['can_auto_approve'] is False and result['checks']['sources_valid'] is False


def test_real_source_quote_and_content_support_are_required(scope):
    db, repo, conv, _ = scope
    message = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Decision: Use SQLite for local development.')
    db.add(message)
    db.flush()
    row = candidate(scope, citations=[{'type': 'chat_message', 'id': str(message.id), 'quote': 'Use SQLite'}], confidence=.9)
    result = assess(scope, row)
    assert result['verdict'] == 'verified' and result['can_auto_approve']
    row.content = 'Deploy to production'
    assert assess(scope, row)['can_auto_approve'] is False
    row.content = 'Use SQLite'
    row.meta = {**row.meta, 'citations': [{'type': 'chat_message', 'id': str(message.id), 'quote': 'not in source'}]}
    assert assess(scope, row)['can_auto_approve'] is False


def test_cross_repo_empty_and_sensitive_candidates_fail(scope):
    db, _, _, other = scope
    message = ChatMessage(repo_id=other.id, role='user', content='Use SQLite')
    db.add(message)
    db.flush()
    row = candidate(scope, citations=[{'type': 'chat_message', 'id': str(message.id), 'quote': 'Use SQLite'}])
    assert assess(scope, row)['checks']['sources_valid'] is False
    row.content = ''
    assert assess(scope, row)['verdict'] == 'rejected'
    row.content = 'api_key=sk-testabcdefghijklmnopqrstuvwxyz123456'
    assert assess(scope, row)['verdict'] == 'rejected'
    assert 'sk-test' not in str(assess(scope, row))


def test_manual_approval_is_idempotent_and_stable_source(scope, monkeypatch):
    from app.services import memory_hub
    db, repo, _, _ = scope
    row = candidate(scope)
    def add(db, repo_id, source_type, source_id, title, content, metadata, **kwargs):
        doc = Document(repo_id=repo_id, source_type=source_type, source_id=source_id, title=title, content=content, meta=metadata)
        db.add(doc)
        db.flush()
        return doc
    monkeypatch.setattr(memory_hub, 'add_document', add)
    _, first = memory_hub.approve_memory_candidate(db, repo, row.id)
    assert first.source_id == uuid.uuid5(uuid.NAMESPACE_URL, f'memory-candidate:{row.id}')
    memory_hub.approve_memory_candidate(db, repo, row.id)
    assert db.query(Document).count() == 1


def test_capture_assesses_and_embedding_failure_keeps_pending(scope, monkeypatch):
    from app.services import memory_hub
    from types import SimpleNamespace
    db, repo, conv, _ = scope
    message = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Use SQLite')
    db.add(message)
    db.flush()
    monkeypatch.setattr(memory_hub, 'settings', SimpleNamespace(memory_auto_approve_verified=True), raising=False)
    monkeypatch.setattr(memory_hub, 'add_document', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('network secret token')))
    rows = memory_hub.capture_memory_candidates(db, repo_id=repo.id, conversation_id=conv.id, session_id=None,
        memory_update={'facts': ['Use SQLite'], 'citations': [{'type': 'chat_message', 'id': str(message.id), 'quote': 'Use SQLite'}]})
    assert len(rows) == 1 and rows[0].status == 'pending'
    assert rows[0].meta['quality']['can_auto_approve']
    assert rows[0].meta['auto_approve_error'] == 'RuntimeError'
    assert db.query(MemoryCandidate).count() == 1


def test_invalid_source_identifiers_never_echo_credentials(scope):
    row = candidate(scope, citations=[{'type': 'secret=supersecretvalue', 'id': 'sk-thisisnotvalidandmustnotbeechoed', 'quote': 'Use SQLite'}])
    result = assess(scope, row)
    assert 'supersecretvalue' not in str(result) and 'sk-thisis' not in str(result)


def test_sensitive_candidate_title_is_rejected(scope):
    row = candidate(scope)
    row.title = 'password=supersecretpassword'
    assert assess(scope, row)['verdict'] == 'rejected'


def test_generated_memory_cannot_be_its_own_evidence(scope):
    from app.services.memory_lifecycle import sync_memory_entries
    db, repo, conv, _ = scope
    entry = sync_memory_entries(db, repo.id, conv.id, {'facts': ['Use SQLite']})[0]
    row = candidate(scope, citations=[{'type': 'memory_entry', 'id': str(entry.id), 'quote': 'Use SQLite'}], confidence=1)
    assert assess(scope, row)['can_auto_approve'] is False


def test_linked_conflict_or_expired_memory_never_autoapproves(scope):
    from datetime import timedelta
    from app.services.memory_lifecycle import create_entry, now_utc, sync_memory_entries
    db, repo, conv, _ = scope
    entry = create_entry(db, repo.id, {'conversation_id': conv.id, 'kind': 'fact', 'key': 'database', 'content': 'Use SQLite'})
    message = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Use SQLite')
    db.add(message)
    db.flush()
    row = candidate(scope, memory_entry_id=str(entry.id), memory_entry_version=entry.version,
                    citations=[{'type': 'chat_message', 'id': str(message.id), 'quote': 'Use SQLite'}])
    assert assess(scope, row)['can_auto_approve']
    entry.expires_at = now_utc() - timedelta(seconds=1)
    assert assess(scope, row)['can_auto_approve'] is False
    entry.expires_at = None
    sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use MySQL'}]})
    assert assess(scope, row)['checks']['no_conflict'] is False


def test_verified_auto_promotion_preserves_memory_links(scope, monkeypatch):
    from app.services import memory_hub
    from app.services.memory_lifecycle import sync_memory_entries
    from types import SimpleNamespace
    db, repo, conv, _ = scope
    message = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Use SQLite')
    db.add(message)
    db.flush()
    update = {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use SQLite', 'confidence': .9,
        'citations': [{'type': 'chat_message', 'id': str(message.id), 'quote': 'Use SQLite'}]}]}
    entry = sync_memory_entries(db, repo.id, conv.id, update)[0]
    monkeypatch.setattr(memory_hub, 'settings', SimpleNamespace(memory_auto_approve_verified=True))
    def add(db, repo_id, source_type, source_id, title, content, metadata, **kwargs):
        doc = Document(repo_id=repo_id, source_type=source_type, source_id=source_id, title=title, content=content, meta=metadata)
        db.add(doc)
        db.flush()
        return doc
    monkeypatch.setattr(memory_hub, 'add_document', add)
    rows = memory_hub.capture_memory_candidates(db, repo_id=repo.id, conversation_id=conv.id, session_id=None, memory_update=update)
    assert rows[0].status == 'approved'
    doc = db.query(Document).one()
    assert doc.meta['memory_entry_id'] == str(entry.id) and doc.meta['memory_entry_version'] == entry.version
    assert doc.meta['citations'][0]['id'] == str(message.id)
    assert memory_hub.capture_memory_candidates(db, repo_id=repo.id, conversation_id=conv.id, session_id=None, memory_update=update) == []


def test_concurrent_approvals_create_one_document(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep
    from app.services import memory_hub
    engine = create_engine('sqlite+pysqlite:///' + str(tmp_path / 'approval.sqlite'), connect_args={'timeout': 10})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner='test', name='concurrent', full_name='test/concurrent')
        db.add(repo)
        db.flush()
        row = MemoryCandidate(repo_id=repo.id, kind='fact', title='Concurrency', content='One document')
        db.add(row)
        db.commit()
        repo_id, candidate_id = repo.id, row.id
    def add(db, repo_id, source_type, source_id, title, content, metadata, **kwargs):
        sleep(.08)  # A real slow embedding boundary while the other session tries its claim.
        doc = Document(repo_id=repo_id, source_type=source_type, source_id=source_id, title=title, content=content, meta=metadata)
        db.add(doc)
        db.flush()
        return doc
    monkeypatch.setattr(memory_hub, 'add_document', add)
    start = Barrier(2)
    def approve():
        with Session(engine) as db:
            repo = db.get(Repository, repo_id)
            start.wait(timeout=5)
            row, doc = memory_hub.approve_memory_candidate(db, repo, candidate_id)
            db.commit()
            return row.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(), range(2)))
    with Session(engine) as db:
        assert results == ['approved', 'approved']
        assert db.query(Document).count() == 1


@pytest.mark.parametrize('confidence', [.69, 1.1, float('nan'), 'bad'])
def test_insufficient_or_invalid_confidence_stays_reviewable(scope, confidence):
    db, repo, conv, _ = scope
    message = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Use SQLite')
    db.add(message)
    db.flush()
    row = candidate(scope, confidence=confidence, citations=[{'type': 'chat_message', 'id': str(message.id), 'quote': 'Use SQLite'}])
    assert assess(scope, row)['verdict'] == 'needs_review'


@pytest.mark.parametrize('role', ['assistant', 'tool', 'system'])
def test_generated_chat_message_is_not_authoritative_evidence(scope, role):
    db, repo, conv, _ = scope
    message = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role=role, content='Use SQLite')
    db.add(message)
    db.flush()
    row = candidate(scope, confidence=1, citations=[{'type': 'chat_message', 'id': str(message.id), 'quote': 'Use SQLite'}])
    assert assess(scope, row)['can_auto_approve'] is False


def test_chat_session_verifies_user_confirmation_only(scope):
    from app.db.models import ChatSession
    db, repo, conv, _ = scope
    user = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Please inspect database options')
    assistant = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='assistant', content='Use SQLite')
    db.add_all([user, assistant])
    db.flush()
    session = ChatSession(repo_id=repo.id, conversation_id=conv.id, user_message_id=user.id, assistant_message_id=assistant.id)
    db.add(session)
    db.flush()
    row = candidate(scope, confidence=1, citations=[{'type': 'chat_session', 'id': str(session.id), 'quote': 'Use SQLite'}])
    assert assess(scope, row)['can_auto_approve'] is False
    session.user_message_id = assistant.id
    assert assess(scope, row)['can_auto_approve'] is False
    user.content = 'Confirmed: Use SQLite'
    session.user_message_id = user.id
    assert assess(scope, row)['can_auto_approve'] is True


@pytest.mark.parametrize('deleted', [False, True])
@pytest.mark.parametrize('changed_kind', [False, True])
def test_keyless_historical_candidate_retains_invalid_entry_version(scope, monkeypatch, deleted, changed_kind):
    from app.services import memory_hub
    from app.services.memory_lifecycle import candidate_memory_link, create_entry, delete_entry, memory_link_active, sync_memory_entries, update_entry
    from types import SimpleNamespace
    db, repo, conv, _ = scope
    entry = create_entry(db, repo.id, {'conversation_id': conv.id, 'kind': 'fact', 'key': 'database', 'content': 'Use SQLite'})
    old_version = entry.version
    update_entry(db, repo.id, entry.id, {'expected_version': entry.version, 'content': 'Use PostgreSQL', **({'kind':'decision'} if changed_kind else {})})
    if deleted:
        delete_entry(db, repo.id, entry.id, entry.version)
    source = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Use SQLite')
    db.add(source)
    db.flush()
    update = {'facts': ['Use SQLite'], 'citations': [{'type': 'chat_message', 'id': str(source.id), 'quote': 'Use SQLite'}]}
    sync_memory_entries(db, repo.id, conv.id, update)
    link = candidate_memory_link(db, repo.id, conv.id, 'fact', 'Use SQLite')
    assert link.get('memory_entry_id') == str(entry.id) and link.get('memory_entry_version') == old_version
    assert memory_link_active(db, repo.id, link) is False
    monkeypatch.setattr(memory_hub, 'settings', SimpleNamespace(memory_auto_approve_verified=True))
    monkeypatch.setattr(memory_hub, 'add_document', lambda *args, **kwargs: pytest.fail('Historical fact must not be promoted'))
    rows = memory_hub.capture_memory_candidates(db, repo_id=repo.id, conversation_id=conv.id, session_id=None, memory_update=update)
    assert len(rows) == 1 and rows[0].meta['memory_entry_id'] == str(entry.id)
    assert rows[0].meta['quality']['can_auto_approve'] is False and rows[0].status == 'pending'
    assert 'auto_approve_error' not in rows[0].meta


def test_capture_does_not_promote_out_of_range_extraction_confidence(scope, monkeypatch):
    from app.services import memory_hub
    from types import SimpleNamespace
    db, repo, conv, _ = scope
    source = ChatMessage(repo_id=repo.id, conversation_id=conv.id, role='user', content='Use SQLite')
    db.add(source)
    db.flush()
    monkeypatch.setattr(memory_hub, 'settings', SimpleNamespace(memory_auto_approve_verified=True))
    monkeypatch.setattr(memory_hub, 'add_document', lambda *args, **kwargs: pytest.fail('Invalid confidence must not be promoted'))
    rows = memory_hub.capture_memory_candidates(db, repo_id=repo.id, conversation_id=conv.id, session_id=None, memory_update={
        '_memory_items': [{'kind': 'fact', 'content': 'Use SQLite', 'confidence': 1.1,
            'citations': [{'type': 'chat_message', 'id': str(source.id), 'quote': 'Use SQLite'}]}]})
    assert rows == []
