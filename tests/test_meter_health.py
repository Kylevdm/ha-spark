"""Meter history health uses only the household SQLite store."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.meter_pull import MeterPullOutcome, pull_meter_history
from ha_spark.health import Status, check_meter_history


@respx.mock
async def test_meter_health_does_not_create_database_or_contact_supplier(tmp_path: Path) -> None:
    db = tmp_path / "household.db"
    settings = Settings(db_path=str(db), octopus_api_key="")
    row = await check_meter_history(settings)
    assert row.name == "Meter history"
    assert row.status is Status.OK
    assert "not configured" in row.detail
    assert not await asyncio.to_thread(db.exists)
    assert not respx.calls

    settings = settings.model_copy(update={
        "octopus_api_key": "sk_test",
        "octopus_mpan": "test-point",
        "octopus_meter_serial": "test-meter",
    })
    row = await check_meter_history(settings)
    assert row.status is Status.OK
    assert "no stored intervals; no pull yet" in row.detail
    assert not await asyncio.to_thread(db.exists)
    assert not respx.calls


@respx.mock
async def test_meter_health_reports_stored_range_and_last_success_after_failure(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=str(tmp_path / "household.db"), timezone="UTC",
        octopus_api_url="https://supplier.test/v1", octopus_api_key="sk_test",
        octopus_mpan="test-point", octopus_meter_serial="test-meter",
    )
    start = datetime(2026, 6, 1, 23, 30, tzinfo=UTC)
    midnight = start + timedelta(minutes=30)
    now = midnight + timedelta(hours=8)
    route = respx.get(
        "https://supplier.test/v1/electricity-meter-points/test-point/meters/test-meter/consumption/",
    ).mock(side_effect=[
        httpx.Response(200, json={"results": [{
            "interval_start": start.isoformat(), "interval_end": midnight.isoformat(),
            "consumption": 1.0,
        }], "next": None}),
        httpx.Response(503),
    ])
    assert await pull_meter_history(settings, now) == MeterPullOutcome.OK
    outcome = await pull_meter_history(settings, now + timedelta(days=1))
    assert outcome == MeterPullOutcome.REQUEST_FAILED
    row = await check_meter_history(settings)
    assert row.status is Status.WARN
    assert "stored range: 2026-06-01 23:30 UTC to 2026-06-02 00:00 UTC" in row.detail
    assert "last pull: 2026-06-03 08:00 UTC (request_failed)" in row.detail
    assert "last successful pull: 2026-06-02 08:00 UTC" in row.detail
    assert route.call_count == 2
