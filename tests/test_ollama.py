"""Tests for the Ollama client chat surface."""

from __future__ import annotations

import httpx
import pytest
import respx

from ha_spark.ollama import OllamaClient

OLLAMA = "http://ollama.test"


@respx.mock
async def test_chat_returns_reply_text() -> None:
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "The plan is fine."}},
        )
    )
    async with OllamaClient(OLLAMA) as client:
        reply = await client.chat(
            [{"role": "user", "content": "how's the plan?"}],
            model="qwen3:14b",
            num_ctx=32768,
        )
    assert reply == "The plan is fine."

    body = route.calls.last.request.content
    assert b'"stream": false' in body or b'"stream":false' in body
    assert b"qwen3:14b" in body
    assert b"32768" in body


@respx.mock
async def test_chat_raises_on_http_error() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(500))
    async with OllamaClient(OLLAMA) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(
                [{"role": "user", "content": "hi"}], model="qwen3:14b", num_ctx=8192
            )
