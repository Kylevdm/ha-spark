"""Day-close readiness joins, with immutable measured-data provenance.

Recorder reads never alter derived base-load history. Complete five-minute
measurements take precedence; hourly-only hours are split, never used to patch
partial short-term coverage. Snapshots and diagnostics are independent of
hardware control and authority. Failed reads are retried while HA's ten-day
short-term statistics retention permits; older days are explicitly excluded.
A closed day is attempted at most once an hour, and next-day evidence that
may still arrive leaves the day unsaved rather than excluded.
"""
from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict

from ha_spark.config import Settings
from ha_spark.energy.derived_base_load import ComponentSeries, derive_base_load
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.onboarding import (
    ENERGY_FACTORS,
    POWER_FACTORS,
    SUPPORTED_UNITS,
    statistic_unit,
)
from ha_spark.energy.readiness_ledger import ReadinessLedger, SlotRecord, slot_start
from ha_spark.ha.statistics import list_statistic_ids, statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)
HALF_HOUR = timedelta(minutes=30)
REQUIRED = ("grid_import", "grid_export", "solar_generation", "battery_charge", "battery_discharge")
COMPONENTS = (*REQUIRED, "ev_charge", "v2l")
# How long an unsaved day waits before the next join attempt (in memory only;
# a restart may retry at once).
RETRY_DELAY = timedelta(hours=1)
# Beyond HA's short-term statistics retention a day can no longer be joined.
STATS_RETENTION = timedelta(days=10)
# HA compiles statistics a few minutes after each period (hourly ones after
# the hour), so a day is first joined only this long after it closes: an
# earlier join would read the last slots as substituted and exclude the day.
SETTLE_DELAY = timedelta(hours=1)
# How long after a day closes its next-day evidence (the next day's start
# energy and the next cheap slot) may still be captured.
EVIDENCE_HORIZON = timedelta(days=2)


class EvidencePending(Exception):
    """Next-day evidence is missing but may still arrive; retry, never exclude."""


class Measurement(BaseModel):
    """A true zero retains its evidence; a missing measurement never becomes one."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    kwh: float
    provenance: Literal["measured", "hourly_spread", "substituted", "absent"]


class MeasuredSlot(BaseModel):
    """Actual flows and the recorded intent for one half-hour."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    slot_start: datetime
    intent: SlotRecord | None
    components: dict[str, Measurement]
    load_kwh: float
    negative_clamped: bool


class DayRecord(BaseModel):
    """Write-once household-local day, including every exclusion and gap."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    local_date: date
    timezone: str
    slots: tuple[MeasuredSlot, ...]
    reasons: tuple[str, ...]
    start_energy_kwh: float | None
    end_energy_kwh: float | None
    next_cheap_import_rate: float | None

    @property
    def qualifying(self) -> bool:
        return not self.reasons


class ReadinessDays:
    """Append-only day snapshots in the readiness database; reads never create it."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def days(self) -> list[DayRecord]:
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return []
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'readiness_days'"
            ) as cursor:
                if await cursor.fetchone() is None:
                    return []
            async with db.execute("SELECT payload FROM readiness_days ORDER BY local_date") as c:
                return [DayRecord.model_validate_json(row[0]) for row in await c.fetchall()]

    async def dates(self) -> set[date]:
        """Saved local dates only; the cheap per-tick check, without payloads."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return set()
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'readiness_days'"
            ) as cursor:
                if await cursor.fetchone() is None:
                    return set()
            async with db.execute("SELECT local_date FROM readiness_days") as c:
                return {date.fromisoformat(row[0]) for row in await c.fetchall()}

    async def record(self, day: DayRecord) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS readiness_days "
                             "(local_date TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            await db.execute(
                "INSERT INTO readiness_days VALUES (?, ?) ON CONFLICT DO NOTHING",
                (day.local_date.isoformat(), day.model_dump_json()),
            )
            await db.commit()


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    """UTC boundaries preserve 46/50-slot household-clock DST days."""
    tz = load_timezone(timezone)
    return (datetime.combine(day, time.min, tz).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC))


def measurement_series(
    five_minute: list[dict[str, object]], hourly: list[dict[str, object]],
    unit: str, invert: bool = False, timezone: str = "UTC",
) -> dict[datetime, Measurement]:
    """Convert recorder rows to half-hours, without scaling partial coverage."""
    if unit not in POWER_FACTORS and unit not in ENERGY_FACTORS:
        raise ValueError(
            f"Unsupported unit {unit!r}; need one of {sorted(SUPPORTED_UNITS)}"
        )
    power = unit in POWER_FACTORS
    factor = (POWER_FACTORS if power else ENERGY_FACTORS)[unit] * (-1 if invert else 1)

    def converted(rows: list[dict[str, object]], duration: float) -> dict[datetime, float]:
        result = {}
        for row in rows:
            try:
                start = datetime.fromtimestamp(float(row["start"]) / 1000, UTC)  # type: ignore[arg-type]
                value = float(row["mean" if power else "change"]) * factor  # type: ignore[arg-type]
                if math.isfinite(value):
                    result[start] = value * duration if power else value
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
        return result

    fine = converted(five_minute, 1 / 12)
    coarse = converted(hourly, 1)
    # Spread only whole hourly-only hours, then join on the household slot grid.
    # A :15/:45-offset household slot may span two recorder hours.
    coarse_ticks = {
        hour + timedelta(minutes=5 * i): value / 12
        for hour, value in coarse.items()
        if not any(hour <= t < hour + timedelta(hours=1) for t in fine)
        for i in range(12)
    }
    tz = load_timezone(timezone)
    starts = {slot_start(t.astimezone(tz)) for t in fine.keys() | coarse_ticks.keys()}
    result = {}
    for start in sorted(starts):
        ticks = [start + timedelta(minutes=5 * i) for i in range(6)]
        if all(t in fine for t in ticks):
            result[start] = Measurement(kwh=sum(fine[t] for t in ticks), provenance="measured")
        elif all(t in coarse_ticks for t in ticks):
            result[start] = Measurement(kwh=sum(coarse_ticks[t] for t in ticks),
                                        provenance="hourly_spread")
    return result


async def snapshot_day(
    settings: Settings, day: date, *, now: datetime | None = None,
    unavailable_reason: str | None = None,
) -> DayRecord:
    """Join once after day close. Transport/storage failures propagate for retry.

    With ``now`` supplied, next-day evidence that may still arrive (the next
    day's start energy, the next cheap slot) leaves the day unsaved instead of
    excluding it: :class:`EvidencePending` propagates for a later attempt.
    """
    store = ReadinessDays(settings.db_path)
    existing = next((row for row in await store.days() if row.local_date == day), None)
    if existing is not None:
        return existing
    start, end = day_bounds(day, settings.timezone)
    ledger = ReadinessLedger(settings.db_path)
    # Future captured slots supply the actual end energy and next cheap rate.
    rows = await ledger.slots(start, end + timedelta(days=2))
    intents = {row.slot_start: row for row in rows}
    reasons = [unavailable_reason] if unavailable_reason else []
    series: dict[str, dict[datetime, Measurement]] = {}
    entities = {name: str(getattr(settings, f"derive_{name}_entity")) for name in COMPONENTS[:-1]}
    entities["v2l"] = settings.v2l_power_entity
    metas = [] if unavailable_reason else await list_statistic_ids(
        settings.ha_websocket_url, settings.auth_token, timeout=settings.ha_timeout,
    )
    for name, entity in entities.items():
        series[name] = {}
        if not entity:
            if name in REQUIRED:
                reasons.append(f"{name}: required component not configured")
            continue
        meta = next((m for m in metas if m.get("statistic_id") == entity), None)
        if meta is None:
            continue
        fine = await statistics_during_period(
            settings.ha_websocket_url, settings.auth_token, entity, start,
            period="5minute", timeout=settings.ha_timeout,
        )
        coarse = await statistics_during_period(
            settings.ha_websocket_url, settings.auth_token, entity, start,
            period="hour", timeout=settings.ha_timeout,
        )
        series[name] = measurement_series(fine, coarse, statistic_unit(meta),
                                          bool(getattr(settings, f"derive_invert_{name}", False)),
                                          settings.timezone)
    slots = []
    instant = start
    while instant < end:
        components = {}
        for name in COMPONENTS:
            value = series[name].get(instant)
            if value is None:
                absent = not entities[name] and name not in REQUIRED
                value = Measurement(kwh=0, provenance="absent" if absent else "substituted")
            components[name] = value
            if value.provenance == "substituted":
                reasons.append(f"{instant.isoformat()} {name}: substituted measurement gap")
        intent = intents.get(instant)
        if intent is None:
            reasons.append(f"{instant.isoformat()}: slot record missing")
        elif intent.operating_mode != "observe":
            reasons.append(f"{instant.isoformat()}: not Observe ({intent.operating_mode})")
        if intent is not None and intent.hold_state == "unknown":
            reasons.append(f"{instant.isoformat()}: hold intent unknown")
        derived = derive_base_load(**{
            name: ComponentSeries(entities[name], {instant: components[name].kwh})
            for name in COMPONENTS[:-1]
        })
        if derived.negative_clamped:
            reasons.append(f"{instant.isoformat()}: negative base load clamped")
        slots.append(MeasuredSlot(
            slot_start=instant, intent=intent, components=components,
            load_kwh=derived.hourly[0][1] + components["v2l"].kwh,
            negative_clamped=bool(derived.negative_clamped),
        ))
        instant += HALF_HOUR
    first, following = intents.get(start), intents.get(end)
    start_energy = first.start_energy_kwh if first else None
    end_energy = following.start_energy_kwh if following else None
    cheap = next((r.import_rate for r in rows if r.slot_start >= end
                  and r.charge_window_fraction > 0), None)
    if start_energy is None:
        reasons.append("actual start energy missing")
    if end_energy is None:
        reasons.append("actual end energy missing (next day's start)")
    if cheap is None:
        reasons.append("next cheap-slot import rate missing")
    if now is not None and now.astimezone(UTC) < end + EVIDENCE_HORIZON and (
            end_energy is None or cheap is None):
        raise EvidencePending(
            f"{day.isoformat()}: next-day evidence may still arrive "
            "(end energy, next cheap slot)"
        )
    result = DayRecord(local_date=day, timezone=settings.timezone, slots=tuple(slots),
                       reasons=tuple(reasons), start_energy_kwh=start_energy,
                       end_energy_kwh=end_energy, next_cheap_import_rate=cheap)
    await store.record(result)
    return next(row for row in await store.days() if row.local_date == day)


# In-memory per-day next-attempt times; a restart starts without them and may
# retry at once. Keyed with the database path so separate stores never share.
_next_attempt: dict[tuple[str, date], datetime] = {}


async def record_closed_days(settings: Settings, now: datetime) -> None:
    """Snapshot unsaved closed days cheaply on every tick; a tick never breaks.

    A closed day is attempted at most once an hour. Only the days being
    attempted are read; a day whose evidence is still arriving stays unsaved,
    and only real evidence outcomes are ever stored as excluded.
    """
    try:
        tz = load_timezone(settings.timezone)
        today = now.astimezone(tz).date()
        ledger = ReadinessLedger(settings.db_path)
        first = await ledger.first_slot_start()
        if first is None:
            return
        saved = await ReadinessDays(settings.db_path).dates()
        day = first.astimezone(tz).date()
        while day < today:
            key = (settings.db_path, day)
            if day not in saved and not (
                (due := _next_attempt.get(key)) is not None and now.astimezone(UTC) < due
            ):
                start, end = day_bounds(day, settings.timezone)
                if now.astimezone(UTC) < end + SETTLE_DELAY:
                    day += timedelta(days=1)
                    continue
                expired = now.astimezone(UTC) - start > STATS_RETENTION
                try:
                    await snapshot_day(
                        settings, day, now=now,
                        unavailable_reason=(
                            "day-close join missed short-term statistics retention"
                            if expired else None
                        ),
                    )
                    _next_attempt.pop(key, None)
                except EvidencePending:
                    _next_attempt[key] = now.astimezone(UTC) + RETRY_DELAY
                except Exception:
                    log.exception("Readiness day-close recording failed for %s", day)
                    _next_attempt[key] = now.astimezone(UTC) + RETRY_DELAY
            day += timedelta(days=1)
    except Exception:
        log.exception("Readiness day-close recording failed")
