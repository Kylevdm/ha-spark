"""SQLite store for forecast accuracy and household signals (aiosqlite).

``forecasts`` appends every forecast revision, including paired baselines.
The compatibility reader selects the latest issue per (target_date, model),
so ``forecast-eval`` can later join against actual
consumption and report MAE/MAPE per model — the referee later ML phases
(6B+) must beat before they drive plans.

``signals`` is a generic half-hourly/snapshot log of household signals
(occupancy, heat-pump energy, outdoor temperature) recorded by the daemon's
sampler so training data accumulates ahead of the models that will consume it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType

import aiosqlite

from ha_spark.energy.models import ForecastHistoryRecord, ForecastRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id          INTEGER PRIMARY KEY,
    issued_at   TEXT NOT NULL,
    target_date TEXT NOT NULL,
    model       TEXT NOT NULL,
    total_kwh   REAL NOT NULL,
    slots_json  TEXT,
    source      TEXT NOT NULL,
    horizon_start TEXT,
    horizon_end TEXT,
    drove_plan INTEGER NOT NULL DEFAULT 0,
    median_total_kwh REAL,
    median_slots_json TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    ts    TEXT NOT NULL,
    name  TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (ts, name)
);
"""

_INSERT_FORECAST = """
INSERT INTO forecasts (issued_at, target_date, model, total_kwh, slots_json, source,
                      horizon_start, horizon_end, drove_plan, median_total_kwh, median_slots_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_MIGRATE_FORECASTS = """
BEGIN IMMEDIATE;
CREATE TABLE forecasts_history (
    id INTEGER PRIMARY KEY,
    issued_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    model TEXT NOT NULL,
    total_kwh REAL NOT NULL,
    slots_json TEXT,
    source TEXT NOT NULL,
    horizon_start TEXT,
    horizon_end TEXT,
    drove_plan INTEGER NOT NULL DEFAULT 0,
    median_total_kwh REAL,
    median_slots_json TEXT
);
INSERT INTO forecasts_history (issued_at, target_date, model, total_kwh, slots_json, source)
SELECT made_at, target_date, model, total_kwh, slots_json, source FROM forecasts;
DROP TABLE forecasts;
ALTER TABLE forecasts_history RENAME TO forecasts;
COMMIT;
"""

_UPSERT_SIGNAL = """
INSERT INTO signals (ts, name, value)
VALUES (?, ?, ?)
ON CONFLICT(ts, name) DO UPDATE SET value = excluded.value
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class ForecastLedger:
    """Async context manager over the forecasts/signals tables."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> ForecastLedger:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        try:
            await self._db.executescript(_SCHEMA)
            cursor = await self._db.execute("PRAGMA table_info(forecasts)")
            if any(row[1] == "made_at" for row in await cursor.fetchall()):
                await self._db.executescript(_MIGRATE_FORECASTS)
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

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ForecastLedger used outside 'async with'")
        return self._db

    async def record_forecast(
        self,
        made_at: datetime,
        target_date: date,
        model: str,
        total_kwh: float,
        slots: Iterable[float] | None,
        source: str,
        *,
        horizon_start: datetime | None = None,
        horizon_end: datetime | None = None,
        drove_plan: bool = False,
        median_total_kwh: float | None = None,
        median_slots: Iterable[float] | None = None,
    ) -> None:
        """Append a forecast revision; absent evidence remains NULL."""
        slots_json = json.dumps(list(slots)) if slots is not None else None
        await self._conn.execute(
            _INSERT_FORECAST,
            (_iso(made_at), target_date.isoformat(), model, total_kwh, slots_json, source,
             _iso(horizon_start) if horizon_start is not None else None,
             _iso(horizon_end) if horizon_end is not None else None,
             drove_plan, median_total_kwh,
             json.dumps(list(median_slots)) if median_slots is not None else None),
        )
        await self._conn.commit()

    async def record_signal(self, ts: datetime, name: str, value: float) -> None:
        """Upsert one ``(ts, name)`` signal sample."""
        await self._conn.execute(_UPSERT_SIGNAL, (_iso(ts), name, value))
        await self._conn.commit()

    async def record_signals(self, samples: Iterable[tuple[datetime, str, float]]) -> None:
        """Bulk-upsert ``(ts, name, value)`` samples in one transaction."""
        await self._conn.executemany(
            _UPSERT_SIGNAL, [(_iso(ts), name, value) for ts, name, value in samples]
        )
        await self._conn.commit()

    async def forecasts_since(self, since: date) -> list[ForecastRecord]:
        """Latest issued forecast per day/model, with insertion order breaking ties."""
        cursor = await self._conn.execute(
            "SELECT issued_at, target_date, model, total_kwh, slots_json, source FROM ("
            "SELECT *, ROW_NUMBER() OVER (PARTITION BY target_date, model "
            "ORDER BY julianday(issued_at) DESC, id DESC) AS revision "
            "FROM forecasts WHERE target_date >= ?) WHERE revision = 1 "
            "ORDER BY target_date, model",
            (since.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [
            ForecastRecord(
                made_at=datetime.fromisoformat(made_at),
                target_date=date.fromisoformat(target_date),
                model=model,
                total_kwh=total_kwh,
                slots=tuple(json.loads(slots_json)) if slots_json else None,
                source=source,
            )
            for made_at, target_date, model, total_kwh, slots_json, source in rows
        ]

    async def forecast_history(self, since: date) -> list[ForecastHistoryRecord]:
        """Every revision for target days since ``since``, in issue order."""
        cursor = await self._conn.execute(
            "SELECT issued_at, target_date, model, total_kwh, slots_json, source, "
            "horizon_start, horizon_end, drove_plan, median_total_kwh, median_slots_json "
            "FROM forecasts WHERE target_date >= ? ORDER BY julianday(issued_at), id",
            (since.isoformat(),),
        )
        return [
            ForecastHistoryRecord(
                made_at=datetime.fromisoformat(issued), target_date=date.fromisoformat(target),
                model=model, total_kwh=total, slots=tuple(json.loads(slots)) if slots else None,
                source=source,
                horizon_start=datetime.fromisoformat(start) if start else None,
                horizon_end=datetime.fromisoformat(end) if end else None,
                drove_plan=bool(drove), median_total_kwh=median_total,
                median_slots=tuple(json.loads(median_slots)) if median_slots else None,
            )
            for issued, target, model, total, slots, source, start, end, drove,
            median_total, median_slots in await cursor.fetchall()
        ]

    async def signal_history(self, name: str, since: datetime) -> list[tuple[datetime, float]]:
        """Recorded ``(ts, value)`` samples for ``name`` since ``since``, oldest first."""
        cursor = await self._conn.execute(
            "SELECT ts, value FROM signals WHERE name = ? AND ts >= ? ORDER BY ts",
            (name, _iso(since)),
        )
        rows = await cursor.fetchall()
        return [(datetime.fromisoformat(ts), float(value)) for ts, value in rows]
