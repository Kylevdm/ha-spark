"""Device-driver core: capabilities, control authority, and the actuation gate."""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid an import cycle: models never imports config/devices at runtime
    from ha_spark.energy.models import ChargeIntent


class Capability(StrEnum):
    CHARGE_WINDOW = "charge_window"   # write window + target SOC (floor)
    CHARGE_RATE = "charge_rate"       # settable live charge power (W) — rate tier
    STOP_DISCHARGE = "stop_discharge" # hold/stop-discharge during a dispatch


class ControlAuthority(StrEnum):
    OBSERVE = "observe"     # never write; read & plan around the device
    HA_SPARK = "ha_spark"   # ha-spark may write, still PROACTIVE_MODE-gated
    SUPPLIER = "supplier"   # reserved; behaves like OBSERVE this phase


def effective_mode(control: ControlAuthority, proactive_mode: str) -> str:
    """Collapse (authority, proactive_mode) -> off|simulate|on|observe.

    The CLAUDE.md actuation invariant in one place: a real write ("on") requires
    control == ha_spark AND proactive_mode == on. Any other authority returns
    "observe" (compute/log only, never actuate), regardless of proactive_mode.
    "observe" is kept distinct from the user's "off" so logs show *why* a write
    was suppressed.
    """
    if control != ControlAuthority.HA_SPARK:
        return "observe"
    return proactive_mode


@runtime_checkable
class Device(Protocol):
    """Realizes a ChargeIntent via a specific inverter; returns action lines."""

    capabilities: frozenset[Capability]

    async def apply(self, intent: ChargeIntent) -> list[str]: ...
    async def set_charge_rate(self, watts: float) -> str: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...
