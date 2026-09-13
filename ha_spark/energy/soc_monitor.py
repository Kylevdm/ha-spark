"""Per-minute SoC integrity monitoring and the pending-failure policy (#114).

The daemon's one-minute loop is the sole SoC observation cadence: each tick
makes exactly one checked measurement (:func:`observe_soc`) and reuses it for
planning, device application, publication, and guard work, so one observation
can increment the failure count at most once (:class:`SocMonitor` dedupes by
observation identity on top of the loop calling ``record`` once per tick).

Operating state built from those measurements lives here, outside the pure
planner: the first failed observation enters **pending failure** — new
SoC-based programming and charge-rate increases are blocked (the chargers
refuse writes on a failed intent measurement; the supply guard caps its
target at the live setpoint) while valid supply-guard reductions remain
available. Consecutive failures are counted and persisted in local durable
storage; any passing observation resets the count. The configured threshold
(default three) marks the fallback-entry point; fallback programming itself
(#115), recovery (#116/#117), and restart reconciliation (#118) extend this
module. Until fallback state exists, a passing observation resets the count
from any state — the reset-before-fallback rule tightens with #115.
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
# states cache). JSON, not SQLite: one integer, written at most once per tick.
MONITOR_FILE = "ha_spark_soc_monitor.json"


class SocOperatingState(StrEnum):
    """The monitoring state derived from consecutive observation failures."""

    NORMAL = "normal"
    PENDING_FAILURE = "pending_failure"
    FALLBACK_THRESHOLD = "fallback_threshold"


@dataclass(frozen=True)
class SocMonitorSnapshot:
    """One tick's monitoring verdict: the observation plus its running state."""

    measurement: SocMeasurement
    consecutive_failures: int
    failure_threshold: int
    state: SocOperatingState


def monitor_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / MONITOR_FILE


async def observe_soc(settings: Settings, rest: HomeAssistantRest) -> SocMeasurement:
    """Make exactly one checked SoC observation from Home Assistant.

    A failed read (HTTP error, missing entity) is a failed measurement, never
    an exception into the caller. The single observation path shared by the
    daemon loop and ``gather_inputs``, so both judge freshness identically.
    """
    state: EntityState | None = None
    try:
        state = await rest.get_state(settings.soc_entity)
    except Exception as exc:  # noqa: BLE001 - a dead sensor is evidence, not a crash
        log.warning("SoC monitor: reading %s failed (%s)", settings.soc_entity, exc)
    return check_soc(
        state,
        observed_at=datetime.now(UTC),
        max_age=timedelta(minutes=settings.soc_max_report_age_minutes),
    )


class SocMonitor:
    """Tracks consecutive SoC observation failures across daemon ticks.

    ``record`` is called exactly once per one-minute observation by the
    control loop; recording the *same* measurement object again (a consumer
    reusing the tick's observation) returns the prior snapshot unchanged, so
    internal call structure can never accelerate failure counting.
    """

    def __init__(self, *, consecutive_failures: int = 0, path: Path | None = None) -> None:
        self._consecutive_failures = consecutive_failures
        self._path = path
        self._last: SocMeasurement | None = None
        self._last_snapshot: SocMonitorSnapshot | None = None

    @classmethod
    def load(cls, settings: Settings) -> SocMonitor:
        """Restore the persisted failure count; degrade to zero on bad state."""
        path = monitor_path(settings)
        count = 0
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            count = int(raw["consecutive_failures"])
        except (OSError, ValueError, KeyError, TypeError):
            count = 0
        if count < 0:
            log.warning("SoC monitor: persisted failure count %d invalid; starting at 0", count)
            count = 0
        return cls(consecutive_failures=count, path=path)

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

    # --- internals ---

    def _record_pass(
        self, measurement: SocMeasurement, failure_threshold: int
    ) -> SocMonitorSnapshot:
        if self._consecutive_failures:
            log.info(
                "SoC integrity: recovered after %d consecutive failure(s); normal operation",
                self._consecutive_failures,
            )
            self._consecutive_failures = 0
            self._save()
        return SocMonitorSnapshot(
            measurement=measurement,
            consecutive_failures=0,
            failure_threshold=failure_threshold,
            state=SocOperatingState.NORMAL,
        )

    def _record_failure(
        self, measurement: SocMeasurement, failure_threshold: int
    ) -> SocMonitorSnapshot:
        self._consecutive_failures += 1
        self._save()
        state = (
            SocOperatingState.FALLBACK_THRESHOLD
            if self._consecutive_failures >= failure_threshold
            else SocOperatingState.PENDING_FAILURE
        )
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
        return SocMonitorSnapshot(
            measurement=measurement,
            consecutive_failures=self._consecutive_failures,
            failure_threshold=failure_threshold,
            state=state,
        )

    def _save(self) -> None:
        """Persist the failure count (best-effort; never raises into the loop)."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"consecutive_failures": self._consecutive_failures}),
                encoding="utf-8",
            )
        except OSError:
            log.warning("SoC monitor: persisting failure count failed", exc_info=True)
