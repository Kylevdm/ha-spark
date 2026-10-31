"""Read-only inverter registers and single-native-inverter bank SoC entities.

No readings are restored across reloads, and no HA state is an input. Frame
failure affects only entities derived from that frame; disconnection masks all
readings on that gateway. Unique ids contain opaque gateway keys, never names,
hosts or serials. Identity exposes only the three allow-list words.
"""

from datetime import datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SparkCoordinator
from .solis_frames import POINTS, Point, decode
from .topology import Inverter, Topology, validate_topology


class NativeEntity(CoordinatorEntity[SparkCoordinator]):
    """A native inverter child of the shared gateway, independent of the add-on."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter, key: str) -> None:
        super().__init__(coordinator)
        self.inverter = inverter
        gateway = coordinator.native.gateway_id(inverter)
        self._attr_unique_id = f"{gateway}_{inverter.gateway_unit_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{gateway}_{inverter.gateway_unit_id}")},
            name="Solis inverter", manufacturer="Solis",
        )

    @property
    def available(self) -> bool:
        native = self.coordinator.native
        return not native.closed and native.connected(self.inverter)

    @property
    def device_info(self) -> DeviceInfo:
        assert self._attr_device_info is not None
        info = cast(DeviceInfo, self._attr_device_info.copy())
        parent = dr.async_get(self.coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, self.coordinator.native.gateway_id(self.inverter)),
            self.coordinator.entry.entry_id,
        )
        if parent:
            info["via_device_id"] = parent.id
        probe = self.coordinator.native.probes.get(self.inverter.id)
        if probe and len(probe.identity) == 3:
            info["model"] = f"{probe.identity[0]:04X}/{probe.identity[1]:04X}"
        return info


class RegisterEntity(NativeEntity):
    """Availability and decoding are local to one successful frame."""

    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter, point: Point) -> None:
        super().__init__(coordinator, inverter, point.key)
        self.point = point
        if point.name.startswith("Register "):
            self._attr_translation_key = "native_register"
            self._attr_translation_placeholders = {"address": str(point.address)}
        self._attr_entity_registry_enabled_default = point.primary
        if not point.primary or point.kind == "clock":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def value(self) -> float | str | bool | datetime | None:
        reading = self.coordinator.native.reading(self.inverter, self.point.frame.key)
        if reading is None or reading.registers is None:
            return None
        return decode(self.point, reading.registers,
                      self.coordinator.control.household_zone(), frame=True)

    @property
    def available(self) -> bool:
        return super().available and self.value() is not None


class NativeSensor(RegisterEntity, SensorEntity):
    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter, point: Point) -> None:
        super().__init__(coordinator, inverter, point)
        self._attr_native_unit_of_measurement = point.unit
        self._attr_device_class = (
            SensorDeviceClass(point.device_class) if point.device_class else None)

        self._attr_state_class = SensorStateClass(point.state_class) if point.state_class else None
        if point.kind == "direction":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["charge", "discharge"]

    @property
    def native_value(self) -> float | str | datetime | None:
        value = self.value()
        return None if isinstance(value, bool) else value


class NativeBit(RegisterEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        value = self.value()
        return value if isinstance(value, bool) else None


class NativeIdentity(NativeEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        probe = self.coordinator.native.probes.get(self.inverter.id)
        if probe and len(probe.identity) == 3:
            return "/".join(f"{word:04X}" for word in probe.identity)
        return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class BankSoC(CoordinatorEntity[SparkCoordinator], SensorEntity):
    """No averaging: ambiguous or absent native source means unavailable."""

    _attr_has_entity_name = True
    _attr_translation_key = "native_bank_soc"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SparkCoordinator, bank_id: str) -> None:
        super().__init__(coordinator)
        self.sources = [i for i in coordinator.native.inverters.values() if i.bank_id == bank_id]
        bank_key = str(uuid5(NAMESPACE_URL, f"{coordinator.entry.entry_id}:bank:{bank_id}"))
        # Include every source gateway, never silently select a shared bank's source.
        # A bank with no native source remains an unavailable site-level entity.
        gateways = sorted({coordinator.native.gateway_id(i) for i in
                           (self.sources or list(coordinator.native.inverters.values()))})
        self._attr_unique_id = f"{'_'.join(gateways)}_{bank_key}_input_33139"
        site = dr.async_get(coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, coordinator.entry.data["site_id"]), coordinator.entry.entry_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, bank_key)}, name="Battery bank",
        )
        if site:
            self._attr_device_info["via_device_id"] = site.id
        self.point = next(p for p in POINTS if p.address == 33139)

    @property
    def native_value(self) -> float | None:
        if len(self.sources) != 1:
            return None
        native = self.coordinator.native
        source = self.sources[0]
        reading = native.reading(source, self.point.frame.key)
        if reading is None or reading.registers is None:
            return None
        value = decode(self.point, reading.registers, None, frame=True)
        return value if isinstance(value, float) and 0 <= value <= 100 else None

    @property
    def available(self) -> bool:
        return self.native_value is not None


def native_sensors(coordinator: SparkCoordinator) -> list[SensorEntity]:
    entities: list[SensorEntity] = []
    for inverter in coordinator.native.inverters.values():
        entities.extend(NativeSensor(coordinator, inverter, p)
                        for p in POINTS if not p.kind.startswith("bit"))
        entities.append(NativeIdentity(coordinator, inverter, "input_33000_identity"))
    topology = validate_topology(coordinator.entry.options.get("topology"))
    if isinstance(topology, Topology) and coordinator.native.inverters:
        entities.extend(BankSoC(coordinator, bank.id) for bank in topology.banks)
    return entities


def native_bits(coordinator: SparkCoordinator) -> list[BinarySensorEntity]:
    return [NativeBit(coordinator, inverter, p)
            for inverter in coordinator.native.inverters.values()
            for p in POINTS if p.kind.startswith("bit")]
