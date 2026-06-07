"""Integration test: run the WS client against a fake HA WebSocket server."""

from __future__ import annotations

import asyncio
import json

from websockets.asyncio.server import ServerConnection, serve

from ha_spark.ha.models import StateChangedEvent
from ha_spark.ha.websocket import HomeAssistantWebSocket


async def _fake_ha_handler(ws: ServerConnection) -> None:
    # Auth handshake.
    await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.6.0"}))
    auth = json.loads(await ws.recv())
    assert auth["type"] == "auth"
    assert auth["access_token"] == "test-token"
    await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.6.0"}))

    # Subscription.
    sub = json.loads(await ws.recv())
    assert sub["type"] == "subscribe_events"
    await ws.send(json.dumps({"id": sub["id"], "type": "result", "success": True}))

    # Push one state_changed event.
    await ws.send(
        json.dumps(
            {
                "id": sub["id"],
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.kitchen",
                        "old_state": {"entity_id": "light.kitchen", "state": "off",
                                      "attributes": {}},
                        "new_state": {"entity_id": "light.kitchen", "state": "on",
                                      "attributes": {}},
                    },
                },
            }
        )
    )
    # Keep the connection open so the client doesn't treat it as a disconnect.
    await asyncio.sleep(1.0)


async def test_auth_subscribe_and_dispatch() -> None:
    async with serve(_fake_ha_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/api/websocket"

        received: list[StateChangedEvent] = []
        done = asyncio.Event()

        async def listener(event: StateChangedEvent) -> None:
            received.append(event)
            done.set()

        client = HomeAssistantWebSocket(url, "test-token")
        client.add_listener(listener)
        client.start()
        try:
            await client.wait_connected(timeout=5)
            await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            await client.stop()

        assert len(received) == 1
        assert received[0].entity_id == "light.kitchen"
        assert received[0].new_state is not None
        assert received[0].new_state.state == "on"
