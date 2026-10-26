"""Dataclasses for the energy charge planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from ha_spark.energy.evidence_freshness import EvidenceFreshness
from ha_spark.energy.soc_integrity import SocMeasurement

if TYPE_CHECKING:
    from ha_spark.energy.horizon import HorizonGeometry
    from ha_spark.energy.plan_view import PlanView

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
class ForecastHistoryRecord(ForecastRecord):
    """One immutable forecast revision and its paired median baseline."""

    horizon_start: datetime | None = None
    horizon_end: datetime | None = None
    drove_plan: bool = False
    median_total_kwh: float | None = None
    median_slots: tuple[float, ...] | None = None

    @property
    def issued_at(self) -> datetime:
        """Issue time, retaining ``made_at`` for existing consumers."""
        return self.made_at


@dataclass(frozen=True)
class SlotProfile:
    """Median home load per local half-hour slot, split weekday/weekend."""

    weekday: tuple[float, ...]  # 48 values, kWh per half-hour slot
    weekend: tuple[float, ...]
    days_used: int


@dataclass(frozen=True)
class LoadForecast:
    """The served day's predicted home load; ``slots`` is None on fallback paths."""

    total_kwh: float
    slots: tuple[float, ...] | None  # 48 local half-hour slot kWh (slot-of-day order)
    source: str
    # The local date the forecast serves (#397): the day the running-or-next
    # charge window ends. None on forecasts built without one (hand-built
    # tests), which read as the pre-#394 "tomorrow".
    target_date: date | None = None
    # P90 day total from the quantile ML model (None elsewhere); feeds the
    # dynamic buffer when buffer_mode is "quantile".
    p90_total_kwh: float | None = None
    median_total_kwh: float | None = None
    median_slots: tuple[float, ...] | None = None


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
    dno_export_limit_kw: float = 3.68
    supply_max_current_a: float = 75.0
    supply_voltage_v: float = 240.0
    rate_offpeak: float = 0.069  # GBP/kWh inside the window / dispatch slots
    rate_peak: float = 0.30
    rate_export: float = 0.0  # GBP/kWh feed-in; 0 disables export revenue
    buffer_pct: float = 20.0  # safety margin applied to the forecast deficit
    charge_efficiency: float = 0.90  # round-trip AC->DC->AC; buy required/efficiency
    strategy: str = "deficit"  # "deficit" (buy the shortfall) | "fill" (to target cap)

    v2l_budget_kwh: float = 0.0
    v2l_round_trip_efficiency: float = 0.85
    v2l_offpeak_rate_gbp: float = 0.07
    v2l_max_rate_kw: float = 3.0
    v2l_preferred_rate_kw: float = 1.5

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
    solar_tomorrow_kwh: float  # served-day total; legacy constructor keyword
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
    # False when the Axle event source could not be read. An empty event then
    # means "unreadable", not "cancelled", so a verified resident export must
    # not be cleared (#148).
    flexibility_event_trusted: bool = True
    car_plugged_in: bool = False
    ev_charging: bool = False
    # Current evidence for the inverter hold: ``None`` means no configured or
    # readable EV status source. The pure planner does not forecast this hold.
    ev_hold_charging: bool | None = None
    ha_template_needed: float | None = None
    # v2 per-slot horizon: one value per ``geometry.slot_starts`` slot, from the
    # current half-hour on live inputs (legacy inputs without geometry: 48 slots
    # from the charge-window start). When load_slots is None the planner uses
    # the v1 daily balance.
    load_slots: tuple[float, ...] | None = None
    solar_slots: tuple[float, ...] | None = None
    horizon_start: datetime | None = None
    # When these inputs were read, on the household clock. Places elapsed
    # time within slot 0 and formats plan times locally.
    now: datetime | None = None
    # Live per-slot import prices from a `dynamic` tariff price sensor, sorted
    # by start; empty when the dynamic provider isn't in use or the read failed.
    dynamic_prices: tuple[PricePoint, ...] = ()
    # Original dispatches before evidence rating. The planner prices and sizes
    # only `dispatches`, but carries these for local per-minute hold re-rating.
    unrated_dispatches: tuple[DispatchSlot, ...] = ()
    # Report-time freshness per source (#419). The planner copies only the Kraken
    # read time onto the intent; the hold gates read freshness at their own seams.
    evidence_freshness: dict[str, EvidenceFreshness] = field(default_factory=dict)
    battery_direction: str = "unknown"
    # Horizon geometry (#395): where the plan's slots fall on the clock, from
    # the current half-hour. ``None`` on inputs built without one (hand-built
    # tests, the backtest, the v1 daily path); read it through
    # :meth:`geometry_for`, which derives the legacy layout from the config.
    geometry: HorizonGeometry | None = None

    @property
    def soc_now(self) -> float:
        """The checked SoC percentage; 0 when the measurement failed."""
        return self.soc.soc_now

    @property
    def served_day_solar_kwh(self) -> float:
        """Served-day solar total (stored under the legacy constructor keyword)."""
        return self.solar_tomorrow_kwh

    def geometry_for(self, config: PlannerConfig) -> HorizonGeometry | None:
        """The horizon geometry, or the legacy one derived from ``config``.

        ``None`` on the v1 daily path (no ``horizon_start``, no geometry). The
        legacy derivation reproduces today's layout exactly — as many 30-minute
        slots as ``load_slots`` (48 without them) from ``horizon_start``, the
        window covering the first ``int(window_hours * 2)`` slots — so inputs
        built without a geometry keep today's indexing and numbers.
        """
        from ha_spark.energy.horizon import legacy_geometry

        if self.geometry is not None:
            return self.geometry
        if self.horizon_start is None:
            return None
        n_slots = len(self.load_slots) if self.load_slots is not None else SLOTS_PER_DAY
        return legacy_geometry(self.horizon_start, config, n_slots)


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
    # False means an empty export is an unreadable Axle event, not a
    # cancellation (#148).
    export_trusted: bool = True
    # A current, per-minute EV hold is separate from dispatch windows because
    # it has no scheduled end. Dispatch trust remains independent of EV trust.
    ev_hold_active: bool = False
    # Daytime windows from the plan's original dispatch read, before the
    # evidence ladder filtered them. Reconcile uses these only for local
    # re-rating; `holds` remains the rated set used by this plan.
    unrated_holds: tuple[tuple[datetime, datetime], ...] = ()
    # Sizing projection only; never replaces the checked SoC measurement.
    expected_window_start_soc: float | None = None
    # When the plan's Kraken dispatch read succeeded (octopus_intelligent only).
    # The per-minute reconcile re-checks its age before trusting `unrated_holds`;
    # None is no evidence, never "fresh".
    octopus_dispatch_reported_at: datetime | None = None

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
        return self.ev_hold_active or any(
            _align(start, now) <= now < _align(end, now) for start, end in self.holds
        )

    def hold_overlaps(self, start: datetime, end: datetime) -> bool:
        """True when any hold intersects ``[start, end)``.

        Distinct from :meth:`hold_active`: a decision about a *future* window
        (does a dispatch cut into tonight's export event?) must compare intervals,
        not sample the clock, or it both refuses windows a passing hold cannot
        reach and admits windows a later hold will interrupt.
        """
        return self.ev_hold_active or any(
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
class V2LRefill:
    """Energy stored in the house battery during a half-hour slot.

    kWh and rate are delivered battery energy; a partial slot keeps the
    preferred rate and runs for kwh / rate_kw hours.
    """

    slot: int
    kwh: float
    rate_kw: float
    reason: str


@dataclass(frozen=True)
class ChargePlan:
    """The computed plan: the numbers, plus the ChargeIntent a Charger realizes."""

    soc: SocMeasurement  # failed -> block real writes
    capacity_kwh: float
    solar_kwh: float  # served-day total; legacy constructor keyword
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
    # The reservations tonight's charge funds: those targeted after the window
    # start (a pre-window one is carried from now, never charged).
    charge_funded_reservations: tuple[Reservation, ...] = ()
    v2l_refills: tuple[V2LRefill, ...] = ()
    v2l_wanted_but_unavailable: bool = False
    # Skips are part of the plan rather than silently becoming lower-current
    # delivery: every accepted export slot is always a complete half-hour.
    export_skips: tuple[ExportSkip, ...] = ()
    # Says so when the post-event reserve funds paid export (#207).
    export_funding: str | None = None
    # Event-start SoC that would fund a skipped-for-funding export suffix.
    export_soc_needed_pct: float | None = None
    export_soc_needed_at: datetime | None = None
    # The charge window's current ceiling, not the target cap, limited the
    # battery energy available to fund the event (#208).
    overnight_charge_capped: bool = False
    # Expected SoC at the start of each slot from the charge-window end, if the
    # plan is followed (slot model only). Lets a later reading show the plan
    # was wrong (#208).
    soc_path: tuple[tuple[datetime, float], ...] = ()
    view: PlanView | None = None

    @property
    def served_day_solar_kwh(self) -> float:
        """Served-day solar total before the configured haircut."""
        return self.solar_kwh

    @property
    def soc_now(self) -> float:
        """The checked SoC percentage; 0 when the measurement failed."""
        return self.soc.soc_now
