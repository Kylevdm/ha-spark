"""Read-only plan sensors and independent cached forecasts on the Solcast device."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SparkConfigEntry
from .const import DOMAIN
from .coordinator import SparkCoordinator
from .entity import SparkEntity, async_clear_unregistered_state
from .solcast_coordinator import SolcastCoordinator
from .wire import Summary


@dataclass(frozen=True, kw_only=True)
class SparkSensorDescription(SensorEntityDescription):
    """Fixed identity, availability and value projection for a site sensor."""

    requested_entity_id: str | None = None
    soc_derived: bool = False
    v2l: bool = False
    value_fn: Callable[[Summary], float | datetime | str | None]
    attributes_fn: Callable[[Summary], dict[str, Any] | None] | None = None


DESCRIPTIONS = (
    SparkSensorDescription(
        key="operating_mode", translation_key="operating_mode",
        requested_entity_id="sensor.ha_spark_operating_mode", device_class=SensorDeviceClass.ENUM,
        options=["observe", "protect", "optimise", "authority_unknown"],
        value_fn=lambda s: s.authority.mode,
        attributes_fn=lambda s: {
            "transition_target": s.authority.transition_target,
            "last_changed_at": (s.authority.last_changed_at.isoformat()
                                if s.authority.last_changed_at else None),
            "save_failed": s.authority.save_failed,
        },
    ),
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
        attributes_fn=lambda s: {
            "avoided_gbp": s.v2l.avoided_gbp, "refill_cost_gbp": s.v2l.refill_cost_gbp,
            "peak_power_w": s.v2l.peak_power_w,
        } if s.v2l else None,
    ),
    SparkSensorDescription(
        key="plan_status", translation_key="plan_status",
        requested_entity_id="sensor.ha_spark_plan_status", device_class=SensorDeviceClass.ENUM,
        options=["hold", "export", "charge", "idle"],
        value_fn=lambda s: s.plan.status.action if s.plan else None,
        attributes_fn=lambda s: {
            "reason": s.plan.status.reason, "model": s.plan.status.model,
            "strategy": s.plan.status.strategy, "soc_status": s.plan.status.soc_status,
            "soc_reason": s.plan.status.soc_reason,
            **({"export_soc_needed_pct": s.plan.status.export_soc_needed_pct,
                "export_soc_needed_at": s.plan.status.export_soc_needed_at.isoformat()}
               if s.plan.status.export_soc_needed_at is not None else {}),
        } if s.plan else None,
    ),
    SparkSensorDescription(
        key="next_change", translation_key="next_change", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.plan.next_change.at if s.plan and s.plan.next_change else None,
        attributes_fn=lambda s: {
            "action": s.plan.next_change.action, "reason": s.plan.next_change.reason,
        } if s.plan and s.plan.next_change else None,
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
        if description.requested_entity_id is not None:
            async_clear_unregistered_state(hass, description.requested_entity_id)
        cls = LastRun if description.key == "last_run" else SparkSensor
        entities.append(cls(entry.runtime_data, description))
    async_add_entities(entities)
    if entry.runtime_data.solcast is not None:
        async_add_entities(SolcastSensor(entry.runtime_data.solcast, description)
                           for description in SOLCAST_DESCRIPTIONS)


class SparkSensor(SparkEntity, SensorEntity):
    """A display value accepted only from a counted heartbeat."""

    entity_description: SparkSensorDescription

    def __init__(self, coordinator: SparkCoordinator, description: SparkSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.requested_entity_id is not None:
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
    def native_value(self) -> float | datetime | str | None:
        summary = self.coordinator.summary
        return self.entity_description.value_fn(summary) if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        summary = self.coordinator.summary
        attributes_fn = self.entity_description.attributes_fn
        return attributes_fn(summary) if attributes_fn and summary else None


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


SOLCAST_DESCRIPTIONS = (
    SensorEntityDescription(
        key="forecast_today", translation_key="forecast_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="forecast_tomorrow", translation_key="forecast_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="last_fetch", translation_key="last_fetch",
        device_class=SensorDeviceClass.TIMESTAMP, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="api_calls_used", translation_key="api_calls_used",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class SolcastSensor(CoordinatorEntity[SolcastCoordinator], SensorEntity):
    """Cached forecasts remain visible independently of add-on connectivity."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SolcastCoordinator, description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        site_id = coordinator.entry.data["site_id"]
        self._attr_unique_id = f"{site_id}_solcast_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, f"{site_id}_solcast")})

    def _totals(self) -> tuple[float, float, float] | None:
        day = dt_util.as_local(dt_util.utcnow()).date()
        if self.entity_description.key == "forecast_tomorrow":
            day += timedelta(days=1)
        return self.coordinator.day_totals(day)

    @property
    def available(self) -> bool:
        if self.entity_description.key == "api_calls_used":
            return self.coordinator.budget.state is not None
        if self.entity_description.key == "last_fetch":
            return self.coordinator.data.fetched_at is not None
        return self._totals() is not None

    @property
    def native_value(self) -> float | datetime | None:
        if self.entity_description.key == "api_calls_used":
            return self.coordinator.budget.calls_used
        if self.entity_description.key == "last_fetch":
            return self.coordinator.data.fetched_at
        totals = self._totals()
        return round(totals[1], 3) if totals else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "api_calls_used":
            return {"limit": self.coordinator.calls_limit}
        if self.entity_description.key == "last_fetch":
            return None
        totals = self._totals()
        return ({"estimate10": round(totals[0], 3), "estimate90": round(totals[2], 3)}
                if totals else None)
