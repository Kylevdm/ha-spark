"""SQLite store for forecast accuracy and household signals (aiosqlite).

``forecasts`` records one row per (target_date, model) every time the daemon
computes tonight's plan, so ``forecast-eval`` can later join against actual
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

from ha_spark.energy.models import ForecastRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    made_at     TEXT NOT NULL,
    target_date TEXT NOT NULL,
    model       TEXT NOT NULL,
    total_kwh   REAL NOT NULL,
    slots_json  TEXT,
    source      TEXT NOT NULL,
    PRIMARY KEY (target_date, model)
);
CREATE TABLE IF NOT EXISTS signals (
    ts    TEXT NOT NULL,
    name  TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (ts, name)
);
"""

_UPSERT_FORECAST = """
INSERT INTO forecasts (made_at, target_date, model, total_kwh, slots_json, source)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(target_date, model) DO UPDATE SET
    made_at = excluded.made_at,
    total_kwh = excluded.total_kwh,
    slots_json = excluded.slots_json,
    source = excluded.source
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
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
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
    ) -> None:
        """Upsert the forecast for ``(target_date, model)``."""
        slots_json = json.dumps(list(slots)) if slots is not None else None
        await self._conn.execute(
            _UPSERT_FORECAST,
            (_iso(made_at), target_date.isoformat(), model, total_kwh, slots_json, source),
        )
        await self._conn.commit()

    async def record_signal(self, ts: datetime, name: str, value: float) -> None:
        """Upsert one ``(ts, name)`` signal sample."""
        await self._conn.execute(_UPSERT_SIGNAL, (_iso(ts), name, value))
        await self._conn.commit()

    async def forecasts_since(self, since: date) -> list[ForecastRecord]:
        """Recorded forecasts with ``target_date >= since``, oldest first."""
        cursor = await self._conn.execute(
            "SELECT made_at, target_date, model, total_kwh, slots_json, source "
            "FROM forecasts WHERE target_date >= ? ORDER BY target_date, model",
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

    async def signal_history(self, name: str, since: datetime) -> list[tuple[datetime, float]]:
        """Recorded ``(ts, value)`` samples for ``name`` since ``since``, oldest first."""
        cursor = await self._conn.execute(
            "SELECT ts, value FROM signals WHERE name = ? AND ts >= ? ORDER BY ts",
            (name, _iso(since)),
        )
        rows = await cursor.fetchall()
        return [(datetime.fromisoformat(ts), float(value)) for ts, value in rows]
