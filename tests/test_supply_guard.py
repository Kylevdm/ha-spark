"""Tests for the live supply guard (throttle math + tick behaviour)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.supply_guard import SupplyGuard, throttled_current
from ha_spark.ha.rest import HomeAssistantRest

# Shared electrical model for the math tests: 51 V battery on a 240 V supply
# with a 75 A limit. A 42 A DC setpoint draws ~8.9 A AC.
_KW = dict(battery_voltage_v=51.0, limit_a=75.0, supply_voltage_v=240.0)


def _guard_settings(**overrides: object) -> Settings:
    return Settings(
        ha_url="http://ha.test",
        ha_token="t",
        grid_power_entity="sensor.house_supply_power",
        **overrides,  # type: ignore[arg-type]
    )


def _mock_state(entity: str, state: str) -> None:
    respx.get(f"http://ha.test/api/states/{entity}").mock(
        return_value=httpx.Response(
            200, json={"entity_id": entity, "state": state, "attributes": {}}
        )
    )


# --- throttled_current math ---


def test_under_limit_restores_to_target() -> None:
    # House draws 5 kW total incl. battery at 10 A DC -> plenty of headroom.
    assert throttled_current(5000, 10, 42, **_KW) == 42


def test_over_limit_sheds_to_fit_headroom() -> None:
    # 20 kW total with the battery at 42 A DC (~2.14 kW). Other load ~74.4 A AC,
    # headroom ~0.6 A AC -> ~2.8 A DC.
    got = throttled_current(20000, 42, 42, **_KW)
    assert got == pytest.approx(2.78, abs=0.05)
    assert 0 < got < 42


def test_extreme_overload_clamps_at_zero() -> None:
    assert throttled_current(30000, 42, 42, **_KW) == 0.0


def test_subtracts_own_contribution_no_self_oscillation() -> None:
    # Exactly at the limit with the battery charging: the battery's own draw
    # must not count as "other load", so the setpoint should hold, not shed.
    assert throttled_current(75.0 * 240.0, 42, 42, **_KW) == pytest.approx(42)


def test_export_counts_as_headroom() -> None:
    # Negative supply power (exporting) -> maximal headroom, capped at target.
    assert throttled_current(-3000, 0, 42, **_KW) == 42


def test_degenerate_voltages_return_target() -> None:
    assert (
        throttled_current(20000, 42, 42, battery_voltage_v=0, limit_a=75, supply_voltage_v=240)
        == 42
    )
    assert (
        throttled_current(20000, 42, 42, battery_voltage_v=51, limit_a=75, supply_voltage_v=0)
        == 42
    )


# --- SupplyGuard.tick ---


@respx.mock
async def test_tick_skips_small_deltas_without_writing() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="on")
    _mock_state(s.grid_power_entity, "5000")  # well under the limit
    _mock_state(s.charge_current_entity, "42")  # already at target
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_a=42)
    assert line is None
    assert posts.call_count == 0


@respx.mock
async def test_tick_simulate_logs_but_does_not_write() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="simulate")
    _mock_state(s.grid_power_entity, "20000")  # over the limit
    _mock_state(s.charge_current_entity, "42")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_a=42)
    assert line is not None and line.startswith("[SIMULATE]")
    assert "throttle charge current" in line
    assert posts.call_count == 0


@respx.mock
async def test_tick_on_writes_and_verifies_read_back() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _guard_settings(proactive_mode="on")
    _mock_state(s.grid_power_entity, "20000")
    _mock_state(s.charge_current_entity, "3")  # also serves the read-back (wants 3 A)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_a=42)
    # 20 kW leaves no headroom -> throttle 3 -> 0 A: a real write, then read-back
    # against the (still 3 A) mock warns about the mismatch.
    assert set_value.called
    assert line is not None and line.startswith("[WARNING]")
    assert "throttle charge current 3 -> 0 A" in line


@respx.mock
async def test_tick_restores_toward_target_when_headroom_returns() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="simulate")
    _mock_state(s.grid_power_entity, "3000")  # EV gone, light house load
    _mock_state(s.charge_current_entity, "5")  # previously throttled
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_a=42)
    assert line is not None and "restore charge current 5 -> 42 A" in line
    assert posts.call_count == 0


@respx.mock
async def test_tick_unreadable_grid_sensor_does_nothing() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="on")
    respx.get(f"http://ha.test/api/states/{s.grid_power_entity}").mock(
        return_value=httpx.Response(500)
    )
    _mock_state(s.charge_current_entity, "42")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_a=42)
    assert line is None
    assert posts.call_count == 0
