"""Device-driver core: capabilities and the device protocol."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ha_spark.devices.outcomes import ActionLine

if TYPE_CHECKING:  # avoid an import cycle: models never imports config/devices at runtime
    from ha_spark.energy.models import ChargeIntent


def fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


class Capability(StrEnum):
    CHARGE_WINDOW = "charge_window"   # write window + target SOC (floor)
    CHARGE_RATE = "charge_rate"       # settable live charge power (W) — rate tier
    STOP_DISCHARGE = "stop_discharge" # hold/stop-discharge during a dispatch


@dataclass(frozen=True)
class HoldContract:
    """Model-qualified hold semantics; evidence policy is implemented by later tickets."""

    hold: str
    release: str
    read_back: str
    evidence_freshness: str


@dataclass(frozen=True)
class HoldValidation:
    identity: dict[str, int | None]
    identity_source: str
    contract: HoldContract | None
    reason: str

    @property
    def validated(self) -> bool:
        return self.contract is not None


@runtime_checkable
class Device(Protocol):
    """Realizes a ChargeIntent; returns string-compatible, typed action verdicts.

    Human prefixes are presentation only. Callers use each line's outcome for
    verification, never infer hardware success from its text.

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

    async def hold_validation(self) -> HoldValidation: ...
    async def resident_diagnostic(self) -> dict[str, object]: ...
    async def apply(self, intent: ChargeIntent) -> list[ActionLine]: ...
    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[ActionLine]: ...
    async def write_safe_state(self) -> list[ActionLine]: ...
    async def set_charge_rate(self, watts: float) -> ActionLine: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...
