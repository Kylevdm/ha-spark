"""Native DC current config entities; charge requests enter the shared clamp."""
from math import isfinite

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SparkConfigEntry
from .coordinator import SparkCoordinator
from .native_write_entities import WriteEntity
from .topology import Inverter, Topology, effective_limits, validate_topology


async def async_setup_entry(hass: HomeAssistant, entry: SparkConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(NativeCurrent(entry.runtime_data, inv, discharge)
                       for inv in entry.runtime_data.native.inverters.values()
                       for discharge in (False, True))


class NativeCurrent(WriteEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter,
                 discharge: bool) -> None:
        self.discharge = discharge
        self.control = "discharge" if discharge else "charge"
        self.address = 43142 if discharge else 43141
        super().__init__(coordinator, inverter, f"holding_{self.address}_set")

    @property
    def native_max_value(self) -> float:
        topology = validate_topology(self.coordinator.entry.options.get("topology"))
        if not isinstance(topology, Topology):
            return 0
        limit = next((i for i in effective_limits(topology).inverters
                      if i.id == self.inverter.id), None)
        return min(62.5, (limit.discharge_a if self.discharge else limit.charge_a)) if limit else 0

    @property
    def native_value(self) -> float | None:
        words = self.words()
        index = int(self.discharge)
        return words[index] / 10 if words and len(words) > index else None

    async def async_set_native_value(self, value: float) -> None:
        epoch = self.epoch()
        if not isfinite(value) or value < 0 or value > self.native_max_value:
            self.error("limits")
        result = await self.coordinator.writes.async_entity(
            self.inverter.id, self.control, value, epoch=epoch)
        self.result(result)
