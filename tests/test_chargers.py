"""Tests for the charger abstraction and PROACTIVE_MODE gating."""

from __future__ import annotations

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.chargers import SolisCharger
from ha_spark.energy.models import ChargeAction, ChargePlan
from ha_spark.ha.rest import HomeAssistantRest


def _plan() -> ChargePlan:
    return ChargePlan(
        soc_now=30, capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
        load_kwh=24.2, cheap_covered_kwh=0.0, usable_now_kwh=2.69, required_kwh=12.8,
        target_soc=77, overnight_current_a=42, window_hours=6.0, ev_charging=False,
        ha_template_needed=19.0,
        actions=(
            ChargeAction("set_charge_current", "set current to 42 A", current_a=42),
            ChargeAction("stop_discharge", "turn inverter off 13:00-13:30"),
        ),
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
async def test_on_executes_service_calls() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    select_option = respx.post("http://ha.test/api/services/select/select_option").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = Settings(ha_url="http://ha.test", ha_token="t", proactive_mode="on")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert set_value.called
    assert select_option.called
    assert any("APPLIED" in line for line in lines)


async def test_off_mode_computes_without_calls() -> None:
    s = Settings(ha_url="http://ha.test", ha_token="t", proactive_mode="off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await SolisCharger(s, rest).apply(_plan())
    assert all("OFF" in line for line in lines)
