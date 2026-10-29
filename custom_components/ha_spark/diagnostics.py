"""Redacted entry diagnostics with validated display data and fixed link outcomes.

Only the last counted summary, version metadata, Solcast health and the supply
guard's bounded state, history and raw readings are exposed, never HTTP objects,
headers, raw payloads, site identifiers or exception text.
"""

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import SparkConfigEntry, wire
from .coordinator import SparkCoordinator

# Entry fields that identify or authenticate the site; redaction never fails open.
TO_REDACT = {
    "auth_token", "host", "port", "session_id", "site_id", "solcast_api_key",
    "solcast_resource_ids", "solcast_cache_generation",
}


def _solcast_health(
    entry: SparkConfigEntry, coordinator: SparkCoordinator | None,
) -> dict[str, Any] | None:
    """Summarise Solcast without periods, ids or the key; None without a key."""
    if not entry.data.get("solcast_api_key"):
        return None
    solcast = coordinator.solcast if coordinator is not None else None
    if solcast is None:
        return {
            "status": None, "calls_used": None, "calls_limit": None,
            "fetch_age_s": None, "cached_periods": None,
        }
    fetched_at = solcast.data.fetched_at
    return {
        "status": solcast.data.status,
        "calls_used": solcast.budget.calls_used,
        "calls_limit": solcast.calls_limit,
        "fetch_age_s": (
            int((dt_util.utcnow() - fetched_at).total_seconds()) if fetched_at else None
        ),
        "cached_periods": len(solcast.data.periods),
    }


def _guard_health(coordinator: SparkCoordinator | None) -> dict[str, Any] | None:
    """The guard's own bounded view: state, history and raw readings."""
    guard = coordinator.guard if coordinator is not None else None
    return guard.diagnostics() if guard is not None else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SparkConfigEntry,
) -> dict[str, Any]:
    """Return the last accepted summary, link outcomes and Solcast health."""
    # The session id is the channel's comparison value; it never leaves the entry.
    redacted = async_redact_data(dict(entry.data), TO_REDACT)
    # Home Assistant deletes runtime_data on unload but still serves diagnostics.
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {
            "entry": redacted, "summary": None, "last_outcome": None, "last_outcome_at": None,
            "integration_api_version": None,
            "supported_api_versions": sorted(wire.SUPPORTED_API_VERSIONS),
            "addon_api_version": None,
            "solcast": _solcast_health(entry, None),
            "supply_guard": None,
        }
    return {
        "entry": redacted,
        "summary": (
            {key: value for key, value in json.loads(
                json.dumps(asdict(coordinator.summary), default=datetime.isoformat)
            ).items() if key != "session_id"}
            if coordinator.summary is not None else None
        ),
        "last_outcome": coordinator.last_outcome,
        "last_outcome_at": (
            coordinator.last_outcome_at.isoformat() if coordinator.last_outcome_at else None
        ),
        "integration_api_version": coordinator.api_version,
        "supported_api_versions": sorted(wire.SUPPORTED_API_VERSIONS),
        "addon_api_version": coordinator.addon_api_version,
        "solcast": _solcast_health(entry, coordinator),
        "supply_guard": _guard_health(coordinator),
    }
