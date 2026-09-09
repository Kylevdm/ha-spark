"""Checked battery SoC measurements (#113).

Forced grid charging is a costly hardware action, so a parseable SoC value is
not by itself trustworthy: it may be stale, malformed, or the product of a
failed read. This module turns exactly one Home Assistant observation into one
immutable :class:`SocMeasurement` recording the value (or read failure), the
integrity verdict, when it was observed, when Home Assistant last reported it,
the measured age, and the violated threshold.

The measurement is the contract: planning and actuation consume the value from
this exact object rather than re-reading the sensor, so one read can never
certify a different read. Operating state built on top of it (failure counting,
fallback, recovery) lives outside this module and outside the pure planner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from ha_spark.ha.models import EntityState

# Home Assistant's placeholders for "there is no reading", plus the empty state.
_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", "none", ""})

# The inclusive percentage range a battery SoC may occupy.
_MIN_SOC_PCT = 0.0
_MAX_SOC_PCT = 100.0


class SocStatus(StrEnum):
    """Why a checked SoC measurement passed or failed."""

    OK = "ok"
    READ_FAILED = "read_failed"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    NOT_FINITE = "not_finite"
    OUT_OF_RANGE = "out_of_range"
    REPORT_TIME_UNUSABLE = "report_time_unusable"
    REPORT_TIME_FUTURE = "report_time_future"
    STALE = "stale"


@dataclass(frozen=True)
class SocMeasurement:
    """One checked observation of the battery SoC sensor.

    Immutable on purpose: the object a planner consumed is the object that was
    checked. ``value`` is the parsed percentage when one could be parsed at all
    (kept even for out-of-range failures, as evidence); ``soc_now`` is the only
    value planning may use, and is 0 unless the measurement passed.
    """

    status: SocStatus
    observed_at: datetime
    value: float | None = None
    raw_state: str | None = None
    reported_at: datetime | None = None
    age_s: float | None = None  # observed_at - reported_at; negative when future
    max_age_s: float | None = None  # the threshold this measurement was judged against

    @property
    def ok(self) -> bool:
        """True only when every integrity check passed."""
        return self.status is SocStatus.OK

    @property
    def soc_now(self) -> float:
        """The SoC planning may use: the checked value, or 0 when untrusted."""
        if self.status is SocStatus.OK and self.value is not None:
            return self.value
        return 0.0

    @property
    def reason(self) -> str:
        """Operator-visible sentence naming the concrete integrity failure."""
        if self.status is SocStatus.OK:
            return f"SoC {self.soc_now:.0f}% reported {self.age_s:.0f}s ago"
        if self.status is SocStatus.READ_FAILED:
            return "SoC read from Home Assistant failed"
        if self.status is SocStatus.UNAVAILABLE:
            return f"SoC sensor unavailable (state {self.raw_state!r})"
        if self.status is SocStatus.MALFORMED:
            return f"SoC state {self.raw_state!r} is not a number"
        if self.status is SocStatus.NOT_FINITE:
            return f"SoC state {self.raw_state!r} is not finite"
        if self.status is SocStatus.OUT_OF_RANGE:
            return (
                f"SoC {self.value}% outside {_MIN_SOC_PCT:.0f}-{_MAX_SOC_PCT:.0f}%"
            )
        if self.status is SocStatus.REPORT_TIME_UNUSABLE:
            return "SoC last_reported is missing or unusable"
        if self.status is SocStatus.REPORT_TIME_FUTURE:
            return f"SoC last_reported is {abs(self.age_s or 0.0):.0f}s in the future"
        return (
            f"SoC last reported {self.age_s:.0f}s ago, over the "
            f"{self.max_age_s:.0f}s maximum"
            if self.age_s is not None and self.max_age_s is not None
            else "SoC report is stale"
        )


def check_soc(
    state: EntityState | None,
    *,
    observed_at: datetime,
    max_age: timedelta,
) -> SocMeasurement:
    """Check one SoC observation and record the verdict with its evidence.

    ``state`` is ``None`` when the Home Assistant read failed or the entity is
    absent — that is a failed measurement, not a crash. Freshness is judged
    solely on the top-level ``last_reported`` timestamp: ``last_updated`` and
    attribute timestamps are never substituted, because an actively reported
    but unchanged SoC must stay usable.
    """
    max_age_s = max_age.total_seconds()
    if state is None:
        return SocMeasurement(
            status=SocStatus.READ_FAILED, observed_at=observed_at, max_age_s=max_age_s
        )

    raw = state.state
    fail = SocMeasurement(
        status=SocStatus.UNAVAILABLE,
        observed_at=observed_at,
        raw_state=raw,
        max_age_s=max_age_s,
    )
    if raw.strip().lower() in _UNAVAILABLE_STATES:
        return fail

    # Parsed here rather than through sources._opt_float: sources imports this
    # module (reuse would be circular), and _opt_float cannot tell a malformed
    # state from a non-finite one, which are separate diagnostic evidence.
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return replace(fail, status=SocStatus.MALFORMED)
    if not math.isfinite(value):
        return replace(fail, status=SocStatus.NOT_FINITE)
    fail = replace(fail, value=value)
    if not _MIN_SOC_PCT <= value <= _MAX_SOC_PCT:
        return replace(fail, status=SocStatus.OUT_OF_RANGE)

    reported_at = state.last_reported
    if reported_at is None or reported_at.tzinfo is None:
        return replace(fail, status=SocStatus.REPORT_TIME_UNUSABLE)
    age_s = (observed_at - reported_at).total_seconds()
    fail = replace(fail, reported_at=reported_at, age_s=age_s)
    if age_s < 0:
        return replace(fail, status=SocStatus.REPORT_TIME_FUTURE)
    if age_s > max_age_s:
        return replace(fail, status=SocStatus.STALE)
    return replace(fail, status=SocStatus.OK)
