"""Native validation at config-entry, snapshot and fake-register seams."""

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority import AuthorityStore
from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.native import async_probe
from custom_components.ha_spark.ownership import OwnershipStore
from custom_components.ha_spark.solis_transport import ReadResult, TransportError
from custom_components.ha_spark.transitions import TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_ownership import peer, peer_body
from tests_ha.test_polling import URL, fixture
from tests_ha.test_topology_flow import STORED_TOPOLOGY


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


async def native_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, *, two_units: bool = False,
) -> MockConfigEntry:
    topology = deepcopy(STORED_TOPOLOGY)
    topology["inverters"][0].update(transport="native", gateway_host="gateway.invalid")
    claims = [{"host": "gateway.invalid", "port": 502, "unit_id": 1}]
    if two_units:
        topology["inverters"].append({
            **topology["inverters"][0], "id": "inv2", "gateway_unit_id": 2,
        })
        claims.append({"host": "gateway.invalid", "port": 502, "unit_id": 2})
    body = peer_body(active="retired")
    body["legacy_overlay"] = {i["id"]: False for i in topology["inverters"]}
    aioclient_mock.post(URL, json=body)
    entry = MockConfigEntry(domain=DOMAIN, data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": "test-token", "session_id": body["session_id"],
    }, options={"topology": topology, "topology_revision": 1,
                "gateway_claims": claims})
    entry.add_to_hass(hass)
    authority = AuthorityStore(hass, entry.entry_id, lambda: None)
    await authority.async_load()
    owner = OwnershipStore(hass, entry.entry_id, authority)
    await owner.async_load()
    assert await owner.async_request("integration", peer()) is None
    await owner.async_advance(peer(active="retired"))
    assert owner.owner == "integration"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def devices(
    entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker,
) -> list[dict[str, Any]]:
    await entry.runtime_data.async_refresh()
    return list(aioclient_mock.mock_calls[-1][2]["devices"])


@pytest.mark.parametrize(("identity", "cached", "connected", "reason"), [
    ({}, False, True, None),
    ({"model": 0x27}, False, True, "unvalidated_identity"),
    ({"inverter_type": 0x2030}, False, True, "unvalidated_identity"),
    ({"tou": 0xAA55, "model": 0x27}, True, True, "v2_tou_firmware"),
    ({}, True, True, "caching_on"),
])
async def test_native_identity_snapshot_and_precondition(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, identity: dict[str, int], cached: bool,
    connected: bool, reason: str | None,
) -> None:
    native_transport.set_identity(**identity)
    native_transport.caching = cached
    native_transport.connected = connected
    entry = await native_entry(hass, aioclient_mock)
    device = (await devices(entry, aioclient_mock))[0]
    assert device["validation"] == ("validated" if reason is None else "read_only")
    assert device.get("reason") == reason
    assert device["capabilities"] == []
    preconditions = entry.runtime_data.control.preconditions()
    assert preconditions.devices_validated is (reason is None)
    assert not preconditions.evidence_configured
    if reason:
        issues = ir.async_get(hass).issues
        assert any(issue.translation_key == f"native_{reason}" for issue in issues.values())
    traffic = native_transport.traffic
    assert [(t.address, t.payload) for t in traffic[:4]] == [
        (33000, 20), (35000, 1), (33289, 1), (34502, 1),
    ]
    assert len(traffic) == 10
    assert all(t.op == "read" and t.bank == "input" for t in traffic)


@pytest.mark.parametrize("address", [33000, 35000, 33289, 34502])
@pytest.mark.parametrize(("error", "reason"), [
    (TransportError.EXCEPTION, "identity_read_failed"),
    (TransportError.BAD_RESPONSE, "identity_read_failed"),
    (TransportError.TIMEOUT, "unreachable"),
    (TransportError.DISCONNECTED, "unreachable"),
])
async def test_each_identity_read_must_succeed(
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    address: int, error: TransportError, reason: str,
) -> None:
    original = native_transport.read_input

    async def read(start: int, count: int, *, device_id: int = 1) -> ReadResult:
        if start == address:
            return ReadResult(error=error)
        return await original(start, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", read)
    assert (await async_probe(native_transport, 1)).reason == reason
    assert all(t.op == "read" for t in native_transport.traffic)


async def test_probe_failure_never_means_caching_off(
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = native_transport.read_input
    calls = 0

    async def read(start: int, count: int, *, device_id: int = 1) -> ReadResult:
        nonlocal calls
        calls += 1
        if calls == 8:
            return ReadResult(error=TransportError.TIMEOUT)
        return await original(start, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", read)
    result = await async_probe(native_transport, 1)
    assert result.reason == "identity_read_failed" and result.caching is None


@pytest.mark.parametrize("overlay", [None, {}, {"inv1": True}])
async def test_claim_and_overlay_rechecked_on_summary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, overlay: dict[str, bool] | None,
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    assert (await devices(entry, aioclient_mock))[0]["validation"] == "validated"
    body = peer_body(active="retired")
    body["legacy_overlay"] = overlay
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=body)
    await entry.runtime_data.async_refresh()
    assert (await devices(entry, aioclient_mock))[0]["reason"] == "gateway_not_owned"
    before = len(native_transport.traffic)
    await entry.runtime_data.async_reprobe("inv1")
    assert len(native_transport.traffic) == before + 10  # claim never blocks reads
    assert (await devices(entry, aioclient_mock))[0]["reason"] == "gateway_not_owned"


async def test_claim_loss_reconnect_and_protect_refusal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zoneinfo import ZoneInfo

    from homeassistant.util import dt as dt_util

    entry = await native_entry(hass, aioclient_mock)
    assert (await devices(entry, aioclient_mock))[0]["validation"] == "validated"
    # A new connection invalidates identity even before the reconnect check runs.
    monkeypatch.setattr(native_transport, "connection_epoch", 1, raising=False)
    assert (await devices(entry, aioclient_mock))[0]["reason"] == "unvalidated_identity"
    await entry.runtime_data.native.async_check_connections()
    assert (await devices(entry, aioclient_mock))[0]["validation"] == "validated"
    hass.config_entries.async_update_entry(entry, options={**entry.options, "gateway_claims": []})
    assert (await devices(entry, aioclient_mock))[0]["reason"] == "gateway_not_owned"
    decision = await entry.runtime_data.control.async_request(
        TransitionRequest(target="protect"), now=dt_util.utcnow(), zone=ZoneInfo("UTC"),
    )
    assert decision.refusal == "preconditions_unmet"
    assert "devices_validated" in decision.reasons
    assert all(t.op == "read" for t in native_transport.traffic)
    await hass.async_block_till_done()


async def test_unreachable_saved_device_has_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_transport, "read_input", AsyncMock(
        return_value=ReadResult(error=TransportError.DISCONNECTED),
    ))
    entry = await native_entry(hass, aioclient_mock)
    assert (await devices(entry, aioclient_mock))[0]["reason"] == "unreachable"
    assert any(i.translation_key == "native_unreachable"
               for i in ir.async_get(hass).issues.values())


@pytest.mark.parametrize("unreachable", [False, True])
async def test_connect_flow_reuses_gateway_and_saves_unreachable_claim(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch, unreachable: bool,
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.ha_spark import native
    from tests_ha.test_topology_flow import choose, open_topology

    entry = await native_entry(hass, aioclient_mock)
    if unreachable:
        monkeypatch.setattr(native_transport, "read_input", AsyncMock(
            return_value=ReadResult(error=TransportError.DISCONNECTED),
        ))
    factory = AsyncMock(side_effect=AssertionError("second connection"))
    monkeypatch.setattr(native, "transport_factory", factory)
    flow_id = await open_topology(hass, entry)
    await choose(hass, flow_id, "inverters")
    await choose(hass, flow_id, "inverters_edit")
    await hass.config_entries.options.async_configure(flow_id, {"id": "inv1"})
    item = dict(entry.options["topology"]["inverters"][0])
    result = await hass.config_entries.options.async_configure(flow_id, item)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "native_connect"
    before = len(native_transport.traffic)
    result = await hass.config_entries.options.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.MENU
    factory.assert_not_called()
    assert native_transport.connected  # the loaded connection was not closed
    if not unreachable:
        assert len(native_transport.traffic) == before + 10
    # Restore factory only for setup after the atomic options save.
    def restored_factory(host: str, port: int) -> FakeSolisDevice:
        native_transport.connected = True
        return native_transport

    monkeypatch.setattr(native, "transport_factory", restored_factory)
    await choose(hass, flow_id, "inverters_done")
    result = await choose(hass, flow_id, "save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["gateway_claims"] == [
        {"host": "gateway.invalid", "port": 502, "unit_id": 1},
    ]
    device = (await devices(entry, aioclient_mock))[0]
    assert device["validation"] == ("read_only" if unreachable else "validated")
    if unreachable:
        assert device["reason"] == "unreachable"


async def test_shared_gateway_and_pending_owner_never_validates(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark import native

    created: list[tuple[str, int]] = []

    def factory(host: str, port: int) -> FakeSolisDevice:
        created.append((host, port))
        return native_transport

    monkeypatch.setattr(native, "transport_factory", factory)
    entry = await native_entry(hass, aioclient_mock, two_units=True)
    assert len(created) == 1
    snapshot_devices = await devices(entry, aioclient_mock)
    assert {d["id"] for d in snapshot_devices} == {"inv1", "inv2"}
    assert all(d["validation"] == "validated" for d in snapshot_devices)
    assert {t.device_id for t in native_transport.traffic} == {1, 2}
    assert await entry.runtime_data.ownership.async_request("addon", peer(active="retired")) is None
    # Pending owner fences validation even while the bus remains readable.
    entry.runtime_data.native.recheck()
    assert all(entry.runtime_data.native.reason(i) == "gateway_not_owned"
               for i in entry.runtime_data.native.inverters.values())
    before = len(native_transport.traffic)
    await entry.runtime_data.async_reprobe("inv2")
    assert len(native_transport.traffic) == before + 10


async def test_identity_cannot_span_connections(
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = native_transport.read_input

    async def read(start: int, count: int, *, device_id: int = 1) -> ReadResult:
        if start == 33289:
            monkeypatch.setattr(native_transport, "connection_epoch", 1, raising=False)
        return await original(start, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", read)
    result = await async_probe(native_transport, 1)
    assert result.reason == "identity_read_failed"


async def test_caching_detection_fences_whole_gateway(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice,
) -> None:
    entry = await native_entry(hass, aioclient_mock, two_units=True)
    native_transport.caching = True
    await entry.runtime_data.async_reprobe("inv1")
    assert all(d["reason"] == "caching_on" for d in await devices(entry, aioclient_mock))




async def test_failed_reprobe_keeps_gateway_caching_fence(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await native_entry(hass, aioclient_mock, two_units=True)
    native_transport.caching = True
    await entry.runtime_data.async_reprobe("inv1")
    original = native_transport.read_input

    async def failing(start: int, count: int, *, device_id: int = 1) -> ReadResult:
        if device_id == 1:
            return ReadResult(0.04, (), TransportError.EXCEPTION, 4)
        return await original(start, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", failing)
    # A failed probe on one unit never clears the gateway-wide caching fence.
    await entry.runtime_data.async_reprobe("inv1")
    assert [d["reason"] for d in await devices(entry, aioclient_mock)] == [
        "identity_read_failed", "caching_on",
    ]
    monkeypatch.setattr(native_transport, "read_input", original)
    native_transport.caching = False
    await entry.runtime_data.async_reprobe("inv1")
    assert all(d["validation"] == "validated" for d in await devices(entry, aioclient_mock))


async def test_connect_probe_of_new_unit_fences_resident_devices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice,
) -> None:
    from custom_components.ha_spark import native

    entry = await native_entry(hass, aioclient_mock)
    coordinator = entry.runtime_data
    native_transport.caching = True
    # What the options flow does for a unit not yet resident on a held gateway.
    probe = await native.async_probe(native_transport, 3)
    resident = coordinator.native.inverters["inv1"]
    coordinator.native.record_caching(resident.model_copy(update={"id": "new"}), probe)
    coordinator.native.recheck()
    assert (await devices(entry, aioclient_mock))[0]["reason"] == "caching_on"


async def test_precondition_uses_current_topology(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    assert entry.runtime_data.control.preconditions().devices_validated
    topology = deepcopy(entry.options["topology"])
    topology["inverters"].append({**topology["inverters"][0], "id": "inv2", "gateway_unit_id": 2})
    # Options saved but not yet reloaded: the unprobed inverter fails closed.
    hass.config_entries.async_update_entry(entry, options={**entry.options, "topology": topology})
    assert not entry.runtime_data.control.preconditions().devices_validated
    await hass.async_block_till_done()


async def test_unload_fences_outstanding_probe(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    entry = await native_entry(hass, aioclient_mock)
    coordinator = entry.runtime_data
    original = native_transport.read_input
    gate = asyncio.Event()

    async def blocked(start: int, count: int, *, device_id: int = 1) -> ReadResult:
        await gate.wait()
        return await original(start, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", blocked)
    task = hass.async_create_task(coordinator.async_reprobe("inv1"))
    await asyncio.sleep(0)
    await coordinator.native.async_close()
    gate.set()
    assert await task is False
    assert await coordinator.async_reprobe("inv1") is False
    gate.set()
    monkeypatch.setattr(native_transport, "read_input", original)
    await hass.async_block_till_done()
