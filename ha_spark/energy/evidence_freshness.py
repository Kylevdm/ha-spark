"""Read-only freshness metadata for hold evidence; no decision authority (#419)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from ha_spark.energy.soc_integrity import MAX_UNCHANGED


@dataclass(frozen=True)
class EvidenceFreshness:
    """Validity at observation time, retaining own and effective report times."""

    validity: str
    reported_at: datetime | None
    age_s: float | None
    max_age_s: float | None
    own_reported_at: datetime | None
    sibling_reported_at: datetime | None = None
    via_sibling: bool = False
    reason: str = ""


def check_report_time(
    reported_at: datetime | None,
    *,
    now: datetime,
    max_age_s: float | None,
    option: str,
    sibling_reported_at: datetime | None = None,
) -> EvidenceFreshness:
    """Classify only report times, never read/change/update/attribute times.

    Own fresh reports take precedence. A sibling can prove liveness only for
    a known non-future own report no older than MAX_UNCHANGED.
    """
    effective = reported_at
    age = None
    via_sibling = False
    validity = "unknown"
    reason = "last_reported missing or unusable"
    if reported_at is not None and reported_at.utcoffset() is not None:
        age = (now - reported_at).total_seconds()
        validity = "future" if age < 0 else "stale"
        reason = ""
        if max_age_s is not None and age >= 0:
            if age <= max_age_s:
                validity = "fresh"
            elif (
                age <= MAX_UNCHANGED.total_seconds()
                and sibling_reported_at is not None
                and sibling_reported_at.utcoffset() is not None
                and 0 <= (now - sibling_reported_at).total_seconds() <= max_age_s
            ):
                effective = sibling_reported_at
                age = (now - effective).total_seconds()
                validity = "fresh"
                via_sibling = True
    if max_age_s is None:
        validity = "unknown"
        reason = f"no validated max-age; set {option}"
    return EvidenceFreshness(
        validity, effective, age, max_age_s, reported_at, sibling_reported_at,
        via_sibling, reason,
    )


def battery_direction(value: object, *, unit: object, discharge_sign: str | None) -> str:
    """Map explicitly signed W to direction, with a strict 50 W idle deadband."""
    if unit != "W" or discharge_sign not in ("positive", "negative"):
        return "unknown"
    try:
        watts = float(str(value))
    except (ValueError, TypeError):
        return "unknown"
    if not isfinite(watts):
        return "unknown"
    if abs(watts) < 50:
        return "idle"
    discharge = watts * (1 if discharge_sign == "positive" else -1)
    return "discharging" if discharge > 0 else "charging"
