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


async def last_repair(db_path: str) -> RepairRun | None:
    """The newest run record, read-only; None with no database or table."""
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
    return RepairRun(
        started_at=datetime.fromisoformat(str(row[0])),
        finished_at=datetime.fromisoformat(str(row[1])) if row[1] is not None else None,
        status=str(row[2]),
        checked_days=None if row[3] is None else int(row[3]),
        fixed_slots=None if row[4] is None else int(row[4]),
        still_gaps=None if row[5] is None else int(row[5]),
    )


async def run_ledger_repair(settings: Settings, repair_id: int, now: datetime) -> RepairRun:
    """Run one claimed repair to completion; never raises into a caller.

    A failed HA read leaves the gaps it could not fill and the run ends
    ``done`` with them counted; any unexpected error ends the run ``failed``.
    """
    status: Literal["done", "failed"] = "done"
    checked_days = fixed_slots = still_gaps = 0
    try:
        checked_days, fixed_slots, still_gaps = await _repair_history(settings, now)
    except Exception:  # noqa: BLE001 - recorded as a failed run, never surfaced raw
        log.warning("Statistics repair failed")
        status = "failed"
    try:
        await _finish_repair(settings.db_path, repair_id, status, now,
                            checked_days, fixed_slots, still_gaps)
    except Exception:  # noqa: BLE001 - the run record degrades, never the caller
        log.warning("Statistics repair failed")
    return RepairRun(
        status=status, started_at=now, finished_at=now,
        checked_days=checked_days, fixed_slots=fixed_slots, still_gaps=still_gaps,
    )


def _gap_names(row: LedgerSlot, configured: dict[str, ComponentSpec]) -> set[str]:
    return {
        name for name in configured
        if (flow := row.components.get(name)) is not None and flow.provenance == "absent"
    }


def _retention_floor(now: datetime, timezone: str) -> datetime:
    """The start of the oldest local day whose five-minute statistics may exist."""
    day = (now - STATS_RETENTION).astimezone(load_timezone(timezone)).date()
    while day_bounds(day, timezone)[0] < now - STATS_RETENTION:
        day += timedelta(days=1)
    return day_bounds(day, timezone)[0]


async def _repair_history(settings: Settings, now: datetime) -> tuple[int, int, int]:
    """(checked_days, fixed_slots, still_gaps) over every stored closed day."""
    ledger = EnergyLedger(settings.db_path)
    configured = _configured_components(settings)
    first = await ledger.first_slot()
    if first is None or not configured:
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
    await _reject_implausible(settings.db_path, rows, configured, bound_kw)
    rows = await ledger.slots(start, end)
    days: dict[date, list[LedgerSlot]] = {}
    for row in rows:
        days.setdefault(row.slot_start.astimezone(tz).date(), []).append(row)
    gaps = {row.slot_start: names for row in rows if (names := _gap_names(row, configured))}
    floor = _retention_floor(now, settings.timezone)
    for day in sorted(days):
        if not any(gaps.get(row.slot_start) for row in days[day]):
            continue
        day_start, day_end = day_bounds(day, settings.timezone)
        if day_start < floor:
            continue
        try:
            # The existing day-close fill: 5-minute rows, gaps only.
            await ledger._fill_day(settings, day_start, day_end, configured)
        except Exception:  # noqa: BLE001 - the day keeps its gaps, the run goes on
            log.warning("Statistics repair fill failed")
    if any(day_bounds(day, settings.timezone)[0] < floor for day in days):
        try:
            await _refill_hourly(settings, start, min(floor, end), configured, bound_kw)
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
    return (len(days), fixed, still)


async def _reject_implausible(
    db_path: str, rows: list[LedgerSlot],
    configured: dict[str, ComponentSpec], bound_kw: float,
) -> None:
    # The one rule exception: a stored value that fails the plausibility rule
    # becomes a rejected gap, whatever its provenance, so the re-fill can retry.
    seconds = HALF_HOUR.total_seconds()
    changes: dict[datetime, dict[str, ComponentFlow]] = {}
    for row in rows:
        for name in configured:
            flow = row.components.get(name)
            if flow is None or flow.kwh is None:
                continue
            if not plausible(flow.kwh, seconds, bound_kw):
                changes.setdefault(row.slot_start, {})[name] = ComponentFlow(reason="rejected")
    if not changes:
        return
    log.warning("Statistics repair rejected an implausible interval")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        for instant, updates in changes.items():
            await _store_flows(db, instant, updates)
        await db.commit()


async def _refill_hourly(
    settings: Settings, start: datetime, cutoff: datetime,
    configured: dict[str, ComponentSpec], bound_kw: float,
) -> None:
    """Fill gaps from hourly long-term statistics; passing values are never replaced."""
    url, token, timeout = settings.ha_websocket_url, settings.auth_token, settings.ha_timeout
    metas = await list_statistic_ids(url, token, timeout=timeout)
    units = {
        meta["statistic_id"]: statistic_unit(meta)
        for meta in metas
        if isinstance(meta, dict) and isinstance(meta.get("statistic_id"), str)
    }
    fills: dict[datetime, dict[str, ComponentFlow]] = {}
    rejected = False
    for name, spec in configured.items():
        entity = spec.entity_id.strip()
        unit = units.get(entity)
        if unit is None or unit not in SUPPORTED_UNITS:
            continue
        try:
            rows = await statistics_during_period(
                url, token, entity, start, period="hour", timeout=timeout, end_time=cutoff,
            )
            series = await asyncio.to_thread(
                _hourly_series, rows, unit, spec.invert, start, cutoff,
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
        return
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        for instant, changes in fills.items():
            await _merge_gaps(db, instant, changes)
        await db.commit()


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


async def _store_flows(
    db: aiosqlite.Connection, instant: datetime, updates: dict[str, ComponentFlow],
) -> None:
    row = await _stored_slot(db, instant)
    if row is not None:
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
    db_path: str, repair_id: int, status: str, now: datetime,
    checked_days: int, fixed_slots: int, still_gaps: int,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE energy_ledger_repairs SET finished_at = ?, status = ?, checked_days = ?, "
            "fixed_slots = ?, still_gaps = ? WHERE id = ?",
            (now.astimezone(UTC).isoformat(), status, checked_days, fixed_slots, still_gaps,
             repair_id),
        )
        await db.commit()
