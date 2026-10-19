import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings
from app.services.context_compression import prompt_cache_model_kwargs


class LLMClient:
    def __init__(self) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model

    async def chat_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return await self._chat_json(system_prompt, user_payload, strict=False)

    async def chat_json_strict(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        """Worker extraction must distinguish a model outage from an empty result."""
        return await self._chat_json(system_prompt, user_payload, strict=True)

    async def _chat_json(self, system_prompt: str, user_payload: dict[str, Any], *, strict: bool) -> dict[str, Any]:
        if not self.api_key:
            if strict:
                raise PermissionError("未配置记忆整理所需的模型 API Key")
            return {}

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        payload.update(prompt_cache_model_kwargs())
        # Qwen3 defaults to a reasoning pass. Background memory extraction
        # only needs structured JSON; long transcripts otherwise time out
        # before any JSON arrives. Other model APIs may reject this option.
        if strict and self.model.lower().startswith("qwen3"):
            payload["enable_thinking"] = False
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                if strict and (not isinstance(parsed, dict) or not parsed):
                    raise RuntimeError("模型未返回有效的结构化记忆")
                return parsed
            except json.JSONDecodeError:
                if strict:
                    raise RuntimeError('model_output_invalid: malformed memory JSON') from None
                return {}
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
                if strict:
                    raise
                return {}

    async def chat_text(self, system_prompt: str, user_message: str) -> str:
        if not self.api_key:
            return ""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        payload.update(prompt_cache_model_kwargs())
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                return ""
            return str(response.json()["choices"][0]["message"].get("content") or "")

    async def chat_text_stream(self, system_prompt: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        if not self.api_key:
            return

        payload_messages = [{"role": "system", "content": system_prompt}, *messages]
        payload: dict[str, Any] = {"model": self.model, "messages": payload_messages, "stream": True}
        payload.update(prompt_cache_model_kwargs())
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError:
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ").strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    choices = chunk.get("choices")
                    # Usage-only stream events can have no completion choices.
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        continue
                    delta = choices[0].get("delta")
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if content:
                        yield str(content)
