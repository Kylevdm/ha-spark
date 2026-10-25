"""Redacted entry diagnostics with validated display data and fixed link outcomes.

Only the last counted summary and version metadata are exposed, never HTTP
objects, headers, raw payloads or exception text.
"""

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SparkConfigEntry, wire


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SparkConfigEntry,
) -> dict[str, Any]:
    """Return the last accepted summary and connection outcomes without credentials."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), {"auth_token", "host"}),
        "summary": (
            json.loads(json.dumps(asdict(coordinator.summary), default=datetime.isoformat))
            if coordinator.summary is not None else None
        ),
        "last_outcome": coordinator.last_outcome,
        "last_outcome_at": (
            coordinator.last_outcome_at.isoformat() if coordinator.last_outcome_at else None
        ),
        "integration_api_version": coordinator.api_version,
        "supported_api_versions": sorted(wire.SUPPORTED_API_VERSIONS),
        "addon_api_version": coordinator.addon_api_version,
    }
