"""Site replacement preserves accepted limits and changes only the revision binding."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ha_spark.api import site_pairing
from ha_spark.api.server import AppState, build_app
from ha_spark.api.snapshot_v4 import parse_snapshot_v4
from ha_spark.authority import AuthorityStore, Transition
from ha_spark.config import Settings
from ha_spark.devices.execution import execution_context
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import (
    LoadedTopology,
    for_site,
    load_topology_record,
    save_topology_record,
    select_limits,
)
from ha_spark.integration_discovery import IntegrationDiscovery

OLD = "00000000-0000-0000-0000-000000000001"
NEW = "00000000-0000-0000-0000-000000000002"


def snapshot(name="site_snapshot_v4"):
    return json.loads((Path(__file__).parent / f"fixtures/contract/{name}.json").read_text())


@pytest.fixture(autouse=True)
def restore_execution_inputs():
    context = execution_context()
    limits, loads = context.limits_record, context.major_loads
    yield
    context.limits_record, context.major_loads = limits, loads


@pytest.fixture
def state(settings):
    return AppState(settings, Path(settings.db_path).parent / "options.json")


@pytest.fixture
def client(state):
    discovery = IntegrationDiscovery(supervisor_token="unused", host="addon", auth_token="test")
    app = build_app(state, ingress_peer="proxy", integration=discovery)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://addon",
        headers={"Authorization": "Bearer test"},
    )


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    return Settings(supervisor_token="unused", db_path=str(tmp_path / "spark.db"))


@pytest.fixture
def paired(settings):
    raw = json.loads((Path(__file__).parent / "fixtures/limits/owner_below_driver_ceiling.json")
                     .read_text())
    topology = Topology.model_validate(raw["topology"])
    save_topology_record(settings, 9, topology, (9, topology))
    assert site_pairing.save_pairing(settings, OLD)
    return load_topology_record(settings)


def test_replace_keeps_limits_and_clears_only_foreign_revision(settings, paired):
    assert site_pairing.replace_pairing(settings, OLD, NEW) is None
    assert site_pairing.load_pairing(settings) == (NEW, True)
    loaded = load_topology_record(settings)
    assert loaded.site_id == OLD
    assert for_site(loaded, OLD).revision == 9
    cleared = for_site(loaded, NEW)
    assert cleared.revision is None
    assert cleared.topology == paired.topology
    assert cleared.supported_topology == paired.supported_topology
    assert cleared.supported_revision == 9
    assert select_limits(cleared, settings) == select_limits(paired, settings)
    assert site_pairing.replace_pairing(settings, NEW, OLD) is None
    assert for_site(load_topology_record(settings), OLD).revision == 9


@pytest.mark.parametrize("old,new,reason", [
    ("not-a-site", NEW, "invalid_site_id"),
    (OLD, "not-a-site", "invalid_site_id"),
    (OLD, NEW.replace("-", ""), "invalid_site_id"),
    (OLD, OLD, "same_site"),
    (NEW, OLD, "pairing_mismatch"),
])
def test_replace_identity_refusals(settings, paired, old, new, reason):
    assert site_pairing.replace_pairing(settings, old, new) == reason
    assert site_pairing.load_pairing(settings) == (OLD, True)
    assert load_topology_record(settings) == paired


@pytest.mark.parametrize("corrupt", [False, True])
def test_replace_requires_trusted_existing_pair(settings, corrupt):
    if corrupt:
        (Path(settings.db_path).parent / site_pairing.PAIRING_FILE).write_bytes(b"broken")
    assert site_pairing.replace_pairing(settings, OLD, NEW) == (
        "pairing_untrusted" if corrupt else "pairing_mismatch"
    )


@pytest.mark.parametrize("after_commit", [False, True])
def test_crash_at_pairing_commit_never_mixes_binding(settings, paired, monkeypatch, after_commit):
    original = site_pairing._durable_write

    class Crash(BaseException):
        pass

    def crash(path, body):
        if after_commit:
            original(path, body)
        raise Crash

    monkeypatch.setattr(site_pairing, "_durable_write", crash)
    with pytest.raises(Crash):
        site_pairing.replace_pairing(settings, OLD, NEW)
    site, trusted = site_pairing.load_pairing(settings)
    assert trusted and site == (NEW if after_commit else OLD)
    restarted = AppState(settings, Path(settings.db_path).parent / "options.json")
    assert restarted.integration_link.topology.revision == (None if after_commit else 9)
    assert restarted.integration_link.topology.topology == paired.topology
    assert restarted.integration_link.topology.supported_topology == paired.supported_topology


async def test_intake_lock_blocks_replace(settings, paired):
    state = AppState(settings, Path(settings.db_path).parent / "options.json")
    async with state.intake_lock:
        task = asyncio.create_task(state.replace_pairing(OLD, NEW))
        await asyncio.sleep(0)
        assert not task.done()
        assert site_pairing.load_pairing(settings) == (OLD, True)
    assert await task is None
    assert state.integration_link.snapshot is None
    assert state.integration_link.topology.revision is None
    assert state.integration_link.topology.topology == paired.topology


@pytest.mark.parametrize("failure", [
    "bind", "bind_reload", "commit", "commit_reload", "topology_reload",
])
def test_failed_replacement_never_unpairs(settings, paired, monkeypatch, failure):
    original_load = site_pairing.load_topology_record
    original_write = site_pairing._durable_write
    original_pair = site_pairing.load_pairing

    def fail(*args, **kwargs):
        raise OSError("PRIVATE-HOST")

    if failure == "bind":
        monkeypatch.setattr(site_pairing, "save_topology_record", fail)
    elif failure == "bind_reload":
        monkeypatch.setattr(site_pairing, "load_topology_record", lambda _: paired)
    elif failure == "commit":
        monkeypatch.setattr(site_pairing, "_durable_write", fail)
    else:
        def write(path, body):
            original_write(path, body)
            if failure == "commit_reload":
                monkeypatch.setattr(site_pairing, "load_pairing", lambda _: (None, False))
            else:
                monkeypatch.setattr(site_pairing, "load_topology_record", lambda _: LoadedTopology(
                    "unavailable", reason="topology_record_unavailable",
                ))
        monkeypatch.setattr(site_pairing, "_durable_write", write)
    assert site_pairing.replace_pairing(settings, OLD, NEW) == "pairing_save_failed"
    assert (Path(settings.db_path).parent / site_pairing.PAIRING_FILE).exists()
    site, trusted = original_pair(settings)
    assert trusted
    committed = failure in ("commit_reload", "topology_reload")
    assert site == (NEW if committed else OLD)
    assert for_site(original_load(settings), site).revision == (None if committed else 9)


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise"])
async def test_replace_mode_admission_and_authority_untouched(settings, paired, state, mode):
    store = AuthorityStore(settings)
    store.initialise()
    store.save(store.state.model_copy(update={"committed_mode": mode}))
    directory = Path(settings.db_path).parent
    paths = [directory / "authority.json", directory / "authority.marker.json"]
    before = [path.read_bytes() for path in paths]
    context = execution_context()
    authority, limits, loads = context.authority, context.limits_record, context.major_loads
    commands = state.commands
    assert await state.replace_pairing(OLD, NEW) == (
        None if mode == "observe" else "mode_not_observe"
    )
    assert [path.read_bytes() for path in paths] == before
    assert context.authority is authority
    assert context.limits_record is limits
    assert context.major_loads is loads
    assert state.commands is commands


async def test_replace_transition_pending(settings, paired, state):
    store = AuthorityStore(settings)
    store.initialise()
    transition = Transition(target="protect", started_at=datetime(2026, 10, 24, tzinfo=UTC))
    store.save(store.state.model_copy(update={"transition": transition}))
    assert await state.replace_pairing(OLD, NEW) == "transition_pending"
    assert site_pairing.load_pairing(settings) == (OLD, True)
    assert load_topology_record(settings) == paired


@pytest.mark.parametrize("owner", ["pending_integration", "pending_addon"])
async def test_replace_snapshot_handshake_pending(settings, paired, state, owner):
    body = snapshot() | {"authority_owner": owner}
    state.integration_link = replace(state.integration_link, v4=parse_snapshot_v4(body, None))
    before = state.integration_link
    assert await state.replace_pairing(OLD, NEW) == "handshake_pending"
    assert state.integration_link is before
    assert site_pairing.load_pairing(settings) == (OLD, True)
    # Identity refusals take precedence over authority checks.
    assert await state.replace_pairing("invalid", NEW) == "invalid_site_id"
    assert await state.replace_pairing(NEW, OLD) == "pairing_mismatch"


async def test_replace_marker_handshake_pending(settings, paired, state):
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    assert store.handover("pending_addon") == "active"
    paths = [Path(settings.db_path).parent / name for name in
             ("authority.json", "authority.marker.json")]
    before = [path.read_bytes() for path in paths]
    assert await state.replace_pairing(OLD, NEW) == "handshake_pending"
    assert [path.read_bytes() for path in paths] == before
    assert load_topology_record(settings) == paired


async def test_replace_keeps_topology_until_new_revision_one(settings, paired, state, client):
    old_limits = execution_context().limits_record
    assert await state.replace_pairing(OLD, NEW) is None
    assert state.integration_link.topology.topology == paired.topology
    assert execution_context().limits_record is old_limits
    body = snapshot() | {"site_id": OLD, "api_version": 3}
    before = state.integration_link
    async with client:
        response = await client.post("/integration/heartbeat", json=body)
        assert response.status_code == 400
        assert response.json() == {
            "error": "site_not_paired", "detail": site_pairing.SITE_NOT_PAIRED_TEXT,
        }
        assert state.refused_site == OLD
        assert state.integration_link is before
        body["site_id"] = NEW
        assert (await client.post("/integration/heartbeat", json=body)).status_code == 200
        assert state.integration_link.topology.topology == paired.topology
        assert state.integration_link.topology.revision is None
        body["topology"] = {
            "configured": True, "revision": 1,
            "topology": paired.topology.model_dump(mode="json"),
        }
        assert (await client.post("/integration/heartbeat", json=body)).status_code == 200
        assert state.integration_link.topology.revision == 1
        assert load_topology_record(settings).revision == 1
        assert load_topology_record(settings).site_id is None
        # Same-revision edits cannot replace the newly accepted topology.
        accepted = state.integration_link.topology.topology
        body["topology"]["topology"]["inverters"][0]["confirmed"] = False
        assert (await client.post("/integration/heartbeat", json=body)).status_code == 200
        assert state.integration_link.topology.topology == accepted
        assert state.integration_link.topology.reason == "invalid_topology"


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_site_not_paired_all_versions(settings, paired, state, client, version):
    before = state.integration_link
    async with client:
        response = await client.post("/integration/heartbeat", json=snapshot() | {
            "site_id": NEW, "api_version": version,
        })
    assert response.status_code == 400
    assert response.json() == {
        "error": "site_not_paired",
        "detail": "The integration was re-added and this site is not paired. "
                  "Use ha-spark pair --replace to replace the pairing.",
    }
    assert state.refused_site == NEW
    assert state.integration_link is before
    assert site_pairing.load_pairing(settings) == (OLD, True)
    assert load_topology_record(settings) == paired


async def test_intake_lock_blocks_snapshot(state, client, settings, paired):
    async with client:
        async with state.intake_lock:
            task = asyncio.create_task(client.post(
                "/integration/heartbeat", json=snapshot() | {"site_id": OLD},
            ))
            await asyncio.sleep(0)
            assert not task.done()
            assert state.integration_link.snapshot is None
        assert (await task).status_code == 200
    assert state.integration_link.snapshot.site_id == OLD


@pytest.mark.parametrize("kind", ["missing", "unavailable"])
def test_replace_without_readable_topology_keeps_its_status(settings, kind):
    assert site_pairing.save_pairing(settings, OLD)
    if kind == "unavailable":
        (Path(settings.db_path).parent / "ha_spark_topology.initialised").write_bytes(b"{}")
        (Path(settings.db_path).parent / "ha_spark_topology.json").write_bytes(b"broken")
    before = load_topology_record(settings)
    assert site_pairing.replace_pairing(settings, OLD, NEW) is None
    assert load_topology_record(settings) == before
    assert before.kind == ("legacy" if kind == "missing" else "unavailable")


@pytest.mark.parametrize("site", ["INVALID", OLD.replace("-", ""), OLD.upper().replace("1", "A")])
def test_topology_binding_requires_canonical_uuid(settings, paired, site):
    with pytest.raises(OSError, match="^topology_save_failed$"):
        save_topology_record(settings, 9, paired.topology, site_id=site)
    assert load_topology_record(settings) == paired


async def test_second_replace_keeps_original_binding(settings, paired, state):
    third = "00000000-0000-0000-0000-000000000003"
    assert await state.replace_pairing(OLD, NEW) is None
    assert await state.replace_pairing(NEW, third) is None
    loaded = load_topology_record(settings)
    assert loaded.site_id == OLD
    assert for_site(loaded, third).revision is None
    assert loaded.topology == paired.topology


async def test_new_v4_site_revision_one_accepted(settings, paired, state, client):
    assert await state.replace_pairing(OLD, NEW) is None
    body = snapshot("site_snapshot_v4_max")
    body.update(site_id=NEW, authority_owner="addon")
    body["topology"]["revision"] = 1
    async with client:
        response = await client.post("/integration/heartbeat", json=body)
    assert response.status_code == 200
    assert state.integration_link.snapshot.site_id == NEW
    assert state.integration_link.topology.revision == 1
    assert load_topology_record(settings).revision == 1


@pytest.mark.parametrize("mode,store_state,transition,reason", [
    ("observe", "ok", None, None),
    ("protect", "ok", None, "mode_not_observe"),
    ("optimise", "ok", None, "mode_not_observe"),
    ("optimise", "unknown", None, None),
    ("observe", "ok", {"target": "protect", "steps": [], "failing_actions": []},
     "transition_pending"),
])
async def test_native_authority_admission_is_read_only(
    settings, paired, state, mode, store_state, transition, reason,
):
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    body = snapshot("site_snapshot_v4_max")
    body["authority"].update(mode=mode, store_state=store_state, transition=transition)
    topology = Topology.model_validate(body["topology"]["topology"])
    state.integration_link = replace(
        state.integration_link, received_at=datetime(2026, 10, 24, 12, tzinfo=UTC),
        v4=parse_snapshot_v4(body, topology),
    )
    directory = Path(settings.db_path).parent
    paths = [directory / "authority.json", directory / "authority.marker.json"]
    before = [path.read_bytes() for path in paths]
    assert await state.replace_pairing(OLD, NEW) == reason
    assert [path.read_bytes() for path in paths] == before


async def test_failed_replace_leaves_live_link_and_limits(settings, paired, state, monkeypatch):
    before = state.integration_link
    limits = execution_context().limits_record

    def fail(*args):
        raise OSError("PRIVATE-HOST")

    monkeypatch.setattr(site_pairing, "_durable_write", fail)
    assert await state.replace_pairing(OLD, NEW) == "pairing_save_failed"
    assert state.integration_link is before
    assert execution_context().limits_record is limits


async def test_replace_back_restores_old_floor(settings, paired, state, client):
    assert await state.replace_pairing(OLD, NEW) is None
    assert state.integration_link.topology.revision is None
    assert await state.replace_pairing(NEW, OLD) is None
    assert state.integration_link.topology.revision == 9
    body = snapshot() | {"site_id": OLD, "api_version": 3, "topology": {
        "configured": True, "revision": 8,
        "topology": paired.topology.model_dump(mode="json"),
    }}
    async with client:
        assert (await client.post("/integration/heartbeat", json=body)).status_code == 200
    assert state.integration_link.topology.revision == 9
    assert state.integration_link.topology.reason == "invalid_topology"
    assert load_topology_record(settings).revision == 9


async def test_failure_after_commit_follows_the_new_pairing(
    settings, paired, state, client, monkeypatch,
):
    original = site_pairing._durable_write

    def write(path, body):
        original(path, body)
        raise OSError("dir fsync")

    body = snapshot() | {"site_id": NEW, "api_version": 3, "topology": {
        "configured": True, "revision": 1,
        "topology": paired.topology.model_dump(mode="json"),
    }}
    async with client:
        assert (await client.post("/integration/heartbeat",
                                  json=snapshot() | {"site_id": OLD})).status_code == 200
        monkeypatch.setattr(site_pairing, "_durable_write", write)
        assert await state.replace_pairing(OLD, NEW) == "pairing_save_failed"
        assert site_pairing.load_pairing(settings) == (NEW, True)
        assert state.integration_link.snapshot is None
        assert state.integration_link.topology.revision is None
        assert state.integration_link.topology.topology == paired.topology
        assert (await client.post("/integration/heartbeat", json=body)).status_code == 200
    assert state.integration_link.topology.revision == 1
