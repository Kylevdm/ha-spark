"""Ownership handovers change durable authority only, never hardware."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ha_spark import authority
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore, load_authority
from ha_spark.config import DeviceConfig, Settings
from ha_spark.devices.execution import ExecutionContext, Ownership, execution_context
from ha_spark.integration_discovery import IntegrationDiscovery
from tests.test_contract_v4 import fixture


def test_retirement_is_unknown_to_existing_loader(tmp_path):
    settings = Settings(ha_url="http://ha", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    original = store.state
    assert store.handover("pending_integration") == "retired"
    assert load_authority(settings).status == "authority_unknown"
    assert not store.known
    context = ExecutionContext()
    context.authority = store
    device = DeviceConfig(id="inv", driver="solis")
    for ownership in Ownership:
        assert context.gate.recheck(ownership, device, settings) == "refuse:authority unknown"
    restarted = AuthorityStore(settings)
    restarted.start_process()
    assert restarted.handover("pending_integration") == "retired"
    with pytest.raises(ValueError, match="ownership"):
        restarted.confirm_observe("cli")
    assert original is not None
    assert restarted.handover("pending_addon") == "active"
    assert load_authority(settings).status == "authority_unknown"
    context.authority = restarted
    for ownership in Ownership:
        assert context.gate.recheck(ownership, device, settings) == "refuse:authority unknown"
    restarted = AuthorityStore(settings)
    assert restarted.handover("pending_addon") == "active"
    assert restarted.handover("addon") == "active"
    fresh = load_authority(settings)
    assert fresh.state is not None and fresh.state.store_id != original.store_id
    assert fresh.state.committed_mode == "observe" and fresh.state.epoch == 1


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unreadable_retirement_marker_cannot_confirm_observe(tmp_path, error):
    settings = Settings(ha_url="http://ha", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    before = (store.store_path.read_bytes(), store.marker_path.read_bytes())
    read = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == store.marker_path:
            raise error("unreadable")
        return read(path, *args, **kwargs)

    with patch.object(Path, "read_text", unreadable):
        with pytest.raises(ValueError, match="authority unknown"):
            store.confirm_observe("cli")
        assert not store.known
        assert store.save_failed
    assert (store.store_path.read_bytes(), store.marker_path.read_bytes()) == before
    assert load_authority(settings).status == "authority_unknown"


def test_handover_refuses_active_authority(tmp_path):
    settings = Settings(ha_url="http://ha", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.state is not None
    store.save(store.state.model_copy(update={"committed_mode": "protect",
                                             "last_changed_at": datetime.now(UTC)}))
    assert store.handover("pending_integration") is None
    assert store.known


@pytest.mark.parametrize("crash_write", [1, 2, 3, 4])
def test_crash_after_each_addon_handshake_write(tmp_path, crash_write):
    settings = Settings(ha_url="http://ha", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    write = authority._durable_write
    count = 0

    def fail_after_write(path, payload):
        nonlocal count
        write(path, payload)
        count += 1
        if count == crash_write:
            raise OSError("interrupted")

    with patch.object(authority, "_durable_write", side_effect=fail_after_write):
        for owner in ("pending_integration", "pending_addon", "addon"):
            if store.handover(owner) is None:
                break
    restarted = AuthorityStore(settings)
    if restarted.known:
        assert restarted.state.committed_mode == "observe"
    else:
        # Any unfinished rollback stays unknown, including under the legacy
        # load path, then resumes without importing the retired mode or holds.
        assert load_authority(settings).status == "authority_unknown"
        if crash_write == 1:
            assert restarted.handover("pending_integration") == "retired"
        assert restarted.handover("pending_addon") == "active"
        assert not restarted.known
        assert restarted.handover("addon") == "active"
        assert restarted.known


def test_heartbeat_handshake_and_summary(tmp_path, monkeypatch):
    settings = Settings(supervisor_token="unused", db_path=str(tmp_path / "db"))
    monkeypatch.setattr(authority, "resolve_data_dir", lambda _: tmp_path)
    store = AuthorityStore(settings)
    store.initialise()
    app_state = AppState(settings, tmp_path / "options.json")
    discovery = IntegrationDiscovery(supervisor_token="unused", host="addon", auth_token="test")
    context = execution_context()
    previous = context.authority
    previous_limits = context.limits_record
    context.authority = store
    body = fixture("site_snapshot_v4_max")
    body["authority_owner"] = "pending_integration"
    for inverter in body["topology"]["topology"]["inverters"]:
        inverter.update(transport="native", gateway_host="gateway.invalid")
    try:
        app = build_app(app_state, ingress_peer="proxy", integration=discovery)
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer test"
            response = client.post("/integration/heartbeat", json=body)
            assert response.status_code == 200
            assert response.json()["addon_authority"]["active"] == "retired"
            assert not store.known
            body["authority_owner"] = "pending_addon"
            body["topology"]["revision"] += 1
            body["topology"]["topology"]["inverters"][0]["transport"] = "legacy"
            response = client.post("/integration/heartbeat", json=body)
            assert response.status_code == 200
            assert response.json()["addon_authority"]["active"] == "active"
            assert not store.known
            body["authority_owner"] = "addon"
            assert client.post("/integration/heartbeat", json=body).status_code == 200
            assert store.known and store.state.committed_mode == "observe"
    finally:
        context.authority = previous
        context.limits_record = previous_limits
