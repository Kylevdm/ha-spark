"""Site connectivity, independent of Home Assistant's coordinator availability."""

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SparkConfigEntry
from .const import DOMAIN
from .coordinator import SparkCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: SparkConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the site's always-available connectivity indicator."""
    async_add_entities([AddonConnected(entry.runtime_data)])


class AddonConnected(CoordinatorEntity[SparkCoordinator], BinarySensorEntity):
    """Whether a heartbeat has counted within the current deadline."""

    _attr_has_entity_name = True
    _attr_translation_key = "addon_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: SparkCoordinator) -> None:
        super().__init__(coordinator)
        site_id = coordinator.entry.data["site_id"]
        self._attr_unique_id = f"{site_id}_addon_connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site_id)}, name="ha-spark", entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        counted = self.coordinator.last_counted
        return {"last_counted_heartbeat": counted.isoformat() if counted else None}
