"""Native bank SoC integrity and in-memory recovery policy, ported from the daemon.

Successful native read time replaces HA last_reported. There is no sibling
liveness substitution: unchanged_too_long remains wire vocabulary but is
unreachable here. Absolute power is integrated by trapezoid; a changed passing
SoC rebaselines it, and two distinct reads may confirm a BMS recalibration.
Neither failure counts nor recovery progress survive a restart. No substitute
SoC, persistence, fallback application or write permission lives here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SocStatus = Literal[
    "ok", "implausible", "read_failed", "unavailable", "malformed", "not_finite",
    "out_of_range", "report_time_unusable", "report_time_future", "stale", "unchanged_too_long",
]
ENERGY_TOLERANCE_PCT = 2.0


class Policy(BaseModel):
    """Owner policy, with the daemon's defaults and finite, positive bounds."""

    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)
    # Upper bounds keep every timedelta representable (a day is far past any sane policy).
    soc_max_report_age_minutes: Annotated[
        float, Field(strict=True, gt=0, le=1440, allow_inf_nan=False)] = 10
    soc_failure_threshold: Annotated[int, Field(strict=True, ge=1, le=1440)] = 3
    soc_recovery_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)] = 10


@dataclass(frozen=True)
class BankTrust:
    """Read-only wire-sized view; invalid values are never published as percentages."""

    value: float | None = None
    read_at: datetime | None = None
    trust: Literal["trusted", "untrusted", "recovering"] = "untrusted"
    reason: SocStatus = "unavailable"


def check_soc(value: object, read_at: datetime | None, *, now: datetime,
              max_age: timedelta, baseline_soc: float | None = None,
              energy_kwh: float | None = None, capacity_kwh: float = 0) -> BankTrust:
    """Judge a native observation without inventing either a value or timestamp."""
    reason: SocStatus = "ok"
    parsed: float | None = None
    if value is None:
        reason = "unavailable"
    elif isinstance(value, bool) or not isinstance(value, (float, int, str)):
        reason = "malformed"
    else:
        try:
            parsed = float(value)
        except (ValueError, OverflowError):
            reason = "malformed"
        else:
            if not isfinite(parsed):
                parsed, reason = None, "not_finite"
            elif not 0 <= parsed <= 100:
                parsed, reason = None, "out_of_range"
    if read_at is not None and read_at.tzinfo is None:
        read_at = None
    if reason == "ok":
        if read_at is None:
            reason = "report_time_unusable"
        elif read_at > now:
            reason = "report_time_future"
        elif (baseline_soc is not None and parsed is not None and energy_kwh is not None
              and isfinite(energy_kwh) and energy_kwh >= 0
              and isfinite(capacity_kwh) and capacity_kwh > 0):
            delta = abs(parsed - baseline_soc)
            explained = energy_kwh / capacity_kwh * 100
            if delta > explained + ENERGY_TOLERANCE_PCT or (
                delta == 0 and explained > ENERGY_TOLERANCE_PCT
            ):
                reason = "implausible"
        if reason == "ok" and read_at is not None and now - read_at > max_age:
            reason = "stale"
    return BankTrust(parsed, read_at, "trusted" if reason == "ok" else "untrusted", reason)


class SocMonitor:
    """One bank, at most one counted observation per wall minute.

    Repeated or backwards read times cannot supply new passing evidence. A
    failed frame may retain its last good read until it ages out; a lost live
    validation is an immediate failure. Failures count once per minute even
    without advancing reads. A missed minute or non-advancing recovery read
    resets continuous recovery. A durable SoC fallback cause forces recovery
    after restart; clearing that cause is the fallback controller's job.
    """

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()
        self.view = BankTrust()
        self.failures = 0
        self._minute: int | None = None
        self._minute_failed = False
        self._read_at: datetime | None = None
        self._baseline: float | None = None
        self._energy = 0.0
        self._power_at: datetime | None = None
        self._power: float | None = None
        self._rejected: float | None = None
        self._blocked = False
        self._since: datetime | None = None
        self._recovery_read: datetime | None = None
        self._recovery_minute: int | None = None
        self.recovery_ready = False

    @property
    def fallback_requested(self) -> bool:
        return self._blocked and not self.recovery_ready

    @property
    def increases_allowed(self) -> bool:
        return self.view.trust == "trusted"

    def _reset_recovery(self) -> None:
        self._since = None
        self._recovery_read = None
        self._recovery_minute = None
        self.recovery_ready = False

    def _fail(self, count: bool) -> None:
        if count:
            self.failures += 1
            self._blocked |= self.failures >= self.policy.soc_failure_threshold
        self._reset_recovery()

    def observe(self, value: object, *, read_at: datetime | None, now: datetime,
                power_w: float | None, capacity_kwh: float, valid: bool = True,
                fallback_active: bool = False, count: bool = True) -> BankTrust:
        """Consume only native evidence; collection outside ownership never counts failures."""
        minute = int(now.timestamp() // 60)
        if fallback_active:
            self._blocked = True
        fresh = (read_at is not None and read_at.tzinfo is not None
                 and (self._read_at is None or read_at > self._read_at))
        if self._minute == minute:
            if fallback_active and self.view.trust == "trusted":
                self.view = BankTrust(self.view.value, self.view.read_at, "recovering", "ok")
            if not valid:
                self.view = BankTrust(self.view.value, self.view.read_at,
                                      "untrusted", "read_failed")
                if not self._minute_failed:
                    # Lost validation supersedes this minute's earlier pass, counted once.
                    self._minute_failed, self._rejected = True, None
                    self._fail(count)
            return self.view
        self._minute = minute
        max_age = timedelta(minutes=self.policy.soc_max_report_age_minutes)
        if fresh:
            if power_w is not None and not isfinite(power_w):
                power_w = None
            if self._power_at is not None and self._power is not None and power_w is not None:
                elapsed = max(0.0, (now - self._power_at).total_seconds())
                self._energy += (abs(self._power) + abs(power_w)) / 2 * elapsed / 3_600_000
            self._power_at, self._power = now, power_w
            result = check_soc(value, read_at, now=now, max_age=max_age,
                               baseline_soc=self._baseline,
                               energy_kwh=self._energy if power_w is not None else None,
                               capacity_kwh=capacity_kwh)
            if result.reason == "implausible" and result.value != self._baseline:
                if result.value == self._rejected:
                    result = check_soc(value, read_at, now=now, max_age=max_age)
                self._rejected = result.value if result.reason != "ok" else None
            else:
                self._rejected = None
            if result.reason == "ok" and result.value != self._baseline:
                self._baseline, self._energy = result.value, 0.0
            self._read_at = read_at
        else:
            # Do not re-run energy/recalibration or adopt a different value on
            # repeated/backwards evidence. A missing power read breaks integration,
            # and a minute without fresh evidence discards a recalibration candidate.
            self._rejected = None
            if power_w is None:
                self._power_at, self._power = now, None
            result = check_soc(self.view.value, self.view.read_at, now=now, max_age=max_age)
            if result.reason == "ok" and self.view.reason != "ok":
                result = BankTrust(result.value, result.read_at, "untrusted", self.view.reason)
        if not valid:
            result = BankTrust(result.value, result.read_at, "untrusted", "read_failed")
        self._minute_failed = result.reason != "ok"
        if result.reason != "ok":
            self._fail(count)
        elif self._blocked:
            if (not fresh or self._recovery_minute not in (None, minute - 1)
                    or (self._recovery_read is not None and read_at is not None
                        and read_at <= self._recovery_read)):
                self._reset_recovery()
            if fresh:
                if self._since is None:
                    self._since = now
                self._recovery_read, self._recovery_minute = read_at, minute
                self.recovery_ready = now - self._since >= timedelta(
                    minutes=self.policy.soc_recovery_minutes)
            if self.recovery_ready and not fallback_active:
                self._blocked, self.failures = False, 0
                self._reset_recovery()
            else:
                result = BankTrust(result.value, result.read_at, "recovering", "ok")
        else:
            self.failures = 0
        self.view = result
        return result
