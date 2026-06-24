"""Tests for serving the FastAPI app via uvicorn (the daemon's HTTP API)."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_serves_health_on_a_port(tmp_path: Path) -> None:
    port = _free_port()
    server = make_server(build_app(_state(tmp_path)), "127.0.0.1", port)
    task = await serve_in_background(server)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        await stop_server(server, task)


async def test_serve_in_background_raises_on_bind_failure(tmp_path: Path) -> None:
    """A second server on the same port must raise promptly, not hang forever.

    Regression test: uvicorn raises ``SystemExit`` internally when the socket
    bind fails, so ``server.started`` never flips True. ``serve_in_background``
    must detect the finished task and surface a normal ``Exception`` instead of
    spinning in its startup poll loop forever.
    """
    port = _free_port()
    first_server = make_server(build_app(_state(tmp_path)), "127.0.0.1", port)
    first_task = await serve_in_background(first_server)
    try:
        second_server = make_server(build_app(_state(tmp_path)), "127.0.0.1", port)
        with pytest.raises(Exception):  # noqa: B017 - asserting "raises, not hangs"
            await asyncio.wait_for(serve_in_background(second_server), timeout=5.0)
    finally:
        await stop_server(first_server, first_task)
