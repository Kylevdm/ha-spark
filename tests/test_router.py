"""Tests for the two-tier message router (Ollama primary, offline fallback)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from ha_spark import router
from ha_spark.config import Settings
from ha_spark.intent_parser import IntentResult
from ha_spark.router import route_message

OLLAMA = "http://ollama.test"
REST = object()  # only forwarded to the offline parser

_TAGS_OK = httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]})


def _settings() -> Settings:
    return Settings(ollama_url=OLLAMA, ollama_model="qwen3:14b")


def _patch_offline(monkeypatch: pytest.MonkeyPatch, text: str = "offline answer") -> None:
    async def fake_offline(message: str, settings: Settings, rest: Any) -> IntentResult:
        return IntentResult(text, matched=True)

    monkeypatch.setattr(router, "parse_offline", fake_offline)


@respx.mock
async def test_routes_to_ollama_when_healthy() -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=_TAGS_OK)
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "hello from the model"}}
        )
    )
    result = await route_message("hi", _settings(), REST)  # type: ignore[arg-type]
    assert result.source == "ollama"
    assert result.text == "hello from the model"


@respx.mock
async def test_falls_back_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("unreachable"))
    _patch_offline(monkeypatch)
    result = await route_message("plan?", _settings(), REST)  # type: ignore[arg-type]
    assert result.source == "offline"
    assert result.text == "offline answer"


@respx.mock
async def test_falls_back_when_chat_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=_TAGS_OK)
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(500))
    _patch_offline(monkeypatch)
    result = await route_message("plan?", _settings(), REST)  # type: ignore[arg-type]
    assert result.source == "offline"
    assert result.text == "offline answer"
