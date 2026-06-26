"""Tests for the MCP surface (tool registration gated by exposure level)."""

from __future__ import annotations

from pathlib import Path

from ha_spark.agent.mcp_server import build_mcp
from ha_spark.api.server import AppState
from ha_spark.config import Settings


def _state(tmp_path: Path, exposure: str) -> AppState:
    return AppState(  # type: ignore[call-arg]
        settings=Settings(ha_url="http://ha.test", ha_token="x", agent_exposure=exposure),
        options_path=tmp_path / "options.json",
    )


async def test_read_mode_excludes_act_tools(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, "read"))
    names = {t.name for t in await mcp.list_tools()}
    assert "get_plan" in names
    assert "run_plan" not in names and "add_context" not in names


async def test_read_write_includes_set_config(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, "read_write"))
    names = {t.name for t in await mcp.list_tools()}
    assert {"run_plan", "set_config"} <= names
