"""Date-ranged household context facts and their deterministic load effect.

A context fact says "between these dates, expect load unlike the learned
profile" — a holiday (``away``), visitors (``guests``), or a free
``high_usage``/``low_usage`` modifier. The planner consumes them as a simple
multiplier on the predicted load; the LLM (Phase 6D) only ever *writes* facts
here, it never touches setpoints. Every active fact is printed in the plan
report so each adjustment is visible and removable (`ha-spark context`).

The store lives in the same SQLite DB as the consumption store and forecast
ledger. Dates are stored as ISO ``YYYY-MM-DD`` strings, so the inclusive
range query is a plain lexicographic comparison.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from ha_spark.config import Settings

# Recognised fact kinds. away/guests take their factor from config (and are
# later learned, Phase 6E); high_usage/low_usage carry an explicit factor.
KINDS = ("away", "guests", "high_usage", "low_usage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    payload_json TEXT,
    note        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class ContextEntry:
    """One stored context fact."""

    id: int
    kind: str
    start_date: date
    end_date: date
    note: str
    source: str
    created_at: datetime
    payload: dict[str, object]

    def factor(self, settings: Settings) -> float:
        """The load multiplier this fact implies for a day it covers."""
        if self.kind == "away":
            return settings.away_load_factor
        if self.kind == "guests":
            return settings.guests_load_factor
        # high_usage / low_usage carry their own factor in the payload.
        raw = self.payload.get("factor")
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1.0


def combined_factor(
    entries: Sequence[ContextEntry], settings: Settings
) -> tuple[float, list[str]]:
    """Multiply the factors of all active facts; return (factor, report lines)."""
    factor = 1.0
    lines: list[str] = []
    for e in entries:
        f = e.factor(settings)
        factor *= f
        span = (
            f"{e.start_date:%Y-%m-%d}"
            if e.start_date == e.end_date
            else f"{e.start_date:%Y-%m-%d}..{e.end_date:%Y-%m-%d}"
        )
        note = f" — {e.note}" if e.note else ""
        lines.append(f"[{e.id}] {e.kind} {span} ×{f:.2f}{note}")
    return factor, lines


class ContextStore:
    """Async context manager over the context table."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> ContextStore:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(_SCHEMA)
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
            raise RuntimeError("ContextStore used outside 'async with'")
        return self._db

    async def add(
        self,
        kind: str,
        start_date: date,
        end_date: date,
        *,
        note: str = "",
        source: str = "cli",
        factor: float | None = None,
    ) -> int:
        """Insert a fact; return its row id. ``factor`` is stored for *_usage kinds."""
        if kind not in KINDS:
            raise ValueError(f"unknown context kind {kind!r}; expected one of {KINDS}")
        if end_date < start_date:
            raise ValueError("end date is before the start date")
        payload = json.dumps({"factor": factor}) if factor is not None else None
        cursor = await self._conn.execute(
            "INSERT INTO context (kind, start_date, end_date, payload_json, note, source, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                start_date.isoformat(),
                end_date.isoformat(),
                payload,
                note,
                source,
                datetime.now(UTC).isoformat(),
            ),
        )
        await self._conn.commit()
        return int(cursor.lastrowid or 0)

    async def remove(self, entry_id: int) -> bool:
        """Delete a fact by id; True when a row was removed."""
        cursor = await self._conn.execute("DELETE FROM context WHERE id = ?", (entry_id,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def list_all(self) -> list[ContextEntry]:
        """Every stored fact, soonest start first."""
        cursor = await self._conn.execute(
            "SELECT id, kind, start_date, end_date, payload_json, note, source, created_at "
            "FROM context ORDER BY start_date, id"
        )
        return [self._row(r) for r in await cursor.fetchall()]

    async def active_on(self, day: date) -> list[ContextEntry]:
        """Facts whose inclusive range covers ``day``."""
        iso = day.isoformat()
        cursor = await self._conn.execute(
            "SELECT id, kind, start_date, end_date, payload_json, note, source, created_at "
            "FROM context WHERE start_date <= ? AND end_date >= ? ORDER BY id",
            (iso, iso),
        )
        return [self._row(r) for r in await cursor.fetchall()]

    @staticmethod
    def _row(r: Sequence[object]) -> ContextEntry:
        payload_raw = r[4]
        payload: dict[str, object] = {}
        if isinstance(payload_raw, str) and payload_raw:
            try:
                loaded = json.loads(payload_raw)
                if isinstance(loaded, dict):
                    payload = loaded
            except ValueError:
                payload = {}
        return ContextEntry(
            id=int(str(r[0])),
            kind=str(r[1]),
            start_date=date.fromisoformat(str(r[2])),
            end_date=date.fromisoformat(str(r[3])),
            note=str(r[5]),
            source=str(r[6]),
            created_at=datetime.fromisoformat(str(r[7])),
            payload=payload,
        )
