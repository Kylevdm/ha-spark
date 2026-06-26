"""FastMCP server exposing the agent tool core, gated by exposure level."""

from __future__ import annotations

from datetime import date

from mcp.server.fastmcp import FastMCP

from ha_spark.agent import tools
from ha_spark.api.server import AppState


def build_mcp(state: AppState) -> FastMCP:
    """Build a FastMCP server registering the tool core for ``state``'s exposure.

    Read tools are always registered; act tools (add_context, run_plan) at
    read_act+; set_config only at read_write. Each tool reads ``state.settings``
    live when called.
    """
    mcp = FastMCP("ha-spark")
    exposure = state.settings.agent_exposure

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

    if exposure in ("read_act", "read_write"):

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

    if exposure == "read_write":

        @mcp.tool()
        async def set_config(updates: dict[str, object]) -> dict[str, object]:
            """Update whitelisted ha-spark options (hot-reloaded)."""
            state.apply_options(updates)
            return state.current_options()

    return mcp
