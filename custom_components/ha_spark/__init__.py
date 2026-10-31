"""ha-spark companion integration.

One config entry per site (ADR-0011), paired through Supervisor discovery. The
heartbeat coordinator loads even with the add-on down and exposes site connectivity.
New discovery reloads the entry, fencing off the old session's in-flight replies.
Authority and ownership storage load read-only; confirmed ownership selects the entity source.
Authority loads before native startup reconciliation; legacy sites retain their mirror.
The optional Solcast service device is registered only while its key is configured.
A configured topology starts an independent supply guard; no native writer is attached.
"""

import logging
from datetime import datetime, timedelta
from typing import TypeAlias
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SparkCoordinator
from .entity import async_clear_unregistered_state
from .guard_loop import SupplyGuardLoop
from .owner_services import async_handover, async_register
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
    hass.data.setdefault(f"{DOMAIN}.unloaded_authority", {}).pop(entry.entry_id, None)
    await coordinator.authority.async_load()
    await coordinator.ownership.async_load()
    async_register(hass)
    await coordinator.native.async_start()

    async def native_tick(now: datetime) -> None:
        await coordinator.native.async_check_connections()

    entry.async_on_unload(async_track_time_interval(hass, native_tick, timedelta(seconds=30)))

    async def authority_tick(now: datetime) -> None:
        zone = coordinator.control.household_zone() or ZoneInfo("UTC")
        await coordinator.control.async_expire(now=now, zone=zone)
        await coordinator.control.async_retry()

    entry.async_on_unload(async_track_time_interval(hass, authority_tick, timedelta(seconds=30)))
    zone = coordinator.control.household_zone() or ZoneInfo("UTC")
    await coordinator.control.async_start(now=dt_util.utcnow(), zone=zone)
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
    if entry.disabled_by is not None:
        await async_handover(hass, entry)
    entry.runtime_data.async_stop()
    if entry.runtime_data.guard is not None:
        await entry.runtime_data.guard.async_stop()
    if entry.runtime_data.solcast is not None:
        await entry.runtime_data.solcast.async_shutdown()
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        # HA deletes runtime_data before invoking async_remove_entry. Retain the
        # writer seam for that one handover attempt; a reload replaces it above.
        hass.data.setdefault(f"{DOMAIN}.unloaded_authority", {})[entry.entry_id] = (
            entry.runtime_data.control
        )
        return True
    return False


async def async_remove_entry(hass: HomeAssistant, entry: SparkConfigEntry) -> None:
    """Removal can follow unloading; reload durable authority for that case."""
    control = hass.data.setdefault(f"{DOMAIN}.unloaded_authority", {}).pop(entry.entry_id, None)
    if control is None:
        coordinator = SparkCoordinator(hass, entry, "unknown")
        await coordinator.authority.async_load()
        control = coordinator.control
    await async_handover(hass, entry, control)
