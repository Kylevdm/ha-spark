"""Solcast discovery is bounded, redacted and counted before dispatch."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.solcast import Outcome, SiteResult, async_list_sites
from custom_components.ha_spark.solcast_budget import SolcastBudget

URL = "https://api.solcast.com.au/rooftop_sites?format=json"
KEY = "canary-key-7f3a"
SITE = "canary-site-91b2"
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def freeze_solcast_day(freezer: FrozenDateTimeFactory) -> None:
    """Quota cases share a deterministic UTC day."""
    freezer.move_to(NOW)


@pytest.mark.parametrize(("status", "body", "outcome", "error"), [
    (200, {"sites": [{"resource_id": SITE}]}, "ok", None),
    (401, {}, "auth_failed", None), (403, {}, "auth_failed", None),
    (200, {"sites": []}, "malformed", "no_sites"),
    (200, {"sites": [{"resource_id": str(i)} for i in range(3)]},
     "malformed", "too_many_sites"),
    (200, {"sites": [{"resource_id": "../bad"}]}, "malformed", None),
    (200, {"sites": [{"resource_id": SITE}] * 2}, "malformed", None),
    (429, {"response_status": {"error_code": "TooManyRequests"}}, "quota_exhausted", None),
    (429, {}, "busy", None), (500, {}, "server_error", None),
    (302, {}, "malformed", None),
])
async def test_site_list_outcomes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    status: int, body: dict[str, Any], outcome: str, error: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.get(URL, status=status, json=body)
    result = await async_list_sites(async_get_clientsession(hass), KEY)
    assert result.outcome == outcome
    assert result.flow_error == error
    assert result.resource_ids == ((SITE,) if outcome == "ok" else ())
    request = aioclient_mock.mock_calls[0]
    assert request[3]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in str(request[1])
    assert KEY not in repr(result) + caplog.text
    assert SITE not in repr(result) + caplog.text


async def test_budget_persists_before_transport_and_resets(hass: HomeAssistant) -> None:
    gate = SolcastBudget(hass, "entry")
    async def request() -> Any:
        saved = await gate.store.async_load()
        assert saved is not None and saved["calls_used"] == 1
        return await async_list_sites(session, KEY)

    session = AsyncMock(spec=aiohttp.ClientSession)
    session.get.side_effect = aiohttp.ClientConnectionError(KEY + SITE)
    with patch("custom_components.ha_spark.solcast_budget.dt_util.utcnow", return_value=NOW):
        result = await gate.async_request(request, calls_limit=10)
        assert result.outcome == Outcome.UNREACHABLE
        reloaded = SolcastBudget(hass, "entry")
        await reloaded.async_load()
        assert reloaded.calls_used == 1
    tomorrow = NOW.replace(day=28)
    with patch("custom_components.ha_spark.solcast_budget.dt_util.utcnow", return_value=tomorrow):
        await reloaded.async_request(request, calls_limit=10)
        assert reloaded.calls_used == 1


async def test_budget_save_failure_refuses_dispatch(hass: HomeAssistant) -> None:
    gate = SolcastBudget(hass, "entry")
    request = AsyncMock()
    with patch.object(gate.store, "async_save", side_effect=OSError(KEY)):
        assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.STALE
    assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.STALE
    request.assert_not_called()


async def test_store_swallowed_disk_failure_still_refuses_dispatch(hass: HomeAssistant) -> None:
    gate = SolcastBudget(hass, "entry")
    request = AsyncMock(return_value=SiteResult(Outcome.OK))
    with patch("homeassistant.helpers.storage.Store._async_write_data", side_effect=WriteError):
        assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.STALE
    request.assert_not_called()


async def test_exhaustion_survives_reload_until_utc_midnight(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(URL, status=429, json={
        "response_status": {"error_code": "TooManyRequests"},
    })
    async def request() -> SiteResult:
        return await async_list_sites(async_get_clientsession(hass), KEY)

    gate = SolcastBudget(hass, "entry")
    with patch("custom_components.ha_spark.solcast_budget.dt_util.utcnow", return_value=NOW):
        result = await gate.async_request(request, calls_limit=10)
        assert result.outcome == Outcome.QUOTA_EXHAUSTED
        gate = SolcastBudget(hass, "entry")
        result = await gate.async_request(request, calls_limit=10)
        assert result.outcome == Outcome.QUOTA_EXHAUSTED
        assert aioclient_mock.call_count == 1
    with patch("custom_components.ha_spark.solcast_budget.dt_util.utcnow",
               return_value=NOW.replace(day=28, hour=0)):
        await gate.async_request(request, calls_limit=10)
        assert aioclient_mock.call_count == 2
        assert gate.calls_used == 1


@pytest.mark.parametrize("raw", [
    {"utc_day": "2026-10-27", "calls_used": -1},
    {"utc_day": "invalid"}, {"utc_day": "2026-10-27", "calls_used": True},
])
async def test_invalid_store_fails_closed(hass: HomeAssistant, raw: dict[str, Any]) -> None:
    gate = SolcastBudget(hass, "entry")
    await gate.store.async_save(raw)
    request = AsyncMock()
    assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.STALE
    request.assert_not_called()


async def test_load_failure_fails_closed(hass: HomeAssistant) -> None:
    gate = SolcastBudget(hass, "entry")
    request = AsyncMock()
    with patch.object(gate.store, "async_load", side_effect=OSError(KEY)):
        assert (await gate.async_request(request, calls_limit=10)).outcome == Outcome.STALE
    request.assert_not_called()


@pytest.mark.parametrize("size", [256 * 1024, 256 * 1024 + 1])
async def test_success_body_cap(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, size: int,
) -> None:
    body = b'{"sites": [{"resource_id": "site"}]}'
    aioclient_mock.get(URL, content=body + b" " * (size - len(body)))
    result = await async_list_sites(async_get_clientsession(hass), KEY)
    assert result.outcome == (Outcome.OK if size == 256 * 1024 else Outcome.MALFORMED)


async def test_error_body_cap(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(URL, status=429, content=b" " * 4096 + b'TooManyRequests')
    assert (await async_list_sites(async_get_clientsession(hass), KEY)).outcome == Outcome.BUSY


async def test_two_sites_are_discovered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(URL, json={"sites": [
        {"resource_id": SITE}, {"resource_id": "second-site"},
    ]})
    result = await async_list_sites(async_get_clientsession(hass), KEY)
    assert result.outcome == Outcome.OK
    assert result.resource_ids == (SITE, "second-site")


async def test_concurrent_requests_cannot_overrun_limit(hass: HomeAssistant) -> None:
    import asyncio

    gate = SolcastBudget(hass, "entry")
    request = AsyncMock(return_value=SiteResult(Outcome.OK))
    results = await asyncio.gather(*(
        gate.async_request(request, calls_limit=10) for _ in range(12)
    ))
    assert sum(result.outcome == Outcome.OK for result in results) == 10
    assert request.await_count == 10
    assert gate.calls_used == 10
