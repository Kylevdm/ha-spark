"""Tests for the load-history backfill pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ha_spark.config import Settings
from ha_spark.energy import onboarding
from ha_spark.energy.onboarding import (
    backfill_load,
    hourly_kwh_from_stats,
    statistic_unit,
    to_import_stats,
)

_T0_MS = 1780304400000  # an hour boundary, epoch ms
_T0 = datetime.fromtimestamp(_T0_MS / 1000, UTC)


def test_statistic_unit_prefers_statistics_unit() -> None:
    assert statistic_unit({"statistics_unit_of_measurement": "W"}) == "W"
    assert statistic_unit({"unit_of_measurement": "kWh"}) == "kWh"
    assert statistic_unit({}) == ""


def test_hourly_kwh_from_mean_watts() -> None:
    rows = [{"start": _T0_MS, "mean": 1500.0}]
    assert hourly_kwh_from_stats(rows, "W", "sensor.x") == [(_T0, 1.5)]


def test_hourly_kwh_from_mean_kilowatts() -> None:
    rows = [{"start": _T0_MS, "mean": 1.5}]
    assert hourly_kwh_from_stats(rows, "kW", "sensor.x") == [(_T0, 1.5)]


def test_hourly_kwh_from_energy_change() -> None:
    assert hourly_kwh_from_stats([{"start": _T0_MS, "change": 2.0}], "kWh", "sensor.x") == [
        (_T0, 2.0)
    ]
    assert hourly_kwh_from_stats([{"start": _T0_MS, "change": 500.0}], "Wh", "sensor.x") == [
        (_T0, 0.5)
    ]


def test_hourly_kwh_clamps_negatives_and_skips_missing() -> None:
    rows = [
        {"start": _T0_MS, "mean": -200.0},
        {"start": _T0_MS + 3600000, "mean": None},
        {"start": None, "mean": 1000.0},
    ]
    assert hourly_kwh_from_stats(rows, "W", "sensor.x") == [(_T0, 0.0)]


def test_hourly_kwh_rejects_unsupported_unit() -> None:
    with pytest.raises(ValueError, match="sensor.x"):
        hourly_kwh_from_stats([], "°C", "sensor.x")


def test_to_import_stats_builds_sorted_cumulative_sum() -> None:
    t1 = _T0.replace(hour=(_T0.hour + 1) % 24)
    later, earlier = max(_T0, t1), min(_T0, t1)
    stats = to_import_stats([(later, 2.0), (earlier, 1.0)])
    assert stats == [
        {"start": earlier.isoformat(), "state": 1.0, "sum": 1.0},
        {"start": later.isoformat(), "state": 2.0, "sum": 3.0},
    ]


def _settings() -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="t")


async def test_backfill_load_imports_converted_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: dict[str, Any] = {}

    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"statistic_id": "sensor.zappi", "statistics_unit_of_measurement": "W"}]

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs.get("period") == "hour"
        return [
            {"start": _T0_MS, "mean": 1000.0},
            {"start": _T0_MS + 3600000, "mean": 500.0},
        ]

    async def fake_import(*args: Any, **kwargs: Any) -> None:
        imported.update(kwargs)

    monkeypatch.setattr(onboarding, "list_statistic_ids", fake_list)
    monkeypatch.setattr(onboarding, "statistics_during_period", fake_stats)
    monkeypatch.setattr(onboarding, "import_statistics", fake_import)

    count, span = await backfill_load(_settings(), "sensor.zappi")

    assert count == 2
    assert "UTC" in span
    assert imported["statistic_id"] == "ha_spark:house_load"
    assert imported["unit_of_measurement"] == "kWh"
    assert imported["stats"] == [
        {"start": _T0.isoformat(), "state": 1.0, "sum": 1.0},
        {
            "start": datetime.fromtimestamp((_T0_MS + 3600000) / 1000, UTC).isoformat(),
            "state": 0.5,
            "sum": 1.5,
        },
    ]


async def test_backfill_load_errors_when_source_has_no_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(onboarding, "list_statistic_ids", fake_list)
    with pytest.raises(ValueError, match="no long-term statistics"):
        await backfill_load(_settings(), "sensor.missing")


async def test_backfill_load_errors_when_no_usable_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"statistic_id": "sensor.zappi", "statistics_unit_of_measurement": "W"}]

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(onboarding, "list_statistic_ids", fake_list)
    monkeypatch.setattr(onboarding, "statistics_during_period", fake_stats)
    with pytest.raises(ValueError, match="No usable"):
        await backfill_load(_settings(), "sensor.zappi")
