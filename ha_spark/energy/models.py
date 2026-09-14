"""Dataclasses for the energy charge planner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from ha_spark.energy.soc_integrity import SocMeasurement

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
class PricePoint:
    """One half-hourly import price read from a dynamic-tariff price sensor."""

    start: datetime
    end: datetime
    price: float  # GBP/kWh, inc. VAT


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
class FlexibilityEvent:
    """A validated aggregator event used to price a planning window."""

    start: datetime
    end: datetime
    direction: str
    updated_at: datetime
    rate_gbp_kwh: float

    @property
    def identity(self) -> tuple[str, datetime, datetime]:
        """Stable event identity from the Axle household snapshot contract.

        Axle's Home Assistant endpoint has no event id.  ``updated_at`` proves
        a snapshot is fresh but changes without making the event a different
        obligation, so identity is exactly direction and window.
        """
        return (self.direction, self.start, self.end)


@dataclass(frozen=True)
class ExportSkip:
    """One paid-event slot deliberately not scheduled, with an auditable reason."""

    start: datetime
    reason: str


@dataclass(frozen=True)
class ExportIntent:
    """Inverter-agnostic export request for selected paid-event slots.

    ``planned_export_kw`` is the lowest calculated export ceiling across the
    selected slots, so a single-window inverter adapter has a conservative
    power figure to report.  ``slot_export_kw`` preserves each slot's actual
    ceiling for audits; the Solis prototype deliberately commands its fixed
    verified current rather than trying to modulate to those figures.
    """

    event_identity: tuple[str, datetime, datetime]
    window_start: datetime
    window_end: datetime
    planned_export_kw: float
    dno_export_limit_kw: float
    selected_slots: tuple[datetime, ...]
    slot_export_kw: tuple[float, ...]


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
    # Conservative DC battery output ceiling used only for export planning.
    # Solis actuation remains fixed at its separately verified 62.5 A command.
    battery_discharge_ceiling_kw: float = 3.2
    dno_export_limit_kw: float = 7.36
    supply_max_current_a: float = 75.0
    supply_voltage_v: float = 240.0
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

    # The checked SoC observation these inputs were built from. Planning reads
    # its value through `soc_now`, never by re-reading the sensor, so one read
    # can never certify a different read.
    soc: SocMeasurement
    solar_tomorrow_kwh: float
    predicted_home_load_kwh: float
    # Forecast battery drain between plan time and the charge-window start
    # (the horizon starts at the window, so this load is otherwise invisible).
    pre_window_drain_kwh: float = 0.0
    dispatches: tuple[DispatchSlot, ...] = ()
    # False when the dispatch source could not be read (the Octopus call raised,
    # or the configured entity was missing/unavailable/unknown), so an empty
    # ``dispatches`` stops meaning both "no dispatch" and "unreadable" (#143 §3).
    dispatches_trusted: bool = True
    flexibility_event: FlexibilityEvent | None = None
    ev_charging: bool = False
    ha_template_needed: float | None = None
    # v2 per-slot horizon (48 half-hour slots starting at the charge-window start
    # tonight). When load_slots is None the planner uses the v1 daily balance.
    load_slots: tuple[float, ...] | None = None
    solar_slots: tuple[float, ...] | None = None
    horizon_start: datetime | None = None
    # Live per-slot import prices from a `dynamic` tariff price sensor, sorted
    # by start; empty when the dynamic provider isn't in use or the read failed.
    dynamic_prices: tuple[PricePoint, ...] = ()

    @property
    def soc_now(self) -> float:
        """The checked SoC percentage; 0 when the measurement failed."""
        return self.soc.soc_now


@dataclass(frozen=True)
class ChargeIntent:
    """Inverter-agnostic charge command: reach ``target_soc_pct`` by ``window_end``.

    ``soc`` is the checked measurement the plan was sized from, carried so a
    rate-based adapter (Solis) can re-derive the kWh to add without re-reading
    the sensor, and so every charger sees the same integrity verdict and its
    evidence. ``holds`` are daytime dispatch windows during which the battery
    must stop discharging (hold for cheap grid). ``hold_trusted`` is the
    dispatch read's verdict on them, as ``soc`` carries the SoC read's: when
    False the holds are degraded, never evidence that no hold is active — the
    reconcile uses the last trusted set instead and no new export window is
    programmed (#143 §3).
    """

    target_soc_pct: float
    soc: SocMeasurement  # failed -> chargers must refuse real writes
    window_start: time
    window_end: time
    holds: tuple[tuple[datetime, datetime], ...] = ()
    export: ExportIntent | None = None
    hold_trusted: bool = True

    @property
    def soc_now(self) -> float:
        """The checked SoC percentage; 0 when the measurement failed."""
        return self.soc.soc_now

    def hold_active(self, now: datetime) -> bool:
        """True when ``now`` falls inside a hold window (start inclusive, end exclusive).

        The inverter-agnostic expression of "the battery must not discharge right
        now". ``now`` must be timezone-aware — the household clock the caller
        already resolved.
        """
        return any(_align(start, now) <= now < _align(end, now) for start, end in self.holds)

    def hold_overlaps(self, start: datetime, end: datetime) -> bool:
        """True when any hold intersects ``[start, end)``.

        Distinct from :meth:`hold_active`: a decision about a *future* window
        (does a dispatch cut into tonight's export event?) must compare intervals,
        not sample the clock, or it both refuses windows a passing hold cannot
        reach and admits windows a later hold will interrupt.
        """
        return any(
            _align(hold_start, start) < end and start < _align(hold_end, start)
            for hold_start, hold_end in self.holds
        )


def _align(moment: datetime, reference: datetime) -> datetime:
    """Read a naive hold bound on ``reference``'s clock.

    Hold bounds come from a dispatch attribute HA renders itself, so they may
    arrive without an offset; a naive bound is read on the caller's resolved
    household clock rather than discarded, so an unannotated dispatch never
    leaves a hold silently inactive. A naive ``reference`` is a caller bug —
    guessing a zone for it is how a BST hold reads as inactive for its whole
    duration — so it is left to raise on comparison rather than coerced.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=reference.tzinfo)


@dataclass(frozen=True)
class Reservation:
    """Battery energy reserved for a named obligation in the slot horizon."""

    name: str
    target_slot: int
    energy_kwh: float
    obligation_kind: str
    reason: str
    target_time: datetime | None = None

    @property
    def obligation(self) -> str:
        """Compatibility spelling for callers describing the obligation."""
        return self.obligation_kind

    @property
    def target(self) -> datetime | int:
        """The concrete target time when available, otherwise the slot index."""
        return self.target_time if self.target_time is not None else self.target_slot


@dataclass(frozen=True)
class ChargePlan:
    """The computed plan: the numbers, plus the ChargeIntent a Charger realizes."""

    soc: SocMeasurement  # failed -> block real writes
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
    model: str = "daily"  # "slots" (per-slot horizon) | "daily" (v1 balance)
    expensive_load_kwh: float | None = None  # net load in peak-rate slots (slot model)
    # Per-slot import price (£/kWh) the planner costed against (slot model only).
    slot_prices: tuple[float, ...] | None = None
    baseline_cost: float | None = None  # projected GBP without battery
    planned_cost: float | None = None  # projected GBP with this plan
    charge_efficiency: float = 1.0  # round-trip efficiency used for sizing
    export_revenue: float | None = None  # projected GBP feed-in (None when disabled)
    strategy: str = "deficit"  # sizing strategy used ("deficit" | "fill")
    pre_window_drain_kwh: float = 0.0  # forecast drain before the window opens
    # EV energy Octopus plans to deliver across the dispatches (None when there
    # are no dispatches) — reported, not planned: Octopus controls the EV.
    dispatch_ev_kwh: float | None = None
    # Named battery obligations computed from the slot horizon. The daily model
    # intentionally leaves this empty to preserve its legacy contract.
    reservations: tuple[Reservation, ...] = ()
    # Skips are part of the plan rather than silently becoming lower-current
    # delivery: every accepted export slot is always a complete half-hour.
    export_skips: tuple[ExportSkip, ...] = ()

    @property
    def soc_now(self) -> float:
        """The checked SoC percentage; 0 when the measurement failed."""
        return self.soc.soc_now
