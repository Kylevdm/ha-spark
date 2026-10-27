"""v2 heartbeat hand-off and strictly future charge-window announcements."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark.api import server
from ha_spark.api.contract import SiteSnapshot, plan_summary
from ha_spark.api.server import build_app
from ha_spark.config import Settings
from ha_spark.energy.solar_source import load_solar_source_kind
from tests.test_integration_route import discovery as discovery
from tests.test_integration_route import state as state

FIXTURES = Path(__file__).parent / "fixtures/contract"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def test_v1_identity_fixture_is_still_accepted():
    body = json.loads((FIXTURES / "site_snapshot_v1.json").read_text())
    snapshot = SiteSnapshot.model_validate(body)
    assert snapshot.api_version == 1
    assert snapshot.solar_forecast is None


@pytest.mark.parametrize("forecast", ["valid", "missing", "invalid", "too_many", "null", "v1"])
def test_forecast_field_does_not_reject_heartbeat(state, discovery, monkeypatch, forecast):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: NOW)
    body = json.loads((FIXTURES / "site_snapshot_forecast.json").read_text())
    if forecast == "missing":
        del body["solar_forecast"]
    elif forecast == "invalid":
        body["solar_forecast"] = {"secret": "CANARY"}
    elif forecast == "too_many":
        body["solar_forecast"]["slots"] *= 121
    elif forecast == "null":
        body["solar_forecast"] = None
    elif forecast == "v1":
        body["api_version"] = 1
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", json=body,
                               headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert state.integration_link.received_at == NOW
    parsed = state.integration_link.solar_forecast
    assert parsed.kind == ("entities" if forecast in {"null", "v1"} else "native")
    assert (parsed.forecast is not None) == (forecast == "valid")
    assert load_solar_source_kind(state.settings) == parsed.kind
    assert ("charge_window_start" in response.json()) == (forecast != "v1")


def test_source_kind_persists_only_on_change(state, discovery, monkeypatch):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: NOW)
    body = json.loads((FIXTURES / "site_snapshot_forecast.json").read_text())
    path = Path(state.settings.db_path).parent / "ha_spark_solar_source.json"
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        headers = {"Authorization": f"Bearer {discovery.auth_token}"}
        assert client.post("/integration/heartbeat", json=body, headers=headers).status_code == 200
        before = path.stat().st_mtime_ns
        assert json.loads(path.read_text()) == {"kind": "native"}
        assert client.post("/integration/heartbeat", json=body, headers=headers).status_code == 200
        assert path.stat().st_mtime_ns == before
        body["api_version"] = 1
        assert client.post("/integration/heartbeat", json=body, headers=headers).status_code == 200
        assert json.loads(path.read_text()) == {"kind": "entities"}


def test_source_store_failure_keeps_heartbeat(state, discovery, monkeypatch):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: NOW)
    # An unusable parent directory prevents both replacement and temp-file creation.
    blocked = Path(state.settings.db_path).parent / "blocked"
    blocked.write_text("not a directory")
    state.settings = state.settings.model_copy(update={"db_path": str(blocked / "db")})
    body = json.loads((FIXTURES / "site_snapshot_forecast.json").read_text())
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", json=body,
                               headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert state.integration_link.received_at == NOW
    assert state.integration_link.solar_forecast.kind == "native"


def test_maximum_snapshot_under_cap(state, discovery, monkeypatch):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: NOW)
    body = (FIXTURES / "site_snapshot_max.json").read_bytes()
    assert len(body) < 16 * 1024
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        headers = {"Authorization": f"Bearer {discovery.auth_token}"}
        response = client.post("/integration/heartbeat", content=body, headers=headers)
        assert response.status_code == 200
        assert len(state.integration_link.solar_forecast.forecast.slots) == 120
        assert client.post("/integration/heartbeat", content=body + b" " * 16384,
                           headers=headers).status_code == 400


@pytest.mark.parametrize("built,expected", [
    ("2026-10-24T20:00:00Z", "2026-10-24T22:30:00+00:00"),
    ("2026-10-24T23:00:00Z", "2026-10-25T23:30:00+00:00"),
    ("2026-03-28T23:45:00Z", "2026-03-29T22:30:00+00:00"),
    ("2026-03-29T22:30:00Z", "2026-03-30T22:30:00+00:00"),
    ("2026-10-25T01:30:00Z", "2026-10-25T23:30:00+00:00"),
])
def test_summary_next_window(built, expected):
    settings = Settings(ha_url="http://ha.test", ha_token="unused", timezone="Europe/London")
    body = plan_summary(api_version=2, session_id="test", built_at=datetime.fromisoformat(built),
                        loop_tick=None, settings=settings)
    assert body["charge_window_start"] == expected
    legacy = plan_summary(api_version=1, session_id="test", built_at=datetime.fromisoformat(built),
                          loop_tick=None, settings=settings)
    assert "charge_window_start" not in legacy
