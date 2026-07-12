"""Tests for the live supply guard (throttle math in watts + tick behaviour)."""

from __future__ import annotations

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.devices import inverter_device
from ha_spark.devices.base import Capability
from ha_spark.energy.scheduler import guard_tick
from ha_spark.energy.supply_guard import SupplyGuard, throttled_rate_w
from ha_spark.ha.rest import HomeAssistantRest

# Shared electrical model for the math tests: 240 V supply, 75 A limit ->
# 18000 W ceiling. 51 V battery so watts <-> amps for the tick read-backs.
_KW = dict(limit_a=75.0, supply_voltage_v=240.0)


def _guard_settings(**overrides: object) -> Settings:
    return Settings(
        ha_url="http://ha.test",
        ha_token="t",
        grid_power_entity="sensor.house_supply_power",
        battery_voltage_v=51.0,
        supply_voltage_v=240.0,
        supply_max_current_a=75.0,
        **overrides,  # type: ignore[arg-type]
    )


def _mock_state(entity: str, state: str) -> None:
    respx.get(f"http://ha.test/api/states/{entity}").mock(
        return_value=httpx.Response(
            200, json={"entity_id": entity, "state": state, "attributes": {}}
        )
    )


# --- throttled_rate_w math (watts in, watts out; no DC<->AC amp mixing) ---


def test_throttle_subtracts_battery_from_supply() -> None:
    # 240 V, limit 75 A -> 18000 W. supply 16000 W incl. battery 4000 W ->
    # other load 12000 W; headroom 6000 W; capped at target 4000 W -> 4000.
    assert throttled_rate_w(16000, 4000, 4000, **_KW) == 4000


def test_throttle_sheds_when_over_limit() -> None:
    # supply 20000 W incl. battery 4000 W -> other load 16000 W; limit 18000 W
    # -> headroom 2000 W < target 4000 -> 2000.
    assert throttled_rate_w(20000, 4000, 4000, **_KW) == 2000


def test_throttle_restores_to_target_under_limit() -> None:
    # House draws 5 kW total incl. battery 510 W -> plenty of headroom.
    assert throttled_rate_w(5000, 510, 4000, **_KW) == 4000


def test_throttle_clamps_at_zero_on_extreme_overload() -> None:
    assert throttled_rate_w(30000, 4000, 4000, **_KW) == 0.0


def test_throttle_subtracts_own_contribution_no_self_oscillation() -> None:
    # Exactly at the limit with the battery charging: the battery's own draw
    # must not count as "other load", so the setpoint should hold, not shed.
    assert throttled_rate_w(18000, 4000, 4000, **_KW) == 4000


def test_throttle_export_counts_as_headroom() -> None:
    # Negative supply power (exporting) -> maximal headroom, capped at target.
    assert throttled_rate_w(-3000, 0, 4000, **_KW) == 4000


# --- SupplyGuard.tick (watts, talking to the charger's rate methods) ---


@respx.mock
async def test_tick_skips_small_deltas_without_writing() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="on")
    _mock_state(s.grid_power_entity, "5000")  # well under the limit
    _mock_state(s.charge_current_entity, "40")  # 40 A * 51 V = 2040 W, already at target
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_w=2040.0)
    assert line is None
    assert posts.call_count == 0


@respx.mock
async def test_tick_simulate_logs_but_does_not_write() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="simulate")
    _mock_state(s.grid_power_entity, "20000")  # over the limit
    _mock_state(s.charge_current_entity, "40")  # setpoint 2040 W
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_w=2040.0)
    # other load ~17960 W; headroom ~40 W -> shed to ~40 W (~1 A).
    assert line is not None and line.startswith("[SIMULATE]")
    assert "set charge current" in line
    assert posts.call_count == 0


@respx.mock
async def test_tick_on_writes_and_verifies_read_back() -> None:
    set_value = respx.post("http://ha.test/api/services/number/set_value").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _guard_settings(proactive_mode="on")
    _mock_state(s.grid_power_entity, "20000")
    _mock_state(s.charge_current_entity, "40")  # also serves the read-back
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_w=2040.0)
    # 20 kW incl. 2040 W battery -> other load ~17960, headroom ~40 W -> ~1 A.
    # A real write, then the read-back against the (still 40 A) mock warns.
    assert set_value.called
    assert line is not None and line.startswith("[WARNING]")
    assert "set charge current to 1 A" in line


@respx.mock
async def test_tick_restores_toward_target_when_headroom_returns() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="simulate")
    _mock_state(s.grid_power_entity, "3000")  # EV gone, light house load
    _mock_state(s.charge_current_entity, "5")  # previously throttled (255 W)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_w=2040.0)
    # headroom huge -> restore to target 2040 W (40 A).
    assert line is not None and "set charge current to 40 A (2040 W)" in line
    assert posts.call_count == 0


@respx.mock
async def test_tick_unreadable_grid_sensor_does_nothing() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="on")
    respx.get(f"http://ha.test/api/states/{s.grid_power_entity}").mock(
        return_value=httpx.Response(500)
    )
    _mock_state(s.charge_current_entity, "40")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_w=2040.0)
    assert line is None
    assert posts.call_count == 0


@respx.mock
async def test_tick_unreadable_setpoint_does_nothing() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _guard_settings(proactive_mode="on")
    _mock_state(s.grid_power_entity, "20000")
    respx.get(f"http://ha.test/api/states/{s.charge_current_entity}").mock(
        return_value=httpx.Response(500)
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await SupplyGuard(s, rest).tick(target_w=2040.0)
    assert line is None
    assert posts.call_count == 0


@respx.mock
async def test_guard_dormant_when_no_live_rate() -> None:
    # AlphaESS has no settable charge rate -> guard_tick no-ops: no reads, no
    # writes, just echoes the target back.
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    gets = respx.route(method="GET").mock(return_value=httpx.Response(200, json={}))
    s = _guard_settings(inverter="alphaess", proactive_mode="on")
    out = await guard_tick(s, 5000.0)
    assert out == 5000.0
    assert posts.call_count == 0
    assert gets.call_count == 0


def test_guard_dormant_for_inverter_without_rate() -> None:
    s = _guard_settings(inverter="alphaess")
    rest = HomeAssistantRest("http://ha.test", "tok")
    assert Capability.CHARGE_RATE not in inverter_device(s, rest).capabilities
