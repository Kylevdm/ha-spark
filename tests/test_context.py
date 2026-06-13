"""Tests for the context store and its deterministic load factor."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ha_spark.config import Settings
from ha_spark.energy.context import ContextStore, combined_factor


async def test_add_list_remove_roundtrip(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    async with ContextStore(db) as store:
        eid = await store.add("away", date(2026, 7, 1), date(2026, 7, 14), note="Italy")
        entries = await store.list_all()
        assert len(entries) == 1
        e = entries[0]
        assert e.id == eid
        assert e.kind == "away"
        assert e.start_date == date(2026, 7, 1)
        assert e.end_date == date(2026, 7, 14)
        assert e.note == "Italy"

        assert await store.remove(eid) is True
        assert await store.list_all() == []
        assert await store.remove(eid) is False  # already gone


async def test_add_rejects_bad_kind_and_range(tmp_path: Path) -> None:
    async with ContextStore(str(tmp_path / "test.db")) as store:
        with pytest.raises(ValueError, match="unknown context kind"):
            await store.add("vacation", date(2026, 7, 1), date(2026, 7, 2))
        with pytest.raises(ValueError, match="before the start"):
            await store.add("away", date(2026, 7, 5), date(2026, 7, 1))


async def test_active_on_inclusive_range(tmp_path: Path) -> None:
    async with ContextStore(str(tmp_path / "test.db")) as store:
        await store.add("away", date(2026, 7, 1), date(2026, 7, 14))
        assert len(await store.active_on(date(2026, 7, 1))) == 1  # first day
        assert len(await store.active_on(date(2026, 7, 14))) == 1  # last day
        assert len(await store.active_on(date(2026, 7, 7))) == 1  # middle
        assert await store.active_on(date(2026, 6, 30)) == []  # before
        assert await store.active_on(date(2026, 7, 15)) == []  # after


async def test_single_day_fact_defaults_end_to_start(tmp_path: Path) -> None:
    async with ContextStore(str(tmp_path / "test.db")) as store:
        await store.add("guests", date(2026, 7, 4), date(2026, 7, 4))
        assert len(await store.active_on(date(2026, 7, 4))) == 1


async def test_usage_factor_persisted_in_payload(tmp_path: Path) -> None:
    settings = Settings()
    async with ContextStore(str(tmp_path / "test.db")) as store:
        await store.add("high_usage", date(2026, 7, 1), date(2026, 7, 2), factor=1.6)
        e = (await store.list_all())[0]
        assert e.factor(settings) == 1.6


def test_combined_factor_multiplies_active_entries() -> None:
    settings = Settings(away_load_factor=0.4, guests_load_factor=1.3)

    class _E:
        def __init__(self, kind: str, f: float) -> None:
            self.id = 1
            self.kind = kind
            self.start_date = date(2026, 7, 1)
            self.end_date = date(2026, 7, 1)
            self.note = ""
            self._f = f

        def factor(self, _s: Settings) -> float:
            return self._f

    factor, lines = combined_factor([_E("away", 0.4), _E("guests", 1.3)], settings)  # type: ignore[list-item]
    assert factor == pytest.approx(0.52)
    assert len(lines) == 2


def test_away_and_guests_factors_from_config(tmp_path: Path) -> None:
    settings = Settings(away_load_factor=0.3, guests_load_factor=1.5)

    import asyncio

    async def _run() -> tuple[float, float]:
        async with ContextStore(str(tmp_path / "test.db")) as store:
            await store.add("away", date(2026, 7, 1), date(2026, 7, 1))
            away = (await store.active_on(date(2026, 7, 1)))[0]
            await store.remove(away.id)
            await store.add("guests", date(2026, 7, 1), date(2026, 7, 1))
            guests = (await store.active_on(date(2026, 7, 1)))[0]
            return away.factor(settings), guests.factor(settings)

    away_f, guests_f = asyncio.run(_run())
    assert away_f == 0.3
    assert guests_f == 1.5
