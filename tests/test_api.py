"""Tests for the add-on HTTP API server."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import INGRESS_PROXY_IP, AppState, build_app
from ha_spark.config import Settings
from ha_spark.energy.models import ChargeIntent, ChargePlan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus


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
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["plan_at"] is None  # no plan computed yet


def test_get_plan_null_without_a_plan(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        body = client.get("/api/plan").json()
    assert body == {"plan": None, "generated_at": None}


def test_get_plan_returns_sensor_entities(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.set_plan(_plan())
    with _client(state) as client:
        body = client.get("/api/plan").json()
    by_id = {e["entity_id"]: e for e in body["plan"]}
    assert by_id["sensor.ha_spark_target_soc"]["state"] == "90"
    assert by_id["sensor.ha_spark_target_soc"]["attributes"]["device_class"] == "battery"
    assert body["generated_at"] is not None
    assert any(e["entity_id"].startswith("sensor.ha_spark") for e in body["plan"])


def test_get_config_returns_options(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    assert "min_soc" in resp.json()


@pytest.mark.parametrize("previous_mode", ["off", "simulate"])
def test_proactive_mode_on_transition_warns_about_conflicting_automation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, previous_mode: str
) -> None:
    state = _state(tmp_path, proactive_mode=previous_mode)

    with caplog.at_level("WARNING"):
        state.apply_options({"proactive_mode": "on"})

    assert any(
        "disable any pre-existing automations or manual schedules" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("previous_mode", ["off", "simulate"])
def test_supervisor_restart_into_on_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, previous_mode: str
) -> None:
    _state(tmp_path, proactive_mode=previous_mode)
    caplog.clear()
    state = _state(tmp_path, proactive_mode="on")
    assert state.settings.proactive_mode == "on"
    assert any("pre-existing automations" in record.message for record in caplog.records)


def test_proactive_mode_reload_without_transition_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = _state(tmp_path, proactive_mode="on")

    caplog.clear()
    with caplog.at_level("WARNING"):
        state.apply_options({"proactive_mode": "on"})

    assert not any("pre-existing automations" in record.message for record in caplog.records)


@pytest.mark.parametrize("mode", ["off", "simulate", "on"])
def test_restart_with_unchanged_mode_does_not_repeat_reminder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    _state(tmp_path, proactive_mode=mode)
    caplog.clear()
    _state(tmp_path, proactive_mode=mode)
    assert not any("pre-existing automations" in record.message for record in caplog.records)


def test_first_start_already_on_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _state(tmp_path, proactive_mode="on")
    assert any("pre-existing automations" in record.message for record in caplog.records)


def test_api_handover_is_remembered_across_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = _state(tmp_path, proactive_mode="simulate")
    state.apply_options({"proactive_mode": "on"})
    caplog.clear()
    _state(tmp_path, proactive_mode="on")
    assert not any("pre-existing automations" in record.message for record in caplog.records)
    _state(tmp_path, proactive_mode="off")
    caplog.clear()
    _state(tmp_path, proactive_mode="on")
    assert any("pre-existing automations" in record.message for record in caplog.records)


@pytest.mark.parametrize("saved", ["invalid", "\udcff"])
def test_invalid_persisted_mode_is_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, saved: str
) -> None:
    (tmp_path / "proactive_mode.txt").write_bytes(saved.encode("utf-8", errors="surrogateescape"))
    state = _state(tmp_path, proactive_mode="on")
    assert state.settings.proactive_mode == "on"
    assert any("pre-existing automations" in record.message for record in caplog.records)


def test_unwritable_mode_record_does_not_block_handover(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A directory at the record path causes read and replace to fail on all platforms.
    (tmp_path / "proactive_mode.txt").mkdir()
    state = _state(tmp_path, proactive_mode="simulate")
    state.apply_options({"proactive_mode": "on"})
    assert state.settings.proactive_mode == "on"
    assert any("Could not persist proactive mode" in record.message for record in caplog.records)
    assert any("pre-existing automations" in record.message for record in caplog.records)


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


def test_get_config_redacts_secrets(tmp_path: Path) -> None:
    """Set secrets must never leave the process in cleartext (CLAUDE.md top rule)."""
    state = _state(
        tmp_path,
        octopus_api_key="SECRET_OCTO",
        axle_api_key="SECRET_AXLE",
        agent_api_token="SECRET_AGENT",
    )
    with _client(state) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    # Raw response text carries neither secret.
    assert "SECRET_OCTO" not in resp.text
    assert "SECRET_AXLE" not in resp.text
    assert "SECRET_AGENT" not in resp.text
    body = resp.json()
    # Keys are still present (response shape preserved) but redacted.
    assert body["octopus_api_key"] == "***"
    assert body["axle_api_key"] == "***"
    assert body["agent_api_token"] == "***"


def test_unset_secret_not_rendered_as_redacted(tmp_path: Path) -> None:
    """An empty secret stays empty so clients can tell it's not configured."""
    with _client(_state(tmp_path)) as client:
        body = client.get("/api/config").json()
    assert body["octopus_api_key"] == ""
    assert body["agent_api_token"] == ""


def test_post_redacted_secret_does_not_clobber_stored_value(tmp_path: Path) -> None:
    """GET-then-POST round-trip of the masked options must not overwrite the secret."""
    # The secrets live in the persisted options file (as in add-on mode), so the
    # reload after apply_options reconstructs them rather than losing them.
    (tmp_path / "options.json").write_text(
        json.dumps({"octopus_api_key": "REAL_OCTO", "agent_api_token": "REAL_AGENT"}),
        encoding="utf-8",
    )
    state = _state(tmp_path, octopus_api_key="REAL_OCTO", agent_api_token="REAL_AGENT")
    with _client(state) as client:
        # Posting the sentinel back (as a client echoing GET /api/config would) is a no-op.
        resp = client.post(
            "/api/config",
            json={"octopus_api_key": "***", "agent_api_token": "***", "min_soc": 25.0},
        )
    assert resp.status_code == 200
    # Stored secrets are unchanged; the non-secret update still applied.
    assert state.settings.octopus_api_key == "REAL_OCTO"
    assert state.settings.agent_api_token == "REAL_AGENT"
    assert state.settings.min_soc == 25.0
    persisted = json.loads((tmp_path / "options.json").read_text(encoding="utf-8"))
    # The sentinel was dropped, so the persisted secrets keep their real values.
    assert persisted["octopus_api_key"] == "REAL_OCTO"
    assert persisted["agent_api_token"] == "REAL_AGENT"
    assert persisted["min_soc"] == 25.0


def test_post_genuine_secret_value_is_stored(tmp_path: Path) -> None:
    """A real (non-sentinel) secret value still updates normally."""
    state = _state(tmp_path)
    with _client(state) as client:
        client.post("/api/config", json={"octopus_api_key": "NEW_OCTO"})
    assert state.settings.octopus_api_key == "NEW_OCTO"
    persisted = json.loads((tmp_path / "options.json").read_text(encoding="utf-8"))
    assert persisted["octopus_api_key"] == "NEW_OCTO"


def _ingress_client(state: AppState, peer: str) -> TestClient:
    return TestClient(build_app(state, ingress_peer=INGRESS_PROXY_IP), client=(peer, 50000))


@pytest.mark.parametrize("peer", ["172.30.32.3", "172.30.33.5", "127.0.0.1"])
def test_ingress_app_rejects_non_proxy_peers(tmp_path: Path, peer: str) -> None:
    state = _state(tmp_path, min_soc=20.0)
    with _ingress_client(state, peer) as client:
        assert client.get("/api/health").status_code == 403
        assert client.post("/api/config", json={"min_soc": 30.0}).status_code == 403
        assert client.post("/mcp/", json={}).status_code == 403
    assert not (tmp_path / "options.json").exists()
    assert state.settings.min_soc == 20.0


def test_ingress_app_ignores_forwarding_headers(tmp_path: Path) -> None:
    spoof = {"X-Forwarded-For": INGRESS_PROXY_IP, "X-Real-IP": INGRESS_PROXY_IP}
    with _ingress_client(_state(tmp_path), "172.30.33.5") as client:
        assert client.get("/api/health", headers=spoof).status_code == 403


def test_ingress_app_serves_the_proxy(tmp_path: Path) -> None:
    state = _state(tmp_path, min_soc=20.0)
    with _ingress_client(state, INGRESS_PROXY_IP) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/config", json={"min_soc": 30.0}).status_code == 200
    assert state.settings.min_soc == 30.0


def test_token_port_is_not_peer_restricted(tmp_path: Path) -> None:
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app, client=("192.0.2.10", 50000)) as client:
        assert client.get("/api/health").status_code == 401
        ok = client.get("/api/health", headers={"Authorization": "Bearer sekret"})
        assert ok.status_code == 200
