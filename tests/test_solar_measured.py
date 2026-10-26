"""Trustworthy five-minute solar measurements and their read-only cache."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ha_spark.config import Settings
from ha_spark.energy.solar_measured import MeasuredSolar

START = datetime(2026, 1, 12, tzinfo=UTC)


def rows():
    return [{"start": (START + timedelta(minutes=i * 5)).isoformat(),
             "change": 100, "mean": 1200} for i in range(6)]


@pytest.mark.parametrize("unit,expected", [("Wh", .6), ("kWh", 600), ("W", .6), ("kW", 600)])
@pytest.mark.parametrize("invert", [False, True])
async def test_energy_power_units_and_inversion(unit, expected, invert):
    async def reader(settings, start):
        return unit, rows()
    cache = MeasuredSolar(reader=reader)
    settings = Settings(derive_solar_generation_entity="sensor.solar",
                        derive_invert_solar_generation=invert)
    result = await cache.read(settings, START, START + timedelta(minutes=30))
    assert result.reason is None
    assert result.values[START] == pytest.approx(-expected if invert else expected)


@pytest.mark.parametrize("bad", ["missing", "nan", "naive", "off_boundary", "duplicate", "bool"])
async def test_bad_half_hours_are_blank(bad):
    data = rows()
    if bad == "missing":
        data.pop()
    elif bad == "nan":
        data[0]["change"] = float("nan")
    elif bad == "naive":
        data[0]["start"] = "2026-01-12T00:00:00"
    elif bad == "off_boundary":
        data[0]["start"] = "2026-01-12T00:01:00+00:00"
    elif bad == "bool":
        data[0]["change"] = True
    else:
        data.append(data[0])
    async def reader(settings, start):
        return "Wh", data
    result = await MeasuredSolar(reader=reader).read(
        Settings(derive_solar_generation_entity="sensor.solar"), START,
        START + timedelta(minutes=30))
    assert result.reason is None
    assert result.values == {}


async def test_cache_single_flight_expiry_and_failure():
    calls = 0
    clock = [0.0]
    fail = False
    async def reader(settings, start):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        if fail:
            raise RuntimeError("hostile")
        return "Wh", rows()
    cache = MeasuredSolar(reader=reader, clock=lambda: clock[0])
    settings = Settings(derive_solar_generation_entity="sensor.solar")
    async def read():
        return await cache.read(settings, START, START + timedelta(minutes=30))
    await asyncio.gather(read(), read(), read())
    await read()
    assert calls == 1
    clock[0] = 300
    fail = True
    assert (await read()).reason == "read_failed"
    assert (await read()).reason == "read_failed"
    assert calls == 3
    fail = False
    assert (await read()).reason is None
    assert calls == 4


@pytest.mark.parametrize("unit,data,reason", [(None, [], "no_statistics"),
                         ("Wh", [], "no_statistics"), ("V", rows(), "unsupported_unit")])
async def test_unavailable(unit, data, reason):
    async def reader(settings, start):
        return unit, data
    result = await MeasuredSolar(reader=reader).read(
        Settings(derive_solar_generation_entity="sensor.solar"), START,
        START + timedelta(minutes=30))
    assert result.reason == reason


async def test_timeout_is_sanitised():
    async def reader(settings, start):
        await asyncio.sleep(10)
        return "Wh", rows()
    result = await MeasuredSolar(reader=reader).read(
        Settings(derive_solar_generation_entity="sensor.solar", ha_timeout=.001),
        START, START + timedelta(minutes=30))
    assert result.reason == "read_failed"


async def test_websocket_reader_requests_five_minute_rows(monkeypatch):
    calls = []

    async def metadata(url, token, **kwargs):
        calls.append("metadata")
        return [{"statistic_id": "sensor.solar", "unit_of_measurement": "Wh"}]

    async def statistics(url, token, entity, start, **kwargs):
        assert entity == "sensor.solar"
        assert start == START
        assert kwargs["period"] == "5minute"
        calls.append("rows")
        # Later rows returned by HA's open-ended command must be ignored.
        return rows() + [{"start": (START + timedelta(hours=1)).isoformat(), "change": 999}]

    monkeypatch.setattr("ha_spark.energy.solar_measured.list_statistic_ids", metadata)
    monkeypatch.setattr("ha_spark.energy.solar_measured.statistics_during_period", statistics)
    result = await MeasuredSolar().read(
        Settings(derive_solar_generation_entity="sensor.solar"), START,
        START + timedelta(minutes=30))
    assert result.values == {START: pytest.approx(.6)}
    assert calls == ["metadata", "rows"]
