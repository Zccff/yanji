import json

import httpx
import pytest

from app.core.config import settings
from app.db.models import KnowledgeBaseConfig
from app.services.rag import knowledge_base


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_rerank_accepts_dashscope_and_compatible_requests(monkeypatch, native):
    native_url = "https://example.test/api/v1/services/rerank/text-rerank/text-rerank"
    monkeypatch.setattr(settings, "rerank_base_url", native_url if native else "https://example.test/v1")
    monkeypatch.setattr(settings, "rerank_api_key", "test-only-token")
    items = [
        {"id": "weather", "title": "Weather", "snippet": "Sunny today."},
        {"id": "guide", "title": "DevFlow guide", "snippet": "DevFlow reviews pull requests."},
    ]

    def respond(request):
        assert str(request.url) == (native_url if native else "https://example.test/v1/reranks")
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3-rerank"
        if native:
            assert payload["input"] == {
                "query": "What is DevFlow?",
                "documents": ["Weather\nSunny today.", "DevFlow guide\nDevFlow reviews pull requests."],
            }
            assert payload["parameters"] == {"top_n": 1, "return_documents": False}
        else:
            assert payload["query"] == "What is DevFlow?"
            assert payload["top_n"] == 1
            assert payload["documents"][1] == "DevFlow guide\nDevFlow reviews pull requests."
        rows = [{"index": 1, "relevance_score": 0.91}]
        return httpx.Response(200, json={"output": {"results": rows}} if native else {"results": rows})

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(knowledge_base.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    config = KnowledgeBaseConfig(rerank_model="qwen3-rerank")
    ranked = await knowledge_base._qwen_rerank("What is DevFlow?", items, config, 1)
    assert [item["id"] for item in ranked] == ["guide"]
    assert ranked[0]["rerank_score"] == 0.91
    assert ranked[0]["retrieval"]["rerank_mode"] == "qwen_api"
