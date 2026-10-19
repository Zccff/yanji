import json

import httpx
import pytest

from app.services.llm import client as client_module
from app.services.llm.client import LLMClient


@pytest.mark.asyncio
@pytest.mark.parametrize('model,strict,thinking', [
    ('qwen3.7-plus', True, False),
    ('qwen3.7-plus', False, None),
    ('deepseek-v4-pro', True, None),
])
async def test_memory_json_disables_optional_qwen_reasoning_only(monkeypatch, model, strict, thinking):
    """Background extraction needs JSON, without an extra Qwen thinking pass."""
    actual_client = httpx.AsyncClient
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[{'message':{'content':'{"summary":"verified fact"}'}}]})
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(client_module.httpx, 'AsyncClient', lambda **kwargs: actual_client(transport=transport, **kwargs))
    llm = LLMClient()
    llm.api_key = 'test-only'
    llm.base_url = 'https://llm.test/v1'
    llm.model = model
    result = await (llm.chat_json_strict if strict else llm.chat_json)('Extract memory', {'message':'test'})
    assert result == {'summary':'verified fact'}
    if thinking is False:
        assert requests[0]['enable_thinking'] is False
    else:
        assert 'enable_thinking' not in requests[0]


@pytest.fixture
def stream_llm(monkeypatch):
    real_client = httpx.AsyncClient

    def create(chunks):
        def respond(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload.get("stream"):
                body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                return httpx.Response(
                    200,
                    text=body + "data: [DONE]\n\n",
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps({"failure_type": "test"})}}
                    ]
                },
            )

        transport = httpx.MockTransport(respond)
        monkeypatch.setattr(
            client_module.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(transport=transport, **kwargs),
        )
        llm = LLMClient()
        llm.api_key = "test-only"
        llm.base_url = "https://llm.test/v1"
        return llm

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "non_content_chunk",
    [
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
        {"usage": {"total_tokens": 12}},
        {"choices": None},
        {"choices": [None]},
        {"choices": [{"delta": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ],
    ids=["usage-only", "no-choices", "null-choices", "null-choice", "null-delta", "finish"],
)
async def test_chat_text_stream_preserves_text_around_non_content_chunks(stream_llm, non_content_chunk):
    llm = stream_llm(
        [
            non_content_chunk,
            {"choices": [{"index": 0, "delta": {"content": "Inspect "}}]},
            non_content_chunk,
            {"choices": [{"index": 0, "delta": {"content": "failed tests."}}]},
            non_content_chunk,
        ]
    )

    chunks = [chunk async for chunk in llm.chat_text_stream("Analyze CI", [{"role": "user", "content": "Why failed?"}])]

    assert chunks == ["Inspect ", "failed tests."]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_text", [True, False], ids=["text-and-usage", "usage-only-fallback"])
async def test_ci_analysis_stream_completes_when_provider_sends_empty_choices(stream_llm, monkeypatch, with_text):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.api.routes import ci
    from app.db.models import AnalysisResult, Base, Repository, WorkflowRun
    from app.services.agents.ci_debug_agent import CIDebugAgent

    provider_chunks = [{"choices": [], "usage": {"total_tokens": 12}}]
    if with_text:
        provider_chunks.insert(0, {"choices": [{"index": 0, "delta": {"content": "Inspect failed tests."}}]})
    llm = stream_llm(provider_chunks)
    monkeypatch.setattr(ci, "CIDebugAgent", lambda: CIDebugAgent(llm=llm))
    monkeypatch.setattr(ci, "_prepare_ci_pr_snapshot", lambda *args, **kwargs: {"status": "skipped"})
    monkeypatch.setattr(
        ci,
        "_build_ci_analysis_context",
        lambda *args, **kwargs: (
            "pytest",
            {"failed_jobs": [{"name": "pytest"}], "first_error": "AssertionError"},
            {"failed_jobs": 1, "failed_steps": 1, "code": 0, "workflow": 0, "documents": 0, "recent_prs": 0, "conversation": 0, "related_files": 0},
        ),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            repo = Repository(owner="local", name="ci-test", full_name="local/ci-test")
            db.add(repo)
            db.flush()
            run = WorkflowRun(
                repo_id=repo.id,
                name="backend-test",
                status="completed",
                conclusion="failure",
                logs_text="pytest tests/test_api.py FAILED AssertionError",
                jobs=[{"name": "pytest", "conclusion": "failure", "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
            )
            db.add(run)
            db.commit()

            frames = [frame async for frame in ci._ci_analysis_sse(run.id, None, db)]
            events = []
            for frame in frames:
                lines = frame.strip().splitlines()
                events.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))

            assert "error" not in [name for name, _ in events], events
            assert events[-1][0] == "final"
            assert events[-1][1]["result"]["failure_type"] == "test"
            assert any(name == "thinking_delta" and payload["content"].strip() for name, payload in events)
            assert db.query(AnalysisResult).filter(AnalysisResult.target_id == run.id).count() == 1
    finally:
        engine.dispose()
