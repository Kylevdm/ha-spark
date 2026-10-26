"""Pure `/api/history` response building for the plan view (#496).

Buckets step by absolute half-hours from local midnight in the household
zone, so a daylight-saving day keeps its real slots. Every displayed value is
the recorded forecast per slot; nothing is interpolated or invented here.
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
    measured_reason: str,
) -> dict[str, Any]:
    """Buckets and totals in one pass; measured solar is ticket 10's to fill."""
    buckets: list[dict[str, Any]] = []
    total = 0.0
    for start in starts:
        value = recorded.get(start)
        if value is not None:
            total += value
        buckets.append({
            "start": start.astimezone(tz).isoformat(),
            "solar_forecast_kwh": value,
            "solar_measured_kwh": None,
        })
    return {
        "buckets": buckets,
        "totals": {"solar_forecast_kwh": total, "solar_measured_kwh": None},
        "coverage": None,
        "unavailable": {"solar_measured": measured_reason},
    }
