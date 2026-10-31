"""Site connectivity and read-only fallback, hold and supply-guard mirrors."""

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
from .guard_loop import SupplyGuardLoop
from .native_entities import native_bits
from .native_gateway import GatewayClaim, GatewayConnected, gateways
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
    entities: list[BinarySensorEntity] = [
        AddonConnected(entry.runtime_data),
        *(SparkBinarySensor(entry.runtime_data, description) for description in DESCRIPTIONS),
    ]
    if entry.runtime_data.guard is not None:
        entities.append(SupplyGuardLimiting(entry.runtime_data, entry.runtime_data.guard))
    for inverter in gateways(entry.runtime_data):
        entities.extend([
            GatewayConnected(entry.runtime_data, inverter, "gateway_connected"),
            GatewayClaim(entry.runtime_data, inverter, "gateway_claim"),
        ])
    entities.extend(native_bits(entry.runtime_data))
    async_add_entities(entities)


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
        if self.local_authority:
            return self.coordinator.authority.state is not None
        return self.coordinator.connected and self.coordinator.summary is not None

    @property
    def is_on(self) -> bool | None:
        if self.local_authority:
            state = self.coordinator.authority.state
            if state is None:
                return None
            key = self.entity_description.key
            if key == "fallback_active":
                return state.fallback.active
            cause = "ev" if key == "ev_hold" else "dispatch"
            return any(hold.cause == cause for hold in state.holds)
        summary = self.coordinator.summary
        return self.entity_description.is_on_fn(summary) if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.local_authority:
            state = self.coordinator.authority.state
            if state is None:
                return None
            key = self.entity_description.key
            if key == "fallback_active":
                return {"causes": list(state.fallback.causes)}
            if key == "ev_hold":
                return {"cause": "ev"}
            ends = [hold.trusted_end for hold in state.holds
                    if hold.cause == "dispatch" and hold.trusted_end is not None]
            return {"cause": "dispatch", "trusted_end": max(ends).isoformat() if ends else None}
        summary = self.coordinator.summary
        return self.entity_description.attributes_fn(summary) if summary else None


class SupplyGuardLimiting(SparkEntity, BinarySensorEntity):
    """Whether the independent supply guard holds a cut, add-on or not."""

    _attr_translation_key = "supply_guard_limiting"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: SparkCoordinator, guard: SupplyGuardLoop,
    ) -> None:
        super().__init__(coordinator, "supply_guard_limiting")
        self._guard = guard

    @property
    def available(self) -> bool:
        return self._guard.started

    @property
    def is_on(self) -> bool | None:
        return self._guard.limiting if self._guard.started else None


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
