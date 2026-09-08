"""FastMCP server exposing the agent tool core, gated per request.

Mounts at ``/mcp`` from :func:`ha_spark.api.server.build_app`. The tools are the
same protocol-agnostic core the ``/agent/*`` routes use (:mod:`ha_spark.agent.tools`);
this module only adapts them to MCP and applies the same exposure gating, so an
LLM client sees exactly the tools the configured tier permits. The act/write
tools route through the same PROACTIVE_MODE-gated / ``_OPTION_KEYS``-whitelisted
paths as the routes -- the model never reaches ``call_service`` directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ContentBlock
from mcp.types import Tool as MCPTool

from ha_spark.agent import tools
from ha_spark.agent.exposure import OPERATION_TIERS, Tier, allowed
from ha_spark.api.server import AppState


class _Gated(FastMCP):
    """FastMCP with every tool registered once, gated per request by :func:`allowed`.

    ``list_tools`` hides and ``call_tool`` rejects (as ``Unknown tool``, so a
    denied tool reads the same as one absent below the tier) anything denied at
    the live tier. ``state.settings`` is read per call -- never captured at
    ``build_mcp`` time -- so a runtime exposure change applies without a
    rebuild, and ``agent_surface == "off"`` denies every tool (#93).
    """

    def __init__(self, state: AppState, **settings: Any) -> None:
        super().__init__("ha-spark", **settings)
        self._state = state

    def _permitted(self, name: str) -> bool:
        tier: Tier | None = OPERATION_TIERS.get(name)
        return tier is not None and allowed(self._state, tier)

    async def list_tools(self) -> list[MCPTool]:
        return [t for t in await super().list_tools() if self._permitted(t.name)]

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        if not self._permitted(name):
            raise ToolError(f"Unknown tool: {name}")
        return await super().call_tool(name, arguments)


def build_mcp(state: AppState) -> FastMCP:
    """Build the FastMCP server: every tool registered once, gated at call time.

    The gate is :func:`~ha_spark.agent.exposure.allowed` evaluated per request
    against the live ``state.settings``, so ``POST /api/config`` changes apply
    without a restart; the ``agent_surface`` master switch denies the surface.
    """
    mcp = _Gated(
        state,
        # Mounting the streamable-HTTP app at /mcp with the default path ("/mcp")
        # would serve the endpoint at /mcp/mcp; "/" makes it /mcp/.
        streamable_http_path="/",
        # Tool server: no per-client session state, so skip session-id round-trips.
        stateless_http=True,
        # DNS-rebinding Host check disabled by design decision (2026-06-25): the
        # /mcp surface already sits behind HA ingress auth (ingress) + the bearer
        # token (published port), which cover the same threat; hosts are dynamic
        # behind ingress/Nabu Casa so a fixed allow-list would reject legitimate
        # clients.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def get_plan() -> dict[str, object]:
        """Current computed charge plan as HA sensor entities."""
        return (await tools.get_plan(state.settings)).model_dump()

    @mcp.tool()
    async def get_state() -> dict[str, object]:
        """Live energy inputs (SoC, solar, EV, rates)."""
        return (await tools.get_state(state.settings)).model_dump()

    @mcp.tool()
    async def get_forecast() -> dict[str, object]:
        """Tomorrow's load forecast and its source."""
        return (await tools.get_forecast(state.settings)).model_dump()

    @mcp.tool()
    async def get_predictions() -> dict[str, object]:
        """Proactive decisions/predictions for tomorrow."""
        return (await tools.get_predictions(state.settings)).model_dump()

    @mcp.tool()
    async def get_health() -> dict[str, object]:
        """Doctor checks (HA/Ollama/DB/history)."""
        return (await tools.get_health(state.settings)).model_dump()

    @mcp.tool()
    async def get_context() -> dict[str, object]:
        """Stored household context facts."""
        return (await tools.get_context(state.settings)).model_dump()

    @mcp.tool()
    async def add_context(
        kind: str, start_date: str, end_date: str, note: str = ""
    ) -> dict[str, object]:
        """Add a household context fact (e.g. away/guests). Dates are ISO YYYY-MM-DD."""
        return (
            await tools.add_context(
                state.settings,
                kind,
                date.fromisoformat(start_date),
                date.fromisoformat(end_date),
                note=note,
            )
        ).model_dump()

    @mcp.tool()
    async def run_plan() -> dict[str, object]:
        """Recompute and apply the plan now (apply still PROACTIVE_MODE-gated)."""
        return (await tools.run_plan(state.settings)).model_dump()

    @mcp.tool()
    async def set_config(updates: dict[str, object]) -> dict[str, object]:
        """Update whitelisted ha-spark options (hot-reloaded)."""
        state.apply_options(updates)
        return state.current_options()

    return mcp
