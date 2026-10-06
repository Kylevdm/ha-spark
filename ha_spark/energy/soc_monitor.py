"""Per-minute SoC integrity monitoring and Solis fallback recovery (#114/#115/#117).

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
failed, passing observations advance an in-memory recovery interval only when
Home Assistant report timestamps do not regress. Recovery progress requires a
report newer than its baseline and is deliberately not persisted. Persisted
fallback confirmation is not hardware truth after restart (#118).
Recovery progress is grouped in one in-memory record and completion consumes
a typed apply verdict; a failed verdict clears fallback confirmation even if
the device may have refused before writing, because no no-write proof crosses
the apply seam.
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


class RecoveryState(StrEnum):
    """Published progress of the Solis fallback recovery policy."""

    WAITING = "waiting"
    STABILIZING = "stabilizing"
    QUALIFIED = "qualified"
    RECOVERED = "recovered"


class EffectiveProgram(StrEnum):
    """Best current evidence about the program resident on the inverter."""

    NORMAL = "normal"
    FALLBACK = "fallback"
    UNKNOWN = "unknown"


class RecoveryOutcome(StrEnum):
    """Apply verdict consumed by :meth:`SocMonitor.complete_recovery`."""

    RECOVERED_VERIFIED = "recovered_verified"
    RECOVERED_UNVERIFIED = "recovered_unverified"
    FAILED = "failed"


@dataclass
class _RecoveryProgress:
    """In-memory stability interval and its latest published transition."""

    started_at: datetime | None = None
    baseline_reported_at: datetime | None = None
    last_reported_at: datetime | None = None
    qualified: bool = False
    state: RecoveryState = RecoveryState.WAITING
    elapsed_seconds: int = 0
    action: str | None = None
    hardware_verified: bool | None = None


@dataclass(frozen=True)
class SocMonitorSnapshot:
    """One tick's monitoring verdict: the observation plus its running state."""

    measurement: SocMeasurement
    consecutive_failures: int
    failure_threshold: int
    state: SocOperatingState
    fallback_confirmed: bool = False
    fallback_action: str | None = None
    recovery_state: RecoveryState = RecoveryState.WAITING
    recovery_elapsed_seconds: int = 0
    recovery_stable_minutes: int = 10
    recovery_action: str | None = None
    recovery_hardware_verified: bool | None = None
    effective_program: EffectiveProgram = EffectiveProgram.UNKNOWN


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
        recovery_stable_minutes: int = 10,
        path: Path | None = None,
    ) -> None:
        self._consecutive_failures = consecutive_failures
        self._fallback_status = fallback_status
        self._recovery_stable_minutes = recovery_stable_minutes
        # A read-back from an earlier process is history, not proof of the
        # current resident program. Only complete_fallback can set this true.
        self._fallback_confirmed = False
        self._path = path
        self._last: SocMeasurement | None = None
        self._last_snapshot: SocMonitorSnapshot | None = None
        self._fallback_attempt_measurement: SocMeasurement | None = None
        self._recovery = _RecoveryProgress()
        # A restored fallback label is history, not proof of resident hardware.
        self._effective_program = EffectiveProgram.UNKNOWN

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
        return cls(
            consecutive_failures=count,
            fallback_status=fallback_status,
            recovery_stable_minutes=settings.soc_recovery_stable_minutes,
            path=path,
        )

    def record(
        self, measurement: SocMeasurement, *, failure_threshold: int
    ) -> SocMonitorSnapshot:
        """Fold one observation into the running state; counts it exactly once."""
        if measurement is self._last and self._last_snapshot is not None:
            return self._last_snapshot
        if measurement.ok:
            snapshot = self._record_pass(measurement, failure_threshold)
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
    def recovery_qualified(self) -> bool:
        """Whether the current passing run has met recovery's stability policy."""
        return self._recovery.qualified

    @property
    def last_snapshot(self) -> SocMonitorSnapshot | None:
        """The latest observation combined with any post-observation outcome."""
        return self._last_snapshot

    def configure_recovery_stability(self, stable_minutes: int) -> None:
        """Adopt a hot-reloaded positive duration and restart stale progress."""
        if stable_minutes < 1:
            raise ValueError("recovery stability duration must be positive")
        if stable_minutes != self._recovery_stable_minutes:
            self._recovery_stable_minutes = stable_minutes
            self._reset_recovery()

    def request_fallback(self) -> SocMonitorSnapshot | None:
        """Mark one eligible, configured fallback attempt as requested."""
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
        return self._refresh_last_snapshot()

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
        self._effective_program = (
            EffectiveProgram.FALLBACK if confirmed else EffectiveProgram.UNKNOWN
        )
        snapshot = self._refresh_last_snapshot(fallback_action=action_line)
        self._save()
        return snapshot

    def invalidate_fallback_confirmation(self) -> None:
        """Forget confirmation after a change to the programmed fallback request."""
        if self._fallback_status is not SocOperatingState.FALLBACK_VERIFIED:
            return
        self._fallback_confirmed = False
        self._effective_program = EffectiveProgram.UNKNOWN
        self._save()

    def complete_recovery(self, outcome: RecoveryOutcome) -> SocMonitorSnapshot:
        """Finish one qualified apply from its typed verdict and retain truthful state.

        A failure verdict is conservative even when the driver refused before its
        first write (for example, the grid-charge permission gate). The verdict
        does not prove that hardware was untouched, so the prior fallback
        confirmation is cleared and charge increases remain blocked until retry.
        """
        if outcome is RecoveryOutcome.FAILED:
            self._fallback_status = SocOperatingState.FALLBACK_FAILED
            self._fallback_confirmed = False
            self._effective_program = EffectiveProgram.UNKNOWN
            self._recovery.qualified = True
            self._recovery.state = RecoveryState.QUALIFIED
            self._recovery.action = (
                "[FAILED] Solis recovery apply was not verified; hardware state is unconfirmed"
            )
            self._recovery.hardware_verified = False
        else:
            self._fallback_status = SocOperatingState.NORMAL
            self._consecutive_failures = 0
            self._fallback_confirmed = False
            # Simulate/off/non-owned control wrote nothing and has no read-back;
            # retain the last hardware evidence while returning control to normal.
            hardware_verified = outcome is RecoveryOutcome.RECOVERED_VERIFIED
            if hardware_verified:
                self._effective_program = EffectiveProgram.NORMAL
            action_line = (
                "[RECOVERED] Solis recovery: normal program read-back verified"
                if hardware_verified
                else "[RECOVERED] Solis recovery resumed without hardware write or read-back"
            )
            self._reset_recovery()
            self._recovery.state = RecoveryState.RECOVERED
            self._recovery.action = action_line
            self._recovery.hardware_verified = hardware_verified
        snapshot = self._refresh_last_snapshot(fallback_action=None)
        self._save()
        return snapshot

    # --- internals ---

    def _record_pass(
        self, measurement: SocMeasurement, failure_threshold: int
    ) -> SocMonitorSnapshot:
        if self.fallback_active:
            self._advance_recovery(measurement)
            return self._snapshot(measurement, failure_threshold, self._fallback_status)
        self._reset_recovery()
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
        *,
        fallback_action: str | None = None,
    ) -> SocMonitorSnapshot:
        return SocMonitorSnapshot(
            measurement=measurement,
            consecutive_failures=self._consecutive_failures,
            failure_threshold=failure_threshold,
            state=state,
            fallback_confirmed=self.fallback_confirmed,
            fallback_action=fallback_action,
            recovery_state=self._recovery.state,
            recovery_elapsed_seconds=self._recovery.elapsed_seconds,
            recovery_stable_minutes=self._recovery_stable_minutes,
            recovery_action=self._recovery.action,
            recovery_hardware_verified=self._recovery.hardware_verified,
            effective_program=self._effective_program,
        )

    def _refresh_last_snapshot(self, *, fallback_action: str | None = None) -> SocMonitorSnapshot:
        if self._last_snapshot is None:
            raise RuntimeError("fallback state has no SoC observation")
        snapshot = self._snapshot(
            self._last_snapshot.measurement,
            self._last_snapshot.failure_threshold,
            self._fallback_status,
            fallback_action=fallback_action,
        )
        self._last_snapshot = snapshot
        return snapshot

    def _advance_recovery(self, measurement: SocMeasurement) -> None:
        """Advance recovery on checked passes; a report regression starts a new run."""
        progress = self._recovery
        reported_at = measurement.reported_at
        if progress.started_at is None or (
            progress.last_reported_at is not None
            and reported_at is not None
            and reported_at < progress.last_reported_at
        ):
            self._recovery = _RecoveryProgress(
                started_at=measurement.observed_at,
                baseline_reported_at=reported_at,
                last_reported_at=reported_at,
                state=RecoveryState.STABILIZING,
            )
            return

        assert progress.started_at is not None
        if reported_at is not None:
            progress.last_reported_at = reported_at
        elapsed = max(
            0.0,
            (measurement.observed_at - progress.started_at).total_seconds(),
        )
        progress.elapsed_seconds = int(elapsed)
        advanced = (
            progress.baseline_reported_at is not None
            and progress.last_reported_at is not None
            and progress.last_reported_at > progress.baseline_reported_at
        )
        progress.qualified = progress.qualified or (
            elapsed >= self._recovery_stable_minutes * 60 and advanced
        )
        progress.state = (
            RecoveryState.QUALIFIED if progress.qualified else RecoveryState.STABILIZING
        )

    def _reset_recovery(self) -> None:
        self._recovery = _RecoveryProgress()

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
