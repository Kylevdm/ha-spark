"""Tests for the one-shot recorder WS commands against a fake HA server."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from ha_spark.ha.statistics import (
    import_statistics,
    list_statistic_ids,
    statistics_during_period,
)


def _handler(
    respond: Callable[[dict[str, Any]], dict[str, Any]],
    requests: list[dict[str, Any]],
) -> Callable[[ServerConnection], Any]:
    """A fake HA WS server: auth handshake, then answer one command."""

    async def handle(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.5.4"}))
        request = json.loads(await ws.recv())
        requests.append(request)
        await ws.send(json.dumps({"id": request["id"], "type": "result", **respond(request)}))

    return handle


async def test_statistics_during_period_returns_rows() -> None:
    rows = [{"start": 1780304400000, "change": 1.0}]
    requests: list[dict[str, Any]] = []
    handler = _handler(
        lambda req: {"success": True, "result": {"sensor.load": rows}}, requests
    )
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await statistics_during_period(
            f"ws://127.0.0.1:{port}", "tok", "sensor.load", datetime.now(UTC), period="hour"
        )
    assert result == rows
    assert requests[0]["type"] == "recorder/statistics_during_period"
    assert requests[0]["statistic_ids"] == ["sensor.load"]


async def test_list_statistic_ids_returns_metadata() -> None:
    metas = [{"statistic_id": "sensor.zappi", "statistics_unit_of_measurement": "W"}]
    requests: list[dict[str, Any]] = []
    handler = _handler(lambda req: {"success": True, "result": metas}, requests)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await list_statistic_ids(f"ws://127.0.0.1:{port}", "tok")
    assert result == metas
    assert requests[0]["type"] == "recorder/list_statistic_ids"


async def test_import_statistics_sends_metadata_and_stats() -> None:
    requests: list[dict[str, Any]] = []
    handler = _handler(lambda req: {"success": True, "result": None}, requests)
    stats = [{"start": "2026-06-01T00:00:00+00:00", "state": 1.0, "sum": 1.0}]
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await import_statistics(
            f"ws://127.0.0.1:{port}",
            "tok",
            statistic_id="ha_spark:house_load",
            name="house load",
            unit_of_measurement="kWh",
            stats=stats,
        )
    sent = requests[0]
    assert sent["type"] == "recorder/import_statistics"
    assert sent["stats"] == stats
    assert sent["metadata"] == {
        "statistic_id": "ha_spark:house_load",
        "source": "ha_spark",
        "name": "house load",
        "unit_of_measurement": "kWh",
        "has_mean": False,
        "has_sum": True,
    }


async def test_import_statistics_raises_on_failure() -> None:
    requests: list[dict[str, Any]] = []
    handler = _handler(
        lambda req: {"success": False, "error": {"message": "bad stats"}}, requests
    )
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(RuntimeError, match="bad stats"):
            await import_statistics(
                f"ws://127.0.0.1:{port}",
                "tok",
                statistic_id="ha_spark:house_load",
                name="house load",
                unit_of_measurement="kWh",
                stats=[],
            )
