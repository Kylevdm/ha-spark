"""Pure `/api/history` response building for the plan view (#496).

Buckets step by absolute half-hours from local midnight in the household
zone, so a daylight-saving day keeps its real slots. Every displayed value is
a recorded forecast or validated measurement; nothing is interpolated or invented here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

# One history bucket: 30 real minutes, whatever the wall clock does that night.
SLOT_MINUTES = 30


def bucket_starts(midnight: datetime, to: datetime) -> list[datetime]:
    """Absolute half-hours from local midnight (UTC) up to ``to``, exclusive."""
    step = timedelta(minutes=SLOT_MINUTES)
    starts: list[datetime] = []
    at = midnight.astimezone(UTC)
    while at < to:
        starts.append(at)
        at += step
    return starts


def history_payload(
    starts: Sequence[datetime],
    recorded: Mapping[datetime, float],
    tz: tzinfo,
    measured_reason: str | None,
    measured: Mapping[datetime, float] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Buckets, totals and completed-half-hour coverage from the same values."""
    measured = measured or {}
    buckets: list[dict[str, Any]] = []
    total = 0.0
    measured_total = 0.0
    completed = trustworthy = 0
    for start in starts:
        value = recorded.get(start)
        if value is not None:
            total += value
        measurement = measured.get(start) if measured_reason is None else None
        if measurement is not None:
            measured_total += measurement
        if now is not None and start + timedelta(minutes=30) <= now:
            completed += 1
            trustworthy += measurement is not None
        buckets.append({
            "start": start.astimezone(tz).isoformat(),
            "solar_forecast_kwh": value,
            "solar_measured_kwh": measurement,
        })
    return {
        "buckets": buckets,
        "totals": {"solar_forecast_kwh": total,
                   "solar_measured_kwh": measured_total if measured_reason is None else None},
        "coverage": (trustworthy / completed
                     if completed and measured_reason is None else None),
        "unavailable": {"solar_measured": measured_reason} if measured_reason else {},
    }
