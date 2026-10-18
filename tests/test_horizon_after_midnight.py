"""Issue #200: forecast dates around a charge window that crosses midnight."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import forecast, sources
from ha_spark.energy.models import SlotProfile
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.rest import HomeAssistantRest

TZ = ZoneInfo("Europe/London")
SUNDAY = date(2026, 1, 4)
MONDAY = date(2026, 1, 5)
TUESDAY = date(2026, 1, 6)


@pytest.mark.parametrize(
    ("day", "hour", "minute", "horizon_day", "solar_kwh", "load_day", "load_kwh", "drain"),
    [
        (SUNDAY, 23, 0, SUNDAY, 8.0, MONDAY, 12.0, 0.25),
        (MONDAY, 0, 30, SUNDAY, 2.0, TUESDAY, 24.0, 0.0),
        (MONDAY, 1, 0, SUNDAY, 2.0, TUESDAY, 24.0, 0.0),
        (MONDAY, 5, 0, SUNDAY, 2.0, TUESDAY, 24.0, 0.0),
        (MONDAY, 6, 0, MONDAY, 2.0, TUESDAY, 24.0, 0.0),
    ],
    ids=["sun-2300", "mon-0030", "mon-0100", "mon-0500", "mon-0600"],
)
async def test_gather_inputs_forecast_dates_across_midnight(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
    day: date,
    hour: int,
    minute: int,
    horizon_day: date,
    solar_kwh: float,
    load_day: date,
    load_kwh: float,
    drain: float,
) -> None:
    frozen = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: ZoneInfo | None = None) -> FixedDatetime:
            return cls.fromtimestamp(frozen.timestamp(), tz or UTC)

    monkeypatch.setattr(sources, "datetime", FixedDatetime)
    monkeypatch.setattr(forecast, "datetime", FixedDatetime)

    async def no_statistics(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return []

    async def no_context(*_args: object) -> tuple[float, list[str]]:
        return 1.0, []

    observed_load_days: list[date] = []

    def day_slots(_profile: SlotProfile, target: date) -> tuple[float, ...]:
        observed_load_days.append(target)
        return (0.25 if target == MONDAY else 0.5,) * 48

    monkeypatch.setattr(forecast, "statistics_during_period", no_statistics)
    monkeypatch.setattr(forecast, "build_slot_profile", lambda *_args, **_kw: SlotProfile(
        weekday=(0.0,) * 48, weekend=(0.0,) * 48, days_used=14
    ))
    monkeypatch.setattr(forecast, "predict_day_slots", day_slots)
    monkeypatch.setattr(forecast, "_context_scale", no_context)

    today_solar = 3.0 if day == SUNDAY else 8.0
    tomorrow_solar = 8.0 if day == SUNDAY else 2.0
    today_route = respx_mock.get("http://ha.test/api/states/sensor.solar_today").mock(
        return_value=httpx.Response(
            200,
            json={"entity_id": "sensor.solar_today", "state": str(today_solar), "attributes": {}},
        )
    )
    tomorrow_route = respx_mock.get("http://ha.test/api/states/sensor.solar_tomorrow").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.solar_tomorrow",
                "state": str(tomorrow_solar),
                "attributes": {},
            },
        )
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="test",
        solar_tomorrow_entity="sensor.solar_tomorrow",
        charge_window_start="23:30",
        charge_window_end="05:30",
        timezone="Europe/London",
        latitude=0.0,
        longitude=0.0,
        load_model="median",
    )
    soc = SocMeasurement(status=SocStatus.OK, observed_at=frozen, value=50.0)

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _config, _source = await sources.gather_inputs(settings, rest, soc=soc)

    # matches intent: slot zero remains at the running window through 05:30.
    assert inputs.horizon_start == datetime.combine(horizon_day, datetime.min.time(), TZ).replace(
        hour=23, minute=30
    )
    # matches intent on Sunday and at 06:00; suspected bug (#200) inside Monday's running
    # window: the configured tomorrow entity holds Tuesday's solar for a Monday horizon.
    assert inputs.solar_tomorrow_kwh == solar_kwh
    # matches intent on Sunday and at 06:00; suspected bug (#200) inside the running window.
    assert inputs.solar_slots is not None
    assert sum(inputs.solar_slots) == pytest.approx(solar_kwh)
    # matches intent on Sunday and at 06:00; suspected bug (#200) inside the running window.
    assert observed_load_days == [load_day]
    assert inputs.predicted_home_load_kwh == load_kwh  # suspected bug (#200) inside window
    # matches intent on Sunday and at 06:00; suspected bug (#200) inside the running window.
    assert inputs.load_slots is not None
    assert inputs.load_slots[0] == (0.25 if load_day == MONDAY else 0.5)
    # matches intent: only the half-hour before opening has forecast pre-window drain.
    assert inputs.pre_window_drain_kwh == pytest.approx(drain)
    # matches intent for the configured entity read; no today-solar setting exists.
    assert tomorrow_route.call_count == 1
    assert today_route.call_count == 0
