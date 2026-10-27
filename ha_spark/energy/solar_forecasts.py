"""SQLite store of the solar forecast each plan was issued with (#496).

``solar_forecasts`` holds one row per half-hour slot a plan was issued with:
the raw, pre-haircut kWh the planner received (what
``PlannerInputs.solar_slots`` holds), stamped with the planning input
snapshot's time. A slot with no forecast is never stored, so it reads as
null, never as zero. Readers pick, per slot, the row issued latest
*strictly before* the slot's start — the forecast the plan was using before
the slot began, not one revised afterwards — with insertion order breaking
ties. Rows for slots more than 7 days old are pruned on write.

A separate table from ``forecasts`` on purpose: readiness reads ``forecasts``
directly and picks any ``drove_plan`` row, and the ledger readers return
every model, so a solar row must never surface as a load-forecast revision.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType

import aiosqlite

from ha_spark.energy.models import utc_iso

# A slot older than this at write time is pruned.
_RETENTION = timedelta(days=7)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS solar_forecasts (
    id         INTEGER PRIMARY KEY,
    issued_at  TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    kwh        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS solar_forecasts_slot ON solar_forecasts (slot_start, issued_at, id);
"""

_INSERT = "INSERT INTO solar_forecasts (issued_at, slot_start, kwh) VALUES (?, ?, ?)"
# The forecast the plan was using before the slot began: the latest issue
# strictly before the slot's start, insertion order breaking ties. Every value
# is written by ``utc_iso`` (UTC isoformat), so text order is instant order.
_SELECT_SLOT = """
SELECT kwh FROM solar_forecasts
WHERE slot_start = ? AND issued_at < ?
ORDER BY issued_at DESC, id DESC
LIMIT 1
"""
_PRUNE = "DELETE FROM solar_forecasts WHERE slot_start < ?"


class SolarForecastStore:
    """Write side of the ``solar_forecasts`` table, as an async context manager."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> SolarForecastStore:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        try:
            await self._db.executescript(_SCHEMA)
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            await self._db.close()
            self._db = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def record(
        self, issued_at: datetime, slots: Iterable[tuple[datetime, float]]
    ) -> None:
        """Append one plan's forecast slots, then prune past retention.

        ``issued_at`` is the planning input snapshot's time (the forecast the
        plan was using), never the post-apply write time.
        """
        if self._db is None:
            raise RuntimeError("SolarForecastStore used outside 'async with'")
        rows = [(utc_iso(issued_at), utc_iso(start), kwh) for start, kwh in slots]
        if rows:
            await self._db.executemany(_INSERT, rows)
        await self._db.execute(_PRUNE, (utc_iso(issued_at - _RETENTION),))
        await self._db.commit()


async def recorded_solar(db_path: str, starts: Sequence[datetime]) -> dict[datetime, float]:
    """The recorded forecast per slot start, read-only, never creating the database.

    A slot with no row issued before it began is absent from the result, so
    the caller reads it as null, never as zero.
    """
    if not await asyncio.to_thread(Path(db_path).exists):
        return {}
    values: dict[datetime, float] = {}
    resolved = await asyncio.to_thread(Path(db_path).resolve)
    async with aiosqlite.connect(f"file:{resolved}?mode=ro", uri=True) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'solar_forecasts'"
        ) as cursor:
            if await cursor.fetchone() is None:
                return values
        for start in starts:
            async with db.execute(_SELECT_SLOT, (utc_iso(start), utc_iso(start))) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                values[start] = row[0]
    return values
