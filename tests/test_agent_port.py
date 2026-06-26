"""Tests for the optional token-protected published port."""

from __future__ import annotations

from pathlib import Path

import httpx

from ha_spark.agent.auth import resolve_token
from ha_spark.api.server import (
    AGENT_PORT,
    AppState,
    build_app,
    make_server,
    serve_in_background,
    stop_server,
)
from ha_spark.config import Settings


async def test_published_port_requires_token(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test",
        ha_token="x",
        agent_api_token="sekret",
        db_path=str(tmp_path / "t.db"),
    )
    state = AppState(settings=settings, options_path=tmp_path / "options.json")
    token = resolve_token(settings, tmp_path / "agent_token")
    server = make_server(
        build_app(state, require_token=True, token=token), "127.0.0.1", AGENT_PORT
    )
    task = await serve_in_background(server)
    try:
        async with httpx.AsyncClient() as client:
            base = f"http://127.0.0.1:{AGENT_PORT}"
            # A plain route is gated.
            assert (await client.get(f"{base}/api/health")).status_code == 401
            ok = await client.get(
                f"{base}/api/health", headers={"Authorization": "Bearer sekret"}
            )
            assert ok.status_code == 200
            # The gate must also cover the mounted MCP app, not just routes.
            unauth_mcp = await client.post(
                f"{base}/mcp",
                json={},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert unauth_mcp.status_code == 401
    finally:
        await stop_server(server, task)
