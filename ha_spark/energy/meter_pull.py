"""Incremental Octopus meter history and its local pull status, for reconciliation only."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import aiosqlite

from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.octopus import ConsumptionReadError, fetch_consumption
from ha_spark.energy.readiness_measurements import day_bounds
from ha_spark.energy.store import ConsumptionStore, has_meter_table, open_meter_db
from ha_spark.logging import get_logger

log = get_logger(__name__)
METER_RETRY_INTERVAL = timedelta(hours=1)


class MeterPullOutcome(StrEnum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    NOT_DUE = "not_due"
    UP_TO_DATE = "up_to_date"
    STARTED = "started"
    AWAITING_PUBLICATION = "awaiting_publication"
    REQUEST_FAILED = "request_failed"
    INVALID_PAYLOAD = "invalid_payload"
    OFFSITE_NEXT_URL = "offsite_next_url"
    STORE_UNAVAILABLE = "store_unavailable"

    @property
    def failed(self) -> bool:
        return self in {self.REQUEST_FAILED, self.INVALID_PAYLOAD, self.OFFSITE_NEXT_URL,
                        self.STORE_UNAVAILABLE}


@dataclass(frozen=True)
class MeterHistory:
    first_start: datetime | None = None
    latest_start: datetime | None = None
    latest_end: datetime | None = None
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    outcome: MeterPullOutcome | None = None
    period_from: datetime | None = None


def meter_history_configured(settings: Settings) -> bool:
    return all(value.strip() for value in (
        settings.octopus_api_key, settings.octopus_mpan, settings.octopus_meter_serial,
    ))


async def read_meter_history(db_path: str) -> MeterHistory:
    """Read local status and the stored interval range without creating tables."""
    first = latest = status = None
    async with open_meter_db(db_path) as db:
        if db is not None and await has_meter_table(db, "consumption"):
            async with db.execute(
                "SELECT interval_start FROM consumption ORDER BY interval_start LIMIT 1",
            ) as cursor:
                first = await cursor.fetchone()
            async with db.execute(
                "SELECT interval_start, interval_end FROM consumption "
                "ORDER BY interval_start DESC LIMIT 1",
            ) as cursor:
                latest = await cursor.fetchone()
        if db is not None and await has_meter_table(db, "meter_pull_status"):
            async with db.execute(
                "SELECT last_attempt, last_success, outcome, period_from "
                "FROM meter_pull_status WHERE id = ?",
                (1,),
            ) as cursor:
                status = await cursor.fetchone()
    return MeterHistory(
        first_start=datetime.fromisoformat(first[0]) if first else None,
        latest_start=datetime.fromisoformat(latest[0]) if latest else None,
        latest_end=datetime.fromisoformat(latest[1]) if latest else None,
        last_attempt=datetime.fromisoformat(status[0]) if status else None,
        last_success=datetime.fromisoformat(status[1]) if status and status[1] else None,
        outcome=MeterPullOutcome(status[2]) if status else None,
        period_from=datetime.fromisoformat(status[3]) if status else None,
    )


async def _save_status(
    db_path: str, now: datetime, outcome: MeterPullOutcome, period_from: datetime,
) -> None:
    await asyncio.to_thread(Path(db_path).parent.mkdir, parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS meter_pull_status (id INTEGER PRIMARY KEY, "
            "last_attempt TEXT NOT NULL, last_success TEXT, outcome TEXT NOT NULL, "
            "period_from TEXT NOT NULL)",
        )
        at = now.isoformat()
        await db.execute(
            "INSERT INTO meter_pull_status VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "last_attempt = excluded.last_attempt, "
            "last_success = COALESCE(excluded.last_success, meter_pull_status.last_success), "
            "outcome = excluded.outcome, period_from = excluded.period_from",
            (1, at, at if outcome == MeterPullOutcome.OK else None, outcome.value,
             period_from.isoformat()),
        )
        await db.commit()


async def pull_meter_history(settings: Settings, now: datetime) -> MeterPullOutcome:
    """Attempt a due pull through yesterday; never raise a recording failure into control."""
    try:
        if not meter_history_configured(settings):
            return MeterPullOutcome.NOT_CONFIGURED
        now = now.astimezone(UTC)
        day = now.astimezone(load_timezone(settings.timezone)).date()
        yesterday, midnight = day_bounds(day - timedelta(days=1), settings.timezone)
        history = await read_meter_history(settings.db_path)
        if history.latest_end is not None and history.latest_end >= midnight:
            return MeterPullOutcome.UP_TO_DATE
        if history.last_attempt is not None and now - history.last_attempt < METER_RETRY_INTERVAL:
            return MeterPullOutcome.NOT_DUE
        # Keep the initial start across midnight if Octopus has yet to publish anything.
        start = history.latest_start or history.period_from or yesterday
        # A restart or cancelled request must not bypass the retry interval.
        await _save_status(settings.db_path, now, MeterPullOutcome.STARTED, start)
        try:
            rows = await fetch_consumption(settings, period_from=start, period_to=midnight)
        except ConsumptionReadError as exc:
            outcome = MeterPullOutcome(exc.reason)
        except Exception:
            outcome = MeterPullOutcome.REQUEST_FAILED
        else:
            rows = [row for row in rows if start <= row.start and row.end <= midnight]
            outcome = (MeterPullOutcome.OK if any(row.start >= yesterday for row in rows)
                       else MeterPullOutcome.AWAITING_PUBLICATION)
            if rows:
                try:
                    async with ConsumptionStore(settings.db_path) as store:
                        await store.upsert(rows, "api", now=now)
                except Exception:
                    outcome = MeterPullOutcome.STORE_UNAVAILABLE
        await _save_status(settings.db_path, now, outcome, start)
    except Exception:
        outcome = MeterPullOutcome.STORE_UNAVAILABLE
    if outcome.failed:
        log.warning("Meter history pull failed")
    return outcome
