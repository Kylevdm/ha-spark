"""The ``health``/doctor command: probe everything ha-spark depends on.

Checks the HA REST API (via the Supervisor proxy or a dev URL), the HA WebSocket
auth handshake, the remote Ollama endpoint, and that the SQLite path is writable.
Output is human-readable. The exit code is ``0`` (all green), ``1`` (a critical
dependency failed — HA or SQLite), or ``2`` (degraded — only Ollama is down, for
which the deterministic offline parser is a valid fallback).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import aiosqlite

from ha_spark.config import Settings
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.ha.websocket import HomeAssistantWebSocket
from ha_spark.ollama import OllamaClient


class Status(Enum):
    """Outcome of a single health check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    """The result of one health check."""

    name: str
    status: Status
    detail: str


# Checks ha-spark cannot run without. Ollama is deliberately excluded: when it is
# unreachable the offline intent parser takes over, so that is degraded, not broken.
_CRITICAL = frozenset({"HA REST", "HA WS", "SQLite"})

_GLYPH = {Status.OK: "✓", Status.WARN: "⚠", Status.FAIL: "✗"}


async def check_ha_rest(settings: Settings) -> CheckResult:
    """Probe the HA REST API by fetching ``/config``."""
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            config = await rest.get_config()
        version = config.get("version", "?")
        return CheckResult("HA REST", Status.OK, f"HA {version} @ {settings.ha_rest_url}")
    except Exception as exc:  # noqa: BLE001 - a doctor reports failures, never raises
        return CheckResult("HA REST", Status.FAIL, f"{settings.ha_rest_url}: {exc!r}")


async def check_ha_websocket(settings: Settings) -> CheckResult:
    """Probe the HA WebSocket API by completing the auth handshake."""
    try:
        version = await HomeAssistantWebSocket.probe(
            settings.ha_websocket_url, settings.auth_token, timeout=settings.ha_timeout
        )
        return CheckResult("HA WS", Status.OK, f"auth ok (HA {version})")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("HA WS", Status.FAIL, f"{settings.ha_websocket_url}: {exc!r}")


async def check_ollama(settings: Settings) -> CheckResult:
    """Probe the remote Ollama endpoint and that the configured model is present."""
    try:
        async with OllamaClient(
            settings.ollama_url, timeout=settings.ollama_health_timeout
        ) as client:
            models = await client.list_models()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "Ollama",
            Status.WARN,
            f"unreachable @ {settings.ollama_url} ({exc!r}); "
            "offline parser will be used, LLM features limited",
        )
    if settings.ollama_model in models:
        return CheckResult(
            "Ollama", Status.OK, f"{settings.ollama_model} @ {settings.ollama_url}"
        )
    return CheckResult(
        "Ollama",
        Status.WARN,
        f"reachable but '{settings.ollama_model}' not pulled "
        f"(ollama pull {settings.ollama_model}); have {models or 'none'}",
    )


async def check_sqlite(settings: Settings) -> CheckResult:
    """Confirm the SQLite path is writable (creates the data dir if absent)."""
    try:
        path = Path(settings.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(path)) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS _ha_spark_healthcheck (ts TEXT)")
            await db.execute("INSERT INTO _ha_spark_healthcheck (ts) VALUES ('ok')")
            await db.execute("DELETE FROM _ha_spark_healthcheck")
            await db.execute("DROP TABLE _ha_spark_healthcheck")
            await db.commit()
        return CheckResult("SQLite", Status.OK, f"writable @ {settings.db_path}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("SQLite", Status.FAIL, f"{settings.db_path}: {exc!r}")


async def run_health(settings: Settings) -> list[CheckResult]:
    """Run all checks concurrently, returning results in a stable order."""
    return list(
        await asyncio.gather(
            check_ha_rest(settings),
            check_ha_websocket(settings),
            check_ollama(settings),
            check_sqlite(settings),
        )
    )


def format_report(results: list[CheckResult]) -> str:
    """Render results as aligned ``glyph name  detail`` lines."""
    width = max((len(r.name) for r in results), default=0)
    return "\n".join(f"{_GLYPH[r.status]} {r.name:<{width}}  {r.detail}" for r in results)


def exit_code(results: list[CheckResult]) -> int:
    """0 = all green, 1 = a critical check failed, 2 = degraded (Ollama only)."""
    if any(r.status is Status.FAIL and r.name in _CRITICAL for r in results):
        return 1
    if any(r.status is not Status.OK for r in results):
        return 2
    return 0
