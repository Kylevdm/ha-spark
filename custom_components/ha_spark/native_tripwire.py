"""Live export tripwire: pure counting rules and the coordinator-bound trip.

While a supervised export is live, two consecutive fresh whole-site grid readings
more than a deadband over the site export limit stop every export on that
connection. A missing or stale reading never cuts and never resets the count: it
only warns and raises a repair. Nothing here writes a register; cleanup goes
through ``NativeChargeBoundary.async_clear_export`` after the stop latch is durable.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from homeassistant.helpers import issue_registry as ir

from . import native_export_log
from .authority_store import ExportRecord, StopLatch
from .const import DOMAIN
from .native_export import site_export_limit_w

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator

log = logging.getLogger(__name__)

TRIP_DEADBAND_W = 200.0
FRESH_MAX_AGE = timedelta(seconds=60)
FRESH_MAX_FUTURE = timedelta(seconds=5)
WARN_AFTER = timedelta(seconds=60)
REPAIR_AFTER = timedelta(seconds=120)


@dataclass(frozen=True, kw_only=True)
class TripwireState:
    over_count: int = 0
    last_counted_at: datetime | None = None
    missing_since: datetime | None = None
    warned: bool = False


@dataclass(frozen=True)
class TripwireDecision:
    trip: bool
    missing_for: timedelta | None
    warn: bool
    repair: bool


def fresh(import_w: float | None, reported_at: datetime | None, now: datetime) -> bool:
    """A finite reading reported within the last minute (5 s of clock skew allowed)."""
    if import_w is None or not isfinite(import_w):
        return False
    if reported_at is None or reported_at.utcoffset() is None:
        return False
    return -FRESH_MAX_FUTURE <= now - reported_at <= FRESH_MAX_AGE


def step(
    state: TripwireState, *, import_w: float | None, reported_at: datetime | None,
    limit_w: float, now: datetime,
) -> tuple[TripwireState, TripwireDecision]:
    """One observation. ``limit_w`` is finite and positive; positive import_w is import."""
    now = now.astimezone(UTC)
    if import_w is None or reported_at is None or not fresh(import_w, reported_at, now):
        since = state.missing_since or now
        gap = now - since
        warn = gap >= WARN_AFTER and not state.warned
        nxt = TripwireState(
            over_count=state.over_count, last_counted_at=state.last_counted_at,
            missing_since=since, warned=state.warned or warn)
        return nxt, TripwireDecision(
            trip=False, missing_for=gap, warn=warn, repair=gap >= REPAIR_AFTER)
    reported = reported_at.astimezone(UTC)
    count = state.over_count
    counted = state.last_counted_at
    if counted is None or reported > counted:
        count = count + 1 if -import_w > limit_w + TRIP_DEADBAND_W else 0
        counted = reported
    nxt = TripwireState(over_count=count, last_counted_at=counted)
    return nxt, TripwireDecision(trip=count >= 2, missing_for=None, warn=False, repair=False)


class ExportTripwire:
    """Per-connection tripwire state and the durable stop."""

    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator
        self.states: dict[str, TripwireState] = {}

    def _members(self, connection_id: str) -> list[str]:
        guard = self.coordinator.guard
        if guard is None:
            return []
        banks = {b.id for b in guard.topology.banks if b.connection_id == connection_id}
        return [i.id for i in guard.topology.inverters if i.bank_id in banks]

    def watching(self, connection_id: str, now: datetime) -> list[ExportRecord]:
        """Live records on the connection that are inside their window."""
        state = self.coordinator.authority.state
        if state is None:
            return []
        instant = now.astimezone(UTC)
        records: list[ExportRecord] = []
        for inverter_id in self._members(connection_id):
            record = self.coordinator.writes._live_record(inverter_id, now)
            if record is not None and record.window.start <= instant < record.window.end:
                records.append(record)
        return records

    def issue_id(self, connection_id: str) -> str:
        key = uuid5(NAMESPACE_URL, f"{self.coordinator.entry.entry_id}:{connection_id}")
        return f"export_reading_missing_{key}"

    def _reset(self, connection_id: str) -> None:
        self.states.pop(connection_id, None)
        ir.async_delete_issue(self.coordinator.hass, DOMAIN, self.issue_id(connection_id))

    def observe(self, connection_id: str, import_w: float | None,
                reported_at: datetime | None, now: datetime) -> bool:
        """Count one grid reading; True when the connection must be stopped.

        Called under guard.lock: no await, no lock.
        """
        guard = self.coordinator.guard
        if guard is None or not self.watching(connection_id, now):
            self._reset(connection_id)
            return False
        limit_w = site_export_limit_w(guard.topology, guard.limits, connection_id)
        if limit_w is None or not isfinite(limit_w) or limit_w <= 0:
            self._reset(connection_id)
            return False
        state, decision = step(
            self.states.get(connection_id, TripwireState()), import_w=import_w,
            reported_at=reported_at, limit_w=limit_w, now=now)
        self.states[connection_id] = state
        if decision.warn and decision.missing_for is not None:
            log.warning(
                "Site grid reading missing or stale for %.0f s while an export is live; "
                "export continues on the device bound", decision.missing_for.total_seconds())
        if decision.repair:
            ir.async_create_issue(
                self.coordinator.hass, DOMAIN, self.issue_id(connection_id),
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key="export_reading_missing")
        else:
            ir.async_delete_issue(self.coordinator.hass, DOMAIN, self.issue_id(connection_id))
        return decision.trip

    async def async_trip(self, connection_id: str, now: datetime) -> None:
        """Latch durably first, then clear; never called while holding guard.lock."""
        c = self.coordinator
        async with c.control.lock:
            state = c.authority.state
            if state is None:
                return
            records = self.watching(connection_id, now)
            if not records:
                return
            members = set(self._members(connection_id))
            latches = list(state.stop_latches)
            latched: list[ExportRecord] = []
            for record in records:
                if not any(latch.inverter_id == record.inverter_id
                           and latch.event == record.event for latch in latches):
                    latches.append(StopLatch(
                        inverter_id=record.inverter_id, event=record.event,
                        window_end=record.window.end, epoch=state.epoch))
                latched.append(record)
            swap = {record: record.model_copy(update={
                "state": "stop_pending", "stop_reason": "tripped", "last_reconcile_at": now,
            }) for record in latched}
            candidate = state.model_copy(update={
                "stop_latches": tuple(latches),
                # Every bound on the connection is unvalidated until a new supervision.
                "export_supervisions": tuple(
                    row for row in state.export_supervisions if row.inverter_id not in members),
                "export_records": tuple(swap.get(row, row) for row in state.export_records),
                "last_changed_at": now,
            })
            for record in latched:
                candidate = native_export_log.with_append(
                    candidate, inverter_id=record.inverter_id, event=record.event,
                    kind="tripped", at=now, reason="tripped")
            if not await c.authority.async_save(candidate):
                log.warning("Export stop latch could not be saved; nothing was written")
                return
            self.states[connection_id] = TripwireState()
            epoch = state.epoch
        for inverter_id in [record.inverter_id for record in latched]:
            await c.writes.async_clear_export(inverter_id, epoch=epoch, cleanup=True)
