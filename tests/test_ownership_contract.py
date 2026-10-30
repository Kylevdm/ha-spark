"""Shared pending heartbeats exercise the real route without crossing Python environments."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore, load_authority
from ha_spark.config import Settings
from ha_spark.devices.execution import execution_context
from ha_spark.integration_discovery import IntegrationDiscovery

FIXTURES = Path(__file__).parent / "fixtures" / "contract"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


@pytest.mark.parametrize("direction", ["integration", "addon"])
def test_pending_heartbeat_refusal_golden(tmp_path, monkeypatch, direction):
    snapshot = json.loads((FIXTURES / f"ownership_pending_{direction}.json").read_text())
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr("ha_spark.api.server._heartbeat_now", lambda: NOW)
    settings = Settings(supervisor_token="unused", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.state is not None
    store.save(store.state.model_copy(update={
        "committed_mode": "optimise", "last_changed_at": NOW,
    }))
    before = load_authority(settings)
    context = execution_context()
    previous = context.authority
    context.authority = store
    try:
        state = AppState(settings, tmp_path / "options.json")
        state.loop_tick = NOW
        discovery = IntegrationDiscovery(
            supervisor_token="unused", host="addon", auth_token="test",
        )
        discovery.session_id = "b79156f6-2fbe-4c30-ae3a-6b7fb4dd18d1"
        with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
            response = client.post(
                "/integration/heartbeat", json=snapshot,
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body == json.loads(
            (FIXTURES / f"ownership_refused_{direction}.json").read_text(),
        )
        assert store.known
        assert body["addon_authority"]["active"] == "active"
        assert load_authority(settings) == before
    finally:
        context.authority = previous
