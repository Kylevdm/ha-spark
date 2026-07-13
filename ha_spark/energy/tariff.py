"""Tariff providers — turn a user's tariff into one normalised schedule.

A :class:`TariffSchedule` is the *sole contract* between tariff logic and the
planner: per-slot import prices and supplier-controlled cheap fractions over the
horizon, plus the daytime controlled windows the battery holds through, plus the
representative rates the daily-balance model and charge-purchase costing use.

The ``fixed`` provider reproduces the legacy fixed-window two-rate behaviour
exactly (the golden baseline pins this). ``dynamic`` costs each slot at its
live price from an HA half-hourly price sensor, falling back to ``fixed``
whenever there's no usable live read. ``octopus_intelligent`` keeps the fixed
provider's dispatch/window folding exactly (dispatches sourced from the
Octopus API instead of an HA sensor) and overlays live per-slot prices from
the Octopus standard-unit-rates API.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Protocol

from ha_spark.energy.models import DispatchSlot, PlannerConfig, PlannerInputs, PricePoint


def _in_overnight_window(t: time, start: time, end: time) -> bool:
    """True if clock time ``t`` falls in the charge window (which may wrap midnight)."""
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def _hours_since(origin: datetime, dt: datetime) -> float:
    """Hours from ``origin`` to ``dt``, coercing a naive ``dt`` into origin's tz."""
    if dt.tzinfo is None and origin.tzinfo is not None:
        dt = dt.replace(tzinfo=origin.tzinfo)
    elif dt.tzinfo is not None and origin.tzinfo is None:
        origin = origin.replace(tzinfo=dt.tzinfo)
    return (dt - origin).total_seconds() / 3600.0


def _cheap_fractions(
    n_slots: int,
    window_hours: float,
    horizon_start: datetime | None,
    dispatches: tuple[DispatchSlot, ...],
) -> list[float]:
    """Per-slot fraction billed off-peak: the fixed window plus dispatch overlap."""
    n_window = int(window_hours * 2)
    fracs = [1.0 if i < n_window else 0.0 for i in range(n_slots)]
    if horizon_start is None:
        return fracs
    for d in dispatches:
        a = _hours_since(horizon_start, d.start)
        b = _hours_since(horizon_start, d.end)
        for i in range(n_slots):
            slot_start, slot_end = i * 0.5, (i + 1) * 0.5
            overlap = max(0.0, min(b, slot_end) - max(a, slot_start)) / 0.5
            if overlap > 0:
                fracs[i] = min(1.0, fracs[i] + overlap)
    return fracs


@dataclass(frozen=True)
class TariffSchedule:
    """Normalised tariff over the planning horizon — the sole contract to the planner.

    ``prices``/``cheap_fracs`` are per-slot over the 48-slot horizon (empty on
    the v1 daily path, which has no horizon). ``cheap_fracs[i]`` is the
    supplier-controlled-cheap fraction of slot ``i``: during it, cheap grid runs
    the house so the battery need not, and import is billed at ``cheap_rate``.
    ``controlled_windows`` are the daytime dispatch windows the battery holds
    through. ``cheap_rate``/``standard_rate``/``export_rate`` and
    ``window_hours`` feed the daily-balance model and charge-purchase costing.
    """

    cheap_rate: float
    standard_rate: float
    export_rate: float
    window_hours: float
    prices: tuple[float, ...] = ()
    cheap_fracs: tuple[float, ...] = ()
    controlled_windows: tuple[tuple[datetime, datetime], ...] = ()


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
        if inputs.load_slots is not None:
            fracs = _cheap_fractions(
                len(inputs.load_slots), cfg.window_hours, inputs.horizon_start, inputs.dispatches
            )
            cheap_fracs = tuple(fracs)
            prices = tuple(
                f * self.cheap_rate + (1.0 - f) * self.standard_rate for f in fracs
            )
        return TariffSchedule(
            cheap_rate=self.cheap_rate,
            standard_rate=self.standard_rate,
            export_rate=self.export_rate,
            window_hours=cfg.window_hours,
            prices=prices,
            cheap_fracs=cheap_fracs,
            controlled_windows=controlled,
        )


def fixed_schedule(inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
    """The default schedule: reproduces the legacy two-rate behaviour from ``cfg``."""
    return FixedTariffProvider(
        cheap_rate=cfg.rate_offpeak,
        standard_rate=cfg.rate_peak,
        export_rate=cfg.rate_export,
    ).schedule(inputs, cfg)


def _slot_prices_from_points(
    n_slots: int, horizon_start: datetime | None, points: tuple[PricePoint, ...]
) -> list[float | None]:
    """Per-slot live price via time overlap with ``points``; ``None`` where uncovered."""
    prices: list[float | None] = [None] * n_slots
    if horizon_start is None:
        return prices
    for pt in points:
        a = _hours_since(horizon_start, pt.start)
        b = _hours_since(horizon_start, pt.end)
        for i in range(n_slots):
            slot_start, slot_end = i * 0.5, (i + 1) * 0.5
            if max(a, slot_start) < min(b, slot_end):
                prices[i] = pt.price
    return prices


@dataclass(frozen=True)
class DynamicTariffProvider:
    """Half-hourly HA price-sensor tariff: costs each slot at its live price.

    The physical charge window stays ``cfg.window_start``/``window_end`` (a
    later ticket may change that); this provider only re-derives which slots
    count as "cheap" for costing — the ``window_hours``-worth of slots with the
    lowest live price, instead of a fixed clock window. Falls back to
    ``fallback`` (the fixed schedule) whenever there's no v2 slot horizon or no
    usable live price for any slot — a sensor hiccup degrades a plan, it never
    blocks one.
    """

    fallback: FixedTariffProvider

    def schedule(self, inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
        if inputs.load_slots is None or not inputs.dynamic_prices:
            return self.fallback.schedule(inputs, cfg)
        n = len(inputs.load_slots)
        raw_prices = _slot_prices_from_points(n, inputs.horizon_start, inputs.dynamic_prices)
        if not any(p is not None for p in raw_prices):
            return self.fallback.schedule(inputs, cfg)
        # ponytail: an uncovered slot (partial sensor read) costs at the
        # standard rate rather than being guessed at.
        prices = [p if p is not None else self.fallback.standard_rate for p in raw_prices]
        n_window = int(cfg.window_hours * 2)
        cheapest = set(sorted(range(n), key=lambda i: prices[i])[:n_window])
        cheap_fracs = tuple(1.0 if i in cheapest else 0.0 for i in range(n))
        controlled = _controlled_windows(inputs.dispatches, cfg.window_start, cfg.window_end)
        return TariffSchedule(
            cheap_rate=self.fallback.cheap_rate,
            standard_rate=self.fallback.standard_rate,
            export_rate=self.fallback.export_rate,
            window_hours=cfg.window_hours,
            prices=tuple(prices),
            cheap_fracs=cheap_fracs,
            controlled_windows=controlled,
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
            len(inputs.load_slots), inputs.horizon_start, inputs.dynamic_prices
        )
        if not any(p is not None for p in raw_prices):
            return base
        prices = tuple(
            p if p is not None else self.fallback.standard_rate for p in raw_prices
        )
        return replace(base, prices=prices)
