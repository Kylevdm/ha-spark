"""Tests for the charger abstraction and PROACTIVE_MODE gating."""

from __future__ import annotations

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.chargers import SolisCharger
from ha_spark.energy.models import ChargeAction, ChargePlan
from ha_spark.ha.rest import HomeAssistantRest


def _plan(*, soc_valid: bool = True) -> ChargePlan:
    return ChargePlan(
        soc_now=30, capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
        load_kwh=24.2, cheap_covered_kwh=0.0, usable_now_kwh=2.69,
        deficit_kwh=12.8, buffer_pct=0.0, required_kwh=12.8,
        target_soc=77, overnight_current_a=42, window_hours=6.0, ev_charging=False,
        ha_template_needed=19.0, soc_valid=soc_valid,
        actions=(
            ChargeAction("set_charge_current", "set current to 42 A", current_a=42),
            ChargeAction("stop_discharge", "turn inverter off 13:00-13:30"),
        ),
    )


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


@respx.mock
async def test_simulate_makes_no_service_calls() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = Settings(ha_url="http://ha.test", ha_token="t", proactive_mode="simulate")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert posts.call_count == 0
    assert any("SIMULATE" in line for line in lines)


@respx.mock
async def test_on_executes_service_calls_and_verifies_read_back() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    select_option = respx.post("http://ha.test/api/services/select/select_option").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        proactive_mode="on",
        charge_current_entity="number.solisac_timed_charge_current",
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    _mock_read_back(s, current="42.0", switch="Off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert set_value.called
    assert select_option.called
    assert all(line.startswith("[APPLIED]") for line in lines)


@respx.mock
async def test_on_warns_when_read_back_mismatches() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        proactive_mode="on",
        charge_current_entity="number.solisac_timed_charge_current",
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    _mock_read_back(s, current="0.0", switch="On")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert "read back 0 A (wanted 42 A)" in lines[0]
    assert lines[0].startswith("[WARNING]")
    assert "read back 'On' (wanted 'Off')" in lines[1]


@respx.mock
async def test_on_warns_when_read_back_read_fails() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    respx.route(method="GET").mock(return_value=httpx.Response(500))
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        proactive_mode="on",
        charge_current_entity="number.solisac_timed_charge_current",
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert all(line.startswith("[WARNING]") for line in lines)
    assert all("read-back failed" in line for line in lines)


@respx.mock
async def test_on_isolates_action_failures() -> None:
    respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(500)
    )
    select_option = respx.post("http://ha.test/api/services/select/select_option").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        proactive_mode="on",
        charge_current_entity="number.solisac_timed_charge_current",
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    _mock_read_back(s, current="42.0", switch="Off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert lines[0].startswith("[FAILED]")
    assert select_option.called  # the second action still ran
    assert lines[1].startswith("[APPLIED]")


@respx.mock
async def test_on_blocks_all_writes_when_soc_invalid() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        proactive_mode="on",
        charge_current_entity="number.solisac_timed_charge_current",
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan(soc_valid=False))
    assert posts.call_count == 0
    assert all(line.startswith("[BLOCKED] SoC unreadable") for line in lines)


@respx.mock
async def test_simulate_unaffected_by_invalid_soc() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = Settings(ha_url="http://ha.test", ha_token="t", proactive_mode="simulate")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan(soc_valid=False))
    assert posts.call_count == 0
    assert all(line.startswith("[SIMULATE]") for line in lines)


async def test_off_mode_computes_without_calls() -> None:
    s = Settings(ha_url="http://ha.test", ha_token="t", proactive_mode="off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert all("OFF" in line for line in lines)


@respx.mock
async def test_apply_action_executes_one_action_with_read_back() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        proactive_mode="on",
        charge_current_entity="number.solisac_timed_charge_current",
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    _mock_read_back(s, current="10.0", switch="Off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SolisCharger(s, rest).apply_action(
            ChargeAction("set_charge_current", "set current to 10 A", current_a=10)
        )
    assert set_value.called
    assert line == "[APPLIED] set current to 10 A"
