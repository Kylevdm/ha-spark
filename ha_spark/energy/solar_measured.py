"""Read-only, short-lived HA solar statistics for the ingress history view.

Only six unique, valid five-minute intervals make a measured half-hour. This
reader is separate from planning and the permissive hourly load converter.
Upstream errors are discarded: HA exceptions can contain credentials or bodies.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard

from ha_spark.config import Settings
from ha_spark.energy.onboarding import ENERGY_FACTORS, POWER_FACTORS, statistic_unit
from ha_spark.ha.statistics import list_statistic_ids, statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)
_HALF_HOUR = timedelta(minutes=30)
Reader = Callable[[Settings, datetime], Awaitable[tuple[str | None, list[dict[str, Any]]]]]
Key = tuple[datetime, datetime, str, str, bool]


@dataclass(frozen=True)
class MeasuredResult:
    """Only validated complete half-hours, or a fixed unavailable reason."""

    values: dict[datetime, float]
    reason: str | None = None


async def read_statistics(settings: Settings, start: datetime) -> tuple[
    str | None, list[dict[str, Any]]
]:
    """Read metadata and five-minute rows, never hourly substitutes."""
    meta = await list_statistic_ids(
        settings.ha_websocket_url, settings.auth_token, timeout=settings.ha_timeout,
    )
    entity = settings.derive_solar_generation_entity
    match = next((m for m in meta if m.get("statistic_id") == entity), None)
    if match is None:
        return None, []
    unit = statistic_unit(match)
    if unit not in ENERGY_FACTORS and unit not in POWER_FACTORS:
        return unit, []
    rows = await statistics_during_period(
        settings.ha_websocket_url, settings.auth_token, entity, start,
        period="5minute", timeout=settings.ha_timeout,
    )
    return unit, rows


def _number(value: object) -> TypeGuard[int | float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _measure(
    rows: list[dict[str, Any]], unit: str, start: datetime, end: datetime,
) -> dict[datetime, float]:
    key = "change" if unit in ENERGY_FACTORS else "mean"
    factor = (ENERGY_FACTORS[unit] if unit in ENERGY_FACTORS
              else POWER_FACTORS[unit] / 12)
    values: dict[datetime, float] = {}
    seen: set[datetime] = set()
    invalid: set[datetime] = set()
    for row in rows:
        raw = row.get("start")
        try:
            if _number(raw):
                at = datetime.fromtimestamp(raw / 1000, UTC)
            elif isinstance(raw, str):
                at = datetime.fromisoformat(raw)
            else:
                continue  # an unlocatable row can't count towards any half-hour
            aware = at.tzinfo is not None and at.utcoffset() is not None
            at = at.replace(tzinfo=UTC) if not aware else at.astimezone(UTC)
        except (ValueError, OverflowError, OSError):
            continue
        if at < start or at >= end:
            continue
        # The half-hour on the requested grid (household midnight need not be
        # a UTC half-hour, e.g. +05:45).
        half = start + (at - start) // _HALF_HOUR * _HALF_HOUR
        value = row.get(key)
        if (not aware or at.minute % 5 or at.second or at.microsecond
                or not _number(value) or at in seen):
            invalid.add(half)
        else:
            converted = float(value) * factor
            if math.isfinite(converted):
                values[at] = converted
            else:
                invalid.add(half)
        seen.add(at)
    result: dict[datetime, float] = {}
    half = start
    while half < end:
        points = [half + timedelta(minutes=5 * i) for i in range(6)]
        if half not in invalid and all(at in values for at in points):
            total = sum(values[at] for at in points)
            if math.isfinite(total):
                result[half] = total
        half += timedelta(minutes=30)
    return result


class MeasuredSolar:
    """App-local five-minute cache, with one bounded read in flight per key."""

    def __init__(
        self, *, reader: Reader = read_statistics, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reader = reader
        self.clock = clock
        self._cache: dict[Key, tuple[float, MeasuredResult]] = {}
        self._pending: dict[Key, asyncio.Task[MeasuredResult]] = {}

    async def read(self, settings: Settings, start: datetime, end: datetime) -> MeasuredResult:
        """Read only on demand; failures and unavailable results aren't cached."""
        entity = settings.derive_solar_generation_entity
        if not entity:
            return MeasuredResult({}, "no_entity")
        # A default `to` is "now" to the microsecond: key on the grid's next
        # half-hour so repeats within the cache's five minutes share one read.
        key_end = start + -((start - end) // _HALF_HOUR) * _HALF_HOUR
        key = (start, key_end, entity, settings.timezone, settings.derive_invert_solar_generation)
        self._cache = {k: v for k, v in self._cache.items() if self.clock() - v[0] < 300}
        if key in self._cache:
            return self._cache[key][1]
        if key not in self._pending:
            self._pending[key] = asyncio.create_task(self._read(settings, start, end, key))
        return await asyncio.shield(self._pending[key])

    async def _read(
        self, settings: Settings, start: datetime, end: datetime, key: Key,
    ) -> MeasuredResult:
        try:
            async with asyncio.timeout(settings.ha_timeout):
                unit, rows = await self.reader(settings, start)
                if unit is None:
                    return MeasuredResult({}, "no_statistics")
                if unit not in ENERGY_FACTORS and unit not in POWER_FACTORS:
                    return MeasuredResult({}, "unsupported_unit")
                if not rows:
                    return MeasuredResult({}, "no_statistics")
                values = _measure(rows, unit, start, end)
                sign = -1 if settings.derive_invert_solar_generation else 1
                result = MeasuredResult({at: value * sign for at, value in values.items()})
                self._cache[key] = (self.clock(), result)
                return result
        except Exception:  # noqa: BLE001 - HA exception text is untrusted
            log.warning("Measured solar statistics read failed")
            return MeasuredResult({}, "read_failed")
        finally:
            self._pending.pop(key, None)
