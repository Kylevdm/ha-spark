"""The carbon picker uses the owner-only revisioned configuration save path."""

import json

import pytest

from ha_spark.api.server import AppState
from ha_spark.devices.execution import execution_context
from ha_spark.settings_service import SettingsService
from tests.api_helpers import owner_client
from tests.authority_helpers import seed_settings


def test_region_picker_save_revision_actor_and_live_reload(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    context = execution_context()
    monkeypatch.setattr(context, "authority", None)
    monkeypatch.setattr(context, "config_generation", context.config_generation)
    service = seed_settings(tmp_path, {})
    replans = []
    service.request_replan = lambda: replans.append(True)
    state = AppState(service.settings, service.options_path, settings_service=service)
    with owner_client(state) as client:
        current = client.get("/api/config").json()
        assert current["settings"]["carbon_region"] is None
        revision = current["revision"]
        response = client.post("/api/config", json={
            "changes": {"carbon_region": 14}, "expected_revision": revision,
        })
        assert response.status_code == 200
        assert response.json()["revision"] == revision + 1
        assert client.get("/api/config").json()["settings"]["carbon_region"] == 14
        stale = client.post("/api/config", json={
            "changes": {"carbon_region": 1}, "expected_revision": revision,
        })
        assert stale.status_code == 409
        assert service.settings.carbon_region == 14
        cleared = client.post("/api/config", json={
            "changes": {"carbon_region": None}, "expected_revision": revision + 1,
        })
        assert cleared.status_code == 200
        assert client.get("/api/config").json()["settings"]["carbon_region"] is None
    assert replans == []
    assert "fields=carbon_region actor=ingress:owner" in caplog.text
    document = json.loads((tmp_path / "ha_spark_config.json").read_text())
    assert document["revision"] == revision + 2
    fresh = SettingsService(service.options_path, settings=service.bootstrap, data_dir=tmp_path)
    assert fresh.load().carbon_region is None


@pytest.mark.parametrize("region", [0, 18, True, False, "1", 1.0])
def test_region_picker_rejects_invalid_region(tmp_path, region):
    service = seed_settings(tmp_path, {})
    state = AppState(service.settings, service.options_path, settings_service=service)
    revision = service.revision
    with owner_client(state) as client:
        response = client.post("/api/config", json={
            "changes": {"carbon_region": region}, "expected_revision": revision,
        })
    assert response.status_code == 400
    assert service.revision == revision
    assert service.settings.carbon_region is None
