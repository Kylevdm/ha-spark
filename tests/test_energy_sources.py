"""Tests for gathering planner inputs from HA."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import sources
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
    async def fake_load(_s: Settings) -> tuple[float, str]:
        return 24.0, "test"

    monkeypatch.setattr(sources, "predict_home_load_kwh", fake_load)

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
    async def fake_load(_s: Settings) -> tuple[float, str]:
        return 24.0, "test"

    monkeypatch.setattr(sources, "predict_home_load_kwh", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _ = await gather_inputs(s, rest)

    assert inputs.soc_now == 0.0
    assert cfg.voltage_v == s.battery_voltage_v  # fell back to config default
    assert inputs.dispatches == ()
