"""ha-spark companion integration.

One config entry per site (ADR-0011), paired through Supervisor discovery. The
heartbeat coordinator loads even with the add-on down and exposes site connectivity.
New discovery reloads the entry, fencing off the old session's in-flight replies.
Plan numbers are read-only sensors; mode, fallback and hold mirrors arrive later.
The optional Solcast service device is registered only while its key is configured.
"""

from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import SparkCoordinator
from .entity import async_clear_unregistered_state

SparkConfigEntry: TypeAlias = ConfigEntry[SparkCoordinator]
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SparkConfigEntry) -> bool:
    """Load connectivity even when the first heartbeat cannot count."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.version is not None
    coordinator = SparkCoordinator(hass, entry, integration.version)
    entry.runtime_data = coordinator
    for key in ("soc_now", "soc_integrity", "v2l_power_w", "predictions"):
        async_clear_unregistered_state(hass, f"sensor.ha_spark_{key}")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    registry = dr.async_get(hass)
    site_id = entry.data["site_id"]
    identifier = (DOMAIN, f"{site_id}_solcast")
    if entry.data.get("solcast_api_key"):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={identifier}, name="Solcast",
            entry_type=dr.DeviceEntryType.SERVICE, via_device=(DOMAIN, site_id),
        )
    elif device := registry.async_get_device_by_identifier(identifier, entry.entry_id):
        registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
    # The first poll runs after setup: a hung add-on must not hold setup open.
    entry.async_create_task(hass, coordinator.async_refresh(), "ha-spark first heartbeat")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SparkConfigEntry) -> bool:
    """End this session before a new discovery loads new connection data."""
    entry.runtime_data.async_stop()
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        return True
    return False
