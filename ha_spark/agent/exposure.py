"""The agent-surface exposure gate: one predicate, evaluated per request.

Both agent adapters -- the ``/agent/*`` routes (:mod:`ha_spark.api.server`) and
the ``/mcp`` tools (:mod:`ha_spark.agent.mcp_server`) -- register every
route/tool once and ask :func:`allowed` per call, which reads the live
``state.settings``. Capturing the gate at build time is the leak this module
exists to prevent: ``POST /api/config`` rewrites ``state.settings`` at runtime
and the surface must follow without a restart (#93).

Tiers are cumulative (``read`` < ``act`` < ``write``) and matched against the
``agent_exposure`` ladder (``read`` < ``read_act`` < ``read_write``).
``agent_surface == "off"`` is the master switch: it denies every tier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ha_spark.api.server import AppState

Tier = Literal["read", "act", "write"]

_TIER_RANK: dict[str, int] = {"read": 0, "act": 1, "write": 2}
_EXPOSURE_RANK: dict[str, int] = {"read": 0, "read_act": 1, "read_write": 2}

# The single operation -> tier table. Both adapters resolve a concrete operation
# (an MCP tool name, or a /agent/* route mapped in api.server) through this map,
# so retiering (or adding) an operation is one edit and the two surfaces can't
# drift apart -- the "matrix written twice" leak #93 flags.
OPERATION_TIERS: dict[str, Tier] = {
    "get_plan": "read",
    "get_state": "read",
    "get_forecast": "read",
    "get_predictions": "read",
    "get_health": "read",
    "get_context": "read",
    "add_context": "act",
    "run_plan": "act",
    "set_config": "write",
}


def surface_on(state: AppState) -> bool:
    """Whether the agent surface exists at all (the ``agent_surface`` master switch)."""
    return state.settings.agent_surface == "on"


def allowed(state: AppState, tier: Tier) -> bool:
    """Whether an operation at ``tier`` may be served right now.

    Reads ``state.settings`` on every call; never cache the answer --
    ``AppState.apply_options`` replaces the settings object at runtime.
    """
    if not surface_on(state):
        return False
    return _TIER_RANK[tier] <= _EXPOSURE_RANK[state.settings.agent_exposure]
