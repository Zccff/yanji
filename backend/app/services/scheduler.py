import asyncio
import logging

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.task_queue import enqueue_job

logger = logging.getLogger(__name__)


async def periodic_repo_sync_loop() -> None:
    await asyncio.sleep(5)
    while settings.auto_sync_enabled:
        db = SessionLocal()
        try:
            repos = db.query(Repository).filter(Repository.provider.in_(["github", "github_compatible"])).all()
            repo_ids = [repo.id for repo in repos]
        finally:
            db.close()

        for repo_id in repo_ids:
            try:
                with SessionLocal() as db:
                    enqueue_job(db, "repo.sync", repo_id, payload=RepoSyncRequest(limit=settings.auto_sync_limit).model_dump())
                    db.commit()
            except Exception as exc:
                logger.warning("Periodic repo sync failed for %s: %s", repo_id, exc)

        await asyncio.sleep(max(settings.auto_sync_interval_seconds, 30))
