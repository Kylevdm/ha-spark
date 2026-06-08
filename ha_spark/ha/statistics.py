"""One-shot Home Assistant long-term statistics query over WebSocket.

Long-term statistics are WS-only (no REST endpoint). This mirrors the one-shot
``HomeAssistantWebSocket.probe()`` pattern: connect, authenticate, send a single
``recorder/statistics_during_period`` command, await the matching result.
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
    req_id = 1
    async with asyncio.timeout(timeout), connect(ws_url, max_size=None) as ws:
        await _authenticate(ws, token)
        await ws.send(
            json.dumps(
                {
                    "id": req_id,
                    "type": "recorder/statistics_during_period",
                    "start_time": start_time.isoformat(),
                    "statistic_ids": [statistic_id],
                    "period": period,
                }
            )
        )
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") != req_id or msg.get("type") != "result":
                continue
            if not msg.get("success"):
                raise RuntimeError(f"statistics query failed: {msg.get('error')}")
            result = msg.get("result", {})
            return list(result.get(statistic_id, []))
