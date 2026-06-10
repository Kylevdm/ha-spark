"""One-shot Home Assistant long-term statistics commands over WebSocket.

Long-term statistics are WS-only (no REST endpoint). Each helper mirrors the
one-shot ``HomeAssistantWebSocket.probe()`` pattern: connect, authenticate,
send a single recorder command, await the matching result.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from websockets.asyncio.client import connect

from ha_spark.ha.websocket import _authenticate
from ha_spark.logging import get_logger

log = get_logger(__name__)


async def _command(
    ws_url: str,
    token: str,
    payload: dict[str, Any],
    timeout: float,  # noqa: ASYNC109 - ergonomic; wraps asyncio.timeout
) -> Any:
    """Connect, authenticate, send one command, and return its ``result``."""
    req_id = 1
    async with asyncio.timeout(timeout), connect(ws_url, max_size=None) as ws:
        await _authenticate(ws, token)
        await ws.send(json.dumps({"id": req_id, **payload}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") != req_id or msg.get("type") != "result":
                continue
            if not msg.get("success"):
                raise RuntimeError(f"{payload['type']} failed: {msg.get('error')}")
            return msg.get("result")


async def statistics_during_period(
    ws_url: str,
    token: str,
    statistic_id: str,
    start_time: datetime,
    *,
    period: str = "day",
    timeout: float = 15.0,  # noqa: ASYNC109 - ergonomic; wraps asyncio.timeout
) -> list[dict[str, Any]]:
    """Return long-term statistics rows for one statistic since ``start_time``.

    Each row is a dict with keys such as ``start``, ``sum``, ``state``, and
    ``change`` (availability depends on HA version). Empty list if none.
    """
    result = await _command(
        ws_url,
        token,
        {
            "type": "recorder/statistics_during_period",
            "start_time": start_time.isoformat(),
            "statistic_ids": [statistic_id],
            "period": period,
        },
        timeout,
    )
    return list((result or {}).get(statistic_id, []))


async def list_statistic_ids(
    ws_url: str,
    token: str,
    *,
    timeout: float = 15.0,  # noqa: ASYNC109 - ergonomic; wraps asyncio.timeout
) -> list[dict[str, Any]]:
    """Return metadata for every statistic HA knows about.

    Each row has ``statistic_id``, ``statistics_unit_of_measurement``,
    ``has_mean``, ``has_sum`` (and display fields, HA-version dependent).
    """
    result = await _command(ws_url, token, {"type": "recorder/list_statistic_ids"}, timeout)
    return list(result or [])


async def import_statistics(
    ws_url: str,
    token: str,
    *,
    statistic_id: str,
    name: str,
    unit_of_measurement: str,
    stats: list[dict[str, Any]],
    timeout: float = 15.0,  # noqa: ASYNC109 - ergonomic; wraps asyncio.timeout
) -> None:
    """Import (upsert) long-term statistics rows via ``recorder/import_statistics``.

    ``statistic_id`` should be an external id (``<domain>:<name>``, e.g.
    ``ha_spark:house_load``) so no backing entity is required; rows upsert by
    (statistic_id, start), making re-imports idempotent. Each row in ``stats``
    is ``{"start": <ISO hour start>, "state": <kWh>, "sum": <cumulative kWh>}``.
    """
    await _command(
        ws_url,
        token,
        {
            "type": "recorder/import_statistics",
            "metadata": {
                "statistic_id": statistic_id,
                "source": statistic_id.split(":", 1)[0],
                "name": name,
                "unit_of_measurement": unit_of_measurement,
                "has_mean": False,
                "has_sum": True,
            },
            "stats": stats,
        },
        timeout,
    )
