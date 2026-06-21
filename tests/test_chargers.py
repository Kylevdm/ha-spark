"""Tests for the charger abstraction and PROACTIVE_MODE gating."""

from __future__ import annotations

import json
from datetime import time

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.chargers import AlphaESSCharger, SolisCharger, charger_for, solis_current_a
from ha_spark.energy.models import ChargeIntent
from ha_spark.ha.rest import HomeAssistantRest


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "ha_url": "http://ha.test",
        "ha_token": "t",
        "proactive_mode": "simulate",
        "charge_current_entity": "number.solisac_timed_charge_current",
        "inverter_power_switch_entity": "select.solisac_power_switch",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _intent(
    target_soc: float = 77.0, soc_now: float = 50.0, holds: tuple[tuple, ...] = ()
) -> ChargeIntent:
    return ChargeIntent(target_soc, soc_now, time(23, 30), time(5, 30), holds=holds)


def _state(entity_id: str, state: str) -> dict[str, object]:
    return {"entity_id": entity_id, "state": state, "attributes": {}}


def _mock_read_back(settings: Settings, current: str, switch: str) -> None:
    respx.get(f"http://ha.test/api/states/{settings.charge_current_entity}").mock(
        return_value=httpx.Response(
            200, json=_state(settings.charge_current_entity, current)
        )
    )
    respx.get(f"http://ha.test/api/states/{settings.inverter_power_switch_entity}").mock(
        return_value=httpx.Response(
            200, json=_state(settings.inverter_power_switch_entity, switch)
        )
    )


def test_solis_current_matches_legacy_sizing() -> None:
    # capacity 26.88 kWh, eff 0.90, voltage 51 V, 6.0 h window, max 62.5 A.
    # needed = (77-50)/100*26.88 = 7.2576 kWh; buy = 7.2576/0.9 = 8.064 kWh;
    # kwh_per_amp = 6.0*51/1000 = 0.306; amps = 8.064/0.306 = 26.35 A.
    s = _settings(
        battery_capacity_kwh=26.88,
        charge_efficiency=0.90,
        battery_voltage_v=51.0,
        max_charge_current_a=62.5,
    )
    assert solis_current_a(_intent(), s) == pytest.approx(26.35, abs=0.05)


def test_solis_current_clamps_to_max() -> None:
    s = _settings(
        battery_capacity_kwh=26.88,
        charge_efficiency=0.90,
        battery_voltage_v=51.0,
        max_charge_current_a=10.0,
    )
    assert solis_current_a(_intent(target_soc=90.0), s) == 10.0


def test_supports_live_rate_true() -> None:
    s = _settings()
    rest = HomeAssistantRest(s.ha_rest_url, s.auth_token)
    assert SolisCharger(s, rest).supports_live_rate is True


@respx.mock
async def test_apply_writes_charge_current() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on")
    intent = _intent()
    expected_a = round(solis_current_a(intent, s))
    _mock_read_back(s, current=f"{expected_a}.0", switch="Off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(intent)
    assert set_value.called
    posted = set_value.calls.last.request.content
    assert f'"value":{expected_a}'.encode() in posted or f'"value": {expected_a}'.encode() in posted
    assert any(line.startswith("[APPLIED]") for line in lines)


@respx.mock
async def test_simulate_makes_no_service_calls() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="simulate")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_intent())
    assert posts.call_count == 0
    assert any("SIMULATE" in line for line in lines)


@respx.mock
async def test_on_executes_service_calls_and_verifies_read_back() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on")
    intent = _intent()
    expected_a = round(solis_current_a(intent, s))
    _mock_read_back(s, current=f"{expected_a}.0", switch="Off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(intent)
    assert set_value.called
    assert all(line.startswith("[APPLIED]") or line.startswith("[SKIP]") for line in lines)


@respx.mock
async def test_on_warns_when_read_back_mismatches() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    intent = _intent()
    _mock_read_back(s, current="0.0", switch="On")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(intent)
    current_line = next(line for line in lines if "set timed charge current" in line)
    assert current_line.startswith("[WARNING]")
    expected_a = round(solis_current_a(intent, s))
    assert current_line.endswith(f"read back 0 A (wanted {expected_a:g} A)")


@respx.mock
async def test_on_warns_when_read_back_read_fails() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    respx.route(method="GET").mock(return_value=httpx.Response(500))
    s = _settings(proactive_mode="on")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_intent())
    current_line = next(line for line in lines if "set timed charge current" in line)
    assert current_line.startswith("[WARNING]")
    assert "read-back failed" in current_line


@respx.mock
async def test_on_isolates_action_failures() -> None:
    respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(500)
    )
    s = _settings(proactive_mode="on")
    intent = _intent(holds=(), target_soc=77.0, soc_now=50.0)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(intent)
    current_line = next(line for line in lines if "set timed charge current" in line)
    assert current_line.startswith("[FAILED]")


@respx.mock
async def test_on_blocks_all_writes_when_soc_invalid() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    intent = _intent(soc_now=0)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(intent)
    assert posts.call_count == 0
    assert all(line.startswith("[BLOCKED] SoC unreadable") for line in lines)


@respx.mock
async def test_simulate_unaffected_by_invalid_soc() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="simulate")
    intent = _intent(soc_now=0)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(intent)
    assert posts.call_count == 0
    assert all(line.startswith("[SIMULATE]") or line.startswith("[SKIP]") for line in lines)


async def test_off_mode_computes_without_calls() -> None:
    s = _settings(proactive_mode="off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_intent())
    assert all("OFF" in line or "SKIP" in line for line in lines)


def test_planned_rate_w_matches_current_times_voltage() -> None:
    s = _settings(
        battery_capacity_kwh=26.88,
        charge_efficiency=0.90,
        battery_voltage_v=51.0,
        max_charge_current_a=62.5,
    )
    intent = _intent()
    rest = HomeAssistantRest(s.ha_rest_url, s.auth_token)
    expected = solis_current_a(intent, s) * s.battery_voltage_v
    assert SolisCharger(s, rest).planned_rate_w(intent) == pytest.approx(expected)


@respx.mock
async def test_set_charge_rate_posts_amps_and_applies() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on", battery_voltage_v=51.0)
    _mock_read_back(s, current="40.0", switch="Off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SolisCharger(s, rest).set_charge_rate(2040.0)
    assert set_value.called
    posted = set_value.calls.last.request.content
    assert b'"value":40' in posted or b'"value": 40' in posted
    assert line.startswith("[APPLIED]")


@respx.mock
async def test_read_charge_rate_converts_amps_to_watts() -> None:
    s = _settings(battery_voltage_v=51.0)
    respx.get(f"http://ha.test/api/states/{s.charge_current_entity}").mock(
        return_value=httpx.Response(200, json=_state(s.charge_current_entity, "30"))
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        watts = await SolisCharger(s, rest).read_charge_rate()
    assert watts == pytest.approx(30 * 51.0)


@respx.mock
async def test_read_charge_rate_raises_on_unreadable_sensor() -> None:
    s = _settings()
    respx.get(f"http://ha.test/api/states/{s.charge_current_entity}").mock(
        return_value=httpx.Response(500)
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        with pytest.raises(httpx.HTTPStatusError):
            await SolisCharger(s, rest).read_charge_rate()


def test_charger_for_selects_by_inverter() -> None:
    rest = HomeAssistantRest(_settings().ha_rest_url, _settings().auth_token)
    assert isinstance(charger_for(_settings(inverter="solis"), rest), SolisCharger)
    assert isinstance(charger_for(_settings(inverter="alphaess"), rest), AlphaESSCharger)


def test_alphaess_does_not_support_live_rate() -> None:
    s = _settings(inverter="alphaess")
    rest = HomeAssistantRest(s.ha_rest_url, s.auth_token)
    assert AlphaESSCharger(s, rest).supports_live_rate is False


@respx.mock
async def test_alphaess_apply_writes_window_and_stop_soc() -> None:
    # mode "on": one alphaess.setbatterycharge call with the window + stop-SOC.
    route = respx.post("http://ha.test/api/services/alphaess/setbatterycharge").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(inverter="alphaess", proactive_mode="on", alphaess_serial="ABC123")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await AlphaESSCharger(s, rest).apply(_intent(target_soc=80.0))
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["serial"] == "ABC123"
    assert body["enabled"] is True
    assert body["cp1start"] == "23:30"
    assert body["cp1end"] == "05:30"
    assert body["chargeStopSOC"] == 80
    assert "[APPLIED]" in lines[0]


@respx.mock
async def test_alphaess_apply_simulate_makes_no_call() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(inverter="alphaess", proactive_mode="simulate")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await AlphaESSCharger(s, rest).apply(_intent())
    assert posts.call_count == 0
    assert "[SIMULATE]" in lines[0]


async def test_alphaess_apply_off_mode_computes_without_calls() -> None:
    s = _settings(inverter="alphaess", proactive_mode="off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await AlphaESSCharger(s, rest).apply(_intent())
    assert "[OFF]" in lines[0]


@respx.mock
async def test_alphaess_apply_blocks_when_soc_invalid() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(inverter="alphaess", proactive_mode="on")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await AlphaESSCharger(s, rest).apply(_intent(soc_now=0))
    assert posts.call_count == 0
    assert "[BLOCKED]" in lines[0]


@respx.mock
async def test_alphaess_apply_isolates_failure() -> None:
    respx.post("http://ha.test/api/services/alphaess/setbatterycharge").mock(
        return_value=httpx.Response(500)
    )
    s = _settings(inverter="alphaess", proactive_mode="on", alphaess_serial="ABC123")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await AlphaESSCharger(s, rest).apply(_intent())
    assert "[FAILED]" in lines[0]


async def test_alphaess_set_charge_rate_and_read_charge_rate_are_noops() -> None:
    s = _settings(inverter="alphaess")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        charger = AlphaESSCharger(s, rest)
        assert "[SKIP]" in await charger.set_charge_rate(1000.0)
        assert await charger.read_charge_rate() == 0.0
        assert charger.planned_rate_w(_intent()) == 0.0
