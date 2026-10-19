import asyncio

from app.db.models import KnowledgeSourceDocument, Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.rag.document_processing import process_knowledge_document


def ingest_document(job):
    with SessionLocal() as db:
        source = db.get(KnowledgeSourceDocument, job["resource_id"])
        if source is None or source.repo_id != job["repo_id"]:
            raise ValueError("文档已删除或不属于任务仓库")
        if source.status == "completed":
            return {"document_id": str(source.id), "status": "completed", "chunk_count": source.chunk_count}
    process_knowledge_document(job["resource_id"], raise_errors=True)
    with SessionLocal() as db:
        source = db.get(KnowledgeSourceDocument, job["resource_id"])
        if source is None or source.status != "completed":
            raise RuntimeError("文档处理未完成")
        return {"document_id": str(source.id), "status": source.status, "chunk_count": source.chunk_count}


async def _sync_repository(job):
    from app.services.repos_sync import sync_repository
    from app.services.code_analysis import sync_repository_code_analysis_by_id

    payload = job["payload"]
    result = {"synced": {"issues": 0, "pull_requests": 0, "workflow_runs": 0}}
    if not payload.get("code_only"):
        with SessionLocal() as db:
            repo = db.get(Repository, job["repo_id"])
            if repo is None:
                raise ValueError("仓库已删除")
            response = await sync_repository(db, repo, RepoSyncRequest.model_validate(payload), strict=True)
            result = response.model_dump(mode="json")
    if payload.get("analyze_code"):
        count = await sync_repository_code_analysis_by_id(job["repo_id"])
        result["code_symbols"] = count
        if count is None:
            if payload.get("index_project"):
                raise RuntimeError("代码检出或分析未完成，项目索引尚未开始，请检查 Worker 日志。")
            result["warnings"] = ["GitHub 数据已同步，但代码检出或代码分析未完成，请检查 Worker 日志。"]
    if payload.get("index_project"):
        result["project_index"] = await _index_project(job)
    return result


def sync_repo(job):
    return asyncio.run(_sync_repository(job))


async def _index_project(job):
    from app.services.code_analysis import sync_repository_code_analysis_by_id
    from app.services.project_indexing import _safe_project_root, scan_project_index

    with SessionLocal() as db:
        repo = db.get(Repository, job["repo_id"])
        if repo is None:
            raise ValueError("仓库已删除")
        needs_checkout = _safe_project_root(repo) is None
    if needs_checkout:
        count = await sync_repository_code_analysis_by_id(job["repo_id"])
        if count is None:
            raise RuntimeError("代码检出未完成，项目索引尚未开始，请检查同步任务或 Worker 日志。")
    with SessionLocal() as db:
        repo = db.get(Repository, job["repo_id"])
        if repo is None:
            raise ValueError("仓库已删除")
        state = scan_project_index(db, repo)
        if state["status"] != "ready":
            raise RuntimeError(state.get("error_message") or "项目索引构建未完成")
        return {key: state[key] for key in ("status", "docs_indexed", "docs_total")}


def index_project(job):
    # process_job holds the same repository lock used by checkout/sync jobs.
    return asyncio.run(_index_project(job))


from app.services.session_enrichment import enrich_session

HANDLERS = {"document.ingest": ingest_document, "repo.sync": sync_repo, "memory.enrich": enrich_session, "project.index": index_project}
