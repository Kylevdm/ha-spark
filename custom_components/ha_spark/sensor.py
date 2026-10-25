"""Read-only plan numbers and V2L session totals on the site device."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SparkConfigEntry
from .coordinator import SparkCoordinator
from .entity import SparkEntity, async_clear_unregistered_state
from .wire import Summary


@dataclass(frozen=True, kw_only=True)
class SparkSensorDescription(SensorEntityDescription):
    """Fixed identity, availability and value projection for a site sensor."""

    requested_entity_id: str
    soc_derived: bool = False
    v2l: bool = False
    value_fn: Callable[[Summary], float | datetime | None]


DESCRIPTIONS = (
    SparkSensorDescription(
        key="charge_needed", translation_key="charge_needed",
        requested_entity_id="sensor.ha_spark_charge_needed_kwh", soc_derived=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda s: s.plan.charge_needed_kwh if s.plan else None,
    ),
    SparkSensorDescription(
        key="target_soc", translation_key="target_soc",
        requested_entity_id="sensor.ha_spark_target_soc", soc_derived=True,
        device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.plan.target_soc_pct if s.plan else None,
    ),
    SparkSensorDescription(
        key="forecast_load", translation_key="forecast_load",
        requested_entity_id="sensor.ha_spark_forecast_load_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda s: s.plan.forecast_load_kwh if s.plan else None,
    ),
    SparkSensorDescription(
        key="solar_forecast", translation_key="solar_forecast",
        requested_entity_id="sensor.ha_spark_solar_forecast_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda s: s.plan.solar_forecast_kwh if s.plan else None,
    ),
    SparkSensorDescription(
        key="deficit", translation_key="deficit", soc_derived=True,
        requested_entity_id="sensor.ha_spark_deficit_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda s: s.plan.deficit_kwh if s.plan else None,
    ),
    SparkSensorDescription(
        key="planned_cost", translation_key="planned_cost", soc_derived=True,
        requested_entity_id="sensor.ha_spark_planned_cost",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement="GBP",
        value_fn=lambda s: s.plan.planned_cost_gbp if s.plan else None,
    ),
    SparkSensorDescription(
        key="baseline_cost", translation_key="baseline_cost", soc_derived=True,
        requested_entity_id="sensor.ha_spark_baseline_cost",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement="GBP",
        value_fn=lambda s: s.plan.baseline_cost_gbp if s.plan else None,
    ),
    SparkSensorDescription(
        key="v2l_energy", translation_key="v2l_energy", v2l=True,
        requested_entity_id="sensor.ha_spark_v2l_energy_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: s.v2l.energy_kwh if s.v2l else None,
    ),
    SparkSensorDescription(
        key="v2l_net_saving", translation_key="v2l_net_saving", v2l=True,
        requested_entity_id="sensor.ha_spark_v2l_net_saving_gbp",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement="GBP",
        value_fn=lambda s: s.v2l.net_saving_gbp if s.v2l else None,
    ),
    SparkSensorDescription(
        key="last_run", translation_key="last_run",
        requested_entity_id="sensor.ha_spark_last_run", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.plan.computed_at if s.plan else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SparkConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Free legacy pushed states before requesting their existing ids."""
    entities = []
    for description in DESCRIPTIONS:
        async_clear_unregistered_state(hass, description.requested_entity_id)
        cls = LastRun if description.key == "last_run" else SparkSensor
        entities.append(cls(entry.runtime_data, description))
    async_add_entities(entities)


class SparkSensor(SparkEntity, SensorEntity):
    """A display value accepted only from a counted heartbeat."""

    entity_description: SparkSensorDescription

    def __init__(self, coordinator: SparkCoordinator, description: SparkSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self.entity_id = description.requested_entity_id

    @property
    def available(self) -> bool:
        summary = self.coordinator.summary
        if not self.coordinator.connected or summary is None:
            return False
        if self.entity_description.v2l and summary.v2l is None:
            return False
        return not (self.entity_description.soc_derived and summary.plan is not None
                    and not summary.plan.soc_trusted)

    @property
    def native_value(self) -> float | datetime | None:
        summary = self.coordinator.summary
        return self.entity_description.value_fn(summary) if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        summary = self.coordinator.summary
        if self.entity_description.key != "v2l_net_saving" or not summary or not summary.v2l:
            return None
        return {"avoided_gbp": summary.v2l.avoided_gbp,
                "refill_cost_gbp": summary.v2l.refill_cost_gbp,
                "peak_power_w": summary.v2l.peak_power_w}


class LastRun(SparkSensor, RestoreSensor):
    """Retain the last computed time through disconnection, null plans and HA restart."""

    _last_run: datetime | None = None

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        return self._last_run

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None:
            if isinstance(data.native_value, datetime):
                self._last_run = data.native_value
        self._remember_plan()

    def _remember_plan(self) -> None:
        summary = self.coordinator.summary
        if self.coordinator.connected and summary is not None and summary.plan is not None:
            self._last_run = summary.plan.computed_at

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember_plan()
        super()._handle_coordinator_update()
