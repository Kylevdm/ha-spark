"""Per-minute SoC integrity monitoring and the Solis fallback policy (#114/#115).

The daemon's one-minute loop is the sole SoC observation cadence: each tick
makes exactly one checked measurement (:func:`observe_soc`) and reuses it for
planning, device application, publication, and guard work, so one observation
can increment the failure count at most once (:class:`SocMonitor` dedupes by
observation identity on top of the loop calling ``record`` once per tick).

Operating state built from those measurements lives here, outside the pure
planner: the first failed observation enters **pending failure** — new
SoC-based programming and charge-rate increases are blocked (the chargers
refuse writes on a failed intent measurement; the supply guard caps its target
at the live setpoint until fallback is verified) while valid supply-guard
reductions remain available. Consecutive failures are counted and persisted in local durable
storage; any passing observation before fallback entry resets the count. The
configured threshold (default three) requests fallback only when a current is
configured. A successful Solis read-back confirms it; a failed or unconfirmed
attempt stays retryable on the next observation. Once requested, verified, or
failed, a passing observation leaves the fallback state in place for recovery.
Persisted confirmation is not hardware truth after restart (#118).

Recovery from an active fallback (#117) needs ``soc_recovery_minutes`` of
continuously passing observations whose report timestamps never go backwards,
with at least one report newer than the one seen when recovery began; any
failure resets progress, while SoC movement is allowed. Recovery progress is
never persisted: downtime is not healthy evidence. Qualifying only makes
recovery *ready* — the fallback stays the effective state until the control
loop applies and verifies a fresh normal plan (:meth:`SocMonitor.complete_recovery`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from ha_spark.config import Settings
from ha_spark.energy.soc_integrity import SocMeasurement, check_soc
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# Durable state lives next to the ledger DB (same convention as the published-
# states cache). JSON, not SQLite: the count and fallback status are tiny.
MONITOR_FILE = "ha_spark_soc_monitor.json"


class SocOperatingState(StrEnum):
    """The monitoring and fallback state for consecutive SoC failures."""

    NORMAL = "normal"
    PENDING_FAILURE = "pending_failure"
    FALLBACK_THRESHOLD = "fallback_threshold"
    FALLBACK_REQUESTED = "fallback_requested"
    FALLBACK_VERIFIED = "fallback_verified"
    FALLBACK_FAILED = "fallback_failed"


@dataclass(frozen=True)
class SocMonitorSnapshot:
    """One tick's monitoring verdict: the observation plus its running state."""

    measurement: SocMeasurement
    consecutive_failures: int
    failure_threshold: int
    state: SocOperatingState
    fallback_confirmed: bool = False
    fallback_action: str | None = None
    recovery_since: datetime | None = None
    recovery_ready: bool = False
    recovery_action: str | None = None


def monitor_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / MONITOR_FILE


async def observe_soc(settings: Settings, rest: HomeAssistantRest) -> SocMeasurement:
    """Make exactly one checked SoC observation from Home Assistant.

    A failed read (HTTP error, missing entity) is a failed measurement, never
    an exception into the caller. The single observation path shared by the
    daemon loop and ``gather_inputs``, so both judge freshness identically.

    The battery-voltage entity, when configured, is read alongside as the SoC
    source's liveness signal (#169); a failed read simply provides none.
    """
    state: EntityState | None = None
    try:
        state = await rest.get_state(settings.soc_entity)
    except Exception as exc:  # noqa: BLE001 - a dead sensor is evidence, not a crash
        log.warning("SoC monitor: reading %s failed (%s)", settings.soc_entity, exc)
    source: EntityState | None = None
    if settings.battery_voltage_entity:
        try:
            source = await rest.get_state(settings.battery_voltage_entity)
        except Exception as exc:  # noqa: BLE001 - no liveness evidence, not a crash
            log.debug(
                "SoC monitor: reading %s failed (%s)", settings.battery_voltage_entity, exc
            )
    return check_soc(
        state,
        observed_at=datetime.now(UTC),
        max_age=timedelta(minutes=settings.soc_max_report_age_minutes),
        source=source,
    )


class SocMonitor:
    """Tracks SoC failures and fallback confirmation across daemon ticks.

    ``record`` is called exactly once per one-minute observation by the
    control loop; recording the *same* measurement object again (a consumer
    reusing the tick's observation) returns the prior snapshot unchanged, so
    internal call structure can never accelerate failure counting.
    """

    def __init__(
        self,
        *,
        consecutive_failures: int = 0,
        fallback_status: SocOperatingState = SocOperatingState.NORMAL,
        path: Path | None = None,
    ) -> None:
        self._consecutive_failures = consecutive_failures
        self._fallback_status = fallback_status
        # A read-back from an earlier process is history, not proof of the
        # current resident program. Only complete_fallback can set this true.
        self._fallback_confirmed = False
        self._path = path
        self._last: SocMeasurement | None = None
        self._last_snapshot: SocMonitorSnapshot | None = None
        self._fallback_attempt_measurement: SocMeasurement | None = None
        # Recovery progress (#117), in memory only: a restart starts it over.
        self._recovery_since: datetime | None = None
        self._recovery_baseline: datetime | None = None
        self._recovery_last_report: datetime | None = None
        self._recovery_advanced = False
        self._recovery_ready = False

    @classmethod
    def load(cls, settings: Settings) -> SocMonitor:
        """Restore the count and fallback status, not hardware truth."""
        path = monitor_path(settings)
        count = 0
        fallback_status = SocOperatingState.NORMAL
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            count = int(raw["consecutive_failures"])
            fallback_status = SocOperatingState(raw.get("fallback_status", "normal"))
        except (OSError, ValueError, KeyError, TypeError):
            count = 0
            fallback_status = SocOperatingState.NORMAL
        if count < 0:
            log.warning("SoC monitor: persisted failure count %d invalid; starting at 0", count)
            count = 0
        return cls(consecutive_failures=count, fallback_status=fallback_status, path=path)

    def record(
        self,
        measurement: SocMeasurement,
        *,
        failure_threshold: int,
        recovery_duration: timedelta = timedelta(minutes=10),
    ) -> SocMonitorSnapshot:
        """Fold one observation into the running state; counts it exactly once."""
        if measurement is self._last and self._last_snapshot is not None:
            return self._last_snapshot
        if measurement.ok:
            snapshot = self._record_pass(measurement, failure_threshold, recovery_duration)
        else:
            snapshot = self._record_failure(measurement, failure_threshold)
        self._last = measurement
        self._last_snapshot = snapshot
        return snapshot

    @property
    def fallback_active(self) -> bool:
        """Whether fallback entry has begun and normal programming must wait."""
        return self._fallback_status in {
            SocOperatingState.FALLBACK_REQUESTED,
            SocOperatingState.FALLBACK_VERIFIED,
            SocOperatingState.FALLBACK_FAILED,
        }

    @property
    def fallback_confirmed(self) -> bool:
        """Whether this process verified the currently reported fallback."""
        return self._fallback_status is SocOperatingState.FALLBACK_VERIFIED and (
            self._fallback_confirmed
        )

    @property
    def recovery_ready(self) -> bool:
        """Whether an active fallback has met the recovery stability policy."""
        return self.fallback_active and self._recovery_ready

    def request_fallback(self) -> SocMonitorSnapshot | None:
        """Mark one eligible, configured fallback attempt as requested."""
        if self.recovery_ready:
            # The control loop is replacing the fallback with a fresh normal
            # plan; re-programming the fallback the same minute is only churn.
            return None
        retryable = self._fallback_status in {
            SocOperatingState.FALLBACK_THRESHOLD,
            SocOperatingState.FALLBACK_REQUESTED,
            SocOperatingState.FALLBACK_FAILED,
        } or (
            self._fallback_status is SocOperatingState.FALLBACK_VERIFIED
            and not self._fallback_confirmed
        )
        if (
            not retryable
            or self._last_snapshot is None
            or self._fallback_attempt_measurement is self._last
        ):
            return None
        self._fallback_attempt_measurement = self._last
        self._fallback_status = SocOperatingState.FALLBACK_REQUESTED
        self._fallback_confirmed = False
        self._save()
        return self._refresh_last_snapshot(fallback_confirmed=False)

    def complete_fallback(self, *, action_line: str) -> SocMonitorSnapshot:
        """Record the latest Solis read-back verdict and persist its status."""
        confirmed = action_line.startswith("[FALLBACK]")
        non_actuation = action_line.startswith(("[SIMULATE]", "[SKIP]", "[OFF]", "[OBSERVE]"))
        failed = action_line.startswith("[FAILED]")
        if not confirmed and not failed and not non_actuation:
            action_line = f"[FAILED] Solis fallback unconfirmed: {action_line}"
            failed = True
        self._fallback_status = (
            SocOperatingState.FALLBACK_VERIFIED
            if confirmed
            else SocOperatingState.FALLBACK_FAILED
            if failed
            else SocOperatingState.FALLBACK_REQUESTED
        )
        self._fallback_confirmed = confirmed
        snapshot = self._refresh_last_snapshot(
            fallback_confirmed=confirmed,
            fallback_action=action_line,
        )
        self._save()
        return snapshot

    def invalidate_fallback_confirmation(self) -> None:
        """Forget confirmation after a change to the programmed fallback request."""
        if self._fallback_status is not SocOperatingState.FALLBACK_VERIFIED:
            return
        self._fallback_confirmed = False
        self._save()

    def complete_recovery(self, *, action_line: str) -> SocMonitorSnapshot:
        """Leave fallback after the recovered normal program was verified."""
        log.info(
            "SoC integrity: recovered from %s after %d consecutive failure(s); "
            "normal operation",
            self._fallback_status.value,
            self._consecutive_failures,
        )
        self._consecutive_failures = 0
        self._fallback_status = SocOperatingState.NORMAL
        self._fallback_confirmed = False
        self._reset_recovery()
        self._save()
        return self._refresh_last_snapshot(
            fallback_confirmed=False, recovery_action=action_line
        )

    # --- internals ---

    def _reset_recovery(self) -> None:
        self._recovery_since = None
        self._recovery_baseline = None
        self._recovery_last_report = None
        self._recovery_advanced = False
        self._recovery_ready = False

    def _advance_recovery(
        self, measurement: SocMeasurement, recovery_duration: timedelta
    ) -> None:
        """Fold one passing observation into recovery progress (#117)."""
        reported_at = measurement.reported_at
        if reported_at is None:  # unreachable for a passing measurement
            self._reset_recovery()
            return
        if (
            self._recovery_last_report is not None
            and reported_at < self._recovery_last_report
        ):
            log.warning(
                "SoC integrity: report time went backwards (%s < %s); recovery restarts",
                reported_at.isoformat(),
                self._recovery_last_report.isoformat(),
            )
            self._reset_recovery()
        if self._recovery_since is None or self._recovery_baseline is None:
            self._recovery_since = measurement.observed_at
            self._recovery_baseline = reported_at
            log.info(
                "SoC integrity: passing observation; recovery from %s needs %s of "
                "stable reports",
                self._fallback_status.value,
                recovery_duration,
            )
        self._recovery_last_report = reported_at
        if reported_at > self._recovery_baseline:
            self._recovery_advanced = True
        ready = (
            self._recovery_advanced
            and measurement.observed_at - self._recovery_since >= recovery_duration
        )
        if ready and not self._recovery_ready:
            log.info(
                "SoC integrity: stable since %s; recovery ready, computing a fresh plan",
                self._recovery_since.isoformat(),
            )
        self._recovery_ready = ready

    def _record_pass(
        self,
        measurement: SocMeasurement,
        failure_threshold: int,
        recovery_duration: timedelta,
    ) -> SocMonitorSnapshot:
        if self.fallback_active:
            self._advance_recovery(measurement, recovery_duration)
            return self._snapshot(measurement, failure_threshold, self._fallback_status)
        if self._consecutive_failures:
            log.info(
                "SoC integrity: recovered after %d consecutive failure(s); normal operation",
                self._consecutive_failures,
            )
            self._consecutive_failures = 0
            self._fallback_status = SocOperatingState.NORMAL
            self._fallback_confirmed = False
            self._save()
        return self._snapshot(measurement, failure_threshold, SocOperatingState.NORMAL)

    def _record_failure(
        self, measurement: SocMeasurement, failure_threshold: int
    ) -> SocMonitorSnapshot:
        self._consecutive_failures += 1
        if self._recovery_since is not None:
            log.warning("SoC integrity: failure during recovery; recovery progress reset")
        self._reset_recovery()
        if self.fallback_active:
            state = self._fallback_status
        else:
            state = (
                SocOperatingState.FALLBACK_THRESHOLD
                if self._consecutive_failures >= failure_threshold
                else SocOperatingState.PENDING_FAILURE
            )
            self._fallback_status = state
        self._save()
        if state is SocOperatingState.FALLBACK_THRESHOLD:
            log.warning(
                "SoC integrity: failure %d/%d — %s; fallback-entry threshold reached",
                self._consecutive_failures,
                failure_threshold,
                measurement.reason,
            )
        else:
            log.warning(
                "SoC integrity: failure %d/%d — %s; new programming and "
                "charge-rate increases blocked",
                self._consecutive_failures,
                failure_threshold,
                measurement.reason,
            )
        return self._snapshot(measurement, failure_threshold, state)

    def _snapshot(
        self,
        measurement: SocMeasurement,
        failure_threshold: int,
        state: SocOperatingState,
    ) -> SocMonitorSnapshot:
        return SocMonitorSnapshot(
            measurement=measurement,
            consecutive_failures=self._consecutive_failures,
            failure_threshold=failure_threshold,
            state=state,
            fallback_confirmed=self.fallback_confirmed,
            recovery_since=self._recovery_since,
            recovery_ready=self.recovery_ready,
        )

    def _refresh_last_snapshot(
        self,
        *,
        fallback_confirmed: bool,
        fallback_action: str | None = None,
        recovery_action: str | None = None,
    ) -> SocMonitorSnapshot:
        if self._last_snapshot is None:
            raise RuntimeError("fallback state has no SoC observation")
        snapshot = SocMonitorSnapshot(
            measurement=self._last_snapshot.measurement,
            consecutive_failures=self._consecutive_failures,
            failure_threshold=self._last_snapshot.failure_threshold,
            state=self._fallback_status,
            fallback_confirmed=fallback_confirmed,
            fallback_action=fallback_action,
            recovery_since=self._recovery_since,
            recovery_ready=self.recovery_ready,
            recovery_action=recovery_action,
        )
        self._last_snapshot = snapshot
        return snapshot

    def _save(self) -> None:
        """Persist the failure count and fallback status; never raises into the loop."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "consecutive_failures": self._consecutive_failures,
                        "fallback_status": self._fallback_status.value,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            log.warning("SoC monitor: persisting failure count failed", exc_info=True)
