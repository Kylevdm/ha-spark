"""Read-only supervised-export diagnostics; never an export request or permission."""

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfPower

from .authority_store import ExportRecord, ExportSupervision
from .coordinator import SparkCoordinator
from .native_entities import NativeEntity
from .native_export import (
    DeviceExportFact,
    bound_refusal,
    latest_record,
    site_export_limit_w,
)
from .topology import Inverter, Topology, effective_limits, validate_topology


class ExportEntity(NativeEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter, key: str) -> None:
        super().__init__(coordinator, inverter, key)

    @property
    def available(self) -> bool:
        return self.coordinator.authority.state is not None

    def _supervision(self) -> ExportSupervision | None:
        state = self.coordinator.authority.state
        if state is None:
            return None
        return next((row for row in reversed(state.export_supervisions)
                     if row.inverter_id == self.inverter.id), None)

    def _record(self) -> ExportRecord | None:
        state = self.coordinator.authority.state
        return latest_record(state.export_records, self.inverter.id) if state else None


class ExportBoundSensor(ExportEntity):
    _attr_translation_key = "native_export_bound"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    @property
    def native_value(self) -> float | None:
        supervision = self._supervision()
        return supervision.rating_w if supervision is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        supervision = self._supervision()
        if supervision is None:
            return None
        topology = validate_topology(self.coordinator.entry.options.get("topology"))
        if not isinstance(topology, Topology):
            return {"validated": False, "reason": "export_bound_unverified"}
        limits = effective_limits(topology)
        bank = next(bank for bank in topology.banks if bank.id == self.inverter.bank_id)
        fact = self.coordinator._export_facts(topology).get(
            self.inverter.id, DeviceExportFact(False))
        state = self.coordinator.authority.state
        total = 0.0
        if state is not None:
            total = sum(row.rating_w for row in state.export_supervisions
                        if row.event == supervision.event)
        reason = bound_refusal(
            supervision, fact, site_limit_w=site_export_limit_w(
                topology, limits, bank.connection_id), connection_sum_w=total)
        return {"validated": reason is None, "reason": reason}


class ExportSupervisionSensor(ExportEntity):
    _attr_translation_key = "native_export_supervision"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        supervision = self._supervision()
        return supervision.ends_at if supervision is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        supervision = self._supervision()
        if supervision is None:
            return None
        return {"supervision_id": str(supervision.id),
                "event_start": supervision.event.start.isoformat(),
                "event_end": supervision.event.end.isoformat()}


class ExportRecordSensor(ExportEntity):
    _attr_translation_key = "native_export_record"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["armed", "active", "preserved", "stop_pending", "cleared", "unknown"]

    @property
    def native_value(self) -> str | None:
        record = self._record()
        return record.state if record is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._record()
        if record is None:
            return None
        return {"window_start": record.window.start.isoformat(),
                "window_end": record.window.end.isoformat(),
                "cleanup_verified": record.cleanup_verified,
                "stop_reason": record.stop_reason}


def native_export_sensors(coordinator: SparkCoordinator) -> list[SensorEntity]:
    return [
        sensor(coordinator, inverter, key)
        for inverter in coordinator.native.inverters.values()
        for sensor, key in ((ExportBoundSensor, "native_export_bound"),
                            (ExportSupervisionSensor, "native_export_supervision"),
                            (ExportRecordSensor, "native_export_record"))
    ]
