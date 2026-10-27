"""Regression coverage for credential privacy and conservative quota recovery."""

from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.solcast import Outcome, SiteList, SiteResult, async_list_sites
from custom_components.ha_spark.solcast_budget import SolcastBudget, async_get_budget
from tests_ha.test_solcast import KEY, NOW, SITE, URL
from tests_ha.test_solcast_flows import entry_for


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    """Use real Store files to exercise corruption and durable recovery."""
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Keep HA's storage files in this test's isolated directory."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def freeze_review_day(freezer: FrozenDateTimeFactory) -> None:
    """Use a deterministic accounting day."""
    freezer.move_to(NOW)


def test_site_model_repr_hides_resource_ids() -> None:
    sites = SiteList.model_validate({"sites": [{"resource_id": SITE}]})
    assert SITE not in repr(sites)
    assert SITE not in repr(sites.sites[0])


async def test_large_valid_site_list_is_too_many_sites(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(URL, json={"sites": [{"resource_id": str(i)} for i in range(1001)]})
    result = await async_list_sites(async_get_clientsession(hass), KEY)
    assert result.flow_error == "too_many_sites"


@pytest.mark.parametrize("limit", [True, False, 10.0, 50.0, float("nan"), "10", None, 0, 100])
async def test_invalid_calls_limit_refuses_dispatch(hass: HomeAssistant, limit: Any) -> None:
    gate = SolcastBudget(hass, "entry")
    request = AsyncMock(return_value=SiteResult(Outcome.OK))
    assert (await gate.async_request(request, calls_limit=limit)).outcome == Outcome.STALE
    request.assert_not_called()


async def test_midnight_during_charge_is_persisted_before_dispatch(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    gate = SolcastBudget(hass, "entry")
    tomorrow = NOW.replace(day=28, hour=0)
    save = gate.store.async_save

    async def save_across_midnight(data: dict[str, object]) -> None:
        await save(data)
        freezer.move_to(tomorrow)

    async def request() -> SiteResult:
        state = await gate.store.async_load()
        assert state is not None
        assert state["utc_day"] == "2026-10-28"
        assert state["calls_used"] == 1
        return SiteResult(Outcome.OK)

    with patch.object(gate.store, "async_save", side_effect=save_across_midnight):
        assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.OK


async def test_exhaustion_uses_response_day(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    gate = SolcastBudget(hass, "entry")

    async def request() -> SiteResult:
        freezer.move_to(NOW.replace(day=28, hour=0))
        return SiteResult(Outcome.QUOTA_EXHAUSTED)

    await gate.async_request(request, calls_limit=10)
    gate = SolcastBudget(hass, "entry")
    blocked = AsyncMock(return_value=SiteResult(Outcome.OK))
    assert (await gate.async_request(blocked, calls_limit=10)).outcome == Outcome.QUOTA_EXHAUSTED
    blocked.assert_not_called()
    state = await gate.store.async_load()
    assert state is not None and state["exhausted_until"] == "2026-10-29T00:00:00Z"


@pytest.mark.parametrize("missing", [
    "utc_day", "calls_used", "exhausted_until", "window_start", "first_fetch_pending",
])
async def test_missing_accounting_field_spends_today(hass: HomeAssistant, missing: str) -> None:
    gate = SolcastBudget(hass, "entry")
    state: dict[str, object] = {
        "utc_day": "2026-10-27", "calls_used": 0, "exhausted_until": None,
        "window_start": None, "first_fetch_pending": False,
    }
    state.pop(missing)
    await gate.store.async_save(state)
    request = AsyncMock(return_value=SiteResult(Outcome.OK))
    assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.QUOTA_EXHAUSTED
    request.assert_not_called()
    assert gate.calls_used == 1000


@pytest.mark.parametrize("corruption", ["json", "schema"])
async def test_corrupt_store_recovers_after_midnight(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, corruption: str,
) -> None:
    gate = SolcastBudget(hass, "entry")
    if corruption == "json":
        path = Path(gate.store.path)
        def corrupt() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{broken", encoding="utf-8")
        await hass.async_add_executor_job(corrupt)
    else:
        await gate.store.async_save({"utc_day": "invalid"})
    request = AsyncMock(return_value=SiteResult(Outcome.OK))
    for _ in range(2):
        result = await gate.async_request(request, calls_limit=10)
        assert result.outcome == Outcome.QUOTA_EXHAUSTED
        state = await gate.store.async_load()
        assert state is not None and state["calls_used"] == 1000
        gate = SolcastBudget(hass, "entry")
    request.assert_not_called()
    freezer.move_to(NOW + timedelta(days=1))
    assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.OK
    assert gate.calls_used == 1
    request.assert_awaited_once()


@pytest.mark.parametrize("broken_store", [False, True])
async def test_clear_commits_without_reload_or_healthy_store(
    hass: HomeAssistant, broken_store: bool,
) -> None:
    entry = entry_for(hass, solcast_api_key=KEY, solcast_resource_ids=[SITE])
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "site_solcast")}, name="Solcast",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id,
    }, data={"solcast_api_key": ""})
    gate = async_get_budget(hass, entry.entry_id)
    with (
        patch.object(gate.store, "async_load", side_effect=OSError())
        if broken_store else nullcontext(),
        patch.object(hass.config_entries, "async_reload", return_value=False),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert "solcast_api_key" not in entry.data
    assert "solcast_resource_ids" not in entry.data
    assert not registry.async_get_device_by_identifier((DOMAIN, "site_solcast"), entry.entry_id)
