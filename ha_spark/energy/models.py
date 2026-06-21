"""Dataclasses for the energy charge planner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

# Half-hour slots in a (non-DST-transition) day; the planner horizon is always 48.
SLOTS_PER_DAY = 48


def window_hours(start: time, end: time) -> float:
    """Length of the (possibly midnight-wrapping) charge window, in hours."""
    s = start.hour + start.minute / 60
    e = end.hour + end.minute / 60
    return (e - s) % 24 or 24.0


@dataclass(frozen=True)
class ConsumptionInterval:
    """One half-hourly meter reading (timestamps tz-aware UTC)."""

    start: datetime
    end: datetime
    kwh: float


@dataclass(frozen=True)
class ForecastRecord:
    """One recorded load forecast, for later joining against actuals (ledger)."""

    made_at: datetime
    target_date: date
    model: str  # short tag: "slots" | "median" | "baseline" | (future ML models)
    total_kwh: float
    slots: tuple[float, ...] | None
    source: str


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
    # P90 day total from the quantile ML model (None elsewhere); feeds the
    # dynamic buffer when buffer_mode is "quantile".
    p90_total_kwh: float | None = None


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
    rate_export: float = 0.0  # GBP/kWh feed-in; 0 disables export revenue
    buffer_pct: float = 20.0  # safety margin applied to the forecast deficit
    charge_efficiency: float = 0.90  # round-trip AC->DC->AC; buy required/efficiency
    strategy: str = "deficit"  # "deficit" (buy the shortfall) | "fill" (to target cap)

    @property
    def window_hours(self) -> float:
        """Length of the (possibly midnight-wrapping) charge window, in hours."""
        return window_hours(self.window_start, self.window_end)


@dataclass(frozen=True)
class PlannerInputs:
    """Live inputs gathered from HA."""

    soc_now: float
    solar_tomorrow_kwh: float
    predicted_home_load_kwh: float
    # False when the SoC sensor was unreadable (soc_now then defaults to 0);
    # chargers must refuse real writes on an invalid SoC.
    soc_valid: bool = True
    # Forecast battery drain between plan time and the charge-window start
    # (the horizon starts at the window, so this load is otherwise invisible).
    pre_window_drain_kwh: float = 0.0
    dispatches: tuple[DispatchSlot, ...] = ()
    ev_charging: bool = False
    ha_template_needed: float | None = None
    # v2 per-slot horizon (48 half-hour slots starting at the charge-window start
    # tonight). When load_slots is None the planner uses the v1 daily balance.
    load_slots: tuple[float, ...] | None = None
    solar_slots: tuple[float, ...] | None = None
    horizon_start: datetime | None = None


@dataclass(frozen=True)
class ChargeIntent:
    """Inverter-agnostic charge command: reach ``target_soc_pct`` by ``window_end``.

    ``soc_now`` is carried so a rate-based adapter (Solis) can re-derive the kWh
    to add without re-reading the sensor. ``holds`` are daytime dispatch windows
    during which the battery must stop discharging (hold for cheap grid).
    """

    target_soc_pct: float
    soc_now: float
    window_start: time
    window_end: time
    holds: tuple[tuple[datetime, datetime], ...] = ()


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
    window_hours: float
    ev_charging: bool
    ha_template_needed: float | None
    charge_intent: ChargeIntent  # control contract: the sole charge-control surface
    soc_valid: bool = True  # False -> SoC sensor unreadable; block real writes
    model: str = "daily"  # "slots" (per-slot horizon) | "daily" (v1 balance)
    expensive_load_kwh: float | None = None  # net load in peak-rate slots (slot model)
    baseline_cost: float | None = None  # projected GBP without battery
    planned_cost: float | None = None  # projected GBP with this plan
    charge_efficiency: float = 1.0  # round-trip efficiency used for sizing
    export_revenue: float | None = None  # projected GBP feed-in (None when disabled)
    strategy: str = "deficit"  # sizing strategy used ("deficit" | "fill")
    pre_window_drain_kwh: float = 0.0  # forecast drain before the window opens
    # EV energy Octopus plans to deliver across the dispatches (None when there
    # are no dispatches) — reported, not planned: Octopus controls the EV.
    dispatch_ev_kwh: float | None = None
