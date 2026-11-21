"""Permanent solar forecast evidence from the last pre-window plan; never a planning input."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, field_validator

if TYPE_CHECKING:
    from ha_spark.energy.models import PlannerConfig, PlannerInputs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evening_solar_forecasts (
    local_date TEXT PRIMARY KEY,
    issued_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    slots TEXT NOT NULL
);
"""
_UPSERT = """
INSERT INTO evening_solar_forecasts (local_date, issued_at, window_start, slots) VALUES (?, ?, ?, ?)
ON CONFLICT(local_date) DO UPDATE SET
    issued_at = excluded.issued_at, window_start = excluded.window_start, slots = excluded.slots
WHERE excluded.issued_at > evening_solar_forecasts.issued_at
  AND excluded.issued_at < evening_solar_forecasts.window_start
"""
_HALF_HOUR = timedelta(minutes=30)
_DAY = timedelta(days=1)


class SolarForecastSlot(BaseModel):
    """Raw full-slot solar forecast in kWh; null means unavailable."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    start: AwareDatetime
    p10_kwh: float | None = None
    p50_kwh: float | None = None
    p90_kwh: float | None = None

    @field_validator("p10_kwh", "p50_kwh", "p90_kwh", mode="before")
    @classmethod
    def finite_energy(cls, value: object) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if math.isfinite(value) and value >= 0:
                    return float(value)
            except OverflowError:
                pass
        return None


_SLOTS = TypeAdapter(list[SolarForecastSlot])


class EveningForecastStore:
    """One complete revision per household date, independent of the plan view."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def record(
        self, inputs: PlannerInputs, cfg: PlannerConfig, tz: tzinfo, *,
        native_slots: tuple[tuple[datetime, float, float, float], ...] = (),
    ) -> None:
        """Snapshot the next configured window's served day, not a dispatch or purchase."""
        if inputs.now is None or inputs.now.utcoffset() is None:
            return
        issued = inputs.now.astimezone(UTC)
        window_day = issued.astimezone(tz).date()
        # As in horizon geometry, fold=0 takes the first repeated instant and
        # resolves a nonexistent wall time forward. Compare real instants.
        window = datetime.combine(window_day, cfg.window_start, tz).astimezone(UTC)
        if issued >= window:
            window_day += _DAY
            window = datetime.combine(window_day, cfg.window_start, tz).astimezone(UTC)
        end_day = window_day if cfg.window_start < cfg.window_end else window_day + _DAY
        end_at = datetime.combine(end_day, cfg.window_end, tz).astimezone(UTC)
        day = end_at.astimezone(tz).date()

        p50_by_start: dict[datetime, float] = {}
        solar, known, geometry = inputs.solar_slots, inputs.solar_slots_known, inputs.geometry
        if (
            solar is not None and known is not None and geometry is not None
            and len(solar) == len(known) == len(geometry.slot_starts)
        ):
            # Match the plan view: use the raw planning input even when the
            # selected solar percentile isn't 50 (native slots replace it below).
            p50_by_start = {
                start.astimezone(UTC): kwh
                for start, kwh, exists in zip(geometry.slot_starts, solar, known, strict=True)
                if exists
            }
        bands = {start.astimezone(UTC): (p10 / 2, p50 / 2, p90 / 2)
                 for start, p10, p50, p90 in native_slots}
        slots: list[SolarForecastSlot] = []
        start = datetime.combine(day, time.min, tz).astimezone(UTC)
        end = datetime.combine(day + _DAY, time.min, tz).astimezone(UTC)
        while start < end:
            # A native date the planner didn't trust must not supply bands.
            # Where it did, store the native median rather than the planning
            # percentile.
            p50 = p50_by_start.get(start)
            p10 = p90 = None
            if p50 is not None and start in bands:
                p10, p50, p90 = bands[start]
            slots.append(SolarForecastSlot(
                start=start, p10_kwh=p10, p50_kwh=p50, p90_kwh=p90,
            ))
            start += _HALF_HOUR

        await asyncio.to_thread(Path(self.db_path).parent.mkdir, parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path, timeout=0.25) as db:
            await db.execute(_SCHEMA)
            await db.execute(_UPSERT, (
                day.isoformat(), issued.isoformat(), window.isoformat(), _SLOTS.dump_json(slots),
            ))
            await db.commit()

    async def slots(self, start: date, end: date) -> list[SolarForecastSlot]:
        """Read forecast slots by household date in [start, end), without creating a DB."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return []
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'evening_solar_forecasts'",
            ) as cursor:
                if await cursor.fetchone() is None:
                    return []
            async with db.execute(
                "SELECT slots FROM evening_solar_forecasts "
                "WHERE local_date >= ? AND local_date < ? ORDER BY local_date",
                (start.isoformat(), end.isoformat()),
            ) as cursor:
                return [slot for row in await cursor.fetchall()
                        for slot in _SLOTS.validate_json(row[0])]
