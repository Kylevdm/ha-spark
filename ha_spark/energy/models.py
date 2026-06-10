"""Dataclasses for the energy charge planner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

# Half-hour slots in a (non-DST-transition) day; the planner horizon is always 48.
SLOTS_PER_DAY = 48


@dataclass(frozen=True)
class ConsumptionInterval:
    """One half-hourly meter reading (timestamps tz-aware UTC)."""

    start: datetime
    end: datetime
    kwh: float


@dataclass(frozen=True)
class SlotProfile:
    """Median home load per local half-hour slot, split weekday/weekend."""

    weekday: tuple[float, ...]  # 48 values, kWh per half-hour slot
    weekend: tuple[float, ...]
    days_used: int


@dataclass(frozen=True)
class LoadForecast:
    """Tomorrow's predicted home load; ``slots`` is None on fallback paths."""

    total_kwh: float
    slots: tuple[float, ...] | None  # 48 local half-hour slot kWh (slot-of-day order)
    source: str


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
    rate_offpeak: float = 0.069  # GBP/kWh inside the window / dispatch slots
    rate_peak: float = 0.30
    buffer_pct: float = 20.0  # safety margin applied to the forecast deficit

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
    # v2 per-slot horizon (48 half-hour slots starting at the charge-window start
    # tonight). When load_slots is None the planner uses the v1 daily balance.
    load_slots: tuple[float, ...] | None = None
    solar_slots: tuple[float, ...] | None = None
    horizon_start: datetime | None = None


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
    deficit_kwh: float
    buffer_pct: float
    required_kwh: float
    target_soc: float
    overnight_current_a: float
    window_hours: float
    ev_charging: bool
    ha_template_needed: float | None
    actions: tuple[ChargeAction, ...]
    model: str = "daily"  # "slots" (per-slot horizon) | "daily" (v1 balance)
    expensive_load_kwh: float | None = None  # net load in peak-rate slots (slot model)
    baseline_cost: float | None = None  # projected GBP without battery
    planned_cost: float | None = None  # projected GBP with this plan
