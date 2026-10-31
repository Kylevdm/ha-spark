"""Durable handshake at the config-entry/persistence and heartbeat seams."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority import AuthorityStore, authority_owner
from custom_components.ha_spark.ownership import OwnershipStore, derived_owner
from custom_components.ha_spark.wire import AddonAuthority, Summary, build_snapshot, parse_summary
from tests_ha.test_polling import URL, fixture, setup, summary
from tests_ha.test_topology_flow import build_base, choose, open_topology


@pytest.fixture(autouse=True)
def freeze_time(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-30T12:00:00+00:00")


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


def peer_body(*, active: str = "active", version: int = 4) -> dict[str, Any]:
    raw = summary()
    raw["api_version"] = version
    raw["addon_authority"] = {"active": active, "mode": "observe", "holds_present": False,
                              "store_state": "ok"}
    raw["authority"]["mode"] = "observe"
    raw["authority"]["transition_target"] = None
    return raw


def peer(*, active: str = "active", version: int = 4) -> Summary:
    return parse_summary(peer_body(active=active, version=version))


async def test_each_pending_step_survives_reload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    ownership = coordinator.ownership
    assert authority_owner(entry) == "addon"
    assert await ownership.async_request("integration", peer()) is None
    assert authority_owner(entry) == "pending_integration"
    assert coordinator.authority.state is None

    async def restart() -> OwnershipStore:
        authority = AuthorityStore(hass, entry.entry_id, lambda: None)
        await authority.async_load()
        reloaded = OwnershipStore(hass, entry.entry_id, authority)
        await reloaded.async_load()
        return reloaded

    ownership = await restart()
    assert ownership.verified and ownership.owner == "pending_integration"
    await ownership.async_advance(peer())
    assert ownership.owner == "pending_integration"
    # Crash after authority initialisation but before the ownership commit.
    assert await ownership.authority.async_initialise()
    identity = ownership.authority.state
    ownership = await restart()
    await ownership.async_advance(peer(active="retired"))
    assert ownership.owner == "integration"
    assert ownership.authority.state == identity
    ownership = await restart()
    assert await ownership.async_request("addon", peer(active="retired")) is None
    ownership = await restart()
    assert ownership.owner == "pending_addon"
    # Crash after retirement but before the ownership commit.
    assert await ownership.authority.async_retire()
    ownership = await restart()
    await ownership.async_advance(peer())
    ownership = await restart()
    assert ownership.owner == "addon" and ownership.authority.state is None
    assert ownership.authority.status == "retired"


@pytest.mark.parametrize("version", [1, 2, 3])
async def test_old_peer_cannot_start_or_finish_switch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, version: int,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    ownership = entry.runtime_data.ownership
    assert await ownership.async_request("integration", peer(version=version)) == (
        "ownership_peer_version"
    )
    assert ownership.owner == "addon"
    assert await ownership.async_request("integration", peer()) is None
    assert await ownership.async_advance(peer(active="retired", version=version)) == (
        "ownership_peer_version"
    )
    assert ownership.owner == "addon"
    assert ownership.authority.state is None


@pytest.mark.parametrize(("facts", "reason"), [
    ({"mode": "protect"}, "ownership_observe_required"),
    ({"holds_present": True}, "ownership_holds_present"),
    ({"store_state": "unknown"}, "ownership_store_unknown"),
])
async def test_named_save_refusals(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    facts: dict[str, Any], reason: str,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    accepted = peer()
    assert accepted.addon_authority is not None
    changed = replace(accepted, addon_authority=AddonAuthority(
        **{**accepted.addon_authority.__dict__, **facts},
    ))
    assert await entry.runtime_data.ownership.async_request("integration", changed) == reason
    assert authority_owner(entry) == "addon"


async def test_native_flow_collects_gateway_without_connecting(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, json=peer_body())
    entry = await setup(hass)
    flow_id = await open_topology(hass, entry)
    await build_base(hass, flow_id)
    await choose(hass, flow_id, "inverters")
    await choose(hass, flow_id, "inverters_edit")
    await hass.config_entries.options.async_configure(flow_id, {"id": "inv1"})
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "inv1", "bank_id": "bank", "driver_kind": "solis", "transport": "native",
    })
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_native_gateway"}
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "inv1", "bank_id": "bank", "driver_kind": "solis", "transport": "native",
        "gateway_host": "gateway.invalid", "gateway_port": 502, "gateway_unit_id": 2,
        "charge_limit_a": 50, "confirmed": True,
    })
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "native_connect"
    result = await hass.config_entries.options.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.MENU
    await choose(hass, flow_id, "inverters_done")
    result = await choose(hass, flow_id, "save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert authority_owner(entry) == "pending_integration"
    inverter = entry.options["topology"]["inverters"][0]
    assert inverter["transport"] == "native"
    assert inverter["gateway_host"] == "gateway.invalid" and inverter["gateway_unit_id"] == 2
    assert entry.options["gateway_claims"] == [
        {"host": "gateway.invalid", "port": 502, "unit_id": 2},
    ]
    # Native identity is probed, but pending ownership cannot validate it.
    sent = aioclient_mock.mock_calls[-1][2]
    assert sent["authority_owner"] == "pending_integration"
    assert sent["devices"][0]["validation"] == "read_only"
    assert sent["devices"][0]["reason"] == "gateway_not_owned"
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=peer_body(active="retired"))
    await entry.runtime_data.async_refresh()
    assert authority_owner(entry) == "integration"
    await entry.runtime_data.async_refresh()
    confirmed = aioclient_mock.mock_calls[-1][2]
    assert confirmed["authority_owner"] == "integration"
    assert confirmed["authority"]["mode"] == "observe"
    assert confirmed["authority"]["epoch"] == 1
    assert confirmed["authority"]["store_state"] == "ok" and confirmed["holds"] == []
    assert confirmed["devices"][0]["validation"] == "read_only"


@pytest.mark.parametrize("failure", ["save", "verify", "cancel_verify"])
async def test_failed_ownership_save_never_requests_handover(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, failure: str,
) -> None:
    aioclient_mock.post(URL, json=peer_body())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    ownership = coordinator.ownership
    load = Store.async_load
    save = Store.async_save

    async def faulty_load(store: Store[dict[str, Any]]) -> dict[str, Any] | None:
        if store.key == ownership.key:
            if failure == "cancel_verify":
                raise asyncio.CancelledError
            raise OSError("unverified")
        return await load(store)

    async def faulty_save(store: Store[dict[str, Any]], data: dict[str, Any]) -> None:
        if store.key == ownership.key and failure == "save":
            raise OSError("unverified")
        await save(store, data)

    with (patch.object(Store, "async_load", faulty_load),
          patch.object(Store, "async_save", faulty_save)):
        if failure == "cancel_verify":
            with pytest.raises(asyncio.CancelledError):
                await ownership.async_request("integration", peer())
        else:
            assert await ownership.async_request("integration", peer()) == "ownership_save_failed"
    assert not ownership.verified
    count = len(aioclient_mock.mock_calls)
    await coordinator.async_refresh()
    assert len(aioclient_mock.mock_calls) == count
    assert not coordinator.connected and coordinator.authority.state is None


def test_owner_derivation_and_legacy_snapshot_projection() -> None:
    document = fixture("site_snapshot_v4_max")["topology"]
    topology = document["topology"]
    assert derived_owner(None) == "addon"
    assert derived_owner(topology) == "addon"
    for inverter in topology["inverters"]:
        inverter.update(transport="native", gateway_host="gateway.invalid")
    assert derived_owner(topology) == "integration"
    topology["inverters"][0]["transport"] = "legacy"
    assert derived_owner(topology) == "addon"
    snapshot = build_snapshot(site_id=fixture("site_snapshot")["site_id"],
                              release_version="0.30.0", sent_at=peer().built_at,
                              api_version=3, topology=document)
    sent = snapshot["topology"]
    assert isinstance(sent, dict)
    for inverter in sent["topology"]["inverters"]:
        native_fields = {"transport", "gateway_host", "gateway_port", "gateway_unit_id"}
        assert not native_fields & inverter.keys()
    assert "transport" in topology["inverters"][0]  # no mutation of the stored document
