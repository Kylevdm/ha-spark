"""Tests for serving the FastAPI app via uvicorn (the daemon's HTTP API)."""

from __future__ import annotations

from pathlib import Path

import httpx

from ha_spark.api.server import (
    AppState,
    build_app,
    make_server,
    serve_in_background,
    stop_server,
)
from ha_spark.config import Settings


def _state(tmp_path: Path) -> AppState:
    return AppState(settings=Settings(), options_path=tmp_path / "options.json")  # type: ignore[call-arg]


async def test_serves_health_on_a_port(tmp_path: Path) -> None:
    server = make_server(build_app(_state(tmp_path)), "127.0.0.1", 8123)
    task = await serve_in_background(server)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:8123/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        await stop_server(server, task)
