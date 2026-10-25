"""Site connectivity and read-only fallback and hold mirrors."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SparkConfigEntry
from .coordinator import SparkCoordinator
from .entity import SparkEntity
from .wire import Summary


@dataclass(frozen=True, kw_only=True)
class SparkBinarySensorDescription(BinarySensorEntityDescription):
    """Fixed identity and allowlisted authority projections."""

    is_on_fn: Callable[[Summary], bool]
    attributes_fn: Callable[[Summary], dict[str, Any]]


DESCRIPTIONS = (
    SparkBinarySensorDescription(
        key="fallback_active", translation_key="fallback_active",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda s: s.authority.fallback.active,
        attributes_fn=lambda s: {"causes": list(s.authority.fallback.causes)},
    ),
    SparkBinarySensorDescription(
        key="ev_hold", translation_key="ev_hold",
        is_on_fn=lambda s: s.authority.holds.ev,
        attributes_fn=lambda s: {"cause": "ev"},
    ),
    SparkBinarySensorDescription(
        key="dispatch_hold", translation_key="dispatch_hold",
        is_on_fn=lambda s: s.authority.holds.dispatch,
        attributes_fn=lambda s: {
            "cause": "dispatch",
            "trusted_end": (s.authority.holds.trusted_end.isoformat()
                            if s.authority.holds.trusted_end else None),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SparkConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Register connectivity and summary-fed mirrors on the site device."""
    async_add_entities([
        AddonConnected(entry.runtime_data),
        *(SparkBinarySensor(entry.runtime_data, description) for description in DESCRIPTIONS),
    ])


class SparkBinarySensor(SparkEntity, BinarySensorEntity):
    """A read-only authority verdict, available only during an accepted session."""

    entity_description: SparkBinarySensorDescription

    def __init__(
        self, coordinator: SparkCoordinator, description: SparkBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.coordinator.connected and self.coordinator.summary is not None

    @property
    def is_on(self) -> bool | None:
        summary = self.coordinator.summary
        return self.entity_description.is_on_fn(summary) if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        summary = self.coordinator.summary
        return self.entity_description.attributes_fn(summary) if summary else None


class AddonConnected(SparkEntity, BinarySensorEntity):
    """Whether a heartbeat has counted within the current deadline."""

    _attr_has_entity_name = True
    _attr_translation_key = "addon_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: SparkCoordinator) -> None:
        super().__init__(coordinator, "addon_connected")

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
