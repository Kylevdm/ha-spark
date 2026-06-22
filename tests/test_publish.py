"""Tests for publishing the computed plan to HA."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.models import ChargeIntent, ChargePlan
from ha_spark.energy.publish import plan_to_payload, publish_plan, republish_last
from ha_spark.ha.rest import HomeAssistantRest

BASE = "http://ha.test/api"


def _plan(**overrides: object) -> ChargePlan:
    defaults: dict[str, object] = dict(
        soc_now=40.0,
        capacity_kwh=26.88,
        solar_kwh=5.0,
        effective_solar_kwh=5.0,
        load_kwh=10.0,
        cheap_covered_kwh=0.0,
        usable_now_kwh=5.0,
        deficit_kwh=8.0,
        buffer_pct=20.0,
        required_kwh=9.6,
        target_soc=90.0,
        window_hours=6.0,
        ev_charging=False,
        ha_template_needed=None,
        charge_intent=ChargeIntent(
            target_soc_pct=90.0, soc_now=40.0, window_start=time(23, 30), window_end=time(5, 30)
        ),
    )
    defaults.update(overrides)
    return ChargePlan(**defaults)  # type: ignore[arg-type]


def test_plan_to_payload_maps_core_sensors() -> None:
    by_id = {eid: (state, attrs) for eid, state, attrs in plan_to_payload(_plan(), Settings())}
    assert by_id["sensor.ha_spark_target_soc"][0] == "90"
    assert by_id["sensor.ha_spark_target_soc"][1]["device_class"] == "battery"
    # optional cost sensors are omitted when the plan didn't cost itself
    assert "sensor.ha_spark_planned_cost" not in by_id


@respx.mock
async def test_publish_plan_pushes_required_entities(tmp_path: Path) -> None:
    for route in [
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_forecast_load_kwh",
        "sensor.ha_spark_solar_forecast_kwh",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_plan_status",
        "sensor.ha_spark_last_run",
    ]:
        respx.post(f"{BASE}/states/{route}").mock(return_value=httpx.Response(200, json={}))

    settings = Settings(db_path=str(tmp_path / "ha_spark.db"))
    async with HomeAssistantRest(BASE, "tok") as rest:
        await publish_plan(rest, _plan(), settings)


@respx.mock
async def test_publish_plan_skips_none_cost_fields(tmp_path: Path) -> None:
    for route in [
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_forecast_load_kwh",
        "sensor.ha_spark_solar_forecast_kwh",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_plan_status",
        "sensor.ha_spark_last_run",
    ]:
        respx.post(f"{BASE}/states/{route}").mock(return_value=httpx.Response(200, json={}))
    planned_route = respx.post(f"{BASE}/states/sensor.ha_spark_planned_cost").mock(
        return_value=httpx.Response(200, json={})
    )

    settings = Settings(db_path=str(tmp_path / "ha_spark.db"))
    async with HomeAssistantRest(BASE, "tok") as rest:
        await publish_plan(rest, _plan(planned_cost=None, baseline_cost=None), settings)

    assert planned_route.call_count == 0


@respx.mock
async def test_republish_last_replays_cached_payload(tmp_path: Path) -> None:
    for route in [
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_forecast_load_kwh",
        "sensor.ha_spark_solar_forecast_kwh",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_plan_status",
        "sensor.ha_spark_last_run",
    ]:
        respx.post(f"{BASE}/states/{route}").mock(return_value=httpx.Response(200, json={}))

    settings = Settings(db_path=str(tmp_path / "ha_spark.db"))
    async with HomeAssistantRest(BASE, "tok") as rest:
        await publish_plan(rest, _plan(), settings)

    calls_before = len(respx.calls)
    async with HomeAssistantRest(BASE, "tok") as rest:
        await republish_last(rest, settings)

    assert len(respx.calls) - calls_before == 8


@respx.mock
async def test_republish_last_noop_when_no_cache(tmp_path: Path) -> None:
    settings = Settings(db_path=str(tmp_path / "missing" / "ha_spark.db"))
    async with HomeAssistantRest(BASE, "tok") as rest:
        await republish_last(rest, settings)  # should not raise, nothing mocked to call
