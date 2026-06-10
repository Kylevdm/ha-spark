"""Tests for the SQLite consumption store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ha_spark.energy.models import ConsumptionInterval
from ha_spark.energy.store import ConsumptionStore


def _interval(hours_offset: int, kwh: float = 0.5) -> ConsumptionInterval:
    start = datetime(2026, 6, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=30 * hours_offset)
    return ConsumptionInterval(start=start, end=start + timedelta(minutes=30), kwh=kwh)


async def test_upsert_is_idempotent(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    rows = [_interval(0), _interval(1), _interval(2)]
    async with ConsumptionStore(db) as store:
        assert await store.upsert(rows, "csv") == 3
        # Same data again: nothing new or changed.
        assert await store.upsert(rows, "csv") == 0
        count, first, last = await store.summary()
    assert count == 3
    assert first == rows[0].start
    assert last == rows[2].start


async def test_upsert_updates_changed_reading(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    async with ConsumptionStore(db) as store:
        await store.upsert([_interval(0, kwh=0.5)], "csv")
        assert await store.upsert([_interval(0, kwh=0.7)], "api") == 1
        loaded = await store.load_since(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(loaded) == 1
    assert loaded[0].kwh == 0.7


async def test_load_since_filters_and_orders(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    rows = [_interval(i) for i in (2, 0, 1)]
    async with ConsumptionStore(db) as store:
        await store.upsert(rows, "csv")
        loaded = await store.load_since(_interval(1).start)
    assert [r.start for r in loaded] == [_interval(1).start, _interval(2).start]


async def test_latest_interval_start(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    async with ConsumptionStore(db) as store:
        assert await store.latest_interval_start() is None
        await store.upsert([_interval(0), _interval(5)], "csv")
        assert await store.latest_interval_start() == _interval(5).start


async def test_creates_parent_directory(tmp_path: Path) -> None:
    db = str(tmp_path / "nested" / "dir" / "test.db")
    async with ConsumptionStore(db) as store:
        count, _, _ = await store.summary()
    assert count == 0
