"""Verify the running application with a temporary repository and document."""
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT / "backend")
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from app.db.models import Repository
from app.db.session import SessionLocal

report = {}
repository_id = None
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120) as client:
    try:
        health = client.get("/health")
        health.raise_for_status()
        assert health.json()["status"] == "ok"
        report["backend"] = "healthy"

        frontend = httpx.get("http://127.0.0.1:3001", timeout=15)
        frontend.raise_for_status()
        assert "研迹" in frontend.text
        report["frontend"] = "healthy"

        skills = client.get("/api/skills")
        skills.raise_for_status()
        report["skills"] = len(skills.json()["skills"])
        assert report["skills"] >= 6
        mcp = client.get("/api/skills/mcp-status")
        mcp.raise_for_status()
        assert mcp.json()["status"] == "online"
        report["mcp"] = {"status": "online", "tools": len(mcp.json()["tools"])}
        print(json.dumps(report), flush=True)

        with SessionLocal() as db:
            name = "deployment-verification-" + uuid.uuid4().hex[:8]
            repository = Repository(owner="local", name=name, full_name="local/" + name, provider="local", local_path=str(ROOT), checkout_mode="local")
            db.add(repository)
            db.commit()
            repository_id = str(repository.id)

        config = client.patch(f"/api/rag/repositories/{repository_id}/config", json={"retrieval_method": "hybrid", "rerank_enabled": True, "rerank_provider": "qwen_api", "rerank_model": "qwen3-rerank", "score_threshold_enabled": False})
        config.raise_for_status()
        content = b"# DevFlow AI local deployment\n\nThe DevFlow AI web frontend is served on port 3001. The backend API listens on port 8000. PostgreSQL uses port 25432 and Milvus uses port 29530.\n"
        upload = client.post(f"/api/rag/repositories/{repository_id}/documents", files={"file": ("deployment-check.md", content, "text/markdown")})
        upload.raise_for_status()
        document_id = upload.json()["id"]
        for attempt in range(90):
            documents = client.get(f"/api/rag/repositories/{repository_id}/documents")
            documents.raise_for_status()
            document = next(item for item in documents.json() if item["id"] == document_id)
            if document["status"] == "completed":
                break
            assert document["status"] != "failed", document.get("error_message")
            time.sleep(1)
        else:
            raise AssertionError("Document indexing timed out")
        report["upload"] = {"status": document["status"], "chunks": document["chunk_count"]}

        status = client.get(f"/api/rag/{repository_id}/status")
        status.raise_for_status()
        report["vector_store"] = status.json()["vector_store"]
        assert report["vector_store"]["status"] in {"ready", "healthy", "ok", "online"}, report["vector_store"]

        search = client.post(f"/api/rag/{repository_id}/search", json={"question": "Which port serves the DevFlow AI web frontend?", "top_k": 3})
        search.raise_for_status()
        results = search.json()["results"]
        assert results, "No retrieval results"
        assert any(item.get("retrieval", {}).get("vector_score", 0) > 0 for item in results), results
        assert all(item["retrieval"]["rerank_mode"] == "qwen_api" for item in results), results
        report["retrieval"] = {"count": len(results), "rerank": "qwen_api", "vector": True}
        print(json.dumps(report), flush=True)

        answer = client.post(f"/api/rag/{repository_id}/ask", json={"question": "DevFlow AI 的前端使用哪个端口？", "top_k": 3})
        answer.raise_for_status()
        response = answer.json()
        assert response["generation_mode"] == "llm", response
        assert "3001" in response["answer"], response["answer"]
        assert response["sources"], "Missing citations"
        report["rag_answer"] = {"mode": response["generation_mode"], "answer": response["answer"], "citations": len(response["sources"])}

        chat = client.post("/api/chat", json={"repo_id": repository_id, "message": "请只回复：DevFlow AI 启动检查通过。不要调用工具。"})
        chat.raise_for_status()
        response = chat.json()
        assert response["route"] != "error", response["answer"]
        assert "DevFlow" in response["answer"], response["answer"]
        report["chat"] = {"route": response["route"], "answer": response["answer"]}
        report["status"] = "passed"
    finally:
        if repository_id:
            cleanup = client.delete(f"/api/repos/{repository_id}")
            cleanup.raise_for_status()
            report["temporary_repository_removed"] = True
        output = ROOT / ".data" / "runtime" / "verification.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
