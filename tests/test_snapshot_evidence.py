"""Integration-owned purchase proof comes from REST, using snapshot options."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from ha_spark.api.contract import SiteSnapshot
from ha_spark.api.snapshot_v4 import Evidence, parse_snapshot_v4
from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.daemon_channel import Reply
from ha_spark.devices.execution import ExecutionContext
from ha_spark.energy import scheduler, sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.solar_source import SolarSource
from ha_spark.health import Status, check_ev_status
from tests.native_helpers import NOW, native_link
from tests.test_contract_v4 import fixture
from tests.test_energy_sources import BASE, _state
from tests.test_planner_dispatch_purchase import _freeze_sources_clock


@pytest.fixture
def settings(tmp_path):
    return Settings(
        ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "spark.db"),
        timezone="UTC", charge_strategy="fill", ev_power_entity="sensor.old_power",
        dispatch_entity="binary_sensor.old_dispatch", ev_status_entity="sensor.old_status",
        ev_status_max_age_seconds=300, ev_power_max_age_seconds=300,
        live_dispatch_max_age_seconds=300,
    )


@pytest.fixture
def retired(settings):
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"


def test_evidence_settings_keeps_legacy_settings(settings):
    options = Evidence.model_validate(fixture("site_snapshot_v7")["evidence"])
    assert sources.evidence_settings(settings, options) is settings
    assert sources.evidence_settings(settings, None) is settings


@pytest.mark.usefixtures("retired")
def test_unconfigured_native_sources_never_use_addon_copies(settings):
    raw = fixture("site_snapshot_v7")["evidence"]
    del raw["ev_power"]
    effective = sources.evidence_settings(settings, Evidence.model_validate(raw))
    assert effective.ev_power_entity == ""
    assert effective.ev_status_entity == "sensor.ev"
    assert effective.ev_status_max_age_seconds == 60
    assert effective.octopus_rate_entity == "sensor.rate"
    assert effective.adjusted_rate_max_age_seconds == 64
    no_evidence = sources.evidence_settings(settings, None)
    assert not (no_evidence.ev_power_entity or no_evidence.ev_status_entity
                or no_evidence.dispatch_entity)
    assert settings.ev_power_entity == "sensor.old_power"


@pytest.mark.usefixtures("retired")
@pytest.mark.parametrize("case", [
    "fresh", "grid_invert", "siblings", "missing", "invalid", "v6", "stale_power", "stale_link",
])
@respx.mock
async def test_native_scheduler_uses_only_fresh_snapshot_purchase_sources(
    settings, monkeypatch, case,
):
    _freeze_sources_clock(monkeypatch, NOW)
    monkeypatch.setattr(scheduler, "datetime", sources.datetime)
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(
        return_value=LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test"),
    ))
    link = native_link(settings)
    body = fixture("site_snapshot_v4_max")
    body.update(api_version=6 if case == "v6" else 7, dispatch_handover=True,
                evidence=fixture("site_snapshot_v7")["evidence"], devices=[{
                    "id": "inverter", "transport": "native", "validation": "validated",
                    "capabilities": [],
                }], holds=[])
    if case == "missing":
        del body["evidence"]
    elif case == "invalid":
        body["evidence"]["dispatch_grid_invert"] = "true"
    elif case == "v6":
        del body["evidence"]["dispatch_grid_invert"]
        del body["evidence"]["ev_liveness_entity"]
        del body["evidence"]["dispatch_liveness_entity"]
    link = replace(
        link, snapshot=SiteSnapshot.model_validate(body),
        v4=replace(link.v4, evidence=parse_snapshot_v4(body, link.topology.topology).evidence),
        received_at=NOW - timedelta(seconds=91) if case == "stale_link" else NOW,
    )
    report = NOW.isoformat()
    old_report = (NOW - timedelta(minutes=10)).isoformat()
    power_report = (NOW - timedelta(seconds=63)).isoformat() if case == "stale_power" else report
    dispatch = {"planned_dispatches": [{
        "start": (NOW - timedelta(minutes=5)).isoformat(),
        "end": (NOW + timedelta(hours=1)).isoformat(), "source": "SMART",
    }]}
    routes = {}
    for entity, state, attrs, ts in (
        ("binary_sensor.dispatch", "on", dispatch, old_report if case == "siblings" else report),
        ("sensor.ev", "Charging" if case == "siblings" else "Idle", {}, old_report),
        ("sensor.ev_live", "on" if case == "siblings" else "unavailable", {}, report),
        ("sensor.dispatch_live", "on" if case == "siblings" else "unavailable", {}, report),
        ("sensor.ev_power", "0" if case in ("grid_invert", "siblings") else "7000",
         {"unit_of_measurement": "W"}, power_report),
        ("sensor.grid_power", "-5000" if case == "grid_invert" else "0",
         {"unit_of_measurement": "W"}, report),
        ("binary_sensor.old_dispatch", "on", dispatch, report),
        ("sensor.old_power", "7000", {"unit_of_measurement": "W"}, report),
        ("sensor.old_status", "Charging", {}, report),
    ):
        routes[entity] = respx.get(f"{BASE}/states/{entity}").mock(
            return_value=_state(entity, state, attrs, last_reported=ts),
        )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    context = ExecutionContext()
    context.limits_record = link.topology
    result = await scheduler.run_once(
        settings, execution=context, source=SolarSource("entities", None, None, NOW),
        native_link=link,
    )
    proven = case in ("fresh", "grid_invert", "siblings")
    assert bool(result.plan_run.run.inputs.dispatch_purchase_slots) is proven
    assert bool(result.plan.daytime_purchases) is proven
    if proven:
        purchase, = result.plan.daytime_purchases
        assert purchase.overlay == "dispatch" and not purchase.refused
        assert purchase.end == NOW + timedelta(minutes=30)
    assert all(not route.called for entity, route in routes.items() if "old_" in entity)
    if case in ("missing", "invalid", "v6", "stale_link"):
        assert not any(route.called for route in routes.values())
        # No integration dispatch source is unknown, never a trusted clearance.
        assert result.plan_run.run.inputs.dispatches_trusted is False


def test_established_retirement_survives_a_failed_marker_read(settings, monkeypatch):
    monkeypatch.setattr(sources, "retirement_status", lambda _settings: None)
    effective = sources.evidence_settings(settings, None, retired=True)
    assert not (effective.ev_power_entity or effective.ev_status_entity
                or effective.dispatch_entity)


@pytest.mark.usefixtures("retired")
@pytest.mark.parametrize("invert", ["absent", None])
def test_grid_source_without_polarity_proves_nothing(settings, invert):
    raw = fixture("site_snapshot_v7")["evidence"]
    if invert == "absent":
        del raw["dispatch_grid_invert"]
    else:
        raw["dispatch_grid_invert"] = None
    effective = sources.evidence_settings(settings, Evidence.model_validate(raw))
    assert effective.dispatch_grid_power_entity == ""
    assert effective.ev_power_entity == "sensor.ev_power"


@pytest.mark.usefixtures("retired")
@pytest.mark.parametrize("power", ["0", "unknown", "not-a-number", None])
@respx.mock
async def test_native_ev_health_warns_only_without_a_fresh_draw_source(
    settings, monkeypatch, power,
):
    _freeze_sources_clock(monkeypatch, NOW)
    options = fixture("site_snapshot_v7")["evidence"] if power is not None else None
    monkeypatch.setattr("ha_spark.health.submit_socket", AsyncMock(return_value=Reply(
        integration_link={"evidence_options": options, "dispatch_handover": True},
    )))
    if power is not None:
        respx.get(f"{BASE}/states/sensor.ev_power").mock(
            return_value=_state("sensor.ev_power", power, last_reported=NOW.isoformat()),
        )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    result = await check_ev_status(settings)
    assert "from the integration" in result.detail
    assert result.status is (Status.OK if power == "0" else Status.WARN)
    if result.status is Status.WARN:
        assert "no fresh draw source" in result.detail
