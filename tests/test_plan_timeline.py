"""Ingress serialises the stored planner view, never recomputing a plan."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import INGRESS_PROXY_IP, build_app
from ha_spark.energy.models import LoadForecast, PlannerInputs
from ha_spark.energy.plan_run import PlanRun, StoredPlanRun
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.tariff import fixed_schedule
from tests.api_helpers import owner_client
from tests.test_api import _soc, _state
from tests.test_planner import cfg
from tests.test_planner_slots_view import LONDON, slot_inputs


def stored_run(*, slots=True, measurement=None):
    inputs = PlannerInputs(
        soc=measurement or _soc(40), solar_tomorrow_kwh=4, predicted_home_load_kwh=12,
        load_slots=(0.25,) * 48 if slots else None,
        horizon_start=datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        now=datetime(2026, 10, 25, 1, 10, tzinfo=UTC),
    )
    config = cfg()
    schedule = fixed_schedule(inputs, config)
    plan = compute_plan(inputs, config, schedule)
    return StoredPlanRun(PlanRun(
        plan, inputs, config, schedule, "test", LoadForecast(12, None, "test"),
        ZoneInfo("Europe/London"),
    ), False)


@pytest.mark.parametrize("slots", [True, False])
def test_ingress_timeline_contract_and_household_dst(tmp_path, slots, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 1, 10, tzinfo=UTC)

    monkeypatch.setattr("ha_spark.energy.publish.datetime", Clock)
    monkeypatch.setattr("ha_spark.api.server.datetime", Clock)
    state = _state(tmp_path)
    record = stored_run(slots=slots)
    state.set_plan(record.run.plan, record)
    with owner_client(state) as client:
        body = client.get("/api/plan").json()
        mode = client.get("/api/health").json()["operating_mode"]
    timeline = body.pop("timeline")
    assert set(timeline) == {
        "model", "operating_mode", "strategy", "headline", "costs", "lines",
        "horizon_start", "today_start", "slot_minutes", "now", "slots", "soc",
    }
    assert timeline["model"] == ("slots" if slots else "daily")
    assert timeline["operating_mode"] == mode
    assert timeline["strategy"] == record.run.plan.strategy
    assert timeline["headline"] == {
        "soc_now": 40, "target_soc": record.run.plan.target_soc,
        "required_kwh": record.run.plan.required_kwh, "min_soc": 20, "capacity_kwh": 26.88,
    }
    assert timeline["costs"] == {
        "planned": record.run.plan.planned_cost, "baseline": record.run.plan.baseline_cost,
        "export_revenue": record.run.plan.export_revenue,
    }
    assert [line["text"] for line in timeline["lines"]] == [
        line.text for line in record.run.plan.view.lines
    ]
    assert any(line["kind"] == "charge" for line in timeline["lines"])
    assert timeline["slot_minutes"] == 30
    assert timeline["horizon_start"] == "2026-10-25T01:30:00+01:00"
    assert timeline["now"] == "2026-10-25T01:10:00+00:00"
    assert timeline["today_start"] == "2026-10-25T00:00:00+01:00"
    if slots:
        assert len(timeline["slots"]) == 48
        assert timeline["slots"][0]["start"] == "2026-10-25T01:30:00+01:00"
        assert timeline["slots"][1]["start"] == "2026-10-25T01:00:00+00:00"
        assert timeline["slots"][0]["load_kwh"] == 0.25
    else:
        assert timeline["slots"] is None
    if slots:
        assert timeline["soc"][0] == {"at": timeline["now"], "soc_pct": 40}
        assert timeline["soc"] == [
            {"at": point.at.astimezone(LONDON).isoformat(), "soc_pct": point.soc_pct}
            for point in record.run.plan.view.soc
        ]
    else:
        assert timeline["soc"] is None
    for kwargs, headers in [({}, {}),
                            ({"require_token": True, "token": "valid"},
                             {"Authorization": "Bearer valid"})]:
        with TestClient(build_app(state, **kwargs)) as client:
            response = client.get("/api/plan", headers=headers)
        assert response.json() == body
        # The existing response's encoding and key order also stay unchanged.
        with TestClient(build_app(state)) as client:
            assert response.content == client.get("/api/plan").content
    with TestClient(build_app(state, ingress_peer=INGRESS_PROXY_IP),
                    client=("192.0.2.1", 50000)) as client:
        assert client.get("/api/plan", headers={
            "X-Forwarded-For": INGRESS_PROXY_IP, "X-Remote-User-Id": "owner",
        }).status_code == 403


@pytest.mark.parametrize("now,expected", [
    # The request's clock decides today_start, on both DST days ...
    (datetime(2026, 3, 29, 12, tzinfo=UTC), "2026-03-29T00:00:00+00:00"),
    (datetime(2026, 10, 25, 12, tzinfo=UTC), "2026-10-25T00:00:00+01:00"),
    # ... and for a plan computed yesterday, whose horizon starts before today.
    (datetime(2026, 10, 26, 12, tzinfo=UTC), "2026-10-26T00:00:00+00:00"),
])
def test_ingress_timeline_today_start_for_dst_and_stale_horizon(
    tmp_path, monkeypatch, now, expected,
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now

    monkeypatch.setattr("ha_spark.api.server.datetime", Clock)
    state = _state(tmp_path)
    record = stored_run()
    state.set_plan(record.run.plan, record)
    with owner_client(state) as client:
        timeline = client.get("/api/plan").json()["timeline"]
    assert timeline["today_start"] == expected
    assert timeline["now"] == record.run.inputs.now.astimezone(LONDON).isoformat()
    assert timeline["horizon_start"] == "2026-10-25T01:30:00+01:00"
    if now.day == 26:
        assert datetime.fromisoformat(timeline["horizon_start"]) < datetime.fromisoformat(expected)


@pytest.mark.parametrize("known", [None, (False, True)])
def test_ingress_timeline_unknown_solar_is_null_not_genuine_zero(tmp_path, known):
    record = stored_run()
    inputs = replace(
        record.run.inputs, solar_slots=(0.0,) * 48,
        solar_slots_known=(known + (True,) * 46 if known is not None else None),
    )
    plan = compute_plan(inputs, record.run.cfg, record.run.schedule)
    record = replace(record, run=replace(record.run, inputs=inputs, plan=plan))
    state = _state(tmp_path)
    state.set_plan(plan, record)
    with owner_client(state) as client:
        slots = client.get("/api/plan").json()["timeline"]["slots"]
    for field in ("solar_kwh", "solar_raw_kwh"):
        assert slots[0][field] == (None if known is not None else 0.0)
        assert slots[1][field] == 0.0


def test_ingress_timeline_null_without_stored_run(tmp_path):
    state = _state(tmp_path)
    with owner_client(state) as client:
        assert client.get("/api/plan").json() == {
            "plan": None, "generated_at": None, "timeline": None,
        }
        state.set_plan(stored_run().run.plan)
        assert client.get("/api/plan").json()["timeline"] is None
        record = stored_run()
        bare = replace(record.run.plan, view=None)
        state.set_plan(bare, replace(record, run=replace(record.run, plan=bare)))
        assert client.get("/api/plan").json()["timeline"] is None


@pytest.mark.parametrize("slots", [True, False])
def test_ingress_timeline_untrusted_soc(tmp_path, slots):
    state = _state(tmp_path)
    record = stored_run(slots=slots, measurement=SocMeasurement(
        status=SocStatus.READ_FAILED, observed_at=datetime(2026, 10, 25, tzinfo=UTC),
    ))
    state.set_plan(record.run.plan, record)
    with owner_client(state) as client:
        timeline = client.get("/api/plan").json()["timeline"]
    assert timeline["headline"] == {
        "soc_now": None, "target_soc": None, "required_kwh": None,
        "min_soc": 20, "capacity_kwh": 26.88,
    }
    assert timeline["costs"] == {"planned": None, "baseline": None, "export_revenue": None}
    assert timeline["lines"] == [{
        "kind": "warning", "start": None, "end": None, "kwh": None,
        "text": "SoC read from Home Assistant failed",
    }]
    if slots:
        assert len(timeline["slots"]) == 48
        assert all(slot["actions"] == [] for slot in timeline["slots"])
    else:
        assert timeline["slots"] is None
    assert timeline["soc"] == ([] if slots else None)


def test_ingress_timeline_uses_daemon_authority_mode(tmp_path):
    state = _state(tmp_path)
    record = stored_run()
    state.set_plan(record.run.plan, record)
    state.executor = Mock()
    state.executor.authority_status.return_value = {"mode": "protect"}
    with owner_client(state) as client:
        timeline = client.get("/api/plan").json()["timeline"]
    assert timeline["operating_mode"] == "protect"


@pytest.mark.parametrize("now", [
    datetime(2026, 3, 29, 0, 17, tzinfo=UTC),
    datetime(2026, 10, 25, 0, 17, tzinfo=UTC),
])
def test_ingress_slots_preserve_remaining_numbers_and_household_offsets(tmp_path, now):
    config = cfg(solar_haircut_k=0.5)
    inputs = slot_inputs(now, config)
    schedule = fixed_schedule(inputs, config)
    plan = compute_plan(inputs, config, schedule)
    record = StoredPlanRun(PlanRun(
        plan, inputs, config, schedule, "test", LoadForecast(12, None, "test"), LONDON,
    ), False)
    state = _state(tmp_path)
    state.set_plan(plan, record)
    with owner_client(state) as client:
        timeline = client.get("/api/plan").json()["timeline"]
    assert timeline["soc"][0] == {"at": now.astimezone(LONDON).isoformat(), "soc_pct": 30}
    assert timeline["soc"] == [
        {"at": point.at.astimezone(LONDON).isoformat(), "soc_pct": point.soc_pct}
        for point in plan.view.soc
    ]
    for i, slot in enumerate(timeline["slots"]):
        source = plan.view.slots[i]
        assert slot == {
            "start": source.start.astimezone(LONDON).isoformat(),
            "import_price": schedule.prices[i], "export_price": schedule.export_rate,
            "cheap_frac": schedule.cheap_fracs[i],
            "load_kwh": pytest.approx(0.6 * (13 / 30 if i == 0 else 1)),
            "solar_kwh": pytest.approx(0.1 * (13 / 30 if i == 0 else 1)),
            "solar_raw_kwh": pytest.approx(0.2 * (13 / 30 if i == 0 else 1)),
            "actions": list(source.actions),
        }
    assert timeline["slots"][0]["actions"] == ["charge"]
    assert timeline["slots"][0]["start"][-6:] != timeline["slots"][6]["start"][-6:]
