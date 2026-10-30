"""Regression coverage for interrupted ownership commits and stale admissions."""

import asyncio
import json
import shutil
from copy import deepcopy
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL as YarlURL

from custom_components.ha_spark.authority import AuthorityStore
from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.diagnostics import async_get_config_entry_diagnostics
from custom_components.ha_spark.ownership import OwnershipStore
from tests_ha.test_ownership import hass_config_dir, hass_storage, peer  # noqa: F401
from tests_ha.test_polling import URL, setup
from tests_ha.test_topology_flow import STORED_TOPOLOGY


@pytest.mark.parametrize("status", ["ok", "unknown", "retired"])
async def test_absent_ownership_keeps_legacy_polling(hass: HomeAssistant, status: Any) -> None:
    authority = AuthorityStore(hass, "legacy", lambda: None)
    authority.status = status
    ownership = OwnershipStore(hass, "legacy", authority)
    await ownership.async_load()
    assert ownership.verified and ownership.owner == "addon"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("condition", ["version", "mode", "holds", "store", "transition"])
async def test_pending_aborts_on_fresh_refusal(
    hass: HomeAssistant, reverse: bool, condition: str,
) -> None:
    authority = AuthorityStore(hass, "abort", lambda: None)
    ownership = OwnershipStore(hass, "abort", authority)
    await ownership.async_load()
    if reverse:
        assert await authority.async_initialise()
        ownership.owner = "integration"
    assert await ownership.async_request("addon" if reverse else "integration", peer()) is None
    summary = peer()
    assert summary.addon_authority is not None
    facts: dict[str, Any] = {}
    if condition == "mode":
        facts["mode"] = "protect"
    if condition == "holds":
        facts["holds_present"] = True
    if condition == "store":
        facts["store_state"] = "unknown"
    summary = replace(summary, addon_authority=replace(summary.addon_authority, **facts))
    if condition == "transition":
        summary = replace(
            summary, authority=replace(summary.authority, transition_target="protect"),
        )
    if condition == "version":
        summary = peer(version=3)
    reason = await ownership.async_advance(summary)
    assert reason is not None
    assert ownership.verified and ownership.owner == ("integration" if reverse else "addon")


@pytest.mark.parametrize("owner", ["pending_integration", "integration", "pending_addon", "addon"])
async def test_cancel_at_each_ownership_commit_restarts_fenced(
    hass: HomeAssistant, owner: Any,
) -> None:
    authority = AuthorityStore(hass, "crash", lambda: None)
    ownership = OwnershipStore(hass, "crash", authority)
    await ownership.async_load()
    if owner != "pending_integration":
        assert await ownership.async_request("integration", peer()) is None
        assert await authority.async_initialise()
    if owner in ("pending_addon", "addon"):
        await ownership.async_advance(peer(active="retired"))
    if owner == "addon":
        assert await ownership.async_request("addon", peer(active="retired")) is None
    load = Store.async_load

    async def cancel(store: Store[dict[str, Any]]) -> dict[str, Any] | None:
        if store.key == ownership.key:
            raise asyncio.CancelledError
        return await load(store)

    with patch.object(Store, "async_load", cancel), pytest.raises(asyncio.CancelledError):
        if owner == "pending_integration":
            await ownership.async_request("integration", peer())
        elif owner == "integration":
            await ownership.async_advance(peer(active="retired"))
        elif owner == "pending_addon":
            await ownership.async_request("addon", peer(active="retired"))
        else:
            await ownership.async_advance(peer())
    assert authority.status == "save_failed"
    assert not ownership.verified
    restarted_authority = AuthorityStore(hass, "crash", lambda: None)
    await restarted_authority.async_load()
    restarted = OwnershipStore(hass, "crash", restarted_authority)
    await restarted.async_load()
    assert restarted.owner == owner
    if owner in ("integration", "pending_addon"):
        assert restarted_authority.state is not None
        assert restarted_authority.state.committed_mode == "observe"
    else:
        assert restarted_authority.state is None
    # Resume from the independently reloaded evidence, not the interrupted object.
    if owner == "pending_integration":
        await restarted.async_advance(peer(active="retired"))
        assert restarted.owner == "integration"
        assert restarted_authority.state is not None
    elif owner == "pending_addon":
        await restarted.async_advance(peer())
        assert restarted.owner == "addon"
        assert restarted_authority.state is None
    assert restarted.verified


@pytest.mark.parametrize("reverse", [False, True])
async def test_crash_between_pending_and_topology_save(
    hass: HomeAssistant, reverse: bool,
) -> None:
    topology = deepcopy(STORED_TOPOLOGY)
    if reverse:
        topology["inverters"][0].update(transport="native", gateway_host="gateway.invalid")
    authority = AuthorityStore(hass, "topology_gap", lambda: None)
    ownership = OwnershipStore(hass, "topology_gap", authority, lambda: topology)
    # The old topology remains on disk when the pending save is interrupted.
    if reverse:
        assert await authority.async_initialise()
        ownership.owner, ownership.verified = "integration", True
    else:
        await ownership.async_load()
    assert await ownership.async_request("addon" if reverse else "integration", peer()) is None
    reloaded_authority = AuthorityStore(hass, "topology_gap", lambda: None)
    await reloaded_authority.async_load()
    reloaded = OwnershipStore(hass, "topology_gap", reloaded_authority, lambda: topology)
    await reloaded.async_load()
    assert reloaded.verified and reloaded.owner == ("integration" if reverse else "addon")
    # Aborting the uncommitted topology change must survive another restart.
    again = OwnershipStore(hass, "topology_gap", reloaded_authority, lambda: topology)
    await again.async_load()
    assert again.owner == reloaded.owner and again.verified
    # Retrying with the intended topology follows the ordinary pending step.
    topology["inverters"][0].update(
        transport="legacy" if reverse else "native", gateway_host="gateway.invalid",
    )
    assert await again.async_request("addon" if reverse else "integration", peer()) is None
    assert again.owner == ("pending_addon" if reverse else "pending_integration")
    await again.async_advance(peer(active="active" if reverse else "retired"))
    assert again.owner == ("addon" if reverse else "integration")


@pytest.mark.parametrize("status", ["uninitialised", "unknown", "save_failed", "retired"])
async def test_confirmed_record_without_authority_never_fabricates_snapshot(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: Any,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    coordinator.ownership.owner = "integration"
    coordinator.ownership.verified = True
    coordinator.authority.status = status
    await coordinator.async_refresh()
    sent = aioclient_mock.mock_calls[-1][2]
    assert "authority_owner" not in sent
    assert "authority" not in sent
    assert "holds" not in sent


async def test_confirmed_integration_with_restored_legacy_topology_requires_handshake(
    hass: HomeAssistant,
) -> None:
    topology = deepcopy(STORED_TOPOLOGY)
    authority = AuthorityStore(hass, "restored", lambda: None)
    assert await authority.async_initialise()
    ownership = OwnershipStore(hass, "restored", authority)
    await ownership.async_load()
    assert await ownership.async_request("integration", peer()) is None
    await ownership.async_advance(peer(active="retired"))
    restarted_authority = AuthorityStore(hass, "restored", lambda: None)
    await restarted_authority.async_load()
    restarted = OwnershipStore(hass, "restored", restarted_authority, lambda: topology)
    await restarted.async_load()
    assert restarted.reconcile
    assert restarted.owner == "integration"
    await restarted.async_advance(peer(version=3))
    assert restarted.reconcile and restarted.owner == "integration"
    await restarted.async_advance(peer(active="retired"))
    assert not restarted.reconcile and restarted.owner == "pending_addon"
    assert restarted_authority.state is not None
    await restarted.async_advance(peer())
    assert restarted.owner == "addon" and restarted_authority.state is None


async def test_admission_uses_latest_summary_before_pending(hass: HomeAssistant) -> None:
    authority = AuthorityStore(hass, "latest", lambda: None)
    ownership = OwnershipStore(hass, "latest", authority)
    await ownership.async_load()
    fresh = peer()
    assert fresh.addon_authority is not None
    fresh = replace(fresh, addon_authority=replace(fresh.addon_authority, mode="optimise"))
    assert await ownership.async_request("integration", peer(), lambda: fresh) == (
        "ownership_observe_required"
    )
    assert ownership.owner == "addon" and ownership.verified


@pytest.mark.parametrize(("facts", "reason"), [
    ({"mode": "optimise"}, "ownership_observe_required"),
    ({"holds_present": True}, "ownership_holds_present"),
    ({"store_state": "unknown"}, "ownership_store_unknown"),
    ({"transition": True}, "ownership_transition_present"),
])
async def test_reverse_admission_checks_peer_before_pending(
    hass: HomeAssistant, facts: dict[str, Any], reason: str,
) -> None:
    authority = AuthorityStore(hass, "reverse_admission", lambda: None)
    ownership = OwnershipStore(hass, "reverse_admission", authority)
    await ownership.async_load()
    assert await ownership.async_request("integration", peer()) is None
    await ownership.async_advance(peer(active="retired"))
    summary = peer(active="retired")
    addon = summary.addon_authority
    assert addon is not None
    if facts.get("transition"):
        summary = replace(
            summary, authority=replace(summary.authority, transition_target="protect"),
        )
    else:
        summary = replace(summary, addon_authority=replace(addon, **facts))
    assert await ownership.async_request("addon", summary) == reason
    assert ownership.owner == "integration" and ownership.verified


async def test_confirmed_addon_with_restored_native_topology_reconciles(
    hass: HomeAssistant,
) -> None:
    topology = deepcopy(STORED_TOPOLOGY)
    topology["inverters"][0].update(transport="native", gateway_host="gateway.invalid")
    authority = AuthorityStore(hass, "restored_native", lambda: None)
    store: Store[dict[str, Any]] = Store(hass, 1, "ha_spark.ownership.restored_native")
    await store.async_save({"owner": "addon"})
    ownership = OwnershipStore(hass, "restored_native", authority, lambda: topology)
    await ownership.async_load()
    assert ownership.reconcile and ownership.verified and ownership.owner == "addon"
    assert await ownership.async_advance(peer(version=3)) == "ownership_peer_version"
    assert ownership.owner == "addon" and ownership.reconcile
    await ownership.async_advance(peer())
    assert str(ownership.owner) == "pending_integration" and not ownership.reconcile
    await ownership.async_advance(peer(active="retired"))
    assert str(ownership.owner) == "integration" and authority.state is not None


@pytest.mark.parametrize("reverse", [False, True])
async def test_real_heartbeat_with_changed_peer_aborts_and_exposes_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, reverse: bool,
) -> None:
    # The HTTP mock is only a byte bridge between the separately pinned Python
    # environments. The response is produced by the real FastAPI route, not a
    # fabricated Summary, and parsed by the real channel and coordinator.
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    ownership = coordinator.ownership
    if reverse:
        assert await ownership.async_request("integration", peer()) is None
        await ownership.async_advance(peer(active="retired"))
    assert await ownership.async_request("addon" if reverse else "integration", peer()) is None
    topology = deepcopy(STORED_TOPOLOGY)
    if not reverse:
        topology["inverters"][0].update(transport="native", gateway_host="gateway.invalid")
    # Don't reload the entry; this represents the completed options commit.
    hass.config_entries.async_update_entry(
        entry, options=entry.options | {"topology": topology, "topology_revision": 1},
    )

    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        python = shutil.which("python")
        assert python is not None
        process = await asyncio.create_subprocess_exec(
            python, "-m", "tests.ownership_http_peer",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(json.dumps({
            "snapshot": data, "mode": "optimise", "session_id": entry.data["session_id"],
        }).encode())
        assert process.returncode == 0, error.decode()
        response = json.loads(output)
        assert response["known"]
        return AiohttpClientMockResponse(
            "post", YarlURL(URL), status=response["status"], json=response["body"],
        )

    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=respond)
    await coordinator.async_refresh()
    assert coordinator.connected
    assert ownership.verified and ownership.owner == ("integration" if reverse else "addon")
    assert ownership.last_refusal == "ownership_observe_required"
    issue_id = f"ownership_switch_refused_{entry.entry_id}"
    issue = ir.async_get(hass).issues[(DOMAIN, issue_id)]
    assert issue.translation_key == "ownership_observe_required"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["last_refusal"] == "ownership_observe_required"
    # A later successful switch clears both the repair and diagnostic reason.
    assert await ownership.async_request("addon" if reverse else "integration", peer()) is None
    await ownership.async_advance(peer(active="active" if reverse else "retired"))
    assert (DOMAIN, issue_id) not in ir.async_get(hass).issues
    assert ownership.last_refusal is None


async def test_downgrade_abort_repair_clears_when_topology_reverted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    ownership = coordinator.ownership
    assert await ownership.async_request("integration", peer()) is None
    topology = deepcopy(STORED_TOPOLOGY)
    topology["inverters"][0].update(transport="native", gateway_host="gateway.invalid")
    hass.config_entries.async_update_entry(entry, options={"topology": topology})
    aioclient_mock.clear_requests()

    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        if data["api_version"] == 4:
            body = {
                "api_version": 3, "release_version": "0.30.0",
                "supported_api_versions": [1, 2, 3], "error": "unsupported_api_version",
            }
            return AiohttpClientMockResponse("post", YarlURL(URL), status=409, json=body)
        from tests_ha.test_ownership import peer_body

        return AiohttpClientMockResponse("post", YarlURL(URL), json=peer_body(version=3))

    aioclient_mock.post(URL, side_effect=respond)
    await coordinator.async_refresh()
    assert ownership.owner == "addon" and ownership.verified
    issue_id = f"ownership_switch_refused_{entry.entry_id}"
    issue = ir.async_get(hass).issues[(DOMAIN, issue_id)]
    assert issue.translation_key == "ownership_peer_version"
    assert (await async_get_config_entry_diagnostics(hass, entry))["last_refusal"] == (
        "ownership_peer_version"
    )
    # Revert the topology. A counted heartbeat clears the issue.
    hass.config_entries.async_update_entry(entry, options={"topology": STORED_TOPOLOGY})
    await coordinator.async_refresh()
    assert coordinator.connected
    assert (DOMAIN, issue_id) not in ir.async_get(hass).issues
    assert ownership.last_refusal is None
