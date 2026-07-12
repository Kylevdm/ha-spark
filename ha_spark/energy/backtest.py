"""Cost backtest over stored half-hourly grid import — pure functions.

The consumption store holds Octopus grid *import* (what the meter actually
drew, already shaped by battery/solar), so this is an actual-cost summary
under the configured two-rate tariff — not a counterfactual planner replay.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ha_spark.energy.models import ConsumptionInterval
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


def backtest_cost(
    intervals: Sequence[ConsumptionInterval],
    *,
    window_start: time,
    window_end: time,
    schedule: TariffSchedule,
    tz: ZoneInfo,
) -> BacktestSummary | None:
    """Rate each interval off-peak/peak by its local start time; None if empty.

    Off-peak is the fixed (possibly midnight-wrapping) charge window, rated at
    the schedule's cheap/standard rates — the same schedule the planner costs
    against. Historic Octopus dispatch slots are not stored, so dispatch-time
    import rates as peak — the summary slightly overstates the true cost.
    """
    if not intervals:
        return None
    offpeak_kwh = peak_kwh = 0.0
    dates = set()
    for interval in intervals:
        local = interval.start.astimezone(tz)
        dates.add(local.date())
        if _in_overnight_window(local.time(), window_start, window_end):
            offpeak_kwh += interval.kwh
        else:
            peak_kwh += interval.kwh
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
