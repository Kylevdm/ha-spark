"""Authority persistence and recovery through a real, read-only config entry."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority import authority_owner
from custom_components.ha_spark.authority_store import (
    FallbackState,
    HoldDevice,
    HoldRecord,
    ManualSession,
    OperatingMode,
    Transition,
)
from custom_components.ha_spark.const import DOMAIN
from tests_ha.test_polling import URL, setup, state_value, summary


@pytest.fixture(autouse=True)
def freeze_authority_time(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-30T12:00:00+00:00")


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


async def test_fresh_setup_only_loads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    assert store.status == "uninitialised" and store.state is None
    assert not await hass.async_add_executor_job(Path(store.store.path).exists)
    assert not await hass.async_add_executor_job(Path(store.marker.path).exists)
    assert await store.async_initialise()
    assert store.status == "ok" and store.state is not None
    assert store.state.committed_mode == "observe" and store.state.epoch == 1
    identity = store.state.store_id
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = entry.runtime_data.authority.state
    assert reloaded is not None and reloaded.store_id == identity
    assert state_value(hass, "sensor.ha_spark_operating_mode") == "unavailable"


@pytest.mark.parametrize("damage", ["store_missing", "marker_missing", "store_corrupt",
                                    "marker_corrupt", "both_corrupt", "mismatch",
                                    "store_invalid", "marker_invalid"])
async def test_damaged_pair_stays_unknown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, damage: str,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    assert await store.async_initialise()

    def damage_files() -> None:
        for name, target in (("store", store.store), ("marker", store.marker)):
            path = Path(target.path)
            if damage == f"{name}_missing":
                path.unlink()
            elif damage in (f"{name}_corrupt", "both_corrupt"):
                path.write_text("not json")
            elif (damage == "mismatch" and name == "marker") or damage == f"{name}_invalid":
                data = json.loads(path.read_text())
                if damage == "mismatch":
                    data["data"]["store_id"] = str(uuid4())
                else:
                    del data["data"]["holds" if name == "store" else "schema_version"]
                path.write_text(json.dumps(data))

    await hass.async_add_executor_job(damage_files)
    for _ in range(2):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        store = entry.runtime_data.authority
        assert store.status == "unknown" and store.state is None
        issue = ir.async_get(hass).async_get_issue(DOMAIN, store.issue_id)
        assert issue is not None and issue.is_fixable
        assert not await store.async_initialise()
    assert await store.async_confirm_observe()
    assert store.state is not None and store.state.epoch == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, store.issue_id) is None
    assert not await store.async_confirm_observe()


async def test_failed_save_fences_memory_until_verified_retry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    assert await store.async_initialise()
    before = store.state
    assert before is not None
    candidate = before.model_copy(update={"epoch": 2, "committed_mode": "protect"})
    with patch.object(store.store, "async_save", new=AsyncMock()):
        assert not await store.async_save(candidate)
    assert store.status == "save_failed" and store.state is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, store.issue_id) is not None
    assert await store.async_save(candidate)
    assert store.state == candidate
    assert ir.async_get(hass).async_get_issue(DOMAIN, store.issue_id) is None


async def test_repair_confirms_only_observe_without_other_writes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    assert await store.async_initialise()
    before = store.state
    assert before is not None
    await hass.async_add_executor_job(Path(store.marker.path).unlink)
    await store.async_load()
    assert await async_setup_component(hass, "repairs", {})
    manager = hass.data["repairs"]["flow_manager"]
    result = await manager.async_init(DOMAIN, context={"issue_id": store.issue_id})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    assert not result["data_schema"].schema  # Confirmation only: no mode selector.
    data, options = dict(entry.data), dict(entry.options)
    with patch("homeassistant.core.ServiceRegistry.async_call", new=AsyncMock()) as calls:
        requests = len(aioclient_mock.mock_calls)
        result = await manager.async_configure(result["flow_id"], {})
        calls.assert_not_called()
        assert len(aioclient_mock.mock_calls) == requests
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data == data and entry.options == options
    state = store.state
    assert state is not None and state.store_id != before.store_id
    assert state.committed_mode == "observe" and state.epoch == 1 and not state.holds
    assert not await store.async_confirm_observe()


async def test_local_entities_survive_addon_loss_and_hide_unknown_state(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        entry = await setup(hass)
        store = entry.runtime_data.authority
        assert state_value(hass, "sensor.ha_spark_operating_mode") == "authority_unknown"
        assert await store.async_initialise()
        state = store.state
        assert state is not None
        now = dt_util.utcnow()
        candidate = state.model_copy(update={
            "committed_mode": "optimise", "epoch": 7,
            "manual_session": ManualSession(deadline=now + timedelta(hours=1),
                                            return_to="optimise", keep_protection=True),
            "fallback": FallbackState(active=True, causes=("addon_liveness",), entered_at=now),
            "holds": (HoldRecord(cause="ev", started_at=now, confirmed=True,
                                 device=HoldDevice(driver="solis", power_switch="PRIVATE-CANARY",
                                                   hub="PRIVATE-CANARY", slave=1)),),
        })
        assert await store.async_save(candidate)
        await hass.async_block_till_done()
        for entity, expected in {
            "sensor.ha_spark_operating_mode": "optimise",
            "sensor.ha_spark_manual_session": "active",
            "sensor.ha_spark_authority_epoch": "7",
            "binary_sensor.ha_spark_fallback_active": "on",
            "binary_sensor.ha_spark_ev_hold": "on",
            "binary_sensor.ha_spark_dispatch_hold": "off",
        }.items():
            assert state_value(hass, entity) == expected
            assert "PRIVATE-CANARY" not in str(hass.states.get(entity))
        manual = hass.states.get("sensor.ha_spark_manual_session")
        assert manual is not None and manual.attributes["keep_protection"] is True
        fallback = hass.states.get("binary_sensor.ha_spark_fallback_active")
        assert fallback is not None and fallback.attributes["causes"] == ["addon_liveness"]
        transitions: tuple[tuple[OperatingMode, str], ...] = (
            ("optimise", "activating"), ("observe", "switching_to_observe"),
        )
        for target, expected in transitions:
            assert await store.async_save(candidate.model_copy(update={
                "transition": Transition(target=target, started_at=now),
            }))
            await hass.async_block_till_done()
            assert state_value(hass, "sensor.ha_spark_operating_mode") == expected
        with patch.object(store.store, "async_save", new=AsyncMock()):
            assert not await store.async_save(candidate.model_copy(update={"epoch": 8}))
        await hass.async_block_till_done()
        assert state_value(hass, "sensor.ha_spark_operating_mode") == "authority_unknown"
        for entity in ("sensor.ha_spark_manual_session", "sensor.ha_spark_authority_epoch",
                       "binary_sensor.ha_spark_fallback_active", "binary_sensor.ha_spark_ev_hold"):
            assert state_value(hass, entity) == "unavailable"


async def test_addon_owner_keeps_mirror_despite_local_authority(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    assert authority_owner(entry) == "addon"
    assert await entry.runtime_data.authority.async_initialise()
    await hass.async_block_till_done()
    assert state_value(hass, "sensor.ha_spark_operating_mode") == "authority_unknown"
    assert state_value(hass, "sensor.ha_spark_manual_session") == "none"
    assert state_value(hass, "sensor.ha_spark_authority_epoch") == "unavailable"


async def test_interrupted_initialisation_never_grants_observe(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    with patch.object(store.marker, "async_save", new=AsyncMock()):
        assert not await store.async_initialise()
    assert store.status == "save_failed" and store.state is None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    store = entry.runtime_data.authority
    assert store.status == "unknown" and not await store.async_initialise()
    assert await store.async_confirm_observe()


async def test_invalid_candidate_and_load_failure_fail_closed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    assert await store.async_initialise()
    before = store.state
    assert before is not None
    with patch.object(store.store, "async_save", new=AsyncMock()) as write:
        assert not await store.async_save(before.model_copy(update={"epoch": True}))
        write.assert_not_called()
    assert store.status == "save_failed" and store.state is None
    assert await store.async_save(before)
    with patch("custom_components.ha_spark.authority.Store.async_load", side_effect=OSError):
        await store.async_load()
    assert store.status == "unknown" and store.state is None
