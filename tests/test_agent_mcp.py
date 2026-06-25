"""MCP surface at /mcp: tool registration gated by exposure + a live initialize.

The registration tests pin which tools each exposure tier exposes (mirroring the
/agent/* route gating). The live initialize test pins that the mount path,
lifespan wiring, and transport settings are all correct -- a registration test
alone would pass even with a broken mount.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ha_spark.agent.mcp_server import build_mcp
from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings


def _state(tmp_path: Path, exposure: str) -> AppState:
    return AppState(  # type: ignore[call-arg]
        settings=Settings(  # type: ignore[call-arg]
            ha_url="http://ha.test",
            ha_token="x",
            db_path=str(tmp_path / "t.db"),
            agent_exposure=exposure,  # type: ignore[arg-type]
        ),
        options_path=tmp_path / "options.json",
    )


async def test_read_mode_excludes_act_tools(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, "read"))
    names = {t.name for t in await mcp.list_tools()}
    assert "get_plan" in names
    assert "run_plan" not in names and "add_context" not in names
    assert "set_config" not in names


async def test_read_act_includes_act_excludes_set_config(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, "read_act"))
    names = {t.name for t in await mcp.list_tools()}
    assert {"add_context", "run_plan"} <= names
    assert "set_config" not in names


async def test_read_write_includes_set_config(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, "read_write"))
    names = {t.name for t in await mcp.list_tools()}
    assert {"run_plan", "set_config"} <= names


def test_mcp_endpoint_initializes(tmp_path: Path) -> None:
    """The mounted /mcp endpoint completes a real MCP initialize handshake.

    POST to /mcp/ WITH the trailing slash (the endpoint lives at /mcp/; bare
    /mcp 307-redirects). This pins mount path + lifespan + transport settings.
    """
    with TestClient(build_app(_state(tmp_path, "read_act"))) as client:
        r = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    assert r.status_code == 200
    assert "serverInfo" in r.text


def test_mcp_requires_token_on_published_port(tmp_path: Path) -> None:
    """On the published port (require_token), /mcp must reject a token-less request.

    FastAPI router dependencies don't reach mounted sub-apps, so without an
    explicit gate /mcp would be the one unauthenticated hole on the published
    port. This pins that it 401s like /api/* and /agent/* do.
    """
    app = build_app(_state(tmp_path, "read_act"), require_token=True, token="sekret")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app) as client:
        no_token = client.post("/mcp/", json=init, headers=hdr)
        assert no_token.status_code == 401
        assert "sekret" not in no_token.text
        ok = client.post("/mcp/", json=init, headers={**hdr, "Authorization": "Bearer sekret"})
        assert ok.status_code == 200
