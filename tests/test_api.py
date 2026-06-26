"""Tests for the add-on HTTP API server."""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings
from ha_spark.energy.models import ChargeIntent, ChargePlan


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


def _state(tmp_path: Path, **settings_kw: object) -> AppState:
    options_path = tmp_path / "options.json"
    # reload rebuilds Settings from the persisted file (stands in for load_settings).
    return AppState(
        settings=Settings(**settings_kw),  # type: ignore[arg-type]
        options_path=options_path,
        reload=lambda: Settings(**json.loads(options_path.read_text(encoding="utf-8"))),
    )


def _client(state: AppState) -> TestClient:
    return TestClient(build_app(state))


def test_health_ok(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.get("/api/health")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["plan_at"] is None  # no plan computed yet


def test_get_plan_null_without_a_plan(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        body = client.get("/api/plan").json()
    assert body["plan"] is None


def test_get_plan_returns_sensor_entities(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.set_plan(_plan())
    with _client(state) as client:
        body = client.get("/api/plan").json()
    by_id = {e["entity_id"]: e for e in body["plan"]}
    assert by_id["sensor.ha_spark_target_soc"]["state"] == "90"
    assert by_id["sensor.ha_spark_target_soc"]["attributes"]["device_class"] == "battery"
    assert body["generated_at"] is not None


def test_config_roundtrip_persists_and_reloads(tmp_path: Path) -> None:
    state = _state(tmp_path, min_soc=20.0)
    with _client(state) as client:
        before = client.get("/api/config").json()
        assert before["min_soc"] == 20.0
        resp = client.post("/api/config", json={"min_soc": 25.0, "not_a_key": "x"})
        after = resp.json()
    assert resp.status_code == 200
    assert after["min_soc"] == 25.0
    # persisted to the options file, and the unknown key was dropped
    persisted = json.loads((tmp_path / "options.json").read_text(encoding="utf-8"))
    assert persisted == {"min_soc": 25.0}
    assert state.settings.min_soc == 25.0


def test_post_config_rejects_non_object(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.post("/api/config", json=[1, 2, 3])
    assert resp.status_code == 400
