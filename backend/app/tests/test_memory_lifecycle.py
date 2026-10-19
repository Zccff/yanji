import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Conversation, ConversationMemory, Repository
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
        conversation = Conversation(repo_id=repo.id)
        db.add(conversation)
        db.flush()
        yield db, repo, conversation, other


def service():
    from app.services import memory_lifecycle
    return memory_lifecycle


def create(scope, **kwargs):
    db, repo, conv, _ = scope
    return service().create_entry(db, repo.id, {'conversation_id': conv.id, 'kind': 'fact', 'content': 'Use SQLite', **kwargs})


def test_create_edit_delete_restore_revert_and_stale_versions(scope):
    db, repo, _, _ = scope
    api = service()
    entry = create(scope, key='database')
    assert entry.version == 1 and entry.confidence == 1 and entry.importance == .7
    api.update_entry(db, repo.id, entry.id, {'expected_version': 1, 'content': 'Use PostgreSQL'})
    with pytest.raises(HTTPException) as error:
        api.update_entry(db, repo.id, entry.id, {'expected_version': 1, 'content': 'stale'})
    assert error.value.status_code == 409
    api.delete_entry(db, repo.id, entry.id, 2)
    assert entry.status == 'deleted'
    api.restore_entry(db, repo.id, entry.id, 3)
    api.revert_entry(db, repo.id, entry.id, 4, 1)
    assert entry.content == 'Use SQLite' and entry.version == 5
    assert [row['operation'] for row in api.entry_revisions(db, repo.id, entry.id)] == ['create', 'edit', 'delete', 'restore', 'revert']


def test_decay_read_does_not_confirm_and_pinned_still_expires(scope):
    db, repo, conv, _ = scope
    api = service()
    entry = create(scope, ttl_days=None)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entry.last_confirmed_at = start
    assert api.entry_score(entry, now=start + timedelta(days=30)) == pytest.approx(.35)
    entry.pinned = True
    assert api.entry_score(entry, now=start + timedelta(days=60)) == pytest.approx(.7)
    entry.expires_at = start + timedelta(days=10)
    assert api.entry_score(entry, now=start + timedelta(days=11)) == 0
    api.managed_memory_context(db, repo.id, conv.id)
    assert api.utc(entry.last_confirmed_at) == start


@pytest.mark.parametrize('operation', ['create', 'rename', 'revert'])
def test_semantic_alias_cannot_be_claimed_by_another_entry(scope, operation):
    db, repo, conv, _ = scope
    api = service()
    owner = create(scope)
    api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items':[{'kind':'fact','key':'database','content':owner.content}]})
    other = None
    if operation == 'rename':
        other = create(scope, key='other', content='Use MySQL')
    elif operation == 'revert':
        # Reverting a key must consult the same identity claims as normal PATCH.
        other = create(scope, key='temporary', content='Use MySQL')
        from app.db.models import MemoryRevision
        db.query(MemoryRevision).filter_by(entry_id=other.id, version=1).one().snapshot = {
            **api.entry_payload(other), 'key':'database'}
        db.flush()
    with pytest.raises(HTTPException) as error:
        if operation == 'create':
            create(scope, key='database', content='Use MySQL')
        elif operation == 'rename':
            api.update_entry(db, repo.id, other.id, {'expected_version':other.version,'key':'database'})
        else:
            api.revert_entry(db, repo.id, other.id, other.version, 1)
    assert error.value.status_code == 409
    assert api._matching_entry(db, repo.id, conv.id, 'fact', 'other', 'database').id == owner.id


def test_deleted_tombstone_blocks_reextraction_and_no_old_summary(scope):
    db, repo, conv, _ = scope
    api = service()
    entry = create(scope, key='database')
    api.delete_entry(db, repo.id, entry.id, 1)
    api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use SQLite'}], 'summary': 'stale summary'})
    result = api.managed_memory_context(db, repo.id, conv.id)
    assert result['managed'] is True and not result['items'] and not result['text']
    assert result['stats']['deleted'] == 1


def test_explicit_key_conflict_requires_resolution_and_preserves_manual_authority(scope):
    db, repo, conv, _ = scope
    api = service()
    entry = create(scope, key='database')
    api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use MySQL'}]})
    assert entry.content == 'Use SQLite'
    conflicts = api.list_conflicts(db, repo.id, conv.id)
    assert len(conflicts) == 1
    assert not api.managed_memory_context(db, repo.id, conv.id)['items']
    api.resolve_conflict(db, repo.id, conflicts[0]['id'], 'replace', entry.version)
    assert entry.content == 'Use MySQL' and entry.source == 'manual'
    assert api.managed_memory_context(db, repo.id, conv.id)['items'][0]['version'] == entry.version


def test_repository_isolation(scope):
    db, repo, conv, other = scope
    api = service()
    entry = create(scope)
    for operation in [lambda: api.update_entry(db, other.id, entry.id, {'expected_version': 1, 'content': 'oops'}), lambda: api.managed_memory_context(db, other.id, conv.id)]:
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 404


def test_lazy_legacy_import_once_and_bounded_context(scope):
    db, repo, conv, _ = scope
    api = service()
    db.add(ConversationMemory(repo_id=repo.id, conversation_id=conv.id, facts=['Legacy fact'], summary='stale broad summary'))
    db.flush()
    result = api.managed_memory_context(db, repo.id, conv.id, max_tokens=80)
    assert 'Legacy fact' in result['text'] and 'stale broad summary' not in result['text']
    assert len(result['text']) <= 80
    entry = result['items'][0]
    api.delete_entry(db, repo.id, uuid.UUID(str(entry['id'])), entry['version'])
    assert not api.managed_memory_context(db, repo.id, conv.id)['items']


def test_default_ttl_and_explicit_null_and_validation(scope):
    from app.schemas.memory_manage import EntryCreate, EntryPatch
    from pydantic import ValidationError
    default = create(scope, kind='task')
    forever = create(scope, content='long term', ttl_days=None)
    assert (service().utc(default.expires_at) - service().utc(default.created_at)).days == 14
    assert forever.expires_at is None
    with pytest.raises(ValidationError):
        EntryCreate(conversation_id=scope[2].id, kind='fact', content=' ', half_life_days=0)
    with pytest.raises(ValidationError):
        EntryPatch(expected_version=1, confidence=None)


def test_multiple_conflicts_show_latest_version_and_restore_cannot_bypass_review(scope):
    db, repo, conv, _ = scope
    api = service()
    entry = create(scope, key='database')
    for content in ['Use MySQL', 'Use PostgreSQL']:
        api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': content}]})
    assert all(row['entry_version'] == entry.version for row in api.list_conflicts(db, repo.id, conv.id))
    with pytest.raises(HTTPException) as error:
        api.restore_entry(db, repo.id, entry.id, entry.version)
    assert error.value.status_code == 409


def test_invalid_model_shapes_are_skipped_without_transcript_failure(scope):
    db, repo, conv, _ = scope
    api = service()
    result = api.sync_memory_entries(db, repo.id, conv.id, {'facts': 'not a list', 'decisions': [None, {'oops': True}]})
    assert result == []
    result = api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'content': 'valid', 'citations': {'oops': True}}]})
    assert len(result) == 1 and result[0].meta['citations'] == []
    assert api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': [], 'content': 'invalid kind'}]}) == []


def test_tombstone_and_old_manual_revisions_block_keyless_reextraction(scope):
    db, repo, conv, _ = scope
    api = service()
    entry = create(scope, key='database')
    api.update_entry(db, repo.id, entry.id, {'expected_version': 1, 'content': 'Use PostgreSQL'})
    api.sync_memory_entries(db, repo.id, conv.id, {'facts': ['Use SQLite']})
    assert api.list_entries(db, repo.id, conv.id, status='all')['total'] == 1
    api.delete_entry(db, repo.id, entry.id, entry.version)
    api.sync_memory_entries(db, repo.id, conv.id, {'facts': ['Use PostgreSQL', 'Use SQLite']})
    result = api.managed_memory_context(db, repo.id, conv.id)
    assert result['stats']['total'] == 1 and not result['items']


def test_empty_legacy_is_unmanaged_and_migrations_use_original_timestamp(scope):
    db, repo, conv, _ = scope
    api = service()
    at = api.now_utc() - timedelta(days=200)
    legacy = ConversationMemory(repo_id=repo.id, conversation_id=conv.id, summary='Stale broad summary', updated_at=at)
    db.add(legacy)
    db.flush()
    assert api.managed_memory_context(db, repo.id, conv.id)['managed'] is False
    legacy.facts = ['Very old fact']
    db.flush()
    legacy.updated_at = at
    db.flush()
    result = api.managed_memory_context(db, repo.id, conv.id)
    assert result['managed'] and result['stats']['expired'] == 1 and not result['items']


def test_real_stale_orm_session_cannot_overwrite_newer_version(scope):
    from app.db.models import MemoryEntry
    db, repo, _, _ = scope
    api = service()
    entry = create(scope)
    db.commit()
    with Session(db.get_bind(), expire_on_commit=False) as stale:
        stale_entry = stale.get(MemoryEntry, entry.id)
        api.update_entry(db, repo.id, entry.id, {'expected_version': 1, 'content': 'New confirmed data'})
        db.commit()
        assert stale_entry.version == 1
        with pytest.raises(HTTPException) as error:
            api.update_entry(stale, repo.id, entry.id, {'expected_version': 1, 'content': 'Stale overwrite'})
        assert error.value.status_code == 409
        assert db.get(MemoryEntry, entry.id).content == 'New confirmed data'


def test_service_savepoints_join_caller_transaction_even_on_sqlite(scope):
    from app.db.models import MemoryEntry, MemoryRevision
    db, repo, conv, _ = scope
    repo_id, conversation_id = repo.id, conv.id
    db.commit()
    entry = service().create_entry(db, repo_id, {'conversation_id': conversation_id, 'kind': 'fact', 'content': 'Rolled back fact'})
    entry_id = entry.id
    db.rollback()
    assert db.get(MemoryEntry, entry_id) is None
    assert db.query(MemoryRevision).filter_by(entry_id=entry_id).count() == 0


@pytest.mark.parametrize('initial', ['legacy', 'keyless', 'manual_key'])
def test_same_content_semantic_key_alias_catches_later_contradiction(scope, initial):
    db, repo, conv, _ = scope
    api = service()
    if initial == 'legacy':
        db.add(ConversationMemory(repo_id=repo.id, conversation_id=conv.id, facts=['Use SQLite']))
        db.flush()
        api.managed_memory_context(db, repo.id, conv.id)
        entry = api._entry_query(db, repo.id, conv.id).one()
    elif initial == 'manual_key':
        entry = create(scope, key='user-database-choice')
    else:
        entry = api.sync_memory_entries(db, repo.id, conv.id, {'facts': ['Use SQLite']})[0]
    original_key, original_confirmation = entry.key, entry.last_confirmed_at
    api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use SQLite'}]})
    api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use MySQL'}]})
    assert api.list_entries(db, repo.id, conv.id, status='all')['total'] == 1
    assert entry.status == 'conflict' and entry.content == 'Use SQLite'
    assert api.utc(entry.last_confirmed_at) == api.utc(original_confirmation)
    if initial == 'manual_key':
        assert entry.key == original_key and entry.source == 'manual'
    assert len(api.list_conflicts(db, repo.id, conv.id)) == 1


def test_manual_create_first_preserves_legacy_facts_before_lazy_read(scope):
    db, repo, conv, _ = scope
    api = service()
    db.add(ConversationMemory(repo_id=repo.id, conversation_id=conv.id, facts=['Existing old fact']))
    db.flush()
    create(scope, content='New manual fact')
    result = api.list_entries(db, repo.id, conv.id, status='all')
    assert {row['content'] for row in result['items']} == {'Existing old fact', 'New manual fact'}


@pytest.mark.parametrize('confidence', [1.1, -.1, float('nan'), float('inf'), 'invalid'])
def test_invalid_extraction_confidence_is_not_normalized_into_validity(scope, confidence):
    db, repo, conv, _ = scope
    assert service().sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [
        {'kind': 'fact', 'content': 'Invalid confidence fact', 'confidence': confidence}]}) == []


def test_manual_create_cannot_take_another_entry_semantic_alias(scope):
    db, repo, conv, _ = scope
    api = service()
    original = create(scope, key='user-database')
    api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use SQLite'}]})
    with pytest.raises(HTTPException) as error:
        create(scope, key='database', content='Use MySQL')
    assert error.value.status_code == 409
    assert api.list_entries(db, repo.id, conv.id, status='all')['total'] == 1
    assert original.key == 'user-database'


def test_manual_key_patch_cannot_take_another_entry_semantic_alias(scope):
    db, repo, conv, _ = scope
    api = service()
    create(scope, key='user-database')
    api.sync_memory_entries(db, repo.id, conv.id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use SQLite'}]})
    other = create(scope, key='other', content='Use MySQL')
    with pytest.raises(HTTPException) as error:
        api.update_entry(db, repo.id, other.id, {'expected_version': other.version, 'key': 'database'})
    assert error.value.status_code == 409
    assert other.key == 'other' and other.version == 1


def test_existing_json_aliases_are_claimed_before_new_manual_write(scope):
    db, _, _, _ = scope
    entry = create(scope, key='original')
    entry.meta = {'semantic_keys': {'old-database-alias': True}}
    db.commit()
    with pytest.raises(HTTPException) as error:
        create(scope, key='old-database-alias', content='Another database')
    assert error.value.status_code == 409


def test_concurrent_alias_registration_and_manual_create_have_one_owner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.db.models import MemoryEntry
    api = service()
    engine = create_engine('sqlite+pysqlite:///' + str(tmp_path / 'identity.sqlite'), connect_args={'timeout': 10})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner='identity', name='race', full_name='identity/race')
        db.add(repo)
        db.flush()
        conv = Conversation(repo_id=repo.id)
        db.add(conv)
        db.flush()
        entry = api.create_entry(db, repo.id, {'conversation_id': conv.id, 'kind': 'fact', 'key': 'user-database', 'content': 'Use SQLite'})
        repo_id, conv_id, entry_id = repo.id, conv.id, entry.id
        db.commit()
    alias_registered, create_started = Event(), Event()
    def register_alias():
        with Session(engine) as db:
            api.sync_memory_entries(db, repo_id, conv_id, {'_memory_items': [{'kind': 'fact', 'key': 'database', 'content': 'Use SQLite'}]})
            alias_registered.set()
            assert create_started.wait(5)
            db.commit()
    def create_competing_entry():
        assert alias_registered.wait(5)
        with Session(engine) as db:
            create_started.set()
            try:
                api.create_entry(db, repo_id, {'conversation_id': conv_id, 'kind': 'fact', 'key': 'database', 'content': 'Use MySQL'})
                db.commit()
                return 200
            except HTTPException as error:
                db.rollback()
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        alias_future = pool.submit(register_alias)
        create_future = pool.submit(create_competing_entry)
        alias_future.result(timeout=15)
        assert create_future.result(timeout=15) == 409
    with Session(engine) as db:
        from app.db.models import MemoryKeyClaim
        assert db.query(MemoryEntry).filter_by(conversation_id=conv_id, status='active').count() == 1
        claim = db.query(MemoryKeyClaim).filter_by(repo_id=repo_id, conversation_id=conv_id, kind='fact', key='database').one()
        assert claim.entry_id == entry_id


def test_http_routes_commit_scope_validation_and_optimistic_conflicts(scope):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.memory_manage import router
    from app.db.session import get_db
    from sqlalchemy.pool import StaticPool
    # The HTTP thread and this fixture share one real SQLite database.
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner='http', name='repo', full_name='http/repo')
        db.add(repo)
        db.flush()
        conv = Conversation(repo_id=repo.id)
        db.add(conv)
        db.commit()
        app = FastAPI()
        app.include_router(router)
        def dependency():
            with Session(engine) as request_db:
                yield request_db
        app.dependency_overrides[get_db] = dependency
        with TestClient(app) as client:
            base = f'/api/repos/{repo.id}/memory'
            response = client.post(base + '/entries', json={'conversation_id': str(conv.id), 'kind': 'fact', 'content': 'HTTP durable fact'})
            assert response.status_code == 200
            entry = response.json()
            assert client.get(base + '/entries', params={'conversation_id': str(conv.id)}).json()['total'] == 1
            assert client.patch(base + '/entries/' + entry['id'], json={'expected_version': 1, 'content': 'edited'}).status_code == 200
            assert client.delete(base + '/entries/' + entry['id'], params={'expected_version': 1}).status_code == 409
            assert client.delete(base + '/entries/' + entry['id'], params={'expected_version': 2}).status_code == 200
            assert client.post(base + '/entries/' + entry['id'] + '/restore', json={'expected_version': 3}).status_code == 200
            assert len(client.get(base + '/entries/' + entry['id'] + '/revisions').json()) == 4
            assert client.post(base + '/entries/' + entry['id'] + '/revert', json={'expected_version': 4, 'target_version': 1}).status_code == 200
            assert client.get(base + '/entries', params={'conversation_id': str(uuid.uuid4())}).status_code == 404
            assert client.post(base + '/entries', json={'conversation_id': str(conv.id), 'kind': 'fact', 'content': ' ', 'importance': 2}).status_code == 422

