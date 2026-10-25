"""Plan numbers render on registered site sensors, with conservative availability."""

from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.sensor import DESCRIPTIONS, SparkSensorDescription
from custom_components.ha_spark.wire import MalformedSummary, parse_summary
from tests_ha.test_polling import URL, fixture, setup, state_value, summary

LAST = "sensor.ha_spark_last_run"


def entity_id(desc: SparkSensorDescription) -> str:
    return desc.requested_entity_id or "sensor.ha_spark_next_planned_change"


async def test_golden_sensors_and_legacy_ids(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    for desc in DESCRIPTIONS:
        if desc.requested_entity_id:
            hass.states.async_set(desc.requested_entity_id, "legacy")
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    # (state, unit, device class, state class), fixed independently of DESCRIPTIONS.
    expected = {
        "sensor.ha_spark_charge_needed_kwh": ("9.6", "kWh", "energy", None),
        "sensor.ha_spark_target_soc": ("90.0", "%", "battery", None),
        "sensor.ha_spark_forecast_load_kwh": ("10.0", "kWh", "energy", None),
        "sensor.ha_spark_solar_forecast_kwh": ("5.0", "kWh", "energy", None),
        "sensor.ha_spark_deficit_kwh": ("8.0", "kWh", "energy", None),
        "sensor.ha_spark_planned_cost": ("1.23", "GBP", "monetary", None),
        "sensor.ha_spark_baseline_cost": ("3.46", "GBP", "monetary", None),
        "sensor.ha_spark_v2l_energy_kwh": ("2.35", "kWh", "energy", "total_increasing"),
        "sensor.ha_spark_v2l_net_saving_gbp": ("0.47", "GBP", "monetary", None),
        "sensor.ha_spark_last_run": ("2026-10-24T11:30:00+00:00", None, "timestamp", None),
        "sensor.ha_spark_plan_status": ("idle", None, "enum", None),
        "sensor.ha_spark_next_planned_change": (
            "2026-10-24T22:30:00+00:00", None, "timestamp", None),
    }
    assert {entity_id(desc) for desc in DESCRIPTIONS} == set(expected)
    for desc in DESCRIPTIONS:
        value, unit, device_class, state_class = expected[entity_id(desc)]
        state = hass.states.get(entity_id(desc))
        assert state is not None and state.state == value
        assert state.attributes.get("device_class") == device_class
        assert state.attributes.get("unit_of_measurement") == unit
        assert state.attributes.get("state_class") == state_class
        registered = er.async_get(hass).async_get(entity_id(desc))
        assert registered is not None
        assert registered.unique_id == f"{entry.data['site_id']}_{desc.key}"
        assert hass.states.get(entity_id(desc) + "_2") is None
    status = hass.states.get("sensor.ha_spark_plan_status")
    assert status is not None
    assert status.attributes["options"] == ["hold", "export", "charge", "idle"]
    for key, value in fixture("plan_summary")["plan"]["status"].items():
        if key != "action" and value is not None:
            assert status.attributes[key] == value
    assert "operating_mode" not in status.attributes
    assert "export_soc_needed_pct" not in status.attributes
    next_change = hass.states.get("sensor.ha_spark_next_planned_change")
    assert next_change is not None
    for key in ("action", "reason"):
        assert next_change.attributes[key] == fixture("plan_summary")["plan"]["next_change"][key]
    saving = hass.states.get("sensor.ha_spark_v2l_net_saving_gbp")
    assert saving is not None
    for key in ("avoided_gbp", "refill_cost_gbp", "peak_power_w"):
        assert saving.attributes[key] == fixture("plan_summary")["v2l"][key]


@pytest.mark.parametrize("change", ["soc", "v2l", "plan", "number", "401", "deadline"])
async def test_sensor_value_rules(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, change: str,
    freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    last = state_value(hass, LAST)
    body = summary()
    later = "2026-10-24T11:45:00+00:00"
    if body["plan"] is not None:
        body["plan"]["computed_at"] = later
    if change == "soc":
        body["plan"]["soc_trusted"] = False
    elif change in {"v2l", "plan"}:
        body[change] = None
    elif change == "number":
        body["plan"]["charge_needed_kwh"] = None
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=body, status=401 if change == "401" else 200)
    await entry.runtime_data.async_refresh()
    if change == "deadline":
        # Drive the public deadline timer with HA's clock.
        from datetime import timedelta

        from homeassistant.util import dt as dt_util
        from pytest_homeassistant_custom_component.common import async_fire_time_changed

        freezer.tick(timedelta(seconds=121))
        aioclient_mock.clear_requests()
        aioclient_mock.post(URL, exc=aiohttp.ClientError())
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    for desc in DESCRIPTIONS:
        value = state_value(hass, entity_id(desc))
        if desc.key == "last_run":
            # Only a counted summary with a plan advances Last run; nothing clears it.
            advanced = change not in {"401", "plan"}
            assert value == (later if advanced else last)
        elif change in {"401", "deadline"} or (change == "soc" and desc.soc_derived) or (
            change == "v2l" and desc.v2l
        ):
            assert value == "unavailable"
        elif (change == "plan" and not desc.v2l) or (
            change == "number" and desc.key == "charge_needed"
        ):
            assert value == "unknown"
        else:
            assert value not in {"unknown", "unavailable"}


async def test_last_run_restores_while_down(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    timestamp = fixture("plan_summary")["plan"]["computed_at"]
    mock_restore_cache_with_extra_data(hass, [(State(LAST, timestamp), {
        "native_value": {"__type": "<class 'datetime.datetime'>", "isoformat": timestamp},
        "native_unit_of_measurement": None,
    })])
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    await setup(hass)
    assert state_value(hass, LAST) == timestamp
    for desc in DESCRIPTIONS:
        if desc.key != "last_run":
            assert state_value(hass, entity_id(desc)) == "unavailable"


@pytest.mark.parametrize("block,key,value", [
    ("plan", "charge_needed_kwh", True), ("plan", "target_soc_pct", "90"),
    ("plan", "deficit_kwh", float("nan")), ("v2l", "energy_kwh", False),
    ("v2l", "peak_power_w", float("inf")), ("v2l", "net_saving_gbp", "1"),
    ("plan", "computed_at", "2026-10-24T11:30:00"), ("plan", "soc_trusted", 1),
    ("plan", "deficit_kwh", type("Float", (float,), {})(1.0)),
    ("v2l", "energy_kwh", type("Int", (int,), {})(1)),
])
def test_invalid_number_blocks(block: str, key: str, value: Any) -> None:
    body = fixture("plan_summary")
    body[block][key] = value
    with pytest.raises(MalformedSummary):
        parse_summary(body)


async def test_registered_requested_ids_are_not_cleared(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    registry = er.async_get(hass)
    for desc in DESCRIPTIONS:
        registry.async_get_or_create("sensor", "other", desc.key,
                                     suggested_object_id=entity_id(desc).split(".")[1])
        hass.states.async_set(entity_id(desc), "owner-state")
    aioclient_mock.post(URL, json=summary())
    await setup(hass)
    for desc in DESCRIPTIONS:
        assert state_value(hass, entity_id(desc)) == "owner-state"
        assert hass.states.get(entity_id(desc) + "_2") is not None


def test_informational_fields_are_ignored_in_number_blocks() -> None:
    body = fixture("plan_summary")
    expected = parse_summary(body)
    body["plan"]["future_display"] = {"anything": True}
    body["v2l"]["future_display"] = [1, 2]
    assert parse_summary(body) == expected


async def test_no_next_change_is_unknown_and_export_soc_attributes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    body = summary()
    body["plan"]["next_change"] = None
    body["plan"]["status"].update({
        "export_soc_needed_pct": 79.0, "export_soc_needed_at": "2026-10-24T14:00:00+00:00",
    })
    aioclient_mock.post(URL, json=body)
    await setup(hass)
    state = hass.states.get("sensor.ha_spark_next_planned_change")
    assert state is not None and state.state == "unknown"
    assert "action" not in state.attributes and "reason" not in state.attributes
    status = hass.states.get("sensor.ha_spark_plan_status")
    assert status is not None
    assert status.attributes["export_soc_needed_pct"] == 79
    assert status.attributes["export_soc_needed_at"] == "2026-10-24T14:00:00+00:00"


@pytest.mark.parametrize("block,key,value", [
    ("status", "action", "simulate"), ("status", "reason", "unsafe\u0001"),
    ("status", "reason", ""), ("status", "reason", "x" * 256),
    ("status", "model", "daily-v2"), ("status", "strategy", "FILL"),
    ("status", "soc_status", "x" * 33), ("status", "soc_reason", "unsafe\n"),
    ("status", "export_soc_needed_pct", 50),
    ("next_change", "at", "2026-10-24T12:00:00"),
    ("next_change", "action", "invalid"), ("next_change", "reason", "unsafe\u0000"),
])
async def test_malformed_status_is_no_heartbeat(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    block: str, key: str, value: Any,
) -> None:
    body = summary()
    body["plan"][block][key] = value
    aioclient_mock.post(URL, json=body)
    entry = await setup(hass)
    assert entry.runtime_data.last_outcome == "malformed"
    for entity in ("sensor.ha_spark_plan_status", "sensor.ha_spark_next_planned_change"):
        assert state_value(hass, entity) == "unavailable"


class _UnhashableStr(str):
    __hash__ = None  # type: ignore[assignment]


@pytest.mark.parametrize("key,value", [
    ("action", _UnhashableStr("idle")), ("action", type("S", (str,), {})("idle")),
    ("reason", type("S", (str,), {})("Fine.")), ("action", None),
])
def test_status_text_is_exact_str(key: str, value: Any) -> None:
    body = fixture("plan_summary")
    body["plan"]["status"][key] = value
    with pytest.raises(MalformedSummary):
        parse_summary(body)


async def test_unresolved_action_shows_unknown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    body = summary()
    body["plan"]["status"]["action"] = body["plan"]["status"]["reason"] = None
    aioclient_mock.post(URL, json=body)
    entry = await setup(hass)
    assert entry.runtime_data.last_outcome == "counted"
    assert state_value(hass, "sensor.ha_spark_plan_status") == "unknown"
