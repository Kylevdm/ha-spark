"""Tariff providers — turn a user's tariff into one normalised schedule.

A :class:`TariffSchedule` is the *sole contract* between tariff logic and the
planner: per-slot import prices and supplier-controlled cheap fractions over the
horizon, plus the daytime controlled windows the battery holds through, plus the
representative rates the daily-balance model and charge-purchase costing use.

Only the ``fixed`` provider ships in this slice; it reproduces the legacy
fixed-window two-rate behaviour exactly (the golden baseline pins this). Later
slices add ``dynamic`` (HA price sensor) and ``octopus_intelligent`` providers
behind the same protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol

from ha_spark.energy.models import DispatchSlot, PlannerConfig, PlannerInputs


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


@dataclass(frozen=True)
class FixedTariffProvider:
    """Legacy fixed-window two-rate tariff: off-peak inside the window/dispatches."""

    cheap_rate: float
    standard_rate: float
    export_rate: float

    def schedule(self, inputs: PlannerInputs, cfg: PlannerConfig) -> TariffSchedule:
        # Daytime dispatches (outside the overnight window) become controlled
        # windows the battery holds through; night dispatches fold into the
        # window's cheap coverage via the per-slot fractions below.
        controlled = tuple(
            (d.start, d.end)
            for d in inputs.dispatches
            if not _in_overnight_window(d.start.time(), cfg.window_start, cfg.window_end)
        )
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
