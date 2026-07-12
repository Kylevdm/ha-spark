"""Device-driver core (Phase 7)."""
from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ha_spark.devices.base import Capability, ControlAuthority, Device, effective_mode

# Import driver modules so their @register side effects run.
from ha_spark.devices.inverters import alphaess as _alphaess  # noqa: F401
from ha_spark.devices.inverters import solis as _solis  # noqa: F401
from ha_spark.devices.registry import lookup

if TYPE_CHECKING:
    from ha_spark.config import DeviceConfig, Settings
    from ha_spark.ha.rest import HomeAssistantRest

__all__ = [
    "Capability", "ControlAuthority", "Device", "effective_mode",
    "get_device", "inverter_device",
]


def get_device(config: DeviceConfig, settings: Settings, rest: HomeAssistantRest) -> Device:
    return cast(Device, lookup(config.driver)(config, settings, rest))


def inverter_device(settings: Settings, rest: HomeAssistantRest) -> Device:
    config = next(d for d in settings.devices if d.type == "inverter")
    return get_device(config, settings, rest)
