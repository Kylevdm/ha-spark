"""Tests for gathering planner inputs from HA."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.sources import gather_inputs, pre_window_drain
from ha_spark.ha.rest import HomeAssistantRest

BASE = "http://ha.test/api"


def _state(eid: str, state: str, attrs: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"entity_id": eid, "state": state, "attributes": attrs or {}}
    )


def _settings() -> Settings:
    return Settings(
        ha_url="http://ha.test",
        ha_token="t",
        soc_entity="sensor.soc",
        battery_voltage_entity="sensor.volt",
        solar_tomorrow_entity="sensor.solar",
        dispatch_entity="binary_sensor.dispatch",
        ev_status_entity="sensor.ev",
        ha_template_charge_needed_entity="sensor.tmpl",
    )


@respx.mock
async def test_gather_inputs_parses_live_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)

    respx.get(f"{BASE}/states/sensor.soc").mock(return_value=_state("sensor.soc", "30"))
    respx.get(f"{BASE}/states/sensor.volt").mock(return_value=_state("sensor.volt", "51"))
    respx.get(f"{BASE}/states/sensor.solar").mock(return_value=_state("sensor.solar", "8.75"))
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", "Charging"))
    respx.get(f"{BASE}/states/sensor.tmpl").mock(return_value=_state("sensor.tmpl", "19.0"))
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state(
            "binary_sensor.dispatch",
            "off",
            {
                "planned_dispatches": [
                    {"start": "2026-06-08T13:00:00+01:00", "end": "2026-06-08T13:30:00+01:00",
                     "charge_in_kwh": -2.0, "source": "SMART"}
                ]
            },
        )
    )

    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, load_source = await gather_inputs(s, rest)

    assert inputs.soc_now == 30.0
    assert inputs.soc_valid is True
    assert cfg.voltage_v == 51.0
    assert inputs.solar_tomorrow_kwh == 8.75
    assert inputs.predicted_home_load_kwh == 24.0
    assert inputs.ev_charging is True
    assert inputs.ha_template_needed == 19.0
    assert len(inputs.dispatches) == 1
    assert inputs.dispatches[0].source == "SMART"
    assert load_source == "test"


@respx.mock
async def test_gather_inputs_tolerates_missing_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _ = await gather_inputs(s, rest)

    assert inputs.soc_now == 0.0
    assert inputs.soc_valid is False
    assert cfg.voltage_v == s.battery_voltage_v  # fell back to config default
    assert inputs.dispatches == ()


def test_pre_window_drain_daily_fallback() -> None:
    fc = LoadForecast(total_kwh=24.0, slots=None, source="t")
    now = datetime(2026, 6, 10, 22, 0)
    # 1.5 h until 23:30 at 1 kW average.
    assert pre_window_drain(fc, now, time(23, 30), time(5, 30)) == pytest.approx(1.5)


def test_pre_window_drain_sums_slots_with_proration() -> None:
    fc = LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="t")
    now = datetime(2026, 6, 10, 22, 45)
    # Half of the 22:30 slot (0.25) plus the full 23:00 slot (0.5).
    assert pre_window_drain(fc, now, time(23, 30), time(5, 30)) == pytest.approx(0.75)


def test_pre_window_drain_zero_inside_window_or_far_away() -> None:
    fc = LoadForecast(total_kwh=24.0, slots=None, source="t")
    inside = datetime(2026, 6, 11, 0, 30)  # window wraps midnight
    assert pre_window_drain(fc, inside, time(23, 30), time(5, 30)) == 0.0
    far = datetime(2026, 6, 10, 9, 0)  # 14.5 h before the window opens
    assert pre_window_drain(fc, far, time(23, 30), time(5, 30)) == 0.0


@respx.mock
async def test_solar_percentile_prefers_estimate_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/sensor.solar").mock(
        return_value=_state("sensor.solar", "8.75", {"estimate10": 5.5, "estimate90": 12.0})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(**{**_settings().model_dump(), "solar_percentile": 10})
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)
    assert inputs.solar_tomorrow_kwh == 5.5


@respx.mock
async def test_solar_percentile_falls_back_to_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/sensor.solar").mock(
        return_value=_state("sensor.solar", "8.75")  # no estimate10 attribute
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(**{**_settings().model_dump(), "solar_percentile": 10})
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)
    assert inputs.solar_tomorrow_kwh == 8.75


def test_parse_detailed_forecast_percentile_key() -> None:
    raw = [
        {"period_start": "2026-06-11T12:00:00+01:00", "pv_estimate": 2.0, "pv_estimate10": 1.0}
    ]
    assert sources._parse_detailed_forecast(raw, 50) == [
        (sources.datetime.fromisoformat("2026-06-11T12:00:00+01:00"), 2.0)
    ]
    assert sources._parse_detailed_forecast(raw, 10) == [
        (sources.datetime.fromisoformat("2026-06-11T12:00:00+01:00"), 1.0)
    ]
    # Missing percentile key falls back to the median estimate.
    bare = [{"period_start": "2026-06-11T12:00:00+01:00", "pv_estimate": 2.0}]
    parsed = sources._parse_detailed_forecast(bare, 10)
    assert parsed is not None and parsed[0][1] == 2.0


@respx.mock
async def test_gather_inputs_flags_unavailable_soc(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/sensor.soc").mock(
        return_value=_state("sensor.soc", "unavailable")
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)

    assert inputs.soc_now == 0.0
    assert inputs.soc_valid is False


@respx.mock
async def test_quantile_buffer_derived_from_ml_p90(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(
            total_kwh=20.0, slots=None, source="ml quantile gbr", p90_total_kwh=23.0
        )

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t", buffer_mode="quantile")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        _, cfg, _ = await gather_inputs(s, rest)
    assert cfg.buffer_pct == pytest.approx(15.0)  # (23/20 - 1) * 100


@respx.mock
async def test_fixed_buffer_ignores_p90(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(
            total_kwh=20.0, slots=None, source="ml quantile gbr", p90_total_kwh=23.0
        )

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t", charge_buffer_pct=20.0)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        _, cfg, _ = await gather_inputs(s, rest)
    assert cfg.buffer_pct == 20.0


@respx.mock
async def test_gather_inputs_reads_location_from_ha_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_load(_s: Settings, **kw: object) -> LoadForecast:
        seen.update(kw)
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/config").mock(
        return_value=httpx.Response(200, json={"latitude": 51.5, "longitude": -0.1})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        await gather_inputs(s, rest)
    assert seen == {"lat": 51.5, "lon": -0.1}


@respx.mock
async def test_explicit_coordinates_override_ha_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_load(_s: Settings, **kw: object) -> LoadForecast:
        seen.update(kw)
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t", latitude=55.9, longitude=-3.2)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        await gather_inputs(s, rest)
    assert seen == {"lat": 55.9, "lon": -3.2}
