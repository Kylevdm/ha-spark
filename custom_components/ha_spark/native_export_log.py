"""Pure append-once, acknowledgement and trim rules for export transitions.

The log is durable evidence of verified export lifecycle steps, never a write
permission. Trimming runs before every append so a save can never fail on the
store's caps; an append failure can never block a write.
"""

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from .authority_store import AuthorityState, EventIdentity, ExportTransition

log = logging.getLogger(__name__)

MAX_TOTAL = 1024
MAX_PER_INVERTER = 64
# Rows offered per inverter per snapshot: 8 inverters x 8 rows keeps the worst-case
# heartbeat inside its 64 KiB cap, and oldest-first means acks always make progress.
SNAPSHOT_PER_INVERTER = 8
TransitionKind = Literal[
    "accepted", "armed", "started", "cleanup_verified", "aborted", "clock_hold", "tripped",
    "unverified",
]


def same_event(first: EventIdentity, second: EventIdentity) -> bool:
    return (first.direction == second.direction and first.start == second.start
            and first.end == second.end)


def held_at_start(
    rows: Sequence[ExportTransition], now: datetime,
) -> tuple[tuple[str, EventIdentity], ...]:
    """Each (inverter, event) held as ``unverified`` whose event has started unsettled.

    A hold is settled by a terminal row (``aborted``/``tripped``), a verified
    ``started``, or an ``armed`` logged after it (#406). The caller also treats a
    live record for the event as settled, since ``armed`` is logged once per event.
    """
    held: list[tuple[str, EventIdentity]] = []
    for index, row in enumerate(rows):
        if (row.kind != "unverified" or row.event.start > now
                or any(inverter_id == row.inverter_id and same_event(event, row.event)
                       for inverter_id, event in held)):
            continue
        if not any(other.inverter_id == row.inverter_id
                   and same_event(other.event, row.event)
                   and (other.kind in ("aborted", "tripped", "started")
                        or (other.kind == "armed" and position > index))
                   for position, other in enumerate(rows)):
            held.append((row.inverter_id, row.event))
    return tuple(held)


def held_aborted(
    rows: Sequence[ExportTransition], inverter_id: str, event: EventIdentity,
) -> bool:
    """A held (``unverified``) event that was then logged ``aborted`` (#406, review M2).

    The held notice promised an abort if the arm did not verify; once that abort
    is logged the event never arms again, under any supervision and across
    restarts (the log is durable; acknowledged rows outlive the event's start).
    """
    held = False
    for row in rows:
        if row.inverter_id != inverter_id or not same_event(row.event, event):
            continue
        if row.kind == "unverified":
            held = True
        elif row.kind == "aborted" and held:
            # Only an abort after the hold latches: an earlier, unheld abort
            # never gains durable blocking semantics retroactively.
            return True
    return False


def _latch_evidence(rows: Sequence[ExportTransition], now: datetime) -> set[UUID]:
    """Row ids of held-abort latches whose event has not ended; trim keeps them."""
    held: dict[tuple[str, datetime, datetime, str], UUID] = {}
    pinned: set[UUID] = set()
    for row in rows:
        if row.event.end <= now:
            continue
        key = (row.inverter_id, row.event.start, row.event.end, row.event.direction)
        if row.kind == "unverified":
            held.setdefault(key, row.id)
        elif row.kind == "aborted" and key in held:
            pinned.update((held[key], row.id))
    return pinned


def visible(rows: Sequence[ExportTransition], inverter_id: str) -> tuple[ExportTransition, ...]:
    """The snapshot's oldest unacknowledged rows, in order, bounded per snapshot."""
    return tuple(row for row in rows if row.inverter_id == inverter_id
                 and not row.acked)[:SNAPSHOT_PER_INVERTER]


def _within(
    rows: Sequence[ExportTransition], *, reserve: int, reserve_inverter: str | None,
) -> tuple[bool, str | None]:
    if len(rows) + reserve > MAX_TOTAL:
        return False, None
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.inverter_id] = counts.get(row.inverter_id, 0) + 1
    if reserve_inverter is not None:
        counts[reserve_inverter] = counts.get(reserve_inverter, 0) + reserve
    for inverter_id, count in counts.items():
        if count > MAX_PER_INVERTER:
            return False, inverter_id
    return True, None


def trim(
    rows: Sequence[ExportTransition], now: datetime, *,
    reserve: int = 0, reserve_inverter: str | None = None,
) -> tuple[tuple[ExportTransition, ...], int]:
    """Drop acked rows past their event, then oldest acked, then oldest unacked.

    Returns the kept rows and how many unacknowledged rows were dropped.
    """
    kept = [row for row in rows if not (row.acked and row.event.end <= now)]
    # A held-abort latch (review M2) is safety evidence until its event ends:
    # evict it only when nothing else is left to drop (two rows per live held
    # event; the per-inverter cap would need 32 such events at once).
    pinned = _latch_evidence(kept, now)
    dropped_unacked = 0
    while True:
        within, over = _within(kept, reserve=reserve, reserve_inverter=reserve_inverter)
        if within:
            break
        index = next((i for i, row in enumerate(kept)
                      if row.acked and row.id not in pinned), None)
        if index is None:
            index = next((i for i, row in enumerate(kept)
                          if row.id not in pinned
                          and (over is None or row.inverter_id == over)), None)
            if index is None:
                index = (next((i for i, row in enumerate(kept)
                               if row.inverter_id == over), 0) if over is not None else 0)
            if not kept[index].acked:
                dropped_unacked += 1
        del kept[index]
    return tuple(kept), dropped_unacked


def with_append(
    state: AuthorityState, *, inverter_id: str, event: EventIdentity,
    kind: TransitionKind, at: datetime, reason: str | None = None,
    cleared: bool | None = None, transition_id: UUID | None = None,
) -> AuthorityState:
    """Append one kind at most once per inverter and event, trimming first."""
    if any(row.inverter_id == inverter_id and row.kind == kind
           and same_event(row.event, event) for row in state.export_transitions):
        return state
    rows, dropped = trim(state.export_transitions, at, reserve=1,
                         reserve_inverter=inverter_id)
    if dropped:
        log.warning("Dropped unacknowledged export transitions at the log cap")
    row = ExportTransition(
        inverter_id=inverter_id, id=transition_id or uuid4(), event=event, at=at,
        kind=kind, reason=reason, cleared=cleared,
    )
    return state.model_copy(update={
        "export_transitions": rows + (row,), "last_changed_at": at,
    })


def with_acks(
    state: AuthorityState, ids: Iterable[str], now: datetime,
) -> AuthorityState:
    """Mark only matching rows acked; delete acked rows whose event has ended."""
    known = set(ids)
    changed = False
    kept: list[ExportTransition] = []
    for row in state.export_transitions:
        if str(row.id) in known and not row.acked:
            row = row.model_copy(update={"acked": True})
            changed = True
        if row.acked and row.event.end <= now:
            changed = True
            continue
        kept.append(row)
    if not changed:
        return state
    return state.model_copy(update={
        "export_transitions": tuple(kept), "last_changed_at": now,
    })


def with_pruned(state: AuthorityState, now: datetime) -> AuthorityState:
    """Delete acknowledged rows whose event has ended."""
    kept = tuple(row for row in state.export_transitions
                 if not (row.acked and row.event.end <= now))
    if len(kept) == len(state.export_transitions):
        return state
    return state.model_copy(update={
        "export_transitions": kept, "last_changed_at": now,
    })
