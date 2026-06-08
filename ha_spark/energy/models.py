"""Dataclasses for the energy charge planner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class DispatchSlot:
    """A planned Octopus dispatch (cheap import) window."""

    start: datetime
    end: datetime
    charge_in_kwh: float = 0.0
    source: str = ""

    @property
    def hours(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 3600.0)


@dataclass(frozen=True)
class PlannerConfig:
    """Fixed model coefficients (from Settings)."""

    capacity_kwh: float
    voltage_v: float
    min_soc: float
    target_cap: float
    max_current_a: float
    solar_haircut_k: float
    window_start: time
    window_end: time

    @property
    def window_hours(self) -> float:
        """Length of the (possibly midnight-wrapping) charge window, in hours."""
        s = self.window_start.hour + self.window_start.minute / 60
        e = self.window_end.hour + self.window_end.minute / 60
        return (e - s) % 24 or 24.0


@dataclass(frozen=True)
class PlannerInputs:
    """Live inputs gathered from HA."""

    soc_now: float
    solar_tomorrow_kwh: float
    predicted_home_load_kwh: float
    dispatches: tuple[DispatchSlot, ...] = ()
    ev_charging: bool = False
    ha_template_needed: float | None = None


@dataclass(frozen=True)
class ChargeAction:
    """One control the planner would apply (kind interpreted by a Charger)."""

    kind: str  # "set_charge_current" | "stop_discharge"
    description: str
    current_a: float | None = None
    slot_start: datetime | None = None
    slot_end: datetime | None = None


@dataclass(frozen=True)
class ChargePlan:
    """The computed plan: the numbers, plus the actions a Charger would take."""

    soc_now: float
    capacity_kwh: float
    solar_kwh: float
    effective_solar_kwh: float
    load_kwh: float
    cheap_covered_kwh: float
    usable_now_kwh: float
    required_kwh: float
    target_soc: float
    overnight_current_a: float
    window_hours: float
    ev_charging: bool
    ha_template_needed: float | None
    actions: tuple[ChargeAction, ...]
