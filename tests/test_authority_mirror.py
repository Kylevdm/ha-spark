"""Authority mirrors cross HTTP only as an allowlisted, read-only projection."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ha_spark import authority
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore, FallbackState, HoldDevice, HoldRecord, Transition
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel
from ha_spark.integration_discovery import IntegrationDiscovery
from tests.test_integration_route import SNAPSHOT_BODY


@pytest.mark.parametrize("store_status", ["missing", "corrupt", "mismatch", "known"])
@pytest.mark.parametrize("save_failed", [False, True])
def test_authority_mirror_http(tmp_path, monkeypatch, store_status, save_failed):
    monkeypatch.setattr(authority, "resolve_data_dir", lambda settings: tmp_path)
    settings = Settings(supervisor_token="test", db_path=str(tmp_path / "events.db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.state is not None
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    canary = "HOUSEHOLD-CANARY"
    device = HoldDevice(driver=canary, power_switch=canary, hub=canary, slave=247)
    store.save(store.state.model_copy(update={
        "committed_mode": "protect", "last_changed_at": now,
        "transition": Transition(target="optimise", started_at=now,
                                 failing_actions=(canary,), steps=(canary,)),
        "fallback": FallbackState(active=True, causes=("soc_integrity", canary)),
        "holds": (
            HoldRecord(cause="ev", started_at=now, device=device),
            HoldRecord(cause="dispatch", started_at=now, device=device, trusted_end=now),
            HoldRecord(cause="dispatch", started_at=now, device=device,
                       trusted_end=datetime(2026, 10, 24, 14, tzinfo=UTC)),
        ),
    }))
    if store_status == "missing":
        store.store_path.unlink()
        store.marker_path.unlink()
    elif store_status == "corrupt":
        store.store_path.write_text("invalid")
    elif store_status == "mismatch":
        marker = json.loads(store.marker_path.read_text())
        marker["store_id"] = "00000000-0000-0000-0000-000000000000"
        store.marker_path.write_text(json.dumps(marker))
    state = AppState(settings, tmp_path / "options.json")
    channel = DaemonChannel(settings)
    channel.authority = store
    store.save_failed = save_failed
    state.executor = channel
    discovery = IntegrationDiscovery(supervisor_token="unused", host="addon", auth_token="test")
    before = {p.name: p.read_bytes() for p in tmp_path.glob("authority*")}
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", content=SNAPSHOT_BODY,
                               headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    block = response.json()["authority"]
    assert canary not in response.text
    assert {p.name: p.read_bytes() for p in tmp_path.glob("authority*")} == before
    assert block == {
        "mode": "protect" if store_status == "known" else "authority_unknown",
        "transition_target": "optimise" if store_status == "known" else None,
        "last_changed_at": now.isoformat() if store_status == "known" else None,
        "save_failed": save_failed,
        "fallback": {"active": store_status == "known",
                     "causes": ["soc_integrity"] if store_status == "known" else []},
        "holds": {"ev": {"active": store_status == "known"}, "dispatch": {
            "active": store_status == "known",
            "trusted_end": "2026-10-24T14:00:00+00:00" if store_status == "known" else None,
        }},
    }
