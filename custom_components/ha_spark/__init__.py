"""ha-spark companion integration.

One config entry per site (ADR-0011), paired through Supervisor discovery. The
heartbeat coordinator loads even with the add-on down and exposes site connectivity.
New discovery reloads the entry, fencing off the old session's in-flight replies.
Authority and ownership storage load read-only; confirmed ownership selects the entity source.
The optional Solcast service device is registered only while its key is configured.
A configured topology starts an independent supply guard; no native writer is attached.
"""

import logging
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import SparkCoordinator
from .entity import async_clear_unregistered_state
from .guard_loop import SupplyGuardLoop
from .solcast_coordinator import SolcastCoordinator
from .topology import Topology, validate_topology

SparkConfigEntry: TypeAlias = ConfigEntry[SparkCoordinator]
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: SparkConfigEntry) -> bool:
    """Load connectivity even when the first heartbeat cannot count."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.version is not None
    coordinator = SparkCoordinator(hass, entry, integration.version)
    entry.runtime_data = coordinator
    await coordinator.authority.async_load()
    await coordinator.ownership.async_load()
    if "topology_revision" in entry.options:
        topology = validate_topology(entry.options.get("topology"))
        if isinstance(topology, Topology):
            coordinator.guard = SupplyGuardLoop(hass, entry, coordinator, topology)
    if entry.data.get("solcast_api_key"):
        coordinator.solcast = SolcastCoordinator(hass, entry)

    for key in ("soc_now", "soc_integrity", "v2l_power_w", "predictions"):
        async_clear_unregistered_state(hass, f"sensor.ha_spark_{key}")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    registry = dr.async_get(hass)
    site_id = entry.data["site_id"]
    identifier = (DOMAIN, f"{site_id}_solcast")
    if entry.data.get("solcast_api_key"):
        site = registry.async_get_device(identifiers={(DOMAIN, site_id)})
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={identifier}, name="Solcast",
            entry_type=dr.DeviceEntryType.SERVICE, via_device_id=site.id if site else None,
        )
    elif device := registry.async_get_device_by_identifier(identifier, entry.entry_id):
        registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
    # The first poll runs after setup: a hung add-on must not hold setup open.
    entry.async_create_task(hass, coordinator.async_refresh(), "ha-spark first heartbeat")

    async def start_solcast() -> None:
        try:
            if coordinator.solcast is not None:
                await coordinator.solcast.async_start()
            else:
                await Store[dict[str, object]](
                    hass, 1, f"{DOMAIN}.solcast_cache.{entry.entry_id}", atomic_writes=True,
                ).async_remove()
        except Exception:
            _LOGGER.warning("Solcast startup failed")

    entry.async_create_background_task(hass, start_solcast(), "Solcast startup")
    if coordinator.guard is not None:
        entry.async_create_background_task(hass, coordinator.guard.async_start(),
                                           "Supply guard startup")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SparkConfigEntry) -> bool:
    """End this session before a new discovery loads new connection data."""
    entry.runtime_data.async_stop()
    if entry.runtime_data.guard is not None:
        await entry.runtime_data.guard.async_stop()
    if entry.runtime_data.solcast is not None:
        await entry.runtime_data.solcast.async_shutdown()
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        return True
    return False
