"""Tests for the add-on HTTP API server."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import INGRESS_PROXY_IP, AppState, build_app
from ha_spark.authority import operating_mode, resolve_data_dir
from ha_spark.config import ConfigError, Settings
from ha_spark.energy.models import ChargeIntent, ChargePlan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.soc_monitor import monitor_path
from tests.api_helpers import OWNER_CONFIG_HEADERS, owner_client
from tests.authority_helpers import seed_settings, settings_bytes


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
    service = seed_settings(tmp_path, settings=Settings(**settings_kw))  # type: ignore[arg-type]
    return AppState(service.settings, options_path, settings_service=service)


def _client(state: AppState) -> TestClient:
    return TestClient(build_app(state))


def test_health_ok(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fail"
    assert body["operating_mode"] == "authority_unknown"
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
    with owner_client(_state(tmp_path)) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    assert "min_soc" in resp.json()["settings"]


@pytest.mark.parametrize("saved", ["off", "simulate", "on", "invalid", "\udcff"])
def test_retired_sidecar_is_not_read_or_changed(tmp_path, caplog, saved):
    path = tmp_path / "proactive_mode.txt"
    original = saved.encode("utf-8", errors="surrogateescape")
    path.write_bytes(original)
    state = _state(tmp_path)
    with pytest.raises(ConfigError, match="retired: use the operating mode"):
        state.apply_options({"proactive_mode": "on"}, state.settings_service.revision, "seed")
    assert path.read_bytes() == original
    assert not caplog.records
    assert "proactive_mode" not in state.current_options()["settings"]


def test_retired_sidecar_directory_is_ignored(tmp_path, caplog):
    (tmp_path / "proactive_mode.txt").mkdir()
    state = _state(tmp_path)
    with pytest.raises(ConfigError, match="retired: use the operating mode"):
        state.apply_options({"proactive_mode": "on"}, state.settings_service.revision, "seed")
    assert not caplog.records
    assert "proactive_mode" not in state.current_options()["settings"]


def test_config_roundtrip_persists_and_reloads(tmp_path: Path) -> None:
    state = _state(tmp_path, min_soc=20.0)
    with owner_client(state) as client:
        before = client.get("/api/config").json()
        assert before["settings"]["min_soc"] == 20.0
        resp = client.post(
            "/api/config",
            json={"expected_revision": before["revision"], "changes": {"min_soc": 25.0}},
        )
        after = client.get("/api/config").json()["settings"]
    assert resp.status_code == 200
    assert after["min_soc"] == 25.0
    persisted = json.loads((tmp_path / "ha_spark_config.json").read_text())
    assert persisted["settings"] == {"min_soc": 25.0}
    assert state.settings.min_soc == 25.0


def test_post_config_rejects_non_object(tmp_path: Path) -> None:
    with owner_client(_state(tmp_path)) as client:
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
    with owner_client(state) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    # Raw response text carries neither secret.
    assert "SECRET_OCTO" not in resp.text
    assert "SECRET_AXLE" not in resp.text
    assert "SECRET_AGENT" not in resp.text
    body = resp.json()["settings"]
    assert body["octopus_api_key"] == {"secret": True, "set": True}
    assert body["axle_api_key"] == {"secret": True, "set": True}
    assert body["agent_api_token"] == {"secret": True, "set": True}


def test_unset_secret_not_rendered_as_redacted(tmp_path: Path) -> None:
    """An empty secret stays empty so clients can tell it's not configured."""
    with owner_client(_state(tmp_path)) as client:
        body = client.get("/api/config").json()["settings"]
    assert body["octopus_api_key"] == {"secret": True, "set": False}
    assert body["agent_api_token"] == {"secret": True, "set": False}


def test_post_redacted_secret_does_not_clobber_stored_value(tmp_path: Path) -> None:
    """GET-then-POST round-trip of the masked options must not overwrite the secret."""
    state = _state(tmp_path, octopus_api_key="REAL_OCTO", agent_api_token="REAL_AGENT")
    with owner_client(state) as client:
        # Posting the sentinel back (as a client echoing GET /api/config would) is a no-op.
        resp = client.post(
            "/api/config",
            json={
                "expected_revision": state.settings_service.revision,
                "changes": {"octopus_api_key": "***", "agent_api_token": "***", "min_soc": 25.0},
            },
        )
    assert resp.status_code == 200
    # Stored secrets are unchanged; the non-secret update still applied.
    assert state.settings.octopus_api_key == "REAL_OCTO"
    assert state.settings.agent_api_token == "REAL_AGENT"
    assert state.settings.min_soc == 25.0
    persisted = json.loads((tmp_path / "ha_spark_secrets.json").read_text())["secrets"]
    assert persisted["octopus_api_key"] == "REAL_OCTO"
    assert persisted["agent_api_token"] == "REAL_AGENT"
    assert (
        json.loads((tmp_path / "ha_spark_config.json").read_text())["settings"]["min_soc"] == 25.0
    )


def test_post_genuine_secret_value_is_stored(tmp_path: Path) -> None:
    """A real (non-sentinel) secret value still updates normally."""
    state = _state(tmp_path)
    with owner_client(state) as client:
        client.post(
            "/api/config",
            json={
                "expected_revision": state.settings_service.revision,
                "changes": {"octopus_api_key": "NEW_OCTO"},
            },
        )
    assert state.settings.octopus_api_key == "NEW_OCTO"
    persisted = json.loads((tmp_path / "ha_spark_secrets.json").read_text())["secrets"]
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
        saved = client.post(
            "/api/config",
            json={
                "expected_revision": state.settings_service.revision,
                "changes": {"min_soc": 30.0},
            },
            headers=OWNER_CONFIG_HEADERS,
        )
        assert saved.status_code == 200
    assert state.settings.min_soc == 30.0


def test_token_port_is_not_peer_restricted(tmp_path: Path) -> None:
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app, client=("192.0.2.10", 50000)) as client:
        assert client.get("/api/health").status_code == 401
        ok = client.get("/api/health", headers={"Authorization": "Bearer sekret"})
        assert ok.status_code == 200


def test_post_config_rejects_invalid_document_before_saving(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#412: the complete merged candidate is validated before anything is saved.

    A rejected save must leave the options file byte-identical and the running
    settings untouched, and the 400 reason must name the failing field without
    echoing its value or the secret sitting next to it in the document.
    """
    state = _state(tmp_path, min_soc=20.0, octopus_api_key="SECRET_OCTO")
    seeded = settings_bytes(tmp_path)
    with caplog.at_level("WARNING"):
        with owner_client(state) as client:
            resp = client.post(
                "/api/config",
                json={
                    "expected_revision": state.settings_service.revision,
                    "changes": {"min_soc": "not-a-number"},
                },
            )
    assert resp.status_code == 400
    assert "min_soc" in resp.json()["error"]
    assert "not-a-number" not in resp.text
    assert "SECRET_OCTO" not in resp.text
    assert "SECRET_OCTO" not in caplog.text
    assert settings_bytes(tmp_path) == seeded
    assert state.settings.min_soc == 20.0


def test_post_config_rejection_never_echoes_a_secret_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad value for a secret option must not echo that value in the 400 or logs.

    pydantic's ValidationError embeds ``input_value``, so a rejected secret
    value would otherwise leave the process in cleartext (CLAUDE.md top rule).
    """
    state = _state(tmp_path, agent_api_token="REAL_AGENT")
    seeded = settings_bytes(tmp_path)
    with caplog.at_level("WARNING"):
        with owner_client(state) as client:
            resp = client.post(
                "/api/config",
                json={
                    "expected_revision": state.settings_service.revision,
                    "changes": {"agent_api_token": ["TOPSECRET_AGENT"]},
                },
            )
    assert resp.status_code == 400
    assert "agent_api_token" in resp.json()["error"]
    assert "TOPSECRET_AGENT" not in resp.text
    assert "TOPSECRET_AGENT" not in caplog.text
    assert settings_bytes(tmp_path) == seeded
    assert state.settings.agent_api_token == "REAL_AGENT"


def test_post_config_rejection_of_nested_device_entry_is_secret_safe(tmp_path: Path) -> None:
    """A bad nested ``devices`` entry must not echo the offending value."""
    state = _state(tmp_path)
    with owner_client(state) as client:
        resp = client.post(
            "/api/config",
            json={
                "expected_revision": state.settings_service.revision,
                "changes": {
                    "devices": [
                        {
                            "id": "main_inverter",
                            "driver": "solis",
                            "entities": {"power_switch": ["TOPSECRET_AGENT"]},
                        }
                    ]
                },
            },
        )
    assert resp.status_code == 400
    assert "devices" in resp.json()["error"]
    assert "TOPSECRET_AGENT" not in resp.text
    assert not (tmp_path / "options.json").exists()
    assert state.settings.agent_api_token == ""


def test_post_config_rejects_a_changed_db_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#441: a settings save cannot move ``db_path``, so no config write can
    relocate the authority store or the runtime state beside it. A changed
    value is rejected before the merged document is validated or persisted,
    with a reason naming ``db_path`` that never echoes the submitted value.
    """
    state = _state(
        tmp_path,
        ha_url="http://ha.test",
        ha_token="x",
        db_path=str(tmp_path / "t.db"),
        min_soc=20.0,
    )
    seeded = settings_bytes(tmp_path)
    before_dir = resolve_data_dir(state.settings)
    before_mode = operating_mode(state.settings)
    with caplog.at_level("WARNING"):
        with owner_client(state) as client:
            resp = client.post(
                "/api/config",
                json={
                    "expected_revision": state.settings_service.revision,
                    "changes": {"db_path": "TOPSECRET/moved-db.db"},
                },
            )
    assert resp.status_code == 400
    assert "db_path" in resp.json()["error"]
    assert "TOPSECRET/moved-db.db" not in resp.text
    assert "moved-db.db" not in caplog.text
    assert settings_bytes(tmp_path) == seeded
    # The running value -- and so the authority directory and mode it decides
    # -- are exactly as they were before the rejected save.
    assert state.settings.db_path == str(tmp_path / "t.db")
    assert resolve_data_dir(state.settings) == before_dir == tmp_path
    assert operating_mode(state.settings) == before_mode == "authority_unknown"


def test_post_config_rejects_null_db_path(tmp_path: Path) -> None:
    """A ``db_path: null`` update would be dropped by the options overlay and
    so fall back to a default path; a settings save has no way to express
    "unset", so it is rejected like any other change (#441). Add-on mode too:
    every runtime-state path derived from ``db_path`` stays where it was.
    """
    state = _state(tmp_path, min_soc=20.0)
    seeded = settings_bytes(tmp_path)
    before_monitor = monitor_path(state.settings)
    with owner_client(state) as client:
        resp = client.post(
            "/api/config",
            json={
                "expected_revision": state.settings_service.revision,
                "changes": {"db_path": None},
            },
        )
    assert resp.status_code == 400
    assert "db_path" in resp.json()["error"]
    assert "None" not in resp.json()["error"]
    assert settings_bytes(tmp_path) == seeded
    assert state.settings.db_path == str(tmp_path / "ha_spark.db")
    assert monitor_path(state.settings) == before_monitor


def test_post_config_rejects_even_the_running_bootstrap_db_path(tmp_path: Path) -> None:
    """db_path is bootstrap-only, even when the submitted value is unchanged."""
    db_path = str(tmp_path / "t.db")
    state = _state(tmp_path, ha_url="http://ha.test", ha_token="x", db_path=db_path, min_soc=20.0)
    with owner_client(state) as client:
        resp = client.post(
            "/api/config",
            json={
                "expected_revision": state.settings_service.revision,
                "changes": {"db_path": db_path, "min_soc": 25.0},
            },
        )
    assert resp.status_code == 400
    assert state.settings.min_soc == 20.0
    assert state.settings.db_path == db_path
    assert "db_path" in resp.json()["error"]


# --- #457: config routes are owner-only ingress routes -----------------------


def test_published_port_has_no_config_routes_even_with_forged_ingress_headers(
    tmp_path: Path,
) -> None:
    """An agent token plus forged ingress headers never reaches an owner config route."""
    state = _state(tmp_path, min_soc=20.0, agent_exposure="read")
    app = build_app(state, require_token=True, token="sekret")
    forged = {"Authorization": "Bearer sekret", **OWNER_CONFIG_HEADERS}
    with TestClient(app, client=("192.0.2.10", 50000)) as client:
        assert client.get("/api/config", headers=forged).status_code == 404
        resp = client.post(
            "/api/config", json={"agent_exposure": "read_write", "min_soc": 30.0}, headers=forged
        )
        assert resp.status_code == 404
    assert not (tmp_path / "options.json").exists()
    assert state.settings.agent_exposure == "read"
    assert state.settings.min_soc == 20.0


def test_standalone_listener_has_no_config_routes(tmp_path: Path) -> None:
    state = _state(tmp_path, min_soc=20.0)
    with TestClient(build_app(state)) as client:
        assert client.get("/api/config").status_code == 404
        resp = client.post("/api/config", json={"min_soc": 30.0}, headers=OWNER_CONFIG_HEADERS)
        assert resp.status_code == 404
    assert not (tmp_path / "options.json").exists()
    assert state.settings.min_soc == 20.0


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"X-HA-Spark-Request": "config"}, 403),
        ({"X-Remote-User-Id": "bad id!", "X-HA-Spark-Request": "config"}, 403),
        ({"X-Remote-User-Id": "owner"}, 403),
        ({"X-Remote-User-Id": "owner", "X-HA-Spark-Request": "mode"}, 403),
    ],
)
def test_ingress_config_save_needs_owner_request_headers(
    tmp_path: Path, headers: dict[str, str], status: int
) -> None:
    state = _state(tmp_path, min_soc=20.0)
    with TestClient(build_app(state, ingress_peer="testclient")) as client:
        resp = client.post("/api/config", json={"min_soc": 30.0}, headers=headers)
    assert resp.status_code == status
    assert not (tmp_path / "options.json").exists()
    assert state.settings.min_soc == 20.0


def test_ingress_config_save_needs_a_json_content_type(tmp_path: Path) -> None:
    state = _state(tmp_path, min_soc=20.0)
    with owner_client(state) as client:
        resp = client.post(
            "/api/config",
            content='{"min_soc": 30.0}',
            headers={"Content-Type": "text/plain"},
        )
    assert resp.status_code == 415
    assert not (tmp_path / "options.json").exists()
    assert state.settings.min_soc == 20.0


def test_config_save_failure_other_than_validation_never_echoes_its_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload failure's message may carry input; only its type is reported."""
    state = _state(tmp_path, min_soc=20.0)

    def _boom(*args, **kwargs):
        raise OSError("TOPSECRET_FROM_RELOAD")

    monkeypatch.setattr("ha_spark.settings_service.os.replace", _boom)
    with owner_client(state) as client:
        resp = client.post(
            "/api/config",
            json={
                "expected_revision": state.settings_service.revision,
                "changes": {"min_soc": 30.0},
            },
        )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Configuration save failed; restart required"}
    assert "TOPSECRET" not in resp.text
    assert "TOPSECRET" not in caplog.text
    assert state.settings.min_soc == 20.0


def test_post_config_rejection_never_echoes_a_submitted_mapping_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A secret pasted as a mapping key is not a field name, so the reason
    reads ``<key>`` in its place (#457 review)."""
    state = _state(tmp_path)
    with owner_client(state) as client:
        resp = client.post(
            "/api/config",
            json={"expected_revision": state.settings_service.revision, "changes": {
                "devices": [{"id": "main", "driver": "solis",
                             "entities": {"TOPSECRET_ENTITY": []}}]}},
        )
    assert resp.status_code == 400
    assert "devices.0.entities.<key>" in resp.json()["error"]
    assert "TOPSECRET" not in resp.text
    assert "TOPSECRET" not in caplog.text
    assert not (tmp_path / "options.json").exists()
