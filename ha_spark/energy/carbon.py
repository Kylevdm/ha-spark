"""Display-only NESO regional estimates: validated HTTP, SQLite cache and refresh task.

Carbon is never a planner input or readiness signal. The daemon ticks the refresher
without awaiting network I/O; the ingress reader uses only the durable cache.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
import httpx

from ha_spark.logging import get_logger

NESO_BASE_URL = "https://api.carbonintensity.org.uk"
ATTRIBUTION = "Carbon intensity data: NESO Carbon Intensity API, CC BY 4.0"
_LABELS = {"very low", "low", "moderate", "high", "very high"}
log = get_logger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS carbon_periods (
    region INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    gco2 REAL NOT NULL, band TEXT NOT NULL, PRIMARY KEY (region, start)
);
CREATE TABLE IF NOT EXISTS carbon_fetches (
    region INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL
);
"""


class _HideNesoRequests(logging.Filter):
    """httpx logs each request line, URL included, at INFO; carbon logs fixed text only."""

    def filter(self, record: logging.LogRecord) -> bool:
        return NESO_BASE_URL not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideNesoRequests())


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def _boundary(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    at = datetime.fromisoformat(value)
    if (at.utcoffset() != timedelta(0) or at.minute not in (0, 30)
            or at.second or at.microsecond):
        raise ValueError
    return at.astimezone(UTC)


def _validate(
    payload: object, region: int, start: datetime, end: datetime,
) -> list[tuple[str, str, float, str]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if (not isinstance(data, dict) or type(data.get("regionid")) is not int
            or data["regionid"] != region or not isinstance(data.get("data"), list)):
        return []
    rows: dict[str, tuple[str, str, float, str]] = {}
    for raw in data["data"]:
        if not isinstance(raw, dict):
            continue
        try:
            first, last = _boundary(raw.get("from")), _boundary(raw.get("to"))
            intensity = raw.get("intensity")
            if not isinstance(intensity, dict):
                continue
            value, band = intensity.get("forecast"), intensity.get("index")
            if (first < start or last > end or last - first != timedelta(minutes=30)
                    or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 2000 or not math.isfinite(value)
                    or not isinstance(band, str) or band not in _LABELS):
                continue
            key = _iso(first)
            if key in rows or len(rows) < 300:
                rows[key] = (key, _iso(last), float(value), band)
        except (ValueError, OverflowError):
            continue
    return [rows[key] for key in sorted(rows)]


async def _store(
    path: str, region: int, now: datetime, rows: list[tuple[str, str, float, str]],
    current: Callable[[], bool] = lambda: True,
) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_SCHEMA)
        await db.executemany(
            "INSERT INTO carbon_periods VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(region, start) DO UPDATE SET "
            "end=excluded.end, gco2=excluded.gco2, band=excluded.band",
            [(region, *row) for row in rows],
        )
        await db.execute(
            "INSERT INTO carbon_fetches VALUES (?, ?) "
            "ON CONFLICT(region) DO UPDATE SET fetched_at=excluded.fetched_at",
            (region, _iso(now)),
        )
        await db.execute("DELETE FROM carbon_periods WHERE start < ?",
                         (_iso(now - timedelta(days=2)),))
        if current():  # The region may have changed while the write awaited.
            await db.commit()


async def carbon_payload(path: str, region: int | None, now: datetime) -> dict[str, object]:
    """Read the selected region only; missing periods are never extrapolated."""
    payload: dict[str, object] = {
        "status": "off" if region is None else "unavailable", "region": region,
        "fetched_at": None, "ends_at": None, "rows": [], "attribution": ATTRIBUTION,
    }
    if region is None:
        return payload
    try:
        async with aiosqlite.connect(path) as db:
            await db.executescript(_SCHEMA)
            async with db.execute(
                "SELECT fetched_at FROM carbon_fetches WHERE region = ?", (region,),
            ) as cursor:
                fetched = await cursor.fetchone()
            async with db.execute(
                "SELECT start, end, gco2, band FROM carbon_periods "
                "WHERE region = ? AND start >= ? ORDER BY start",
                (region, _iso(now - timedelta(days=2))),
            ) as cursor:
                rows = list(await cursor.fetchall())
        payload["rows"] = [
            {"start": a, "end": b, "gco2": value, "index": band,
             "estimated": datetime.fromisoformat(b) <= now}
            for a, b, value, band in rows
        ]
        payload["fetched_at"] = fetched[0] if fetched else None
        payload["ends_at"] = rows[-1][1] if rows else None
        if any(datetime.fromisoformat(a) <= now < datetime.fromisoformat(b)
               for a, b, _, _ in rows) and fetched:
            payload["status"] = (
                "stale" if now - datetime.fromisoformat(fetched[0]) > timedelta(hours=2) else "ok"
            )
    except (aiosqlite.Error, OSError, ValueError):
        log.warning("Carbon cache unavailable")
    return payload


class CarbonRefresher:
    """One independent request task, scheduled by live-settings control-loop ticks."""

    def __init__(self, path: str, region: Callable[[], int | None]) -> None:
        self._path = path
        self._region = region
        self._selected: int | None = None
        self._attempted_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._retired: list[asyncio.Task[None]] = []

    def tick(self, now: datetime, timezone: str, horizon_end: datetime | None) -> None:
        """Schedule without waiting, including immediately after a region change."""
        now = now.astimezone(UTC)
        region = self._region()
        if region != self._selected:
            if self._task is not None:
                self._task.cancel()
                self._retired.append(self._task)
                self._task = None
            self._selected = region
            self._attempted_at = None
        self._retired = [task for task in self._retired if not task.done()]
        if region is None or (self._task is not None and not self._task.done()):
            return
        if self._attempted_at is not None and now - self._attempted_at < timedelta(minutes=30):
            return
        self._attempted_at = now
        local = now.astimezone(ZoneInfo(timezone))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        end = (horizon_end.astimezone(UTC) + timedelta(hours=2) if horizon_end is not None
               else now + timedelta(hours=48))
        self._task = asyncio.create_task(self._fetch(region, now, start, end))

    async def _fetch(self, region: int, now: datetime, start: datetime, end: datetime) -> None:
        url = (f"{NESO_BASE_URL}/regional/intensity/{start:%Y-%m-%dT%H:%MZ}/"
               f"{end:%Y-%m-%dT%H:%MZ}/regionid/{region}")
        try:
            async with (
                asyncio.timeout(20),
                httpx.AsyncClient(timeout=20, trust_env=False) as client,
            ):
                response = await client.get(url)
                if response.status_code == 500 and self._region() == region:
                    response = await client.get(url)
                if response.status_code != 200:
                    log.warning("Carbon fetch failed: http_status")
                    return
                rows = _validate(response.json(), region, start, end)
                if not rows:
                    log.warning("Carbon fetch failed: invalid_payload")
                    return
                if self._region() == region:
                    await _store(self._path, region, now, rows,
                                 lambda: self._region() == region)
        except Exception:  # noqa: BLE001 - fixed text only, never the exception's
            log.warning("Carbon fetch failed: unavailable")

    async def wait(self) -> None:
        """Wait for outstanding cache work (not used by the control loop)."""
        if self._task is not None:
            await self._task

    async def close(self) -> None:
        """Cancel and await every outstanding request on daemon shutdown."""
        tasks = [*self._retired, *([self._task] if self._task is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
