"""Async Home Assistant WebSocket client.

Handles the auth handshake, subscribes to ``state_changed`` events, dispatches
them to registered listeners, and reconnects with exponential backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from ha_agent.ha.models import StateChangedEvent
from ha_agent.logging import get_logger

log = get_logger(__name__)

StateListener = Callable[[StateChangedEvent], Awaitable[None]]

# Reconnect backoff schedule (seconds), mirroring the project's retry guidance.
_BACKOFF_SCHEDULE = (2, 4, 8, 16)


class HomeAssistantAuthError(RuntimeError):
    """Raised when the WebSocket auth handshake is rejected."""


class HomeAssistantWebSocket:
    """Maintains a WebSocket connection to Home Assistant and streams events."""

    def __init__(self, ws_url: str, token: str) -> None:
        self._ws_url = ws_url
        self._token = token
        self._listeners: list[StateListener] = []
        self._ids = itertools.count(1)
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()

    def add_listener(self, listener: StateListener) -> None:
        """Register a coroutine called for every ``state_changed`` event."""
        self._listeners.append(listener)

    def start(self) -> None:
        """Start the background connection loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ha-websocket")

    async def stop(self) -> None:
        """Stop the background connection loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected.clear()

    async def wait_connected(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Block until the initial subscription is established."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                async with connect(self._ws_url, max_size=None) as ws:
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    attempt = 0  # reset backoff after a clean connect
                    self._connected.set()
                    await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except HomeAssistantAuthError:
                log.error("WebSocket authentication failed; not retrying")
                raise
            except (OSError, websockets.WebSocketException) as exc:
                self._connected.clear()
                delay = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
                attempt += 1
                log.warning("WebSocket disconnected (%s); reconnecting in %ss", exc, delay)
                await asyncio.sleep(delay)

    async def _authenticate(self, ws: ClientConnection) -> None:
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise HomeAssistantAuthError(f"unexpected first message: {msg.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        result = json.loads(await ws.recv())
        if result.get("type") != "auth_ok":
            raise HomeAssistantAuthError(f"auth rejected: {result.get('type')}")
        log.info("WebSocket authenticated (HA %s)", result.get("ha_version", "?"))

    async def _subscribe(self, ws: ClientConnection) -> None:
        sub_id = next(self._ids)
        await ws.send(
            json.dumps(
                {"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}
            )
        )
        result = json.loads(await ws.recv())
        if not (result.get("type") == "result" and result.get("success")):
            raise HomeAssistantAuthError(f"subscribe failed: {result}")
        log.info("Subscribed to state_changed events")

    async def _consume(self, ws: ClientConnection) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "event":
                continue
            event = msg.get("event", {})
            if event.get("event_type") != "state_changed":
                continue
            await self._dispatch(event.get("data", {}))

    async def _dispatch(self, data: dict[str, Any]) -> None:
        try:
            parsed = StateChangedEvent.model_validate(data)
        except Exception:  # noqa: BLE001 - never let a bad payload kill the loop
            log.exception("Failed to parse state_changed payload")
            return
        for listener in self._listeners:
            try:
                await listener(parsed)
            except Exception:  # noqa: BLE001 - isolate listener failures
                log.exception("State listener raised")
