"""#200 regression: the load forecast targets the served day (#397).

Flipped from ``research/200-horizon-after-midnight`` (which pinned the old
behaviour): a replan inside Sunday night's running charge window now sizes
against *Monday's* load profile — the day the charge serves — instead of
Tuesday's. The horizon itself still starts at the running-or-next window
(the current-slot horizon is activated in #404); only the forecast's target
day moves, so the plan describes the day it is living through.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import forecast, sources
from ha_spark.energy.models import LoadForecast, PlannerInputs, SlotProfile
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.rest import HomeAssistantRest

TZ = ZoneInfo("Europe/London")
SUNDAY = date(2026, 1, 4)
MONDAY = date(2026, 1, 5)
TUESDAY = date(2026, 1, 6)


def at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, frozen: datetime) -> None:
    """Pin ``now`` in every module the gather path reads the clock from."""

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> FixedDatetime:
            return cls.fromtimestamp(frozen.timestamp(), tz or UTC)

    monkeypatch.setattr(sources, "datetime", FixedDatetime)
    monkeypatch.setattr(forecast, "datetime", FixedDatetime)


async def _gather(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    window: tuple[str, str],
    *,
    solar_today_entity: str = "",
    solar_today: tuple[str, dict[str, Any]] | None = None,
    solar_tomorrow: tuple[str, dict[str, Any]] | None = None,
) -> tuple[PlannerInputs, LoadForecast, list[date]]:
    """Run ``gather_inputs`` at a frozen clock; return inputs, forecast, targets.

    The mocked forecast pipeline yields 0.25 kWh per slot for Monday's
    day-type profile (12 kWh/day) and 0.5 per slot otherwise (24 kWh/day), so
    the served day is visible in every number the plan carries. Solar
    payloads are ``(state, attributes)`` pairs for the today/tomorrow
    entities; an unset pair leaves that date's solar unread (zero).
    """
    _freeze_clock(monkeypatch, now)

    async def no_statistics(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        return []

    async def no_context(*_args: object, **_kwargs: object) -> tuple[float, list[str]]:
        return 1.0, []

    observed: list[date] = []

    def day_slots(_profile: SlotProfile, target: date) -> tuple[float, ...]:
        observed.append(target)
        return (0.25 if target == MONDAY else 0.5,) * 48

    monkeypatch.setattr(forecast, "statistics_during_period", no_statistics)
    monkeypatch.setattr(
        forecast,
        "build_slot_profile",
        lambda *_args, **_kwargs: SlotProfile(
            weekday=(0.0,) * 48, weekend=(0.0,) * 48, days_used=14
        ),
    )
    monkeypatch.setattr(forecast, "predict_day_slots", day_slots)
    monkeypatch.setattr(forecast, "_context_scale", no_context)

    respx.get("http://ha.test/api/states/sensor.solar_today").mock(
        return_value=httpx.Response(404)
        if solar_today is None
        else httpx.Response(
            200,
            json={
                "entity_id": "sensor.solar_today",
                "state": solar_today[0],
                "attributes": solar_today[1],
            },
        )
    )
    respx.get("http://ha.test/api/states/sensor.solar").mock(
        return_value=httpx.Response(404)
        if solar_tomorrow is None
        else httpx.Response(
            200,
            json={
                "entity_id": "sensor.solar",
                "state": solar_tomorrow[0],
                "attributes": solar_tomorrow[1],
            },
        )
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="test",
        charge_window_start=window[0],
        charge_window_end=window[1],
        timezone="Europe/London",
        latitude=0.0,
        longitude=0.0,
        load_model="median",
        solar_tomorrow_entity="sensor.solar",
        solar_today_entity=solar_today_entity,
    )
    soc = SocMeasurement(status=SocStatus.OK, observed_at=now, value=50.0)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _cfg, forecast_out = await sources.gather_inputs(settings, rest, soc=soc)
    return inputs, forecast_out, observed


@respx.mock
@pytest.mark.parametrize(
    ("now", "served", "horizon_start", "load_kwh", "drain"),
    [
        # Before the window: tonight's window (Sun 23:30) serves Monday.
        (at(SUNDAY, 23, 0), MONDAY, at(SUNDAY, 23, 30), 12.0, 0.25),
        # Inside Sunday night's running window: the charge serves Monday, not
        # Tuesday (#200) — the whole 00:30–05:30 stretch replans on Monday.
        (at(MONDAY, 0, 30), MONDAY, at(SUNDAY, 23, 30), 12.0, 0.0),
        (at(MONDAY, 1, 0), MONDAY, at(SUNDAY, 23, 30), 12.0, 0.0),
        (at(MONDAY, 5, 0), MONDAY, at(SUNDAY, 23, 30), 12.0, 0.0),
        # The window has closed: the next one (Mon 23:30) serves Tuesday.
        (at(MONDAY, 6, 0), TUESDAY, at(MONDAY, 23, 30), 24.0, 0.0),
    ],
    ids=["sun-2300", "mon-0030", "mon-0100", "mon-0500", "mon-0600"],
)
async def test_gather_inputs_loads_the_served_days_profile(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    served: date,
    horizon_start: datetime,
    load_kwh: float,
    drain: float,
) -> None:
    inputs, load_forecast, observed = await _gather(monkeypatch, now, ("23:30", "05:30"))

    # The forecast pipeline was asked for the served day's profile.
    assert observed == [served]
    assert load_forecast.target_date == served
    # The plan carries the served day's numbers.
    assert inputs.predicted_home_load_kwh == pytest.approx(load_kwh)
    assert inputs.load_slots is not None
    assert inputs.load_slots[0] == pytest.approx(load_kwh / 48.0)
    # The horizon itself still starts at the running-or-next window (#404).
    assert inputs.horizon_start == horizon_start
    assert inputs.pre_window_drain_kwh == pytest.approx(drain)


@respx.mock
@pytest.mark.parametrize(
    ("now", "served", "horizon_start"),
    [
        # A non-wrapping 00:30-04:30 window serves its own day while it runs.
        (at(MONDAY, 0, 0), MONDAY, at(MONDAY, 0, 30)),
        (at(MONDAY, 1, 0), MONDAY, at(MONDAY, 0, 30)),
        # Half-open membership: at 04:30 the window has ended, so Tuesday's
        # window is the running-or-next one and Tuesday is served.
        (at(MONDAY, 4, 30), TUESDAY, at(TUESDAY, 0, 30)),
    ],
    ids=["mon-0000", "mon-0100", "mon-0430"],
)
async def test_served_day_for_a_non_wrapping_window_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    served: date,
    horizon_start: datetime,
) -> None:
    """The 00:30-04:30 spec table, end to end through ``gather_inputs``."""
    inputs, load_forecast, observed = await _gather(monkeypatch, now, ("00:30", "04:30"))

    assert observed == [served]
    assert load_forecast.target_date == served
    assert inputs.horizon_start == horizon_start


# Solar payloads: (state, attributes). One detailed entry each, so every
# placed value is unambiguous: the today entity forecasts 4 kWh at Monday
# 17:00, the tomorrow entity 8 kWh at Tuesday 12:00.
_SOLAR_TODAY = (
    "4.0",
    {"detailedForecast": [{"period_start": "2026-01-05T17:00:00+00:00", "pv_estimate": 1.0}]},
)
_SOLAR_TOMORROW = (
    "8.0",
    {"detailedForecast": [{"period_start": "2026-01-06T12:00:00+00:00", "pv_estimate": 1.0}]},
)


@respx.mock
async def test_gather_inputs_places_solar_by_calendar_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#398: solar is placed per calendar date, from the today/tomorrow entities.

    The #200 research test pinned the old behaviour (the today's-solar entity
    was configured but never read); flipped, the entity is read and its solar
    lands on today-dated slots, while any other date in the horizon (the
    yesterday-dated tail of a running window) stays zero.
    """
    inputs, _load_forecast, _observed = await _gather(
        monkeypatch,
        at(MONDAY, 1, 0),
        ("23:30", "05:30"),
        solar_today_entity="sensor.solar_today",
        solar_today=_SOLAR_TODAY,
        solar_tomorrow=_SOLAR_TOMORROW,
    )

    # The horizon still starts at the running window (Sun 23:30): slot 0 is
    # Sunday 23:30 (yesterday's date) and Monday 17:00 is slot 35.
    assert inputs.horizon_start == at(SUNDAY, 23, 30)
    assert inputs.solar_slots is not None
    # Yesterday-dated slots are zero: slot 0 is Sunday 23:30.
    assert inputs.solar_slots[0] == 0.0
    # Today's (Monday's) forecast solar lands on the Monday-dated slots.
    assert inputs.solar_slots[35] == pytest.approx(4.0)  # Monday 17:00
    assert inputs.solar_slots[24] == 0.0  # Monday 12:00 has no forecast solar
    # The tomorrow entity is read but its Tuesday date is not in this
    # horizon, so nothing else carries solar.
    assert max(inputs.solar_slots) == pytest.approx(4.0)
    # The served day is Monday (today), so the day total is the today entity's.
    assert inputs.solar_tomorrow_kwh == pytest.approx(4.0)


@respx.mock
async def test_gather_inputs_serves_tomorrows_solar_after_the_window_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the window closes, the next window's day is tomorrow: its entity serves."""
    inputs, _load_forecast, _observed = await _gather(
        monkeypatch,
        at(MONDAY, 6, 0),
        ("23:30", "05:30"),
        solar_today_entity="sensor.solar_today",
        solar_today=_SOLAR_TODAY,
        solar_tomorrow=_SOLAR_TOMORROW,
    )

    # The horizon starts at tonight's window (Mon 23:30): slot 0 is Monday
    # 23:30 (today) and Tuesday 12:00 is slot 25.
    assert inputs.horizon_start == at(MONDAY, 23, 30)
    assert inputs.solar_slots is not None
    # Today-dated slots carry today's forecast (nothing at 23:30), and
    # tomorrow's date carries the tomorrow entity's solar.
    assert inputs.solar_slots[0] == 0.0
    assert inputs.solar_slots[25] == pytest.approx(8.0)  # Tuesday 12:00
    assert max(inputs.solar_slots) == pytest.approx(8.0)
    # The served day is Tuesday (tomorrow), so the day total is the tomorrow
    # entity's.
    assert inputs.solar_tomorrow_kwh == pytest.approx(8.0)


@respx.mock
async def test_gather_inputs_zero_solar_for_today_without_the_today_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a today's-solar entity, today-dated solar counts as zero.

    A missing forecast is zero everywhere (decision A): the served day is
    Monday (the running window serves it), so the day-total input is zero and
    no slot carries solar, even though the tomorrow entity reads fine — its
    Tuesday date is not in this horizon.
    """
    inputs, _load_forecast, _observed = await _gather(
        monkeypatch,
        at(MONDAY, 1, 0),
        ("23:30", "05:30"),
        solar_tomorrow=_SOLAR_TOMORROW,
    )

    assert inputs.solar_slots is not None
    assert inputs.solar_slots == (0.0,) * 48
    assert inputs.solar_tomorrow_kwh == 0.0
