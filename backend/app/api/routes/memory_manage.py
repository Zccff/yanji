import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models import MemoryCandidate
from app.db.session import get_db
from app.schemas.memory_manage import ConflictResolve, EntryCreate, EntryPatch, EntryRevert, ExpectedVersion
from app.services import memory_lifecycle as lifecycle
from app.services.memory_quality import assess_candidate

router = APIRouter(prefix='/api/repos', tags=['memory'])


@router.get('/{repo_id}/memory/entries')
def entries(repo_id: uuid.UUID, conversation_id: uuid.UUID | None = None, q: str = '',
            status: Literal['active', 'all', 'deleted', 'expired', 'conflict'] = 'active', kind: str | None = None,
            limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    result = lifecycle.list_entries(db, repo_id, conversation_id, q, status, kind, limit, offset)
    db.commit()  # Persist a lazy legacy import if one happened.
    return result


@router.post('/{repo_id}/memory/entries')
def create(repo_id: uuid.UUID, payload: EntryCreate, db: Session = Depends(get_db)):
    entry = lifecycle.create_entry(db, repo_id, payload.model_dump(exclude_unset=True))
    result = lifecycle.entry_payload(entry)
    db.commit()
    return result


@router.patch('/{repo_id}/memory/entries/{entry_id}')
def edit(repo_id: uuid.UUID, entry_id: uuid.UUID, payload: EntryPatch, db: Session = Depends(get_db)):
    entry = lifecycle.update_entry(db, repo_id, entry_id, payload.model_dump(exclude_unset=True))
    result = lifecycle.entry_payload(entry)
    db.commit()
    return result


@router.delete('/{repo_id}/memory/entries/{entry_id}')
def delete(repo_id: uuid.UUID, entry_id: uuid.UUID, expected_version: int = Query(..., ge=1), db: Session = Depends(get_db)):
    result = lifecycle.entry_payload(lifecycle.delete_entry(db, repo_id, entry_id, expected_version))
    db.commit()
    return result


@router.post('/{repo_id}/memory/entries/{entry_id}/restore')
def restore(repo_id: uuid.UUID, entry_id: uuid.UUID, payload: ExpectedVersion, db: Session = Depends(get_db)):
    result = lifecycle.entry_payload(lifecycle.restore_entry(db, repo_id, entry_id, payload.expected_version))
    db.commit()
    return result


@router.get('/{repo_id}/memory/entries/{entry_id}/revisions')
def revisions(repo_id: uuid.UUID, entry_id: uuid.UUID, db: Session = Depends(get_db)):
    return lifecycle.entry_revisions(db, repo_id, entry_id)


@router.post('/{repo_id}/memory/entries/{entry_id}/revert')
def revert(repo_id: uuid.UUID, entry_id: uuid.UUID, payload: EntryRevert, db: Session = Depends(get_db)):
    result = lifecycle.entry_payload(lifecycle.revert_entry(db, repo_id, entry_id, payload.expected_version, payload.target_version))
    db.commit()
    return result


@router.get('/{repo_id}/memory/conflicts')
def conflicts(repo_id: uuid.UUID, conversation_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    return lifecycle.list_conflicts(db, repo_id, conversation_id)


@router.post('/{repo_id}/memory/conflicts/{conflict_id}/resolve')
def resolve(repo_id: uuid.UUID, conflict_id: uuid.UUID, payload: ConflictResolve, db: Session = Depends(get_db)):
    result = lifecycle.entry_payload(lifecycle.resolve_conflict(db, repo_id, conflict_id, payload.resolution, payload.expected_version))
    db.commit()
    return result


@router.post('/{repo_id}/memory/candidates/{candidate_id}/assess')
def assess(repo_id: uuid.UUID, candidate_id: uuid.UUID, db: Session = Depends(get_db)):
    lifecycle.ensure_scope(db, repo_id)
    candidate = db.query(MemoryCandidate).filter_by(repo_id=repo_id, id=candidate_id).one_or_none()
    if candidate is None:
        raise HTTPException(404, 'candidate_not_found')
    if candidate.conversation_id:
        lifecycle.ensure_scope(db, repo_id, candidate.conversation_id)
    result = assess_candidate(db, candidate)
    db.commit()
    return result
