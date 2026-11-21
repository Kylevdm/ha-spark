"""SQLite store for half-hourly Octopus meter history (aiosqlite).

Octopus consumption is grid *import*, not house load — the battery/solar shape
it, so it must not feed the load model (see forecast.py). It is retained to
reconcile what was bought from the grid against the energy ledger, and for the
legacy cost backtest.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import aiosqlite

from ha_spark.energy.models import ConsumptionInterval

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consumption (
    interval_start TEXT PRIMARY KEY,
    interval_end   TEXT NOT NULL,
    kwh            REAL NOT NULL,
    source         TEXT NOT NULL,
    imported_at    TEXT NOT NULL
)
"""

# Only count a conflicting row as "updated" when the reading actually changed,
# so re-importing the same CSV reports 0 new/updated.
_UPSERT = """
INSERT INTO consumption (interval_start, interval_end, kwh, source, imported_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(interval_start) DO UPDATE SET
    interval_end = excluded.interval_end,
    kwh = excluded.kwh,
    source = excluded.source,
    imported_at = excluded.imported_at
WHERE consumption.kwh != excluded.kwh
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


@asynccontextmanager
async def open_meter_db(db_path: str) -> AsyncIterator[aiosqlite.Connection | None]:
    """Open the household DB without creating or modifying it."""
    path = Path(db_path)
    if not await asyncio.to_thread(path.exists):
        yield None
        return
    resolved = await asyncio.to_thread(path.resolve)
    async with aiosqlite.connect(resolved.as_uri() + "?mode=ro", uri=True) as db:
        yield db


async def has_meter_table(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)) as cursor:
        return await cursor.fetchone() is not None


async def read_consumption(
    db_path: str, start: datetime, end: datetime,
) -> list[ConsumptionInterval]:
    """Read meter intervals starting in [start, end); a missing store is empty."""
    async with open_meter_db(db_path) as db:
        if db is None or not await has_meter_table(db, "consumption"):
            return []
        async with db.execute(
            "SELECT interval_start, interval_end, kwh FROM consumption "
            "WHERE interval_start >= ? AND interval_start < ? ORDER BY interval_start",
            (_iso(start), _iso(end)),
        ) as cursor:
            return [ConsumptionInterval(_parse(s), _parse(e), float(k))
                    for s, e, k in await cursor.fetchall()]


class ConsumptionStore:
    """Async context manager over the consumption table; idempotent upserts."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> ConsumptionStore:
        await asyncio.to_thread(Path(self._db_path).parent.mkdir, parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._db_path)
        try:
            await db.execute(_SCHEMA)
            await db.commit()
        except BaseException:
            await db.close()
            raise
        self._db = db
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

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ConsumptionStore used outside 'async with'")
        return self._db

    async def upsert(
        self, rows: Iterable[ConsumptionInterval], source: str, *, now: datetime | None = None,
    ) -> int:
        """Write intervals; return how many rows were actually new or changed."""
        db = self._conn
        imported_at = _iso(now or datetime.now(UTC))
        before = db.total_changes
        await db.executemany(
            _UPSERT,
            ((_iso(row.start), _iso(row.end), row.kwh, source, imported_at) for row in rows),
        )
        await db.commit()
        return db.total_changes - before

    async def load_since(self, since: datetime) -> list[ConsumptionInterval]:
        db = self._conn
        cursor = await db.execute(
            "SELECT interval_start, interval_end, kwh FROM consumption "
            "WHERE interval_start >= ? ORDER BY interval_start",
            (_iso(since),),
        )
        rows = await cursor.fetchall()
        return [ConsumptionInterval(_parse(s), _parse(e), float(k)) for s, e, k in rows]

    async def latest_interval_start(self) -> datetime | None:
        cursor = await self._conn.execute("SELECT MAX(interval_start) FROM consumption")
        row = await cursor.fetchone()
        return _parse(row[0]) if row and row[0] else None

    async def summary(self) -> tuple[int, datetime | None, datetime | None]:
        """Return (row count, earliest interval start, latest interval start)."""
        cursor = await self._conn.execute(
            "SELECT COUNT(*), MIN(interval_start), MAX(interval_start) FROM consumption"
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return 0, None, None
        return int(row[0]), _parse(row[1]), _parse(row[2])
