"""Tests for the health/doctor command."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from websockets.asyncio.server import ServerConnection, serve

from ha_spark import health
from ha_spark.config import Settings
from ha_spark.ha.websocket import HomeAssistantAuthError, HomeAssistantWebSocket
from ha_spark.health import (
    CheckResult,
    Status,
    check_ha_rest,
    check_ha_websocket,
    check_load_history,
    check_ollama,
    check_sqlite,
    exit_code,
    format_report,
)

HA = "http://ha.test"
OLLAMA = "http://ollama.test"


def _r(name: str, status: Status) -> CheckResult:
    return CheckResult(name, status, "")


# --- exit_code mapping (the core decision logic) ---


def test_exit_code_all_ok() -> None:
    results = [_r("HA REST", Status.OK), _r("HA WS", Status.OK), _r("SQLite", Status.OK)]
    assert exit_code([*results, _r("Ollama", Status.OK)]) == 0


def test_exit_code_degraded_when_only_ollama_down() -> None:
    results = [_r("HA REST", Status.OK), _r("HA WS", Status.OK), _r("SQLite", Status.OK)]
    assert exit_code([*results, _r("Ollama", Status.WARN)]) == 2


def test_exit_code_critical_failure() -> None:
    results = [_r("HA REST", Status.FAIL), _r("HA WS", Status.OK), _r("SQLite", Status.OK)]
    assert exit_code([*results, _r("Ollama", Status.OK)]) == 1


def test_exit_code_critical_beats_degraded() -> None:
    results = [_r("SQLite", Status.FAIL), _r("Ollama", Status.WARN)]
    assert exit_code(results) == 1


def test_format_report_uses_glyphs() -> None:
    out = format_report([_r("HA REST", Status.OK), _r("Ollama", Status.WARN)])
    assert "✓ HA REST" in out
    assert "⚠ Ollama" in out


# --- HA REST check ---


@respx.mock
async def test_check_ha_rest_ok() -> None:
    respx.get(f"{HA}/api/config").mock(
        return_value=httpx.Response(200, json={"version": "2026.5.4"})
    )
    res = await check_ha_rest(Settings(ha_url=HA, ha_token="tok"))
    assert res.status is Status.OK
    assert "2026.5.4" in res.detail


@respx.mock
async def test_check_ha_rest_fail() -> None:
    respx.get(f"{HA}/api/config").mock(side_effect=httpx.ConnectError("boom"))
    res = await check_ha_rest(Settings(ha_url=HA, ha_token="tok"))
    assert res.status is Status.FAIL


# --- Ollama check ---


@respx.mock
async def test_check_ollama_ok() -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]})
    )
    res = await check_ollama(Settings(ollama_url=OLLAMA, ollama_model="qwen3:14b"))
    assert res.status is Status.OK


@respx.mock
async def test_check_ollama_model_missing_warns() -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})
    )
    res = await check_ollama(Settings(ollama_url=OLLAMA, ollama_model="qwen3:14b"))
    assert res.status is Status.WARN
    assert "not pulled" in res.detail


@respx.mock
async def test_check_ollama_unreachable_warns() -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("down"))
    res = await check_ollama(Settings(ollama_url=OLLAMA, ollama_model="qwen3:14b"))
    assert res.status is Status.WARN
    assert "unreachable" in res.detail


# --- SQLite check ---


async def test_check_sqlite_ok(tmp_path: Path) -> None:
    db = tmp_path / "data" / "ha_spark.db"
    res = await check_sqlite(Settings(db_path=str(db)))
    assert res.status is Status.OK
    assert db.exists()


# --- Load history check ---


def _hourly_rows(days: int) -> list[dict[str, Any]]:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        {"start": (today - timedelta(days=d, hours=-h)).timestamp() * 1000, "change": 1.0}
        for d in range(days, 0, -1)
        for h in range(24)
    ]


async def test_check_load_history_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14)

    monkeypatch.setattr(health, "statistics_during_period", fake_stats)
    res = await check_load_history(Settings())
    assert res.status is Status.OK
    assert "slot profile ready" in res.detail


async def test_check_load_history_warns_when_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=3)

    monkeypatch.setattr(health, "statistics_during_period", fake_stats)
    res = await check_load_history(Settings())
    assert res.status is Status.WARN
    assert "backfill-load" in res.detail


async def test_check_load_history_warns_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(health, "statistics_during_period", fake_stats)
    res = await check_load_history(Settings())
    assert res.status is Status.WARN
    assert "no hourly history" in res.detail


async def test_check_load_history_warns_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ws down")

    monkeypatch.setattr(health, "statistics_during_period", boom)
    res = await check_load_history(Settings())
    assert res.status is Status.WARN


# --- WebSocket probe + check ---


async def _auth_ok_handler(ws: ServerConnection) -> None:
    await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.5.4"}))
    auth = json.loads(await ws.recv())
    assert auth["type"] == "auth"
    await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.5.4"}))


async def _auth_reject_handler(ws: ServerConnection) -> None:
    await ws.send(json.dumps({"type": "auth_required"}))
    await ws.recv()
    await ws.send(json.dumps({"type": "auth_invalid", "message": "bad token"}))


async def test_ws_probe_success() -> None:
    async with serve(_auth_ok_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        version = await HomeAssistantWebSocket.probe(
            f"ws://127.0.0.1:{port}/api/websocket", "test-token", timeout=5
        )
    assert version == "2026.5.4"


async def test_ws_probe_auth_reject() -> None:
    async with serve(_auth_reject_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(HomeAssistantAuthError):
            await HomeAssistantWebSocket.probe(
                f"ws://127.0.0.1:{port}/api/websocket", "test-token", timeout=5
            )


async def test_check_ha_websocket_unreachable_fails() -> None:
    # Nothing is listening on port 1 → probe errors → check reports FAIL.
    res = await check_ha_websocket(Settings(ha_url="http://127.0.0.1:1", ha_token="tok"))
    assert res.status is Status.FAIL
