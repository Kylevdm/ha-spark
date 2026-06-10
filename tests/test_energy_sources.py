"""Tests for gathering planner inputs from HA."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.sources import gather_inputs
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
    async def fake_load(_s: Settings) -> LoadForecast:
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
    async def fake_load(_s: Settings) -> LoadForecast:
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


@respx.mock
async def test_solar_percentile_prefers_estimate_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings) -> LoadForecast:
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
    async def fake_load(_s: Settings) -> LoadForecast:
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
    async def fake_load(_s: Settings) -> LoadForecast:
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
