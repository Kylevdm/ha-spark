"""Protection-owned power select, gated before the #580 power adapter."""
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SparkConfigEntry, native_power_seam
from .coordinator import SparkCoordinator
from .native_write_entities import WriteEntity
from .topology import Inverter


async def async_setup_entry(hass: HomeAssistant, entry: SparkConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(NativePower(entry.runtime_data, inv)
                       for inv in entry.runtime_data.native.inverters.values())


class NativePower(WriteEntity, SelectEntity):
    _attr_options = ["on", "off"]
    control = "power"
    frame = "holding_43007"

    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter) -> None:
        super().__init__(coordinator, inverter, "holding_43007_set")

    @property
    def current_option(self) -> str | None:
        words = self.words()
        return {190: "on", 222: "off"}.get(words[0]) if words else None

    async def async_select_option(self, option: str) -> None:
        epoch = self.epoch()
        if option not in self._attr_options:
            self.error("invalid_intent")
        if self.current_option is None:
            self.error("read_only")
        self.result(await native_power_seam.async_set_power_switch(
            self.coordinator, self.inverter.id, option == "on", epoch=epoch))
