"""Only fresh native snapshot evidence can admit same-day solar export."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.solar_source import NativeForecast, SolarSource
from ha_spark.ha.rest import HomeAssistantRest
from tests.test_solar_forecasts import _frozen_clock


@pytest.mark.parametrize("case,usable", [
    ("ok", True), ("quota_exhausted", True), ("boundary", True),
    ("old", False), ("stale", False), ("auth_failed", False), ("no_data", False),
    ("link_old", False), ("link_future", False), ("link_missing", False),
    ("fetch_future", False), ("fetch_missing", False), ("entities", False),
    ("missing", False),
])
@pytest.mark.parametrize("now", [
    datetime(2026, 3, 29, 0, 15, tzinfo=UTC), datetime(2026, 10, 25, 0, 15, tzinfo=UTC),
])
@respx.mock
async def test_export_evidence_inputs(
    monkeypatch: pytest.MonkeyPatch, case: str, usable: bool, now: datetime,
) -> None:
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", latitude=51, longitude=0,
        timezone="Europe/London", solar_haircut_k=0.1, solar_percentile=10,
    )
    monkeypatch.setattr(sources, "datetime", _frozen_clock(now))
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(return_value=LoadForecast(
        24, (0.5,) * 48, "test",
    )))
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(return_value=httpx.Response(404))
    fetched = now
    received = now
    if case in {"boundary", "old"}:
        fetched -= timedelta(hours=6, seconds=1 if case == "old" else 0)
        received -= timedelta(seconds=90)
    if case == "link_old":
        received -= timedelta(seconds=91)
    if case == "link_future":
        received += timedelta(seconds=1)
    if case == "fetch_future":
        fetched += timedelta(seconds=1)
    start = now.replace(minute=0)
    # One uncovered slot; coverage is per instant, not a whole-day requirement.
    slots = tuple((start + timedelta(minutes=30 * i), 1., 2., 3.) for i in range(1, 96))
    status = case if case in {"quota_exhausted", "stale", "auth_failed", "no_data"} else "ok"
    forecast = NativeForecast(status, None if case == "fetch_missing" else fetched, 2, 10, slots)
    source = SolarSource(
        "entities" if case == "entities" else "native",
        None if case == "missing" else forecast,
        None if case == "link_missing" else received, now,
    )
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _, _ = await sources.gather_inputs(
            settings, rest, source=source, soc=SocMeasurement(SocStatus.OK, now, 50),
        )
    if not usable:
        assert inputs.export_p90_kw is None
        return
    assert inputs.geometry is not None and inputs.now is not None
    assert inputs.export_p90_kw is not None
    expected = tuple(
        3.0 if instant != start and instant.astimezone(inputs.now.tzinfo).date()
        == inputs.now.date() else None for instant in inputs.geometry.slot_starts
    )
    assert inputs.export_p90_kw == expected
    assert inputs.export_p90_kw[0] is None
    assert 3.0 in inputs.export_p90_kw
