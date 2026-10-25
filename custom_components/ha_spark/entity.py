"""Shared site entity identity and migration of the add-on's former pushed states."""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SparkCoordinator


class SparkEntity(CoordinatorEntity[SparkCoordinator]):
    """A read-only entity on the site's service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SparkCoordinator, key: str) -> None:
        super().__init__(coordinator)
        site_id = coordinator.entry.data["site_id"]
        self._attr_unique_id = f"{site_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site_id)}, name="ha-spark", entry_type=DeviceEntryType.SERVICE,
        )


@callback
def async_clear_unregistered_state(hass: HomeAssistant, entity_id: str) -> None:
    """Free a requested id without touching any registered entity's state."""
    if er.async_get(hass).async_get(entity_id) is None:
        hass.states.async_remove(entity_id)
