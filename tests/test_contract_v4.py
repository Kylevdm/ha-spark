"""Contract v4 intake is display-only and rejected snapshots have no side effects."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark.api import server, site_pairing
from ha_spark.api.contract import (
    API_VERSION,
    SUPPORTED_API_VERSIONS,
    AuthorityInputs,
    IntegrationLink,
    SiteSnapshot,
    link_view,
    plan_summary,
    snapshot_acceptable,
)
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore, load_authority
from ha_spark.config import Settings
from ha_spark.devices.execution import execution_context
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import LoadedTopology
from ha_spark.integration_discovery import IntegrationDiscovery

FIXTURES = Path(__file__).parent / "fixtures" / "contract"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
OTHER_SITE = "00000000-0000-0000-0000-000000000001"


def fixture(name="site_snapshot_v4"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def state(tmp_path):
    return AppState(Settings(supervisor_token="unused", db_path=str(tmp_path / "spark.db")),
                    tmp_path / "options.json")


@pytest.fixture
def client(state):
    discovery = IntegrationDiscovery(supervisor_token="unused", host="addon", auth_token="test")
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        client.headers["Authorization"] = "Bearer test"
        yield client


def post(client, body):
    return client.post("/integration/heartbeat", json=body)


def test_shared_versions():
    contract = fixture("contract")
    assert API_VERSION == contract["api_version"] == 4
    assert sorted(SUPPORTED_API_VERSIONS) == contract["supported_api_versions"] == [1, 2, 3, 4]


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("seconds,accepted", [
    (-301, False), (-300, True), (300, True), (301, False),
])
def test_snapshot_clock_boundary(version, seconds, accepted):
    raw = fixture() | {"api_version": version,
                       "sent_at": (NOW + timedelta(seconds=seconds)).isoformat()}
    snapshot = SiteSnapshot.model_validate(raw)
    assert snapshot_acceptable(IntegrationLink(None, None, False), snapshot, NOW) == accepted


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("change", ["older", "other_site", "future", "past"])
def test_rejected_snapshot_changes_nothing(client, state, monkeypatch, version, change):
    body = fixture() | {"api_version": version}
    assert post(client, body).status_code == 200
    before = state.integration_link
    limits = execution_context().limits_record
    changed = dict(body)
    if change == "other_site":
        changed["site_id"] = OTHER_SITE
    else:
        changed["sent_at"] = {
            "older": "2026-10-24T11:59:57Z", "future": "2026-10-24T12:10:00Z",
            "past": "2026-10-24T11:50:00Z",
        }[change]
    # No fresh receipt, no resurrected solar/topology, and the old link ages out normally.
    monkeypatch.setattr(server, "_heartbeat_now", lambda: NOW + timedelta(seconds=91))
    response = post(client, changed)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid heartbeat"}
    assert state.integration_link is before
    assert execution_context().limits_record is limits
    assert link_view(before, NOW + timedelta(seconds=91))["state"] == "unreachable"


def test_equal_timestamp_is_accepted(client):
    assert post(client, fixture()).status_code == 200
    assert post(client, fixture()).status_code == 200


def test_pairing_survives_restart(client, state):
    assert post(client, fixture()).status_code == 200
    path = Path(state.settings.db_path).parent / site_pairing.PAIRING_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    state.integration_link = IntegrationLink(None, None, False)
    assert post(client, fixture() | {"site_id": OTHER_SITE}).status_code == 400
    assert state.integration_link.snapshot is None
    assert post(client, fixture()).status_code == 200


@pytest.mark.parametrize("contents", [b"broken", b"{}", b"[]", b"x" * 1025])
def test_corrupt_pairing_never_repairs_itself(client, state, contents):
    path = Path(state.settings.db_path).parent / site_pairing.PAIRING_FILE
    path.write_bytes(contents)
    assert post(client, fixture()).status_code == 400
    assert path.read_bytes() == contents
    assert state.integration_link.snapshot is None


@pytest.mark.parametrize("failure", ["save", "reload", "read"])
def test_pairing_failure_is_closed(client, state, monkeypatch, failure):
    if failure == "save":
        def fail(*args):
            raise OSError("PAIRING-SECRET")
        monkeypatch.setattr(site_pairing.os, "replace", fail)
    elif failure == "reload":
        original = site_pairing.load_pairing
        monkeypatch.setattr(site_pairing, "load_pairing", lambda settings: (None, False))
        # The server imported its read function separately; only the verification fails.
        assert original(state.settings) == (None, True)
    else:
        path = Path(state.settings.db_path).parent / site_pairing.PAIRING_FILE
        path.mkdir()
    response = post(client, fixture())
    assert response.status_code == 400
    assert response.json() == {"error": "invalid heartbeat"}
    assert state.integration_link.snapshot is None


@pytest.mark.parametrize("key,value", [
    ("authority_owner", None), ("authority_owner", "invalid"),
    ("authority", None), ("holds", None), ("devices", None), ("devices", []),
])
def test_invalid_required_v4_blocks_reject_whole(client, state, key, value):
    body = fixture("site_snapshot_v4_max")
    body[key] = value
    assert post(client, body).status_code == 400
    assert state.integration_link.snapshot is None
    assert site_pairing.load_pairing(state.settings) == (None, True)


def test_absent_owner_rejects(client):
    body = fixture()
    del body["authority_owner"]
    assert post(client, body).status_code == 400


@pytest.mark.parametrize("mutation", ["duplicate", "foreign", "reason"])
def test_device_identity_set_is_exact(client, mutation):
    body = fixture("site_snapshot_v4_max")
    if mutation == "duplicate":
        body["devices"][1] = body["devices"][0]
    elif mutation == "foreign":
        body["devices"][0]["id"] = "foreign"
    else:
        body["devices"][0]["validation"] = "read_only"
    assert post(client, body).status_code == 400


@pytest.mark.parametrize("known,save_failed,expected", [
    (True, False, "ok"), (True, True, "save_failed"), (False, False, "unknown"),
])
def test_v4_summary_addon_facts(state, monkeypatch, tmp_path, known, save_failed, expected):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda settings: tmp_path)
    store = AuthorityStore(state.settings)
    store.initialise()
    topology = Topology.model_validate(fixture("site_snapshot_v4_max")["topology"]["topology"])
    settings = state.settings.model_copy(update={"solis_control_hub": "configured"})
    summary = plan_summary(
        api_version=4, session_id="session", built_at=NOW, loop_tick=NOW, settings=settings,
        authority=AuthorityInputs(store.state, known, save_failed),
        topology=LoadedTopology("native", 1, topology),
    )
    assert summary["addon_authority"] == {
        "active": "active", "mode": "observe" if known else "authority_unknown",
        "holds_present": False, "store_state": expected,
    }
    assert summary["legacy_overlay"] == {
        inverter.id: inverter.driver_kind == "solis" for inverter in topology.inverters
    }
    assert summary["export_acks"] == []
    legacy = plan_summary(api_version=3, session_id="session", built_at=NOW, loop_tick=NOW,
                          authority=AuthorityInputs(store.state, known, save_failed))
    assert summary["authority"] == legacy["authority"]
    assert "addon_authority" not in legacy


def test_epoch_monotonic_within_store_identity(client, state):
    body = fixture("site_snapshot_v4_max")
    assert post(client, body).status_code == 200
    before = state.integration_link
    body["authority"]["epoch"] -= 1
    assert post(client, body).status_code == 400
    assert state.integration_link is before
    body["authority"]["store_identity"] = "new_store"
    assert post(client, body).status_code == 200
    assert state.integration_link.authority_epoch == 41


def test_field_local_v4_failures(client, state):
    body = fixture("site_snapshot_v4_max")
    body["evidence"] = {"ev_status": "SECRET"}
    body["export"] = "SECRET"
    body["supply_guard"]["connections"][0]["writer"] = "SECRET"
    del body["banks"][0]["soc"]
    body["banks"][1]["soc"]["value"] = 101
    assert post(client, body).status_code == 200
    v4 = state.integration_link.v4
    assert v4.evidence is None and v4.evidence_reason == "invalid_evidence"
    assert v4.export == () and v4.export_reason == "invalid_export"
    assert v4.writers == () and v4.writers_reason == "invalid_writer"
    assert v4.banks[0].soc is None and v4.banks[1].soc is None
    assert state.integration_link.supply_guard is not None


def test_addon_owner_ignores_remote_authority_holds_export(client, state):
    body = fixture() | {"authority": "bad", "holds": "bad", "export": "bad"}
    assert post(client, body).status_code == 200
    v4 = state.integration_link.v4
    assert v4.authority is None and v4.holds == () and v4.export == ()


def test_max_v4_fits_and_is_accepted(client, state):
    body = fixture("site_snapshot_v4_max")
    compact = json.dumps(body, separators=(",", ":")).encode()
    assert len(compact) < 48 * 1024
    assert client.post("/integration/heartbeat", content=compact).status_code == 200
    v4 = state.integration_link.v4
    assert len(v4.devices) == 8 and len(v4.banks) == 6
    assert len(v4.writers) == 4 and len(v4.export) == 8
    assert client.post("/integration/heartbeat", content=b" " * (64 * 1024 + 1)).status_code == 400


@pytest.mark.parametrize("bad_time", [0, True, "2026-10-24T12:00:00",
                                      "0001-01-01T00:00:00+14:00"])
def test_invalid_native_timestamp_rejects(client, bad_time):
    body = fixture("site_snapshot_v4_max")
    body["authority"]["manual_session"]["deadline"] = bad_time
    assert post(client, body).status_code == 400


def test_incomplete_trusted_soc_is_untrusted(client, state):
    body = fixture("site_snapshot_v4_max")
    body["banks"][0]["soc"]["value"] = None
    assert post(client, body).status_code == 200
    assert state.integration_link.v4.banks[0].soc is None


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_durable_pairing_applies_to_legacy_snapshots(client, state, version):
    assert post(client, fixture()).status_code == 200
    state.integration_link = IntegrationLink(None, None, False)
    body = fixture() | {"api_version": version, "site_id": OTHER_SITE}
    assert post(client, body).status_code == 400


@pytest.mark.parametrize("devices", ["bad", [{"id": "foreign"}], [{
    "id": "foreign", "transport": "native", "validation": "validated", "capabilities": [],
}]])
def test_addon_owner_invalid_devices_drop_only_the_block(client, state, devices):
    assert post(client, fixture() | {"devices": devices}).status_code == 200
    v4 = state.integration_link.v4
    assert v4.devices == () and v4.devices_reason == "invalid_devices"


@pytest.mark.parametrize("version", [1, 2, 3])
def test_corrupt_pairing_rejects_legacy_snapshots(client, state, version):
    (Path(state.settings.db_path).parent / site_pairing.PAIRING_FILE).write_bytes(b"broken")
    assert post(client, fixture() | {"api_version": version}).status_code == 400
    assert state.integration_link.snapshot is None


def test_integration_owned_snapshot_grants_nothing(client, state):
    mirror = post(client, fixture()).json()["authority"]
    authority = execution_context().authority
    stored = load_authority(state.settings)
    response = post(client, fixture("site_snapshot_v4_max"))
    assert response.status_code == 200
    assert execution_context().authority is authority
    assert load_authority(state.settings) == stored
    assert response.json()["authority"] == mirror
