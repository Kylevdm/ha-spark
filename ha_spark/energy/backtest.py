"""Cost backtest over stored half-hourly grid import — pure functions.

The consumption store holds Octopus grid *import* (what the meter actually
drew, already shaped by battery/solar), so this is an actual-cost summary
rated against the current tariff schedule — not a counterfactual planner replay.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ha_spark.energy.models import SLOTS_PER_DAY, ConsumptionInterval
from ha_spark.energy.tariff import TariffSchedule, _in_overnight_window


@dataclass(frozen=True)
class BacktestSummary:
    """Tariff-rated totals for a span of stored import intervals."""

    days: int
    offpeak_kwh: float
    peak_kwh: float
    rate_offpeak: float
    rate_peak: float
    first: datetime
    last: datetime

    @property
    def total_kwh(self) -> float:
        return self.offpeak_kwh + self.peak_kwh

    @property
    def offpeak_cost(self) -> float:
        return self.offpeak_kwh * self.rate_offpeak

    @property
    def peak_cost(self) -> float:
        return self.peak_kwh * self.rate_peak

    @property
    def total_cost(self) -> float:
        return self.offpeak_cost + self.peak_cost


def _slot_index(t: time) -> int:
    """Half-hour slot-of-day for a clock time (0 == 00:00, 47 == 23:30)."""
    return t.hour * 2 + t.minute // 30


def _window_end(window_start: time, window_hours: float) -> time:
    """The charge window's end clock time, ``window_hours`` after its start (wraps)."""
    total = window_start.hour * 60 + window_start.minute + round(window_hours * 60)
    total %= 24 * 60
    return time(total // 60, total % 60)


def _cheap_fraction_by_time(schedule: TariffSchedule) -> Callable[[time], float]:
    """A local-clock-time → off-peak-fraction classifier drawn from ``schedule``.

    With per-slot ``prices`` (the dynamic/intelligent path), the schedule's own
    ``cheap_fracs`` decide: each slot's supplier-controlled-cheap fraction, keyed
    off the clock via ``window_start`` (``cheap_fracs[0]`` is the window start).
    Without them (the v1 fixed path) it falls back to the flat charge window
    ``[window_start, window_start + window_hours)``. A schedule missing
    ``window_start`` cannot anchor either, so everything rates peak.
    """
    window_start = schedule.window_start
    if window_start is None:
        return lambda _t: 0.0
    if schedule.prices and schedule.cheap_fracs:
        fracs = schedule.cheap_fracs
        start_idx = _slot_index(window_start)

        def by_slot(t: time) -> float:
            slot = (_slot_index(t) - start_idx) % SLOTS_PER_DAY
            return fracs[slot] if slot < len(fracs) else 0.0

        return by_slot
    window_end = _window_end(window_start, schedule.window_hours)
    return lambda t: 1.0 if _in_overnight_window(t, window_start, window_end) else 0.0


def backtest_cost(
    intervals: Sequence[ConsumptionInterval],
    *,
    schedule: TariffSchedule,
    tz: ZoneInfo,
) -> BacktestSummary | None:
    """Rate each interval off-peak/peak by its local start time; None if empty.

    Off-peak coverage comes from ``schedule`` alone: its per-slot ``cheap_fracs``
    on a dynamic/intelligent tariff, or the flat charge window it carries on the
    fixed path (see :func:`_cheap_fraction_by_time`) — so a dynamic install is
    rated on the tariff it's actually on, not the two flat rates. A partly-cheap
    slot splits its energy between the buckets. Off-peak is rated at
    ``cheap_rate``, peak at ``standard_rate``. Historic Octopus dispatch slots
    are not stored, so dispatch-time import rates by the current cheap pattern,
    not its own — a documented approximation.
    """
    if not intervals:
        return None
    cheap_fraction = _cheap_fraction_by_time(schedule)
    offpeak_kwh = peak_kwh = 0.0
    dates = set()
    for interval in intervals:
        local = interval.start.astimezone(tz)
        dates.add(local.date())
        frac = cheap_fraction(local.time())
        offpeak_kwh += frac * interval.kwh
        peak_kwh += (1.0 - frac) * interval.kwh
    starts = [interval.start for interval in intervals]
    return BacktestSummary(
        days=len(dates),
        offpeak_kwh=offpeak_kwh,
        peak_kwh=peak_kwh,
        rate_offpeak=schedule.cheap_rate,
        rate_peak=schedule.standard_rate,
        first=min(starts),
        last=max(starts),
    )


def format_backtest(s: BacktestSummary) -> str:
    """Render the summary as an aligned, scannable block."""
    avg = s.total_cost / s.days if s.days else 0.0
    return "\n".join(
        [
            f"Grid import backtest ({s.days} days: "
            f"{s.first:%Y-%m-%d} .. {s.last:%Y-%m-%d}):",
            f"  Off-peak import    {s.offpeak_kwh:8.2f} kWh  @ £{s.rate_offpeak:.3f}"
            f"  ->  £{s.offpeak_cost:7.2f}",
            f"  Peak import        {s.peak_kwh:8.2f} kWh  @ £{s.rate_peak:.3f}"
            f"  ->  £{s.peak_cost:7.2f}",
            f"  Total              {s.total_kwh:8.2f} kWh"
            f"               £{s.total_cost:7.2f}  (£{avg:.2f}/day)",
        ]
    )
