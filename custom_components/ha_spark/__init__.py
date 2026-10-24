"""ha-spark companion integration.

One config entry per site (ADR-0011), created from the Supervisor discovery the
add-on announces. This phase only ships the skeleton: the entry cannot be
created yet, so setup has nothing to start. Later tickets add the heartbeat
coordinator, the plan sensors and the mode, fallback and hold mirrors.
"""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> bool:
    """Set up a ha-spark config entry."""
    return True
