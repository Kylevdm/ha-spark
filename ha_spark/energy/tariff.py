"""Tariff providers — turn a user's tariff into one normalised schedule.

A :class:`TariffSchedule` is the *sole contract* between tariff logic and the
planner: per-slot import prices and supplier-controlled cheap fractions over the
horizon, plus the daytime controlled windows the battery holds through, plus the
representative rates the daily-balance model and charge-purchase costing use.
When the horizon starts at the current half-hour (#394) the schedule also
carries the *charge cycle* — the running-or-next window start to the next
window start — so the cheap pattern stays anchored to the window for
non-planner consumers (the cost backtest classifies against it).

The ``fixed`` provider reproduces the legacy fixed-window two-rate behaviour
exactly (the golden baseline pins this). ``dynamic`` costs each slot at its
live price from an HA half-hourly price sensor, falling back to ``fixed``
whenever there's no usable live read; its cheap set is chosen over the charge
cycle, so a replan at any hour selects exactly the slots a window-start
replan selected (#399). ``octopus_intelligent`` keeps the fixed
provider's dispatch/window folding exactly (dispatches sourced from the
Octopus API instead of an HA sensor) and overlays live per-slot prices from
the Octopus standard-unit-rates API.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Protocol

from ha_spark.energy.horizon import HorizonGeometry
from ha_spark.energy.models import (
    DispatchSlot,
    FlexibilityEvent,
    PlannerConfig,
    PlannerInputs,
    PricePoint,
)


def _in_overnight_window(t: time, start: time, end: time) -> bool:
    """True if clock time ``t`` falls in the charge window (which may wrap midnight).

    Legacy end-inclusive clock membership; the horizon geometry (#395) uses
    half-open ``[start, end)`` — kept apart for its non-geometry callers.
    """
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def _hours_since(origin: datetime, dt: datetime, *, absolute: bool = False) -> float:
    """Hours from ``origin`` to ``dt``, coercing a naive ``dt`` into origin's tz."""
    if dt.tzinfo is None and origin.tzinfo is not None:
        dt = dt.replace(tzinfo=origin.tzinfo)
    elif dt.tzinfo is not None and origin.tzinfo is None:
        origin = origin.replace(tzinfo=dt.tzinfo)
    if absolute and origin.tzinfo is not None and dt.tzinfo is not None:
        origin, dt = origin.astimezone(UTC), dt.astimezone(UTC)
    return (dt - origin).total_seconds() / 3600.0


def _cheap_fractions(
    n_slots: int,
    window_hours: float,
    horizon_start: datetime | None,
    dispatches: tuple[DispatchSlot, ...],
    inputs: PlannerInputs,
    cfg: PlannerConfig,
) -> list[float]:
    """Per-slot fraction billed off-peak: the fixed window plus dispatch overlap."""
    geometry = inputs.geometry_for(cfg)
    fracs = (
        list(geometry.window_fracs) if geometry is not None
        else [1.0 if i < int(window_hours * 2) else 0.0 for i in range(n_slots)]
    )
    if horizon_start is None:
        return fracs
    absolute = inputs.geometry is not None
    origin = inputs.now if absolute and inputs.now is not None else horizon_start
    for d in dispatches:
        a = _hours_since(origin, d.start, absolute=absolute)
        b = _hours_since(origin, d.end, absolute=absolute)
        for i in range(n_slots):
            slot_start = (
                _hours_since(origin, geometry.slot_starts[i], absolute=absolute)
                if geometry is not None else i * 0.5
            )
            slot_end = slot_start + 0.5
            # Slot 0's fraction is over its part still to come, as its window
            # coverage is.
            length = 0.5 * (geometry.slot0_remaining_frac if geometry and i == 0 else 1.0)
            overlap = max(0.0, min(b, slot_end) - max(a, slot_end - length)) / length
            if overlap > 0:
                fracs[i] = min(1.0, fracs[i] + overlap)
    return fracs


def _geometry_origin(inputs: PlannerInputs, geometry: HorizonGeometry) -> datetime:
    """The clock supplier timestamps are read on: the replan time when the
    inputs carry one, else the horizon start (as :func:`_cheap_fractions`
    reads it for a real geometry)."""
    return inputs.now if inputs.now is not None else geometry.horizon_start


def _cycle_slot_starts(geometry: HorizonGeometry) -> tuple[datetime, ...]:
    """Charge-cycle slot starts: 30 real minutes apart from the running-or-next
    window start to the next window start (the horizon end) — 48 slots on an
    ordinary day, 46/50 across a DST change (decision C, #399)."""
    total = (geometry.horizon_end - geometry.window_start).total_seconds()
    return tuple(
        geometry.window_start + timedelta(seconds=1800 * j)
        for j in range(math.ceil(total / 1800.0))
    )


def _cycle_cheap_slots(
    inputs: PlannerInputs, geometry: HorizonGeometry
) -> tuple[tuple[datetime, float], ...]:
    """The fixed provider's window+dispatch folding over the charge cycle.

    Each cycle slot carries its window-coverage fraction plus dispatch
    overlap, capped at 1.0 so a partial slot never double-discounts a
    dispatch/window — the same rule :func:`_cheap_fractions` folds onto the
    horizon slots. Elapsed slots (a running window) are included: the cycle is
    the whole window-to-window stretch.
    """
    origin = _geometry_origin(inputs, geometry)
    ws = _hours_since(origin, geometry.window_start, absolute=True)
    we = _hours_since(origin, geometry.window_end, absolute=True)
    dispatches: list[tuple[float, float]] = []
    for d in inputs.dispatches:
        da = _hours_since(origin, d.start, absolute=True)
        db = _hours_since(origin, d.end, absolute=True)
        dispatches.append((da, db))
    slots: list[tuple[datetime, float]] = []
    for start in _cycle_slot_starts(geometry):
        a = _hours_since(origin, start, absolute=True)
        b = a + 0.5
        frac = max(0.0, min(b, we) - max(a, ws)) / 0.5
        for da, db in dispatches:
            frac = min(1.0, frac + max(0.0, min(b, db) - max(a, da)) / 0.5)
        slots.append((start, frac))
    return tuple(slots)


def _cycle_slot_prices(
    starts: tuple[datetime, ...],
    points: tuple[PricePoint, ...],
    inputs: PlannerInputs,
    geometry: HorizonGeometry,
) -> list[float | None]:
    """Per-cycle-slot live price via time overlap with ``points``, elapsed
    slots included; ``None`` where the feed misses, exactly as
    :func:`_slot_prices_from_points` prices the horizon slots."""
    origin = _geometry_origin(inputs, geometry)
    prices: list[float | None] = [None] * len(starts)
    for pt in points:
        a = _hours_since(origin, pt.start, absolute=True)
        b = _hours_since(origin, pt.end, absolute=True)
        for j, start in enumerate(starts):
            sa = _hours_since(origin, start, absolute=True)
            if max(a, sa) < min(b, sa + 0.5):
                prices[j] = pt.price
    return prices


def _carry_cycle_marks(
    geometry: HorizonGeometry,
    starts: tuple[datetime, ...],
    cheapest: set[int],
    inputs: PlannerInputs,
) -> list[float]:
    """Carry the cycle's cheap marks onto the horizon slots by real overlap
    (a window boundary off the half-hour splits a mark across two slots).
    Slots before the window are never marked; slot 0's fraction is over its
    part still to come."""
    origin = _geometry_origin(inputs, geometry)
    horizon = [_hours_since(origin, s, absolute=True) for s in geometry.slot_starts]
    fracs = [0.0] * len(horizon)
    for j in sorted(cheapest):
        a = _hours_since(origin, starts[j], absolute=True)
        b = a + 0.5
        for i, sa in enumerate(horizon):
            length = 0.5 * (geometry.slot0_remaining_frac if i == 0 else 1.0)
            overlap = max(0.0, min(b, sa + 0.5) - max(a, sa + 0.5 - length))
            if overlap > 0:
                fracs[i] = min(1.0, fracs[i] + overlap / length)
    return fracs


@dataclass(frozen=True)
class TariffSchedule:
    """Normalised tariff over the planning horizon — the sole contract to the planner.

    ``prices``/``cheap_fracs``/``export_prices`` fields are per-slot over the
    48-slot horizon (empty on the v1 daily path, which has no horizon).
    ``cheap_fracs[i]`` is the
    supplier-controlled-cheap fraction of slot ``i``: during it, cheap grid runs
    the house so the battery need not, and import is billed at ``cheap_rate``.
    ``controlled_windows`` are the daytime dispatch windows the battery holds
    through. ``cheap_rate``/``standard_rate``/``export_rate`` and
    ``window_hours`` feed the daily-balance model and charge-purchase costing.
    ``window_start`` is the charge window's local start clock time — slot 0 of
    ``cheap_fracs``/``prices`` on a legacy, window-anchored horizon — so the
    schedule anchors its per-slot pattern to the clock on its own (the cost
    backtest classifies stored import against it). A current-half-hour
    horizon (#394) anchors its cheap pattern to the charge cycle instead and
    exposes it as ``cycle_cheap_slots``; empty keeps the legacy anchoring
    above.
    """

    cheap_rate: float
    standard_rate: float
    export_rate: float
    window_hours: float
    window_start: time | None = None
    prices: tuple[float, ...] = ()
    cheap_fracs: tuple[float, ...] = ()
    controlled_windows: tuple[tuple[datetime, datetime], ...] = ()
    export_prices: tuple[float, ...] = ()
    # The charge cycle the cheap pattern was chosen/folded over (#399), for
    # non-planner consumers: each cycle slot's absolute start instant with its
    # supplier-controlled-cheap fraction, 30 real minutes apart from the
    # running-or-next window start to the next window start (46/48/50 slots
    # across a DST change; elapsed slots included). Empty on legacy,
    # window-anchored inputs — their ``cheap_fracs`` is the cycle already.
    cycle_cheap_slots: tuple[tuple[datetime, float], ...] = ()


class TariffProvider(Protocol):
    """Turns live inputs + config into one normalised :class:`TariffSchedule`."""

    def schedule(self, inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule: ...


def _controlled_windows(
    dispatches: tuple[DispatchSlot, ...], window_start: time, window_end: time
) -> tuple[tuple[datetime, datetime], ...]:
    """Daytime dispatches (outside the overnight window) become controlled windows
    the battery holds through; night dispatches fold into cheap coverage instead."""
    return tuple(
        (d.start, d.end)
        for d in dispatches
        if not _in_overnight_window(d.start.time(), window_start, window_end)
    )


@dataclass(frozen=True)
class FixedTariffProvider:
    """Legacy fixed-window two-rate tariff: off-peak inside the window/dispatches."""

    cheap_rate: float
    standard_rate: float
    export_rate: float

    def schedule(self, inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
        controlled = _controlled_windows(inputs.dispatches, cfg.window_start, cfg.window_end)
        prices: tuple[float, ...] = ()
        cheap_fracs: tuple[float, ...] = ()
        cycle: tuple[tuple[datetime, float], ...] = ()
        if inputs.load_slots is not None:
            fracs = _cheap_fractions(
                len(inputs.load_slots), cfg.window_hours, inputs.horizon_start, inputs.dispatches,
                inputs, cfg,
            )
            cheap_fracs = tuple(fracs)
            prices = tuple(
                f * self.cheap_rate + (1.0 - f) * self.standard_rate for f in fracs
            )
            if inputs.geometry is not None:
                # A real geometry exposes the cycle for non-planner consumers
                # (the cost backtest); legacy inputs are window-anchored already.
                cycle = _cycle_cheap_slots(inputs, inputs.geometry)
        return TariffSchedule(
            cheap_rate=self.cheap_rate,
            standard_rate=self.standard_rate,
            export_rate=self.export_rate,
            window_hours=cfg.window_hours,
            window_start=cfg.window_start,
            prices=prices,
            cheap_fracs=cheap_fracs,
            controlled_windows=controlled,
            cycle_cheap_slots=cycle,
        )


def fixed_schedule(inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
    """The default schedule: reproduces the legacy two-rate behaviour from ``cfg``."""
    return FixedTariffProvider(
        cheap_rate=cfg.rate_offpeak,
        standard_rate=cfg.rate_peak,
        export_rate=cfg.rate_export,
    ).schedule(inputs, cfg)


def _slot_prices_from_points(
    n_slots: int, horizon_start: datetime | None, points: tuple[PricePoint, ...],
    inputs: PlannerInputs, cfg: PlannerConfig,
) -> list[float | None]:
    """Per-slot live price via time overlap with ``points``; ``None`` where uncovered."""
    prices: list[float | None] = [None] * n_slots
    if horizon_start is None:
        return prices
    geometry = inputs.geometry_for(cfg)
    absolute = inputs.geometry is not None
    origin = inputs.now if absolute and inputs.now is not None else horizon_start
    for pt in points:
        a = _hours_since(origin, pt.start, absolute=absolute)
        b = _hours_since(origin, pt.end, absolute=absolute)
        for i in range(n_slots):
            slot_start = (
                _hours_since(origin, geometry.slot_starts[i], absolute=absolute)
                if geometry is not None else i * 0.5
            )
            slot_end = slot_start + 0.5
            if max(a, slot_start) < min(b, slot_end):
                prices[i] = pt.price
    return prices


@dataclass(frozen=True)
class DynamicTariffProvider:
    """Half-hourly HA price-sensor tariff: costs each slot at its live price.

    The physical charge window stays ``cfg.window_start``/``window_end`` (a
    later ticket may change that); this provider only re-derives which slots
    count as "cheap" for costing — the ``window_hours``-worth of slots with the
    lowest live price over the charge cycle (the running-or-next window start
    to the next window start), instead of a fixed clock window, so a replan at
    any hour selects exactly the slots a window-start replan selected (#399).
    Falls back to
    ``fallback`` (the fixed schedule) whenever there's no v2 slot horizon or no
    usable live price for any slot — a sensor hiccup degrades a plan, it never
    blocks one.
    """

    fallback: FixedTariffProvider

    def schedule(self, inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
        if inputs.load_slots is None or not inputs.dynamic_prices:
            return self.fallback.schedule(inputs, cfg)
        n = len(inputs.load_slots)
        raw_prices = _slot_prices_from_points(
            n, inputs.horizon_start, inputs.dynamic_prices, inputs, cfg
        )
        if not any(p is not None for p in raw_prices):
            return self.fallback.schedule(inputs, cfg)
        # ponytail: an uncovered slot (partial sensor read) costs at the
        # standard rate rather than being guessed at.
        prices = [p if p is not None else self.fallback.standard_rate for p in raw_prices]
        n_window = int(cfg.window_hours * 2)
        geometry = inputs.geometry
        if geometry is None:
            # Legacy, window-anchored horizon: the horizon *is* the charge
            # cycle, so the selection is exactly the pre-#399 one and no
            # separate cycle view is exposed.
            cheapest = set(sorted(range(n), key=lambda i: prices[i])[:n_window])
            cheap_fracs = tuple(1.0 if i in cheapest else 0.0 for i in range(n))
            cycle: tuple[tuple[datetime, float], ...] = ()
        else:
            # The cheap set is chosen over the charge cycle — the running-or-
            # next window start to the next window start (decision C) — elapsed
            # slots included, so a mid-day or in-window replan selects exactly
            # the slots a window-start replan selected. An elapsed slot prices
            # from the feed, else the standard rate; its mark carries onto the
            # horizon or falls off it (already past). Slots before the window
            # are priced live but never cheap (no daytime charging, #236).
            starts = _cycle_slot_starts(geometry)
            cycle_prices = [
                p if p is not None else self.fallback.standard_rate
                for p in _cycle_slot_prices(starts, inputs.dynamic_prices, inputs, geometry)
            ]
            cheapest = set(
                sorted(range(len(starts)), key=lambda j: cycle_prices[j])[:n_window]
            )
            cheap_fracs = tuple(_carry_cycle_marks(geometry, starts, cheapest, inputs))
            cycle = tuple(
                (start, 1.0 if j in cheapest else 0.0) for j, start in enumerate(starts)
            )
        controlled = _controlled_windows(inputs.dispatches, cfg.window_start, cfg.window_end)
        return TariffSchedule(
            cheap_rate=self.fallback.cheap_rate,
            standard_rate=self.fallback.standard_rate,
            export_rate=self.fallback.export_rate,
            window_hours=cfg.window_hours,
            window_start=cfg.window_start,
            prices=tuple(prices),
            cheap_fracs=cheap_fracs,
            controlled_windows=controlled,
            cycle_cheap_slots=cycle,
        )


@dataclass(frozen=True)
class OctopusIntelligentProvider:
    """Octopus Intelligent: dispatch windows + per-slot prices from the Octopus API.

    Dispatch/cheap-window costing is identical to ``fallback`` (the same
    ``_cheap_fractions``/``_controlled_windows`` folding as the fixed
    provider) — dispatches are supplier-controlled cheap windows exactly as
    they are today, per spec. The only thing this provider adds is live
    per-slot import prices (``inputs.dynamic_prices``, sourced from the
    Octopus standard-unit-rates API rather than a generic HA sensor) laid
    over that same schedule, for accurate cost reporting. Falls back to
    ``fallback`` verbatim whenever there's no v2 slot horizon or no usable
    live price — an API hiccup degrades a plan, it never blocks one.
    """

    fallback: FixedTariffProvider

    def schedule(self, inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
        base = self.fallback.schedule(inputs, cfg)
        if inputs.load_slots is None or not inputs.dynamic_prices:
            return base
        raw_prices = _slot_prices_from_points(
            len(inputs.load_slots), inputs.horizon_start, inputs.dynamic_prices, inputs, cfg
        )
        if not any(p is not None for p in raw_prices):
            return base
        prices = tuple(
            p if p is not None else self.fallback.standard_rate for p in raw_prices
        )
        return replace(base, prices=prices)


def _event_export_prices(
    n_slots: int,
    horizon_start: datetime | None,
    event: FlexibilityEvent | None,
    default_rate: float,
    inputs: PlannerInputs,
    cfg: PlannerConfig,
) -> tuple[float, ...]:
    """Price only the part of the slot horizon covered by an export event."""
    prices = [default_rate] * n_slots
    if horizon_start is None or event is None or event.direction != "export":
        return tuple(prices)
    geometry = inputs.geometry_for(cfg)
    absolute = inputs.geometry is not None
    origin = inputs.now if absolute and inputs.now is not None else horizon_start
    start = _hours_since(origin, event.start, absolute=absolute)
    end = _hours_since(origin, event.end, absolute=absolute)
    for i in range(n_slots):
        slot_start = (
            _hours_since(origin, geometry.slot_starts[i], absolute=absolute)
            if geometry is not None else i * 0.5
        )
        slot_end = slot_start + 0.5
        if max(start, slot_start) < min(end, slot_end):
            prices[i] = event.rate_gbp_kwh
    return tuple(prices)


@dataclass(frozen=True)
class AxleTariffProvider:
    """Overlay a validated Axle export event on the existing tariff schedule."""

    fallback: TariffProvider
    event_rate_gbp_kwh: float | None = None

    def schedule(self, inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
        base = self.fallback.schedule(inputs, cfg)
        if inputs.load_slots is None:
            return base
        default_rate = base.export_rate
        event = inputs.flexibility_event
        if event is not None and self.event_rate_gbp_kwh is not None:
            event = replace(event, rate_gbp_kwh=self.event_rate_gbp_kwh)
        return replace(
            base,
            export_prices=_event_export_prices(
                len(inputs.load_slots),
                inputs.horizon_start,
                event,
                default_rate,
                inputs,
                cfg,
            ),
        )
