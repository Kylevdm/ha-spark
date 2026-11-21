"""Meter history pulls against the Octopus REST contract, with no live network."""

import ast
import asyncio
import logging
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import meter_pull
from ha_spark.energy.meter_pull import MeterPullOutcome, pull_meter_history, read_meter_history
from ha_spark.energy.models import ConsumptionInterval
from ha_spark.energy.octopus import OctopusApiError, fetch_consumption
from ha_spark.energy.store import read_consumption
from ha_spark.health import Status, check_meter_history

API = "https://supplier.test/v1"
URL = f"{API}/electricity-meter-points/test-point/meters/test-meter/consumption/"
SECRET = "sk_test"
NOW = datetime(2026, 6, 2, 9, tzinfo=UTC)
MIDNIGHT = datetime(2026, 6, 1, 23, tzinfo=UTC)  # Household midnight in BST.
SLOT = timedelta(minutes=30)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "household.db"), timezone="Europe/London",
        octopus_api_url=API, octopus_api_key=SECRET,
        octopus_mpan="test-point", octopus_meter_serial="test-meter",
    )


def reading(start: datetime, kwh: float = 1.0) -> dict[str, object]:
    return {"interval_start": start.isoformat(), "interval_end": (start + SLOT).isoformat(),
            "consumption": kwh}


def page(*rows: dict[str, object], next_url: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"results": rows, "next": next_url})


@respx.mock
async def test_pull_resumes_at_latest_interval_and_stops_at_household_midnight(
    settings: Settings,
) -> None:
    first = MIDNIGHT - timedelta(days=1)
    last = MIDNIGHT - SLOT
    route = respx.get(URL).mock(side_effect=[page(reading(first)), page(reading(last, 2.0))])

    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.OK
    assert await pull_meter_history(settings, NOW + timedelta(hours=1)) == MeterPullOutcome.OK
    outcome = await pull_meter_history(settings, NOW + timedelta(hours=2))
    assert outcome == MeterPullOutcome.UP_TO_DATE
    assert route.call_count == 2
    for call in route.calls:
        assert call.request.url.params["period_from"] == first.isoformat()
        assert call.request.url.params["period_to"] == MIDNIGHT.isoformat()
    assert await read_consumption(settings.db_path, first, MIDNIGHT) == [
        ConsumptionInterval(first, first + SLOT, 1.0), ConsumptionInterval(last, MIDNIGHT, 2.0),
    ]


@respx.mock
async def test_unpublished_meter_data_is_retried_after_an_hour(
    settings: Settings, caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.get(URL).mock(side_effect=[page(), page(reading(MIDNIGHT - SLOT))])
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.AWAITING_PUBLICATION
    health = await check_meter_history(settings)
    assert health.status is Status.OK
    assert "awaiting_publication" in health.detail
    assert "failed" not in caplog.text
    assert await pull_meter_history(settings, NOW + SLOT) == MeterPullOutcome.NOT_DUE
    assert route.call_count == 1
    assert await pull_meter_history(settings, NOW + 2 * SLOT) == MeterPullOutcome.OK
    assert route.call_count == 2


@respx.mock
async def test_empty_store_keeps_its_start_if_publication_lags_past_midnight(
    settings: Settings,
) -> None:
    first = MIDNIGHT - timedelta(days=1)
    route = respx.get(URL).mock(side_effect=[page(), page(reading(first))])
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.AWAITING_PUBLICATION
    next_day = NOW + timedelta(days=1)
    assert await pull_meter_history(settings, next_day) == MeterPullOutcome.AWAITING_PUBLICATION
    assert route.calls.last.request.url.params["period_from"] == first.isoformat()
    assert await read_consumption(settings.db_path, first, MIDNIGHT) == [
        ConsumptionInterval(first, first + SLOT, 1.0),
    ]


@respx.mock
async def test_cancelled_pull_still_counts_as_an_attempt(settings: Settings) -> None:
    requested = asyncio.Event()

    async def blocked(_request):
        requested.set()
        await asyncio.Event().wait()

    respx.get(URL).mock(side_effect=blocked)
    task = asyncio.create_task(pull_meter_history(settings, NOW))
    await asyncio.wait_for(requested.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await read_meter_history(settings.db_path)).last_attempt == NOW
    outcome = await asyncio.wait_for(pull_meter_history(settings, NOW + SLOT), timeout=3)
    assert outcome == MeterPullOutcome.NOT_DUE


@pytest.mark.parametrize("status", [401, 503])
@respx.mock
async def test_api_failure_is_visible_and_does_not_expose_credentials(
    settings: Settings, status: int, caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(URL).respond(status, text=SECRET)
    with caplog.at_level(logging.WARNING):
        result = await pull_meter_history(settings, NOW)
    history = await read_meter_history(settings.db_path)
    health = await check_meter_history(settings)
    assert result == MeterPullOutcome.REQUEST_FAILED
    assert history.last_attempt == NOW
    assert history.last_success is None
    assert health.status is Status.WARN
    assert "request_failed" in health.detail
    assert "Meter history pull failed" in caplog.text
    for output in (caplog.text, repr(history), health.detail, repr(result)):
        assert all(value not in output for value in (SECRET, "test-point", "test-meter", API))


@respx.mock
async def test_consumption_request_exception_is_sanitised(settings: Settings) -> None:
    respx.get(URL).mock(side_effect=httpx.ReadError(SECRET))
    with pytest.raises(OctopusApiError, match="request failed") as caught:
        await fetch_consumption(settings, period_from=MIDNIGHT - timedelta(days=1))
    error = "".join(traceback.format_exception(caught.value))
    assert SECRET not in error
    assert "test-point" not in error
    assert "test-meter" not in error
    assert API not in error


@pytest.mark.parametrize("body", ["not JSON", "[]", '{"results": {}}'])
@respx.mock
async def test_invalid_api_page_is_a_fixed_payload_failure(settings: Settings, body: str) -> None:
    respx.get(URL).respond(200, text=body)
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.INVALID_PAYLOAD
    assert await read_consumption(settings.db_path, MIDNIGHT - timedelta(days=1), MIDNIGHT) == []


@pytest.mark.parametrize("invalid", [
    {"consumption": "NaN"}, {"consumption": -1}, {"consumption": True},
    {"interval_start": "2026-06-01T00:00:00"},
    {"interval_end": "2026-06-01T00:01:00Z"},
    {"interval_end": "2026-06-01T00:00:00Z"},
])
@respx.mock
async def test_invalid_api_interval_rejects_entire_pull(
    settings: Settings, invalid: dict[str, object],
) -> None:
    respx.get(URL).mock(side_effect=[
        page(reading(MIDNIGHT - SLOT), next_url=f"{URL}?page=2"),
        page(reading(datetime(2026, 6, 1, tzinfo=UTC)) | invalid),
    ])
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.INVALID_PAYLOAD
    assert await read_consumption(settings.db_path, MIDNIGHT - timedelta(days=1), MIDNIGHT) == []


@pytest.mark.parametrize("unsafe_url", [
    "https://other.test/v1/consumption/", "http://supplier.test/v1/consumption/",
    "https://supplier.test:444/v1/consumption/", "https://supplier.test/v10/consumption/",
    "https://supplier.test/v1/%2e%2e/private/",
])
@respx.mock
async def test_pagination_cannot_leave_configured_endpoint(
    settings: Settings, unsafe_url: str,
) -> None:
    respx.get(URL).mock(return_value=page(reading(MIDNIGHT - SLOT), next_url=unsafe_url))
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.OFFSITE_NEXT_URL
    assert len(respx.calls) == 1
    assert await read_consumption(settings.db_path, MIDNIGHT - timedelta(days=1), MIDNIGHT) == []


@respx.mock
async def test_unconfigured_meter_pull_never_requests(settings: Settings) -> None:
    settings.octopus_meter_serial = " "
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.NOT_CONFIGURED
    assert not respx.calls
    assert not await asyncio.to_thread(Path(settings.db_path).exists)


def test_meter_pull_has_no_route_to_device_writes() -> None:
    tree = ast.parse(Path(meter_pull.__file__).read_text())
    forbidden = {"devices", "execution", "authority", "transitions", "supply_guard", "soc_monitor",
                 "v2l", "mode_preconditions", "daemon_channel", "commands", "native_apply"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not forbidden.intersection((node.module or "").split("."))
            assert not forbidden.intersection(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            assert not any(forbidden.intersection(alias.name.split(".")) for alias in node.names)
        elif isinstance(node, ast.Attribute):
            assert node.attr != "call_service"
        elif isinstance(node, ast.Name):
            assert node.id != "call_service"


@respx.mock
async def test_encoded_separator_in_next_path_is_refused(settings: Settings) -> None:
    respx.get(URL).mock(return_value=page(
        reading(MIDNIGHT - SLOT), next_url=f"{API}/electricity-meter-points%2Fother/?page=2",
    ))
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.OFFSITE_NEXT_URL
    assert len(respx.calls) == 1


@respx.mock
async def test_endless_distinct_cursors_stop_at_the_page_cap(
    settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_spark.energy import octopus

    monkeypatch.setattr(octopus, "_MAX_CONSUMPTION_PAGES", 3)
    respx.get(URL).mock(side_effect=[
        page(reading(MIDNIGHT - SLOT), next_url=f"{URL}?page={n}") for n in range(2, 6)
    ])
    assert await pull_meter_history(settings, NOW) == MeterPullOutcome.INVALID_PAYLOAD
    assert len(respx.calls) == 3
    assert await read_consumption(settings.db_path, MIDNIGHT - timedelta(days=1), MIDNIGHT) == []


@respx.mock
async def test_cli_fetch_still_skips_bad_items(settings: Settings) -> None:
    respx.get(URL).mock(return_value=page(
        reading(MIDNIGHT - SLOT), reading(MIDNIGHT - 2 * SLOT) | {"consumption": "NaN"},
    ))
    rows = await fetch_consumption(settings, period_from=MIDNIGHT - timedelta(days=1))
    assert rows == [ConsumptionInterval(MIDNIGHT - SLOT, MIDNIGHT, 1.0)]
