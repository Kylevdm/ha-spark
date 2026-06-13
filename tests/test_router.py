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


# --- Phase 6D: context extraction / query routing ---

from datetime import date  # noqa: E402

from ha_spark.energy.context import ContextStore  # noqa: E402


def _ctx_settings(tmp_path: Any) -> Settings:
    return Settings(ollama_url=OLLAMA, ollama_model="qwen3:14b", db_path=str(tmp_path / "c.db"))


@respx.mock
async def test_context_statement_extracted_via_ollama(tmp_path: Any) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=_TAGS_OK)
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={"message": {"content": '{"kind":"away","start":"2026-07-01",'
                                          '"end":"2026-07-14","note":"Italy"}'}},
        )
    )
    s = _ctx_settings(tmp_path)
    result = await route_message("I'm away from 1 to 14 July", s, REST)  # type: ignore[arg-type]
    assert result.source == "ollama"
    assert "Noted" in result.text and "away" in result.text

    async with ContextStore(s.db_path) as store:
        assert len(await store.active_on(date(2026, 7, 7))) == 1


@respx.mock
async def test_context_statement_extracted_offline_when_ollama_down(tmp_path: Any) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("down"))
    s = _ctx_settings(tmp_path)
    result = await route_message("I'm away from 2026-07-01 to 2026-07-14", s, REST)  # type: ignore[arg-type]
    assert result.source == "offline"
    assert "Noted" in result.text

    async with ContextStore(s.db_path) as store:
        assert len(await store.active_on(date(2026, 7, 7))) == 1


@respx.mock
async def test_ollama_null_extraction_falls_through_to_chat(tmp_path: Any) -> None:
    # Reachable model declines to extract (null), then answers as plain chat.
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=_TAGS_OK)
    chat = respx.post(f"{OLLAMA}/api/chat")
    chat.side_effect = [
        httpx.Response(200, json={"message": {"content": "null"}}),  # extraction
        httpx.Response(200, json={"message": {"content": "the plan is fine"}}),  # chat
    ]
    s = _ctx_settings(tmp_path)
    # "next week" trips the prefilter but the model declines -> chat answers.
    result = await route_message("what should I do next week with the battery", s, REST)  # type: ignore[arg-type]
    assert result.source == "ollama"
    assert result.text == "the plan is fine"

    async with ContextStore(s.db_path) as store:
        assert await store.list_all() == []


@respx.mock
async def test_context_query_answered_from_store(tmp_path: Any) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=_TAGS_OK)
    s = _ctx_settings(tmp_path)
    async with ContextStore(s.db_path) as store:
        await store.add("away", date(2026, 7, 1), date(2026, 7, 14), note="Italy")
    result = await route_message("what do you know about my holidays?", s, REST)  # type: ignore[arg-type]
    assert result.source == "offline"
    assert "away" in result.text and "Italy" in result.text


@respx.mock
async def test_non_context_message_unaffected(tmp_path: Any) -> None:
    # No context hints -> straight to chat, no extraction call.
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=_TAGS_OK)
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "42% charged"}})
    )
    s = _ctx_settings(tmp_path)
    result = await route_message("what's the battery soc", s, REST)  # type: ignore[arg-type]
    assert result.source == "ollama"
    assert result.text == "42% charged"
