"""Tests for publishing the computed plan to HA."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.models import ChargeIntent, ChargePlan
from ha_spark.energy.publish import (
    plan_to_payload,
    publish_plan,
    publish_soc_integrity,
    republish_last,
)
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.rest import HomeAssistantRest


def _soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )

BASE = "http://ha.test/api"


def _plan(**overrides: object) -> ChargePlan:
    defaults: dict[str, object] = dict(
        soc=_soc(40.0),
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
            target_soc_pct=90.0, soc=_soc(40.0), window_start=time(23, 30), window_end=time(5, 30)
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
    cached = json.loads((tmp_path / "ha_spark_published.json").read_text(encoding="utf-8"))
    cached_by_id = {entity_id: state for entity_id, state, _ in cached}
    async with HomeAssistantRest(BASE, "tok") as rest:
        await republish_last(rest, settings)

    assert len(respx.calls) - calls_before == 8
    assert cached_by_id["sensor.ha_spark_soc_now"] == "40"
    assert cached_by_id["sensor.ha_spark_charge_needed_kwh"] == "9.60"
    replayed = {
        call.request.url.path.rsplit("/", 1)[-1]: json.loads(call.request.content)["state"]
        for call in respx.calls[calls_before:]
    }
    assert replayed["sensor.ha_spark_soc_now"] == "40"
    assert replayed["sensor.ha_spark_charge_needed_kwh"] == "9.60"


@respx.mock
async def test_untrusted_soc_is_published_and_replayed_as_unavailable(tmp_path: Path) -> None:
    bad = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=datetime.now(UTC),
        value=70.0,
        raw_state="70",
        age_s=746.0,
        max_age_s=600.0,
    )
    plan = _plan(soc=bad, planned_cost=8.25, baseline_cost=10.5)
    settings = Settings(db_path=str(tmp_path / "ha_spark.db"))
    payload = plan_to_payload(plan, settings)
    routes = {
        entity_id: respx.post(f"{BASE}/states/{entity_id}").mock(
            return_value=httpx.Response(200, json={})
        )
        for entity_id, _, _ in payload
    }

    async with HomeAssistantRest(BASE, "tok") as rest:
        await publish_plan(rest, plan, settings)

    cache = json.loads((tmp_path / "ha_spark_published.json").read_text(encoding="utf-8"))
    cached = {entity_id: state for entity_id, state, _ in cache}
    calls_before_replay = len(respx.calls)
    async with HomeAssistantRest(BASE, "tok") as rest:
        await republish_last(rest, settings)

    replayed = {
        call.request.url.path.rsplit("/", 1)[-1]: json.loads(call.request.content)["state"]
        for call in respx.calls[calls_before_replay:]
    }
    for entity_id in (
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_planned_cost",
        "sensor.ha_spark_baseline_cost",
    ):
        assert json.loads(routes[entity_id].calls[0].request.content)["state"] == "unavailable"
        assert cached[entity_id] == "unavailable"
        assert replayed[entity_id] == "unavailable"
    assert cached["sensor.ha_spark_forecast_load_kwh"] == "10.00"
    assert cached["sensor.ha_spark_solar_forecast_kwh"] == "5.00"
    assert replayed["sensor.ha_spark_forecast_load_kwh"] == "10.00"
    assert replayed["sensor.ha_spark_solar_forecast_kwh"] == "5.00"


@respx.mock
async def test_republish_last_hides_untrusted_values_in_an_older_cache(tmp_path: Path) -> None:
    bad = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=datetime.now(UTC),
        value=70.0,
        raw_state="70",
        age_s=746.0,
        max_age_s=600.0,
    )
    settings = Settings(db_path=str(tmp_path / "ha_spark.db"))
    old_payload = plan_to_payload(_plan(soc=bad, planned_cost=8.25, baseline_cost=10.5), settings)
    soc_dependent_states = {
        "sensor.ha_spark_charge_needed_kwh": "24.19",
        "sensor.ha_spark_target_soc": "90",
        "sensor.ha_spark_soc_now": "0",
        "sensor.ha_spark_deficit_kwh": "24.19",
        "sensor.ha_spark_planned_cost": "8.25",
        "sensor.ha_spark_baseline_cost": "10.50",
    }
    old_payload = [
        (entity_id, soc_dependent_states.get(entity_id, state), attributes)
        for entity_id, state, attributes in old_payload
    ]
    (tmp_path / "ha_spark_published.json").write_text(
        json.dumps(old_payload), encoding="utf-8"
    )
    routes = {
        entity_id: respx.post(f"{BASE}/states/{entity_id}").mock(
            return_value=httpx.Response(200, json={})
        )
        for entity_id, _, _ in old_payload
    }

    async with HomeAssistantRest(BASE, "tok") as rest:
        await republish_last(rest, settings)

    for entity_id in soc_dependent_states:
        assert json.loads(routes[entity_id].calls[0].request.content)["state"] == "unavailable"
    assert (
        json.loads(routes["sensor.ha_spark_forecast_load_kwh"].calls[0].request.content)["state"]
        == "10.00"
    )
    assert (
        json.loads(routes["sensor.ha_spark_solar_forecast_kwh"].calls[0].request.content)["state"]
        == "5.00"
    )


@respx.mock
async def test_republish_last_noop_when_no_cache(tmp_path: Path) -> None:
    settings = Settings(db_path=str(tmp_path / "missing" / "ha_spark.db"))
    async with HomeAssistantRest(BASE, "tok") as rest:
        await republish_last(rest, settings)  # should not raise, nothing mocked to call


def test_plan_status_publishes_soc_status_and_reason() -> None:
    bad = SocMeasurement(
        status=SocStatus.READ_FAILED,
        observed_at=datetime.now(UTC),
        max_age_s=600.0,
    )
    entities = plan_to_payload(
        _plan(soc=bad, planned_cost=8.25, baseline_cost=10.5), Settings()
    )
    by_id = {eid: (state, attrs) for eid, state, attrs in entities}
    attrs = by_id["sensor.ha_spark_plan_status"][1]

    assert attrs["soc_status"] == "read_failed"
    assert attrs["soc_reason"] == bad.reason
    assert "soc_valid" not in attrs
    for entity_id in (
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_planned_cost",
        "sensor.ha_spark_baseline_cost",
    ):
        assert by_id[entity_id][0] == "unavailable"
    assert by_id["sensor.ha_spark_forecast_load_kwh"][0] == "10.00"
    assert by_id["sensor.ha_spark_solar_forecast_kwh"][0] == "5.00"


def test_plan_status_publishes_ok_status_for_a_checked_soc() -> None:
    entities = plan_to_payload(_plan(), Settings())
    attrs = {eid: a for eid, _, a in entities}["sensor.ha_spark_plan_status"]

    assert attrs["soc_status"] == "ok"


def test_plan_status_publishes_event_soc_needed_only_when_present() -> None:
    needed_at = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
    attrs = {
        eid: attributes
        for eid, _, attributes in plan_to_payload(
            _plan(export_soc_needed_pct=61.2, export_soc_needed_at=needed_at), Settings()
        )
    }["sensor.ha_spark_plan_status"]
    without_need = {
        eid: attributes for eid, _, attributes in plan_to_payload(_plan(), Settings())
    }["sensor.ha_spark_plan_status"]

    assert attrs["export_soc_needed_pct"] == 61
    assert attrs["export_soc_needed_at"] == needed_at.isoformat()
    assert "export_soc_needed_pct" not in without_need
    assert "export_soc_needed_at" not in without_need


@respx.mock
async def test_publish_soc_integrity_exposes_pending_state_and_evidence() -> None:
    """The per-minute monitor sensor carries the pending state, the failure
    count, and the observation's evidence — never a computed plan."""
    from ha_spark.energy.soc_integrity import SocStatus
    from ha_spark.energy.soc_monitor import SocMonitor, SocOperatingState

    push = respx.post(f"{BASE}/states/sensor.ha_spark_soc_integrity").mock(
        return_value=httpx.Response(200, json={})
    )
    now = datetime.now(UTC)
    stale = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=now,
        value=30.0,
        raw_state="30",
        reported_at=now - timedelta(hours=1),
        age_s=3600.0,
        max_age_s=600.0,
    )
    monitor = SocMonitor()  # no path: persistence is not under test here
    snapshot = monitor.record(stale, failure_threshold=3)
    assert snapshot.state is SocOperatingState.PENDING_FAILURE

    async with HomeAssistantRest(BASE, "t") as rest:
        await publish_soc_integrity(rest, snapshot, Settings())

    assert push.called
    body = json.loads(push.calls[0].request.content)
    assert body["state"] == "pending_failure"
    attrs = body["attributes"]
    assert attrs["consecutive_failures"] == 1
    assert attrs["failure_threshold"] == 3
    assert attrs["soc_status"] == "stale"
    assert "over the 600s maximum" in attrs["soc_reason"]
    assert attrs["soc_value"] == 30.0
    assert attrs["soc_age_s"] == 3600.0
    # A monitoring verdict, not a plan: no plan attribute ever appears.
    assert not any(k.startswith("plan") or k == "model" for k in attrs)
