"""Pure supervised-export bounds, fixed-command fit and live-record rules.

No register is written here. These helpers only narrow or refuse; the one
charge-write path still rechecks authority, ownership and limits before any
window is armed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from math import isfinite
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .authority_store import (
    EventIdentity,
    ExportRecord,
    ExportSupervision,
    GatewayClaim,
    StopLatch,
)
from .topology import EffectiveLimits, Topology

EXPORT_CURRENT_A = 62.5


@dataclass(frozen=True)
class DeviceExportFact:
    """Live validation facts used to bind a confirmed rating to one device."""

    validated: bool
    identity: tuple[int, ...] | None = None
    claim: GatewayClaim | None = None


def event_from_raw(raw: object) -> EventIdentity | None:
    """A strict export event, or None when the payload is malformed."""
    if not isinstance(raw, dict):
        return None
    try:
        event = EventIdentity.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        return None
    return event if event.direction == "export" else None


def claim_from_raw(raw: object) -> GatewayClaim | None:
    """A gateway claim from options, or None when it is missing or malformed."""
    if not isinstance(raw, dict):
        return None
    try:
        return GatewayClaim.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        return None


def claim_data(claim: GatewayClaim | None) -> dict[str, object] | None:
    if claim is None:
        return None
    return {"host": claim.host, "port": claim.port, "unit_id": claim.unit_id}


def site_export_limit_w(
    topology: Topology, limits: EffectiveLimits, connection_id: str,
) -> float | None:
    connection = next((c for c in topology.connections if c.id == connection_id), None)
    limit = next((c for c in limits.connections if c.id == connection_id), None)
    if connection is None or limit is None:
        return None
    # The accepted topology supplies the site limit; a zero limit never widens.
    return min(connection.dno_export_limit_kw * 1000, limit.site_export_kw * 1000)


def ratings_refusal(
    event: EventIdentity, ratings: Mapping[str, float], topology: Topology,
    limits: EffectiveLimits, facts: Mapping[str, DeviceExportFact], *,
    now: datetime,
) -> str | None:
    """Validate one supervision's ratings and the per-connection sum rule."""
    if event.end <= now.astimezone(UTC):
        return "invalid_intent"
    if not ratings:
        return "invalid_intent"
    by_id = {inverter.id: inverter for inverter in topology.inverters}
    if any(key not in by_id for key in ratings):
        return "invalid_intent"
    if any(not isfinite(value) or value <= 0 for value in ratings.values()):
        return "invalid_intent"
    banks = {bank.id: bank for bank in topology.banks}
    connections = {banks[by_id[key].bank_id].connection_id for key in ratings}
    for connection_id in connections:
        members = [inverter for inverter in topology.inverters
                   if banks[inverter.bank_id].connection_id == connection_id]
        # Every export-capable inverter on the connection has a bound; an
        # unrated peer's output is unknown, never assumed zero.
        if any(inverter.id not in ratings for inverter in members):
            return "export_bound_unverified"
        if any(not facts.get(inverter.id, DeviceExportFact(False)).validated
               or not facts.get(inverter.id, DeviceExportFact(False)).identity
               or facts.get(inverter.id, DeviceExportFact(False)).claim is None
               for inverter in members):
            return "export_bound_unverified"
        limit_w = site_export_limit_w(topology, limits, connection_id)
        if limit_w is None or limit_w <= 0:
            return "export_bounds_exceed_site_limit"
        values = [ratings[inverter.id] for inverter in members]
        if any(value > limit_w for value in values) or sum(values) > limit_w:
            return "export_bounds_exceed_site_limit"
    return None


def fixed_export_refusal(
    exporting: Sequence[str], topology: Topology, limits: EffectiveLimits,
) -> str | None:
    """Refuse the fixed 62.5 A command when it cannot fit each inverter and bank."""
    by_id = {inverter.id: inverter for inverter in topology.inverters}
    effective = {inverter.id: inverter for inverter in limits.inverters}
    banks = {bank.id: bank for bank in topology.banks}
    exporting_set = set(exporting)
    for inverter_id in exporting_set:
        inverter = by_id.get(inverter_id)
        inverter_limit = effective.get(inverter_id)
        if (inverter is None or inverter_limit is None
                or EXPORT_CURRENT_A > inverter_limit.discharge_a):
            return "limits"
    for bank in banks.values():
        count = sum(1 for inverter_id in exporting_set
                    if by_id.get(inverter_id) is not None
                    and by_id[inverter_id].bank_id == bank.id)
        if not count:
            continue
        bank_a = min(bank.discharge_limit_a, sum(
            limit.discharge_a for limit in limits.inverters if limit.bank_id == bank.id))
        if EXPORT_CURRENT_A * count > bank_a:
            return "limits"
    return None


def bound_refusal(
    supervision: ExportSupervision, fact: DeviceExportFact, *,
    site_limit_w: float | None, connection_sum_w: float,
) -> str | None:
    """Re-verify one confirmed bound before a window is armed."""
    if (not fact.validated or fact.identity != supervision.identity
            or fact.claim != supervision.claim):
        return "export_bound_unverified"
    if site_limit_w is None or site_limit_w <= 0:
        return "export_bounds_exceed_site_limit"
    if supervision.rating_w > site_limit_w or connection_sum_w > site_limit_w:
        return "export_bounds_exceed_site_limit"
    return None


def record_is_live(
    record: ExportRecord, *, now: datetime, epoch: int, store_id: UUID,
    supervisions: Sequence[ExportSupervision], stop_latches: Sequence[StopLatch],
    fallback_active: bool,
) -> bool:
    """The spec's live-record predicate: every other record is cleanup."""
    if record.state not in ("armed", "active", "preserved"):
        return False
    if record.epoch != epoch or record.store_id != store_id:
        return False
    if record.event.end <= now.astimezone(UTC) or fallback_active:
        return False
    if any(latch.event == record.event for latch in stop_latches):
        return False
    return any(row.id == record.supervision_id and row.event == record.event
               for row in supervisions)


def supervision_for(
    supervisions: Sequence[ExportSupervision], inverter_id: str,
    event: EventIdentity,
) -> ExportSupervision | None:
    return next((row for row in reversed(supervisions)
                 if row.inverter_id == inverter_id and row.event == event), None)


def latest_record(records: Sequence[ExportRecord], inverter_id: str) -> ExportRecord | None:
    return next((row for row in reversed(records) if row.inverter_id == inverter_id), None)


def latest_latch(latches: Sequence[StopLatch], inverter_id: str) -> StopLatch | None:
    return next((row for row in reversed(latches) if row.inverter_id == inverter_id), None)


def snapshot_event(event: EventIdentity) -> dict[str, Any]:
    return {"direction": event.direction,
            "start": event.start.astimezone(UTC).isoformat(),
            "end": event.end.astimezone(UTC).isoformat()}


def clock_window_open(moment: time, start: time, end: time) -> bool:
    """True when clock time ``moment`` is inside ``[start, end)``, wrapping midnight."""
    if start < end:
        return start <= moment < end
    if start > end:
        return moment >= start or moment < end
    return False


def next_wall_clock_occurrence(wall: datetime, now: datetime) -> datetime:
    """First instant at or after ``now`` showing ``wall``'s clock face.

    Recombines a date with the time of day so a DST boundary moves the offset
    but not the face the Slot 1 registers hold. The first fold is chosen
    deliberately: the register fires on the first occurrence.
    """
    face = wall.time().replace(fold=0)
    today = datetime.combine(now.date(), face, tzinfo=now.tzinfo)
    if today.astimezone(UTC) >= now.astimezone(UTC):
        return today
    return datetime.combine(now.date() + timedelta(days=1), face, tzinfo=now.tzinfo)


def export_not_yet_armed(start: datetime, end: datetime, now: datetime) -> bool:
    """A window whose clock face would come round before its own event is deferred.

    Slot 1 carries a time of day and no date, so programming tomorrow's window
    today would discharge at the same clock time today. An earlier day's still
    open window is deferred too, and an event inside a repeated fall-back hour
    is refused outright (its face comes round twice).
    """
    if now.astimezone(UTC) >= start.astimezone(UTC):
        return False
    if clock_window_open(now.time(), start.time(), end.time()):
        return True
    occurrence = next_wall_clock_occurrence(start, now)
    return occurrence.astimezone(UTC) != start.astimezone(UTC)


def clock_face(window_start: datetime, window_end: datetime, zone: tzinfo) -> list[int]:
    """Slot 1's discharge half: the selected window on the household clock."""
    start = window_start.astimezone(zone)
    end = window_end.astimezone(zone)
    return [start.hour, start.minute, end.hour, end.minute]
