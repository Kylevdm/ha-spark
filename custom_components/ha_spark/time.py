"""Slot 1 time config entities commit a fresh whole eight-register block."""
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SparkConfigEntry
from .coordinator import SparkCoordinator
from .native_write_entities import WriteEntity
from .topology import Inverter

_KEYS = {0: "charge_start", 2: "charge_end", 4: "discharge_start", 6: "discharge_end"}


async def async_setup_entry(hass: HomeAssistant, entry: SparkConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(NativeTime(entry.runtime_data, inv, offset)
                       for inv in entry.runtime_data.native.inverters.values()
                       for offset in _KEYS)


class NativeTime(WriteEntity, TimeEntity):
    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter, offset: int) -> None:
        self.offset = offset
        self.control = "charge" if offset < 4 else "discharge"
        super().__init__(coordinator, inverter, f"holding_43143_{_KEYS[offset]}")

    @property
    def native_value(self) -> time | None:
        words = self.words()
        index = self.offset + 2
        if words is None or len(words) < index + 2:
            return None
        hour, minute = words[index:index + 2]
        return time(hour, minute) if hour < 24 and minute < 60 else None

    async def async_set_value(self, value: time) -> None:
        epoch = self.epoch()
        if value.second or value.microsecond or value.tzinfo is not None:
            self.error("invalid_intent")
        result = await self.coordinator.writes.async_entity(
            self.inverter.id, self.control, [value.hour, value.minute], epoch=epoch,
            offset=self.offset)
        self.result(result)
