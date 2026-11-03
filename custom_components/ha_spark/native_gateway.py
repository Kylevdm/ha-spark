"""Gateway-only diagnostic entities; never expose the host or identity serial."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SparkCoordinator
from .topology import Inverter


class GatewayEntity(CoordinatorEntity[SparkCoordinator]):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SparkCoordinator, inverter: Inverter, key: str) -> None:
        super().__init__(coordinator)
        self.inverter = inverter
        gateway_id = coordinator.native.gateway_id(inverter)
        self._attr_unique_id = f"{gateway_id}_{key}"
        self._attr_translation_key = key
        site = dr.async_get(coordinator.hass).async_get_or_create(
            config_entry_id=coordinator.entry.entry_id,
            identifiers={(DOMAIN, coordinator.entry.data["site_id"])}, name="ha-spark",
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, gateway_id)}, name="Solis gateway",
            via_device_id=site.id,
        )

    @property
    def available(self) -> bool:
        return True


class GatewayConnected(GatewayEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool:
        return self.coordinator.native.connected(self.inverter)


class GatewayClaim(GatewayEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool:
        native = self.coordinator.native
        # The connection claim must cover every configured unit on this gateway.
        return all(native.claimed(i) for i in native.inverters.values()
                   if native.gateway_id(i) == native.gateway_id(self.inverter))


class GatewayCaching(GatewayEntity, SensorEntity):
    @property
    def native_value(self) -> str:
        native = self.coordinator.native
        probes = [native.probes.get(i.id) for i in native.inverters.values()
                  if native.gateway_id(i) == native.gateway_id(self.inverter)]
        if any(p is not None and p.caching is True for p in probes):
            return "on"
        if all(p is not None and p.caching is False for p in probes):
            return "off"
        return "unknown"


def gateways(coordinator: SparkCoordinator) -> list[Inverter]:
    """One diagnostic device per connection, even for multiple unit ids."""
    return list({coordinator.native.gateway_id(i): i
                 for i in coordinator.native.inverters.values()}.values())
