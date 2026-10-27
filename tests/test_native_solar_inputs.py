"""Native solar reaches planning by real UTC half-hours, never entity reads."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.api.contract import SiteSnapshot
from ha_spark.config import Settings
from ha_spark.energy import scheduler, sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.plan_view import build_plan_view
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.solar_forecasts import recorded_solar
from ha_spark.energy.solar_source import (
    NativeForecast,
    SolarSource,
    parse_native_forecast,
    save_solar_source_kind,
    solar_source_from_store,
)
from ha_spark.ha.rest import HomeAssistantRest
from tests.test_solar_forecasts import _frozen_clock

NOW = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
TZ = ZoneInfo("Europe/London")


@pytest.mark.parametrize("percentile,power", [(10, 1), (50, 2), (90, 3)])
@pytest.mark.parametrize("case", [
    "ok", "stale", "auth_failed", "quota_exhausted", "no_data", "incomplete",
    "old", "future", "restart", "boundary", "missing", "incomplete_today",
])
@pytest.mark.parametrize("now", [
    NOW, datetime(2026, 10, 24, 12, tzinfo=UTC), datetime(2026, 3, 28, 12, tzinfo=UTC),
])
@pytest.mark.parametrize("daily", [False, True])
@respx.mock
async def test_native_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, percentile: int, power: int, case: str,
    now: datetime, daily: bool,
) -> None:
    settings = Settings(
        db_path=str(tmp_path / "spark.db"), ha_url="http://ha.test", ha_token="t",
        timezone="Europe/London",
        solar_today_entity="sensor.forbidden_today", solar_tomorrow_entity="sensor.forbidden",
        solar_percentile=percentile, latitude=51, longitude=0, solar_haircut_k=0.7,
    )
    monkeypatch.setattr(sources, "datetime", _frozen_clock(now))
    monkeypatch.setattr(scheduler, "datetime", _frozen_clock(now))
    monkeypatch.setattr("ha_spark.energy.solar_source.datetime", _frozen_clock(now))
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(return_value=LoadForecast(
        24, None if daily else (0.5,) * 48, "test",
    )))
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(return_value=httpx.Response(404))
    start = now.replace(minute=0)
    tomorrow = now.astimezone(TZ).date() + timedelta(days=1)
    end = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time(), TZ).astimezone(UTC)
    slots = tuple((start + timedelta(minutes=30 * i), 1., 2., 3.)
                  for i in range(int((end - start).total_seconds() / 1800)))
    if case == "incomplete":
        slots = slots[:-1]
    if case == "incomplete_today":
        slots = slots[1:]
    fetched = now - timedelta(hours=37) if case == "old" else now
    if case == "future":
        fetched = now + timedelta(seconds=1)
    if case == "boundary":
        fetched = now - timedelta(hours=36)
    status = case if case in {"stale", "auth_failed", "quota_exhausted", "no_data"} else "ok"
    forecast = NativeForecast(status, fetched, 2, 10, slots)
    source = SolarSource(
        "native", None if case == "missing" else forecast, now - timedelta(minutes=5), now,
    )
    if case == "restart":
        save_solar_source_kind(settings, "native")
        source = solar_source_from_store(settings)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _, _ = await sources.gather_inputs(
            settings, rest, source=source,
            soc=SocMeasurement(SocStatus.OK, now, 50),
        )
    assert all("forbidden" not in str(call.request.url) for call in respx.calls)
    if daily:
        assert inputs.solar_slots is None and inputs.solar_slots_known is None
        served_today = now == NOW
        unusable = case in {"old", "future", "restart", "missing"} or (
            case == "incomplete_today" if served_today else case == "incomplete"
        )
        served = now.astimezone(TZ).date() if served_today else tomorrow
        expected = sum(power * 0.5 for instant, *_ in slots
                       if instant.astimezone(TZ).date() == served)
        assert inputs.solar_tomorrow_kwh == (0 if unusable else expected)
        return
    assert inputs.solar_slots is not None and inputs.solar_slots_known is not None
    if case in {"old", "future", "restart", "missing"}:
        assert not any(inputs.solar_slots_known)
        assert not any(inputs.solar_slots)
        assert inputs.solar_tomorrow_kwh == 0
    else:
        assert inputs.geometry is not None
        for instant, kwh, known in zip(
            inputs.geometry.slot_starts, inputs.solar_slots, inputs.solar_slots_known, strict=True,
        ):
            usable = instant < end and not (
                case == "incomplete" and instant.astimezone(TZ).date() == tomorrow
            ) and not (
                case == "incomplete_today" and instant.astimezone(TZ).date() != tomorrow
            )
            assert known == usable
            assert kwh == (power * 0.5 if usable else 0)
        served = inputs.geometry.served_day
        expected = sum(power * 0.5 for instant, *_ in slots
                       if instant.astimezone(TZ).date() == served)
        if (case == "incomplete" and served == tomorrow) or (
            case == "incomplete_today" and served != tomorrow
        ):
            expected = 0
        assert inputs.solar_tomorrow_kwh == expected
        if now == NOW:
            # Repeated 01:00 has distinct instants, each with one full-slot value.
            folds = [i for i, instant in enumerate(inputs.geometry.slot_starts)
                     if instant.astimezone(TZ).hour == 1
                     and instant.astimezone(TZ).date() == now.astimezone(TZ).date()]
            assert len(folds) == 4
            assert sum(inputs.solar_slots[i] for i in folds) == (
                0 if case == "incomplete_today" else 2 * power
            )
    # The daemon pipeline records only genuine raw native values, not haircut or unknown zeros.
    result = await scheduler.run_once(settings, source=source, soc=SocMeasurement(
        SocStatus.OK, now, 50,
    ))
    run = result.plan_run.run
    assert run.inputs.geometry is not None
    starts = run.inputs.geometry.slot_starts
    recorded = await recorded_solar(settings.db_path, starts)
    assert recorded == {
        instant: kwh for instant, kwh, known in zip(
            starts, run.inputs.solar_slots, run.inputs.solar_slots_known, strict=True,
        ) if known and instant > source.now
    }
    view = build_plan_view(run.plan, run.cfg, run.inputs, run.schedule)
    assert view.slots is not None
    assert [slot.solar_kwh for slot in view.slots] == [
        slot.solar_raw_kwh * 0.7 for slot in view.slots
    ]
    assert [slot.solar_raw_kwh for slot in view.slots] == [
        kwh * (run.inputs.geometry.slot0_remaining_frac if i == 0 else 1)
        for i, kwh in enumerate(run.inputs.solar_slots)
    ]


@pytest.mark.parametrize("version", [1, 2])
@respx.mock
async def test_unconfigured_snapshots_use_entities(
    monkeypatch: pytest.MonkeyPatch, version: int,
) -> None:
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", latitude=51, longitude=0,
        solar_tomorrow_entity="sensor.legacy", solar_today_entity="sensor.legacy_today",
    )
    snapshot = SiteSnapshot.model_validate({
        "api_version": version, "release_version": "0.28.1",
        "site_id": "12345678-1234-5678-1234-567812345678", "sent_at": NOW.isoformat(),
        **({"solar_forecast": None} if version == 2 else {}),
    })
    parsed = parse_native_forecast(snapshot.solar_forecast, received_at=NOW)
    monkeypatch.setattr(sources, "datetime", _frozen_clock(NOW))
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(return_value=LoadForecast(
        24, (0.5,) * 48, "test",
    )))
    for entity in ("sensor.legacy", "sensor.legacy_today"):
        respx.get(f"http://ha.test/api/states/{entity}").mock(
            return_value=httpx.Response(200, json={
                "entity_id": entity, "state": "12", "attributes": {},
            }),
        )
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _, _ = await sources.gather_inputs(
            settings, rest, source=SolarSource(parsed.kind, parsed.forecast, NOW, NOW),
            soc=SocMeasurement(SocStatus.OK, NOW, 50),
        )
    assert inputs.solar_tomorrow_kwh == 12
    assert {call.request.url.path for call in respx.calls} == {
        "/api/states/sensor.legacy", "/api/states/sensor.legacy_today",
    }
