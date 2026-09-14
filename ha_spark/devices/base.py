"""Device-driver core: capabilities, control authority, and the actuation gate."""
from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid an import cycle: models never imports config/devices at runtime
    from ha_spark.energy.models import ChargeIntent


def fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


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
    """Realizes a ChargeIntent via a specific inverter; returns action lines.

    ``apply`` is the plan seam: driven by a plan diff, on the half-hourly replan
    cadence. ``reconcile_holds`` is the *clock* seam (#143): a cheap read-first
    pass every caller makes before it applies, and the daemon repeats every
    minute, because whether a dispatch hold is active right now is a function of
    the clock and cannot wait for a plan field to change. ``write_safe_state``
    is written once when ha-spark stops steering (#143 §5): past every known
    hold end with no trusted picture, or on a clean shutdown. Inverters with no
    hold surface answer both with an empty list.
    """

    capabilities: frozenset[Capability]

    async def apply(self, intent: ChargeIntent) -> list[str]: ...
    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]: ...
    async def write_safe_state(self) -> list[str]: ...
    async def set_charge_rate(self, watts: float) -> str: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...
