"""Owner-run repair of stored ledger history: re-check, clear gaps, re-fill.

The repair re-checks every stored slot against the plausibility rule, replaces
a value that fails it with a ``rejected`` gap, and re-fills gaps (``rejected``
included) from Home Assistant's recorder statistics: five-minute rows within
retention, hourly rows beyond it. A value that passes the rule is never
touched. The job writes only the energy ledger and its own run record; it
never reaches control, planning or hardware, and one run at a time is claimed
across the daemon and the CLI through the shared database file.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict

from ha_spark.config import Settings
from ha_spark.energy.derived_base_load import ComponentSpec
from ha_spark.energy.energy_ledger import (
    ComponentFlow,
    EnergyLedger,
    LedgerSlot,
    _configured_components,
    _derive_loads,
)
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.ledger_backfill import _hourly_series
from ha_spark.energy.onboarding import SUPPORTED_UNITS, statistic_unit
from ha_spark.energy.plausibility import plausible, plausible_kw
from ha_spark.energy.readiness_ledger import slot_start
from ha_spark.energy.readiness_measurements import HALF_HOUR, STATS_RETENTION, day_bounds
from ha_spark.ha.statistics import list_statistic_ids, statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)
# A running row older than this counts as failed: its process died mid-run.
REPAIR_STALE_AFTER = timedelta(hours=6)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS energy_ledger_repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    checked_days INTEGER,
    fixed_slots INTEGER,
    still_gaps INTEGER
);
"""


class RepairRun(BaseModel):
    """One stored repair run, as the Settings section and the CLI report it."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    status: Literal["running", "done", "failed"]
    started_at: datetime
    finished_at: datetime | None = None
    checked_days: int | None = None
    fixed_slots: int | None = None
    still_gaps: int | None = None


async def claim_repair(db_path: str, now: datetime) -> int | None:
    """Claim the single repair slot; None while a fresh run holds it.

    The claim is one immediate transaction on the shared database file, so the
    daemon and a CLI process cannot both start a run. A running row older than
    six hours is marked failed first: its owner died without finishing.
    """
    await asyncio.to_thread(Path(db_path).parent.mkdir, parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE energy_ledger_repairs SET status = 'failed', finished_at = ? "
            "WHERE status = 'running' AND started_at <= ?",
            (
                now.astimezone(UTC).isoformat(),
                (now - REPAIR_STALE_AFTER).astimezone(UTC).isoformat(),
            ),
        )
        async with db.execute(
            "SELECT 1 FROM energy_ledger_repairs WHERE status = 'running'",
        ) as cursor:
            if await cursor.fetchone() is not None:
                await db.commit()
                return None
        await db.execute(
            "INSERT INTO energy_ledger_repairs (started_at, status) VALUES (?, 'running')",
            (now.astimezone(UTC).isoformat(),),
        )
        async with db.execute("SELECT MAX(id) FROM energy_ledger_repairs") as cursor:
            row = await cursor.fetchone()
        await db.commit()
        return int(row[0]) if row is not None and row[0] is not None else None


async def last_repair(db_path: str, *, now: datetime | None = None) -> RepairRun | None:
    """The newest run record, read-only; None with no database or table.

    A claim still marked running past the stale window is presented as
    failed: its process died mid-run, and the owner may retry immediately.
    The row itself is left untouched for the next claim to reclaim.
    """
    if not await asyncio.to_thread(Path(db_path).exists):
        return None
    path = await asyncio.to_thread(Path(db_path).resolve)
    async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'energy_ledger_repairs'",
        ) as cursor:
            if await cursor.fetchone() is None:
                return None
        async with db.execute(
            "SELECT started_at, finished_at, status, checked_days, fixed_slots, still_gaps "
            "FROM energy_ledger_repairs ORDER BY id DESC LIMIT 1",
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    run = RepairRun(
        started_at=datetime.fromisoformat(str(row[0])),
        finished_at=datetime.fromisoformat(str(row[1])) if row[1] is not None else None,
        status=str(row[2]),
        checked_days=None if row[3] is None else int(row[3]),
        fixed_slots=None if row[4] is None else int(row[4]),
        still_gaps=None if row[5] is None else int(row[5]),
    )
    at = datetime.now(UTC) if now is None else now
    if run.status == "running" and run.started_at <= at - REPAIR_STALE_AFTER:
        return run.model_copy(update={"status": "failed"})
    return run


async def run_ledger_repair(
    settings: Settings, repair_id: int, now: datetime, *, finished_at: datetime | None = None,
) -> RepairRun:
    """Run one claimed repair to completion; never raises into a caller.

    A failed HA read leaves the gaps it could not fill and the run ends
    ``done`` with them counted. A storage failure or a finalization that did
    not persist ends the run ``failed``: an outcome is never reported as
    done unless the database holds it.
    """
    status: Literal["done", "failed"] = "done"
    checked_days = fixed_slots = still_gaps = 0
    try:
        checked_days, fixed_slots, still_gaps = await _repair_history(settings, now, repair_id)
    except Exception:  # noqa: BLE001 - recorded as a failed run, never surfaced raw
        log.warning("Statistics repair failed")
        status = "failed"
    # Captured after the history pass, not at the claim: the run's own duration.
    finished = finished_at if finished_at is not None else datetime.now(UTC)
    applied = False
    try:
        applied = await _finish_repair(settings.db_path, repair_id, status, finished,
                                       checked_days, fixed_slots, still_gaps)
    except Exception:  # noqa: BLE001 - the run record degrades, never the caller
        log.warning("Statistics repair failed")
    if not applied:
        # Another process reclaimed the claim, or the record never persisted.
        log.warning("Statistics repair was superseded")
        status = "failed"
    return RepairRun(
        status=status, started_at=now, finished_at=finished,
        checked_days=checked_days, fixed_slots=fixed_slots, still_gaps=still_gaps,
    )


def _gap_names(row: LedgerSlot, configured: dict[str, ComponentSpec]) -> set[str]:
    return {
        name for name in configured
        if (flow := row.components.get(name)) is not None and flow.provenance == "absent"
    }


def _implausible(rows: list[LedgerSlot], bound_kw: float) -> dict[datetime, set[str]]:
    """(slot, names) of stored numeric flows failing the rule, configured or not.

    A sensor removed after a spike was recorded is still checked: the stored
    history is validated in full, and current configuration only decides what
    can be re-filled and what counts toward the remaining gaps.
    """
    seconds = HALF_HOUR.total_seconds()
    candidates: dict[datetime, set[str]] = {}
    for row in rows:
        for name, flow in row.components.items():
            if flow.kwh is not None and not plausible(flow.kwh, seconds, bound_kw):
                candidates.setdefault(row.slot_start, set()).add(name)
    return candidates


def _day_ranges(
    days: set[date], cutoff: datetime, timezone: str,
) -> list[tuple[datetime, datetime]]:
    """Contiguous local-day [start, end) ranges, each capped at the retention edge."""
    ranges: list[tuple[datetime, datetime]] = []
    for day in sorted(days):
        start = day_bounds(day, timezone)[0]
        end = min(day_bounds(day, timezone)[1], cutoff)
        if ranges and start == ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges


async def _repair_history(
    settings: Settings, now: datetime, repair_id: int,
) -> tuple[int, int, int]:
    """(checked_days, fixed_slots, still_gaps) over every stored closed day."""
    ledger = EnergyLedger(settings.db_path)
    configured = _configured_components(settings)
    first = await ledger.first_slot()
    if first is None:
        return (0, 0, 0)
    tz = load_timezone(settings.timezone)
    now = now.astimezone(UTC)
    yesterday = now.astimezone(tz).date() - timedelta(days=1)
    first_day = first.astimezone(tz).date()
    if first_day > yesterday:
        return (0, 0, 0)
    start = day_bounds(first_day, settings.timezone)[0]
    end = day_bounds(yesterday, settings.timezone)[1]
    bound_kw = plausible_kw(settings)
    rows = await ledger.slots(start, end)
    if not rows:
        return (0, 0, 0)
    await _reject_implausible(
        settings.db_path, _implausible(rows, bound_kw), bound_kw, repair_id,
    )
    rows = await ledger.slots(start, end)
    days: dict[date, list[LedgerSlot]] = {}
    for row in rows:
        days.setdefault(row.slot_start.astimezone(tz).date(), []).append(row)
    checked = len(days)
    if not configured:
        # Without a configured source nothing can be re-filled; the re-check
        # above still covered every stored flow, spikes of removed sensors too.
        return (checked, 0, 0)
    gaps = {row.slot_start: names for row in rows if (names := _gap_names(row, configured))}
    if not gaps:
        return (checked, 0, 0)
    # Five-minute evidence exists for slots starting at or after the retention
    # edge, so the day the edge falls inside is served per slot, not discarded.
    edge = now - STATS_RETENTION
    cutoff = slot_start(edge)
    if cutoff < edge:
        cutoff += HALF_HOUR
    hourly_days: set[date] = set()
    for day in sorted(days):
        day_gaps = [row for row in days[day] if row.slot_start in gaps]
        if not day_gaps:
            continue
        day_start, day_end = day_bounds(day, settings.timezone)
        if any(row.slot_start >= cutoff for row in day_gaps):
            try:
                # The existing day-close fill: 5-minute rows, gaps only.
                await ledger._fill_day(
                    settings, max(day_start, cutoff), day_end, configured, claim=repair_id,
                )
            except sqlite3.Error:
                raise  # Storage, not a missing source: never report such a run as done.
            except Exception:  # noqa: BLE001 - the day keeps its gaps, the run goes on
                log.warning("Statistics repair fill failed")
        if any(row.slot_start < cutoff for row in day_gaps):
            hourly_days.add(day)
    # Only days with gaps are read, in bounded contiguous ranges: an old ledger
    # never holds one write transaction over its whole history.
    ranges = _day_ranges(hourly_days, cutoff, settings.timezone)
    if ranges:
        try:
            await _refill_hourly(settings, ranges, configured, bound_kw, repair_id)
        except sqlite3.Error:
            raise  # Storage, not a missing source: never report such a run as done.
        except Exception:  # noqa: BLE001 - those days keep their gaps, the run goes on
            log.warning("Statistics repair fill failed")
    final = await ledger.slots(start, end)
    fixed = still = 0
    for row in final:
        current = _gap_names(row, configured)
        if current:
            still += 1
        if gaps.get(row.slot_start) and any(name not in current for name in gaps[row.slot_start]):
            fixed += 1
    return (checked, fixed, still)


async def _claim_running(db: aiosqlite.Connection, repair_id: int) -> bool:
    """True while the given claim is the running one; a reclaimed run writes nothing."""
    async with db.execute(
        "SELECT 1 FROM energy_ledger_repairs WHERE id = ? AND status = 'running'",
        (repair_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _reject_implausible(
    db_path: str, candidates: dict[datetime, set[str]], bound_kw: float, repair_id: int,
) -> None:
    # The one rule exception: a stored value that fails the plausibility rule
    # becomes a rejected gap, whatever its provenance, so the re-fill can retry.
    if not candidates:
        return
    seconds = HALF_HOUR.total_seconds()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if not await _claim_running(db, repair_id):
            await db.rollback()
            return
        rejected = False
        for instant, names in candidates.items():
            row = await _stored_slot(db, instant)
            if row is None:
                continue
            # Re-check the current value: a boundary capture that replaced the
            # spike after the scan wins; a value that passes is never touched.
            updates = {
                name: ComponentFlow(reason="rejected") for name in names
                if (flow := row.components.get(name)) is not None
                and flow.kwh is not None and not plausible(flow.kwh, seconds, bound_kw)
            }
            if updates:
                rejected = True
                await _store_updates(db, instant, row, updates)
        if rejected:
            log.warning("Statistics repair rejected an implausible interval")
        await db.commit()


async def _refill_hourly(
    settings: Settings, ranges: list[tuple[datetime, datetime]],
    configured: dict[str, ComponentSpec], bound_kw: float, repair_id: int,
) -> None:
    """Fill gaps from hourly long-term statistics; passing values are never replaced."""
    url, token, timeout = settings.ha_websocket_url, settings.auth_token, settings.ha_timeout
    metas = await list_statistic_ids(url, token, timeout=timeout)
    units = {
        meta["statistic_id"]: statistic_unit(meta)
        for meta in metas
        if isinstance(meta, dict) and isinstance(meta.get("statistic_id"), str)
    }
    for start, end in ranges:
        # One bounded range at a time: neither the read nor the write holds a
        # transaction over the whole stored history.
        fills: dict[datetime, dict[str, ComponentFlow]] = {}
        rejected = False
        for name, spec in configured.items():
            entity = spec.entity_id.strip()
            unit = units.get(entity)
            if unit is None or unit not in SUPPORTED_UNITS:
                continue
            try:
                rows = await statistics_during_period(
                    url, token, entity, start, period="hour", timeout=timeout, end_time=end,
                )
                series = await asyncio.to_thread(
                    _hourly_series, rows, unit, spec.invert, start, end,
                    settings.timezone, bound_kw,
                )
            except Exception:  # noqa: BLE001 - one component's read never fails the rest
                log.warning("Statistics repair fill failed")
                continue
            for instant, flow in series.items():
                fills.setdefault(instant, {})[name] = flow
                rejected |= flow.reason == "rejected"
        if rejected:
            log.warning("Energy ledger rejected an implausible interval")
        if not fills:
            continue
        async with aiosqlite.connect(settings.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if await _claim_running(db, repair_id):
                for instant, changes in fills.items():
                    await _merge_gaps(db, instant, changes)
                await db.commit()
            else:
                await db.rollback()


async def _merge_gaps(
    db: aiosqlite.Connection, instant: datetime, changes: dict[str, ComponentFlow],
) -> None:
    """Apply fills to a stored row's gap components only; no row is created."""
    row = await _stored_slot(db, instant)
    if row is None:
        return
    updates = {
        name: flow for name, flow in changes.items()
        if (current := row.components.get(name)) is not None and current.provenance == "absent"
    }
    await _store_updates(db, instant, row, updates)


async def _stored_slot(db: aiosqlite.Connection, instant: datetime) -> LedgerSlot | None:
    async with db.execute(
        "SELECT payload FROM energy_ledger_slots WHERE slot_start = ?",
        (instant.astimezone(UTC).isoformat(),),
    ) as cursor:
        existing = await cursor.fetchone()
    return LedgerSlot.model_validate_json(existing[0]) if existing is not None else None


async def _store_updates(
    db: aiosqlite.Connection, instant: datetime, row: LedgerSlot,
    updates: dict[str, ComponentFlow],
) -> None:
    if not updates:
        return
    components = row.components | updates
    house, base, clamped = _derive_loads(components)
    updated = row.model_copy(update={
        "components": components, "house_load_kwh": house, "base_load_kwh": base,
        "negative_clamped": clamped,
    })
    await db.execute(
        "INSERT INTO energy_ledger_slots VALUES (?, ?) "
        "ON CONFLICT(slot_start) DO UPDATE SET payload = excluded.payload",
        (instant.astimezone(UTC).isoformat(), updated.model_dump_json()),
    )


async def _finish_repair(
    db_path: str, repair_id: int, status: str, finished_at: datetime,
    checked_days: int, fixed_slots: int, still_gaps: int,
) -> bool:
    """Persist one run's outcome; False when its claim is no longer the running one."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "UPDATE energy_ledger_repairs SET finished_at = ?, status = ?, checked_days = ?, "
            "fixed_slots = ?, still_gaps = ? WHERE id = ? AND status = 'running'",
            (finished_at.astimezone(UTC).isoformat(), status, checked_days, fixed_slots,
             still_gaps, repair_id),
        )
        await db.commit()
        return cursor.rowcount == 1
