"""Tests for gathering planner inputs from HA."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import DispatchSlot, FlexibilityEvent, LoadForecast
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.sources import build_config, build_schedule, gather_inputs, pre_window_drain
from ha_spark.energy.tariff import fixed_schedule
from ha_spark.ha.rest import HomeAssistantRest

BASE = "http://ha.test/api"
_HOUSEHOLD_TZ = ZoneInfo("Europe/London")


def _state(
    eid: str,
    state: str,
    attrs: dict[str, Any] | None = None,
    *,
    last_reported: str | None = "now",
) -> httpx.Response:
    """A state payload. ``last_reported`` defaults to a just-now timestamp."""
    body: dict[str, Any] = {"entity_id": eid, "state": state, "attributes": attrs or {}}
    if last_reported == "now":
        body["last_reported"] = datetime.now(UTC).isoformat()
    elif last_reported is not None:
        body["last_reported"] = last_reported
    return httpx.Response(200, json=body)


def _settings() -> Settings:
    return Settings(
        ha_url="http://ha.test",
        ha_token="t",
        soc_entity="sensor.soc",
        battery_voltage_entity="sensor.volt",
        solar_tomorrow_entity="sensor.solar",
        dispatch_entity="binary_sensor.dispatch",
        ev_status_entity="sensor.ev",
        ha_template_charge_needed_entity="sensor.tmpl",
    )


def test_build_config_threads_export_limits_to_the_pure_planner() -> None:
    cfg = build_config(
        Settings(
            battery_discharge_ceiling_kw=3.2,
            dno_export_limit_kw=7.36,
            supply_max_current_a=60.0,
            supply_voltage_v=230.0,
        ),
        voltage_v=51.0,
    )

    assert cfg.battery_discharge_ceiling_kw == 3.2
    assert cfg.dno_export_limit_kw == 7.36
    assert cfg.supply_max_current_a == 60.0
    assert cfg.supply_voltage_v == 230.0


@pytest.mark.parametrize(
    ("now", "window_start", "window_end", "expected_start", "expected_first_slot"),
    [
        pytest.param(
            datetime(2026, 10, 4, 23, 45, tzinfo=_HOUSEHOLD_TZ),
            time(23, 30),
            time(5, 30),
            datetime(2026, 10, 4, 23, 30, tzinfo=_HOUSEHOLD_TZ),
            47.0,
            id="overnight-before-midnight-inside-window",
        ),
        pytest.param(
            datetime(2026, 10, 5, 0, 30, tzinfo=_HOUSEHOLD_TZ),
            time(23, 30),
            time(5, 30),
            datetime(2026, 10, 4, 23, 30, tzinfo=_HOUSEHOLD_TZ),
            47.0,
            id="overnight-after-midnight-inside-window",
        ),
        pytest.param(
            datetime(2026, 10, 5, 12, 0, tzinfo=_HOUSEHOLD_TZ),
            time(23, 30),
            time(5, 30),
            datetime(2026, 10, 5, 23, 30, tzinfo=_HOUSEHOLD_TZ),
            47.0,
            id="overnight-during-day",
        ),
        pytest.param(
            datetime(2026, 10, 5, 1, 0, tzinfo=_HOUSEHOLD_TZ),
            time(0, 30),
            time(4, 30),
            datetime(2026, 10, 5, 0, 30, tzinfo=_HOUSEHOLD_TZ),
            1.0,
            id="same-day-window-inside-window",
        ),
        pytest.param(
            datetime(2026, 10, 5, 6, 0, tzinfo=_HOUSEHOLD_TZ),
            time(0, 30),
            time(4, 30),
            datetime(2026, 10, 6, 0, 30, tzinfo=_HOUSEHOLD_TZ),
            1.0,
            id="same-day-window-after-window",
        ),
        pytest.param(
            datetime(2026, 10, 5, 5, 30, tzinfo=_HOUSEHOLD_TZ),
            time(23, 30),
            time(5, 30),
            datetime(2026, 10, 5, 23, 30, tzinfo=_HOUSEHOLD_TZ),
            47.0,
            id="overnight-window-end-is-exclusive",
        ),
        pytest.param(
            datetime(2026, 10, 5, 4, 30, tzinfo=_HOUSEHOLD_TZ),
            time(0, 30),
            time(4, 30),
            datetime(2026, 10, 6, 0, 30, tzinfo=_HOUSEHOLD_TZ),
            1.0,
            id="same-day-window-end-is-exclusive",
        ),
        pytest.param(
            datetime(2026, 10, 5, 23, 30, tzinfo=_HOUSEHOLD_TZ),
            time(23, 30),
            time(5, 30),
            datetime(2026, 10, 5, 23, 30, tzinfo=_HOUSEHOLD_TZ),
            47.0,
            id="overnight-window-start-is-inclusive",
        ),
        pytest.param(
            datetime(2026, 10, 5, 0, 30, tzinfo=_HOUSEHOLD_TZ),
            time(0, 30),
            time(4, 30),
            datetime(2026, 10, 5, 0, 30, tzinfo=_HOUSEHOLD_TZ),
            1.0,
            id="same-day-window-start-is-inclusive",
        ),
        pytest.param(
            datetime(2026, 10, 5, 0, 10, tzinfo=_HOUSEHOLD_TZ),
            time(0, 30),
            time(4, 30),
            datetime(2026, 10, 5, 0, 30, tzinfo=_HOUSEHOLD_TZ),
            1.0,
            id="same-day-window-before-opening-today",
        ),
    ],
)
def test_slot_horizon_starts_at_the_running_or_next_charge_window(
    now: datetime,
    window_start: time,
    window_end: time,
    expected_start: datetime,
    expected_first_slot: float,
) -> None:
    day_slots = tuple(float(i) for i in range(48))

    rotated, horizon_start = sources._slot_horizon(
        day_slots, window_start, window_end, _HOUSEHOLD_TZ, now
    )

    assert horizon_start == expected_start
    assert rotated[0] == expected_first_slot


@respx.mock
async def test_gather_inputs_parses_live_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)

    respx.get(f"{BASE}/states/sensor.soc").mock(return_value=_state("sensor.soc", "30"))
    respx.get(f"{BASE}/states/sensor.volt").mock(return_value=_state("sensor.volt", "51"))
    respx.get(f"{BASE}/states/sensor.solar").mock(return_value=_state("sensor.solar", "8.75"))
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", "Charging"))
    respx.get(f"{BASE}/states/sensor.tmpl").mock(return_value=_state("sensor.tmpl", "19.0"))
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state(
            "binary_sensor.dispatch",
            "off",
            {
                "planned_dispatches": [
                    {"start": "2026-06-08T13:00:00+01:00", "end": "2026-06-08T13:30:00+01:00",
                     "charge_in_kwh": -2.0, "source": "SMART"}
                ]
            },
        )
    )

    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, load_source = await gather_inputs(s, rest)

    assert inputs.soc_now == 30.0
    assert inputs.soc.ok is True
    assert inputs.soc.value == 30.0
    assert inputs.soc.status is SocStatus.OK
    assert cfg.voltage_v == 51.0
    assert inputs.solar_tomorrow_kwh == 8.75
    assert inputs.predicted_home_load_kwh == 24.0
    assert inputs.ev_charging is True
    assert inputs.ev_hold_charging is True
    assert inputs.ha_template_needed == 19.0
    assert len(inputs.dispatches) == 1
    assert inputs.dispatches[0].source == "SMART"
    assert load_source == "test"


@respx.mock
async def test_disconnected_ev_evidence_drops_dispatch_from_every_planner_use(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    now = datetime.now(UTC)
    horizon = now.replace(hour=23, minute=30, second=0, microsecond=0)
    if horizon <= now:
        horizon += timedelta(days=1)
    paid_start = horizon + timedelta(hours=17)
    paid_end = paid_start + timedelta(minutes=30)
    dispatch = {
        "start": paid_start.isoformat(),
        "end": paid_end.isoformat(),
        "charge_in_kwh": -2.0,
        "source": "SMART",
    }
    respx.get(f"{BASE}/states/sensor.soc").mock(
        return_value=_state("sensor.soc", "80")
    )
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state(
            "binary_sensor.dispatch", "off", {"planned_dispatches": [dispatch]}
        )
    )
    respx.get(f"{BASE}/states/sensor.ev_plug").mock(
        return_value=_state("sensor.ev_plug", " EV Disconnected ")
    )
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", "Paused"))
    respx.get(f"{BASE}/states/sensor.rate").mock(
        return_value=_state("sensor.rate", "0.20", {"is_intelligent_adjusted": False})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    settings = _settings().model_copy(
        update={
            "tariff_provider": "axle",
            "ev_plug_entity": "sensor.ev_plug",
            "octopus_rate_entity": "sensor.rate",
        }
    )
    caplog.set_level("INFO", logger="ha_spark.energy.sources")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, cfg, _src = await gather_inputs(settings, rest)

    assert inputs.dispatches == ()
    assert len(inputs.unrated_dispatches) == 1
    assert inputs.unrated_dispatches[0].start == paid_start
    assert inputs.unrated_dispatches[0].end == paid_end
    dropped = [
        record.getMessage()
        for record in caplog.records
        if record.name == "ha_spark.energy.sources"
        and "Dropped contradicted dispatch" in record.getMessage()
    ]
    assert len(dropped) == 1
    assert paid_start.isoformat() in dropped[0]
    assert paid_end.isoformat() in dropped[0]
    assert "EV plug: EV Disconnected" in dropped[0]
    event = FlexibilityEvent(
        start=paid_start,
        end=paid_end,
        direction="export",
        updated_at=now,
        rate_gbp_kwh=1.0,
    )
    planning_inputs = replace(
        inputs,
        load_slots=(0.0,) * 48,
        solar_slots=(0.0,) * 48,
        horizon_start=horizon,
        flexibility_event=event,
    )
    schedule = build_schedule(settings, planning_inputs, cfg)
    paid_slot_index = int((paid_start - horizon).total_seconds() // 1800)
    assert schedule.controlled_windows == ()
    assert schedule.cheap_fracs[paid_slot_index] == 0.0

    plan = compute_plan(planning_inputs, cfg, schedule)
    assert plan.charge_intent is not None
    assert plan.charge_intent.holds == ()
    assert plan.charge_intent.unrated_holds == ((paid_start, paid_end),)
    assert plan.charge_intent.export is not None
    assert paid_start in plan.charge_intent.export.selected_slots
    assert all("Octopus dispatch hold" not in skip.reason for skip in plan.export_skips)


@pytest.mark.parametrize(
    ("dispatch_state", "extra_attributes", "expected_count"),
    [("on", {}, 1), ("off", {"current_start": "2026-10-03T14:00:00"}, 0)],
    ids=["entity-on", "off-with-leftover-current-start"],
)
@respx.mock
async def test_live_dispatch_beats_disconnected_plug_in_gather_inputs(
    monkeypatch: pytest.MonkeyPatch,
    dispatch_state: str,
    extra_attributes: dict[str, str],
    expected_count: int,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    now = datetime.now(UTC)
    slot = {
        "start": (now + timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=2)).isoformat(),
    }
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state(
            "binary_sensor.dispatch",
            dispatch_state,
            {"planned_dispatches": [slot], **extra_attributes},
        )
    )
    respx.get(f"{BASE}/states/sensor.ev_plug").mock(
        return_value=_state("sensor.ev_plug", "EV Disconnected")
    )
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", "Paused"))
    respx.get(f"{BASE}/states/sensor.rate").mock(
        return_value=_state("sensor.rate", "0.20", {"is_intelligent_adjusted": False})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings().model_copy(
        update={"ev_plug_entity": "sensor.ev_plug", "octopus_rate_entity": "sensor.rate"}
    )

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(settings, rest)

    assert len(inputs.dispatches) == expected_count
    dispatch_reads = [
        call
        for call in respx.calls
        if str(call.request.url).endswith("/states/binary_sensor.dispatch")
    ]
    assert len(dispatch_reads) == 1


@pytest.mark.parametrize(
    ("adjusted_value", "expected_count"),
    [(True, 1), (" TRUE ", 1), (False, 0), ("yes", 0), (1, 0)],
    ids=["bool-true", "string-true", "bool-false", "other-string", "integer"],
)
@respx.mock
async def test_adjusted_rate_evidence_is_strictly_coerced(
    monkeypatch: pytest.MonkeyPatch, adjusted_value: object, expected_count: int
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    now = datetime.now(UTC)
    slot = {
        "start": (now + timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=2)).isoformat(),
    }
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state(
            "binary_sensor.dispatch", "off", {"planned_dispatches": [slot]}
        )
    )
    respx.get(f"{BASE}/states/sensor.ev_plug").mock(
        return_value=_state("sensor.ev_plug", "EV Disconnected")
    )
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", "Paused"))
    respx.get(f"{BASE}/states/sensor.rate").mock(
        return_value=_state(
            "sensor.rate", "0.20", {"is_intelligent_adjusted": adjusted_value}
        )
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings().model_copy(
        update={"ev_plug_entity": "sensor.ev_plug", "octopus_rate_entity": "sensor.rate"}
    )

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(settings, rest)

    assert len(inputs.dispatches) == expected_count


@pytest.mark.parametrize(
    ("adjusted_value", "expected"),
    [(True, True), (" true ", True), (False, False), ("yes", False), (1, False), (None, None)],
    ids=["bool-true", "string-true", "bool-false", "other-string", "integer", "unset"],
)
@respx.mock
async def test_dispatch_evidence_reader_coerces_adjusted_rate(
    adjusted_value: object, expected: bool | None
) -> None:
    respx.get(f"{BASE}/states/sensor.rate").mock(
        return_value=_state(
            "sensor.rate", "0.20", {"is_intelligent_adjusted": adjusted_value}
        )
    )
    settings = _settings().model_copy(
        update={
            "ev_plug_entity": "",
            "ev_status_entity": "",
            "octopus_rate_entity": "sensor.rate",
        }
    )
    slot = DispatchSlot(datetime(2026, 10, 3, 14, 0), datetime(2026, 10, 3, 15, 0))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        reading = await sources.read_dispatch_evidence(
            settings, rest, planned_dispatches=(slot,), live_dispatch=False
        )

    assert reading.evidence.rate_adjusted is expected


@respx.mock
async def test_gather_inputs_skips_plug_and_rate_reads_without_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    plug = respx.get(f"{BASE}/states/sensor.ev_plug").mock(
        return_value=_state("sensor.ev_plug", "EV Connected")
    )
    rate = respx.get(f"{BASE}/states/sensor.rate").mock(
        return_value=_state("sensor.rate", "0.20", {"is_intelligent_adjusted": False})
    )
    ev_status = respx.get(f"{BASE}/states/sensor.ev").mock(
        return_value=_state("sensor.ev", "Paused")
    )
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state("binary_sensor.dispatch", "off", {"planned_dispatches": []})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings().model_copy(
        update={"ev_plug_entity": "sensor.ev_plug", "octopus_rate_entity": "sensor.rate"}
    )

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(settings, rest)

    assert inputs.dispatches == ()
    assert ev_status.call_count == 1
    assert plug.called is False
    assert rate.called is False


@respx.mock
async def test_gather_inputs_tolerates_missing_entities(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _ = await gather_inputs(s, rest)

    assert inputs.soc_now == 0.0
    assert inputs.soc.ok is False
    assert inputs.soc.status is SocStatus.READ_FAILED
    assert cfg.voltage_v == s.battery_voltage_v  # fell back to config default
    assert inputs.dispatches == ()
    assert any("Could not read sensor.volt" in record.getMessage() for record in caplog.records)


@respx.mock
async def test_gather_inputs_skips_unset_optional_entities(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    blank_entity = respx.get(f"{BASE}/states/").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/states/sensor.soc").mock(return_value=httpx.Response(404))

    s = _settings().model_copy(
        update={
            "battery_voltage_entity": "",
            "solar_tomorrow_entity": "",
            "ev_status_entity": "",
            "ha_template_charge_needed_entity": "",
            "dispatch_entity": "",
            "latitude": 51.5,
            "longitude": -0.1,
        }
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _ = await gather_inputs(s, rest)

    assert blank_entity.called is False
    assert not any("Could not read  (" in record.getMessage() for record in caplog.records)
    assert cfg.voltage_v == s.battery_voltage_v
    assert inputs.solar_tomorrow_kwh == 0.0
    assert inputs.ev_charging is False
    assert inputs.ha_template_needed is None


@respx.mock
async def test_gather_inputs_reads_axle_event_source(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    now = datetime.now(UTC)
    event = {
        "start_time": (now + timedelta(hours=1)).isoformat(),
        "end_time": (now + timedelta(hours=2)).isoformat(),
        "import_export": "export",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    respx.get("http://axle.test/vpp/home-assistant/event").mock(
        return_value=httpx.Response(200, json=event)
    )
    respx.route(method="GET", url__startswith=BASE).mock(return_value=httpx.Response(404))

    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        tariff_provider="axle",
        axle_api_url="http://axle.test",
        axle_api_key="secret-token",
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)

    assert inputs.flexibility_event is not None
    assert inputs.flexibility_event.direction == "export"
    assert inputs.flexibility_event_trusted is True


@respx.mock
async def test_gather_inputs_degrades_on_malformed_axle_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get("http://axle.test/vpp/home-assistant/event").mock(
        return_value=httpx.Response(200, json={"start_time": "not-a-timestamp"})
    )
    respx.route(method="GET", url__startswith=BASE).mock(return_value=httpx.Response(404))

    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        tariff_provider="axle",
        axle_api_url="http://axle.test",
        axle_api_key="secret-token",
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _ = await gather_inputs(s, rest)

    assert inputs.flexibility_event is None
    assert inputs.flexibility_event_trusted is False
    assert build_schedule(s, inputs, cfg).export_prices == ()


def test_pre_window_drain_daily_fallback() -> None:
    fc = LoadForecast(total_kwh=24.0, slots=None, source="t")
    now = datetime(2026, 6, 10, 22, 0)
    # 1.5 h until 23:30 at 1 kW average.
    assert pre_window_drain(fc, now, time(23, 30), time(5, 30)) == pytest.approx(1.5)


def test_pre_window_drain_sums_slots_with_proration() -> None:
    fc = LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="t")
    now = datetime(2026, 6, 10, 22, 45)
    # Half of the 22:30 slot (0.25) plus the full 23:00 slot (0.5).
    assert pre_window_drain(fc, now, time(23, 30), time(5, 30)) == pytest.approx(0.75)


def test_pre_window_drain_zero_inside_window_or_far_away() -> None:
    fc = LoadForecast(total_kwh=24.0, slots=None, source="t")
    inside = datetime(2026, 6, 11, 0, 30)  # window wraps midnight
    assert pre_window_drain(fc, inside, time(23, 30), time(5, 30)) == 0.0
    far = datetime(2026, 6, 10, 9, 0)  # 14.5 h before the window opens
    assert pre_window_drain(fc, far, time(23, 30), time(5, 30)) == 0.0


@respx.mock
async def test_solar_percentile_prefers_estimate_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/sensor.solar").mock(
        return_value=_state("sensor.solar", "8.75", {"estimate10": 5.5, "estimate90": 12.0})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(**{**_settings().model_dump(), "solar_percentile": 10})
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)
    assert inputs.solar_tomorrow_kwh == 5.5


@respx.mock
async def test_solar_percentile_falls_back_to_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/sensor.solar").mock(
        return_value=_state("sensor.solar", "8.75")  # no estimate10 attribute
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(**{**_settings().model_dump(), "solar_percentile": 10})
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)
    assert inputs.solar_tomorrow_kwh == 8.75


def test_parse_detailed_forecast_percentile_key() -> None:
    raw = [
        {"period_start": "2026-06-11T12:00:00+01:00", "pv_estimate": 2.0, "pv_estimate10": 1.0}
    ]
    assert sources._parse_detailed_forecast(raw, 50) == [
        (sources.datetime.fromisoformat("2026-06-11T12:00:00+01:00"), 2.0)
    ]
    assert sources._parse_detailed_forecast(raw, 10) == [
        (sources.datetime.fromisoformat("2026-06-11T12:00:00+01:00"), 1.0)
    ]
    # Missing percentile key falls back to the median estimate.
    bare = [{"period_start": "2026-06-11T12:00:00+01:00", "pv_estimate": 2.0}]
    parsed = sources._parse_detailed_forecast(bare, 10)
    assert parsed is not None and parsed[0][1] == 2.0


@respx.mock
async def test_gather_inputs_flags_unavailable_soc(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/sensor.soc").mock(
        return_value=_state("sensor.soc", "unavailable")
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)

    assert inputs.soc_now == 0.0
    assert inputs.soc.ok is False
    assert inputs.soc.status is SocStatus.UNAVAILABLE


@respx.mock
async def test_quantile_buffer_derived_from_ml_p90(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(
            total_kwh=20.0, slots=None, source="ml quantile gbr", p90_total_kwh=23.0
        )

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t", buffer_mode="quantile")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        _, cfg, _ = await gather_inputs(s, rest)
    assert cfg.buffer_pct == pytest.approx(15.0)  # (23/20 - 1) * 100


@respx.mock
async def test_fixed_buffer_ignores_p90(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(
            total_kwh=20.0, slots=None, source="ml quantile gbr", p90_total_kwh=23.0
        )

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t", charge_buffer_pct=20.0)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        _, cfg, _ = await gather_inputs(s, rest)
    assert cfg.buffer_pct == 20.0


@respx.mock
async def test_gather_inputs_reads_location_from_ha_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_load(_s: Settings, **kw: object) -> LoadForecast:
        seen.update(kw)
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/config").mock(
        return_value=httpx.Response(200, json={"latitude": 51.5, "longitude": -0.1})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        await gather_inputs(s, rest)
    assert seen == {"lat": 51.5, "lon": -0.1}


@respx.mock
async def test_explicit_coordinates_override_ha_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_load(_s: Settings, **kw: object) -> LoadForecast:
        seen.update(kw)
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t", latitude=55.9, longitude=-3.2)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        await gather_inputs(s, rest)
    assert seen == {"lat": 55.9, "lon": -3.2}


# --- dynamic-tariff price sensor reads (P8.4, #38) ---


def _dynamic_settings(**kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        ha_url="http://ha.test", ha_token="t",
        tariff_provider="dynamic", dynamic_rates_entity="event.rates_today",
    )
    base.update(kw)
    return Settings(**base)


@respx.mock
async def test_gather_inputs_parses_dynamic_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/event.rates_today").mock(
        return_value=_state(
            "event.rates_today",
            "2026-06-08T00:00:00+00:00",
            {
                "rates": [
                    {"start": "2026-06-08T00:00:00+00:00", "end": "2026-06-08T00:30:00+00:00",
                     "value_inc_vat": 0.12},
                    {"start": "2026-06-08T00:30:00+00:00", "end": "2026-06-08T01:00:00+00:00",
                     "value_inc_vat": 0.09},
                ]
            },
        )
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _dynamic_settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(s, rest)
    assert [p.price for p in inputs.dynamic_prices] == [0.12, 0.09]


@respx.mock
async def test_gather_inputs_merges_today_and_tomorrow_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/event.rates_today").mock(
        return_value=_state(
            "event.rates_today", "x",
            {"rates": [{"start": "2026-06-08T00:00:00+00:00", "end": "2026-06-08T00:30:00+00:00",
                        "value_inc_vat": 0.20}]},
        )
    )
    respx.get(f"{BASE}/states/event.rates_tomorrow").mock(
        return_value=_state(
            "event.rates_tomorrow", "x",
            {"rates": [{"start": "2026-06-09T00:00:00+00:00", "end": "2026-06-09T00:30:00+00:00",
                        "value_inc_vat": 0.05}]},
        )
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _dynamic_settings(dynamic_rates_entity_tomorrow="event.rates_tomorrow")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(s, rest)
    # Sorted by start: today's slot, then tomorrow's.
    assert [p.price for p in inputs.dynamic_prices] == [0.20, 0.05]


@respx.mock
async def test_gather_inputs_tolerates_malformed_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/event.rates_today").mock(
        return_value=_state(
            "event.rates_today", "x",
            {"rates": [
                "not-a-dict",
                {"start": "bad-timestamp", "end": "also-bad", "value_inc_vat": 0.1},
                {"start": "2026-06-08T00:00:00+00:00", "end": "2026-06-08T00:30:00+00:00"},
            ]},
        )
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _dynamic_settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(s, rest)
    assert inputs.dynamic_prices == ()


@respx.mock
async def test_gather_inputs_dynamic_unavailable_sensor_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _dynamic_settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _src = await gather_inputs(s, rest)
    assert inputs.dynamic_prices == ()
    # Never blocks a plan: the schedule falls back to fixed.
    assert build_schedule(s, inputs, cfg) == fixed_schedule(inputs, cfg)


async def test_gather_inputs_skips_dynamic_fetch_when_fixed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No dynamic_rates_entity fetch at all when tariff_provider isn't "dynamic"."""

    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    with respx.mock:
        respx.route(method="GET").mock(return_value=httpx.Response(404))
        s = Settings(
            ha_url="http://ha.test", ha_token="t", dynamic_rates_entity="event.rates_today"
        )
        async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
            inputs, _cfg, _src = await gather_inputs(s, rest)
        assert inputs.dynamic_prices == ()


# --- octopus_intelligent: dispatches + prices from the Octopus API (P8.5, #39) ---


def _octopus_settings(**kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        ha_url="http://ha.test", ha_token="t",
        tariff_provider="octopus_intelligent",
        octopus_api_url="http://octo.test/v1",
        octopus_api_key="sk_test",
        octopus_account_number="A-1234ABCD",
        octopus_product_code="INTELLI-VAR-22-10-14",
        octopus_tariff_code="E-1R-INTELLI-VAR-22-10-14-A",
    )
    base.update(kw)
    return Settings(**base)


@respx.mock
async def test_gather_inputs_octopus_intelligent_fetches_dispatches_and_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET", url__startswith="http://ha.test").mock(
        return_value=httpx.Response(404)
    )
    respx.post("http://octo.test/v1/graphql/").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": "jwt-abc"}}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "plannedDispatches": [
                            {"startDt": "2026-06-01T13:00:00Z", "endDt": "2026-06-01T13:30:00Z",
                             "delta": -2.0, "meta": {"source": "smart-charge"}}
                        ]
                    }
                },
            ),
        ]
    )
    respx.get(url__startswith="http://octo.test/v1/products/").mock(
        return_value=httpx.Response(
            200,
            json={
                "next": None,
                "results": [
                    {"valid_from": "2026-06-01T00:00:00Z", "valid_to": "2026-06-01T00:30:00Z",
                     "value_inc_vat": 0.12}
                ],
            },
        )
    )

    s = _octopus_settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(s, rest)
    assert len(inputs.dispatches) == 1
    assert inputs.dispatches[0].source == "smart-charge"
    assert [p.price for p in inputs.dynamic_prices] == [0.12]


@pytest.mark.parametrize(
    ("live_state", "expected_count"), [("off", 0), ("on", 1)],
    ids=["disconnected-without-live-dispatch", "live-dispatch-beats-disconnection"],
)
@respx.mock
async def test_octopus_dispatches_use_configured_ha_live_evidence(
    monkeypatch: pytest.MonkeyPatch, live_state: str, expected_count: int
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    now = datetime.now(UTC)
    start = now + timedelta(hours=1)
    end = start + timedelta(minutes=30)
    respx.post("http://octo.test/v1/graphql/").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": "jwt-test"}}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "plannedDispatches": [
                            {
                                "startDt": start.isoformat(),
                                "endDt": end.isoformat(),
                                "delta": -2.0,
                                "meta": {"source": "smart-charge"},
                            }
                        ]
                    }
                },
            ),
        ]
    )
    respx.get(url__startswith="http://octo.test/v1/products/").mock(
        return_value=httpx.Response(200, json={"next": None, "results": []})
    )
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state(
            "binary_sensor.dispatch", live_state, {"planned_dispatches": "malformed"}
        )
    )
    respx.get(f"{BASE}/states/sensor.ev_plug").mock(
        return_value=_state("sensor.ev_plug", "EV Disconnected")
    )
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", "Paused"))
    respx.get(f"{BASE}/states/sensor.rate").mock(
        return_value=_state("sensor.rate", "0.20", {"is_intelligent_adjusted": False})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    settings = _octopus_settings(
        dispatch_entity="binary_sensor.dispatch",
        ev_plug_entity="sensor.ev_plug",
        ev_status_entity="sensor.ev",
        octopus_rate_entity="sensor.rate",
    )
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(settings, rest)

    assert len(inputs.dispatches) == expected_count


@respx.mock
async def test_gather_inputs_octopus_intelligent_degrades_on_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET", url__startswith="http://ha.test").mock(
        return_value=httpx.Response(404)
    )
    respx.route(url__startswith="http://octo.test").mock(return_value=httpx.Response(401))

    s = _octopus_settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _src = await gather_inputs(s, rest)
    # Never crashes or blocks the plan: dispatches/prices degrade to empty,
    # and the schedule falls back to fixed.
    assert inputs.dispatches == ()
    assert inputs.dynamic_prices == ()
    assert build_schedule(s, inputs, cfg) == fixed_schedule(inputs, cfg)


async def test_gather_inputs_reads_configured_live_dispatch_for_octopus_intelligent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kraken supplies plans, while a configured HA entity can confirm live state."""

    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    with respx.mock:
        respx.route(method="GET", url__startswith="http://ha.test").mock(
            return_value=httpx.Response(404)
        )
        respx.route(url__startswith="http://octo.test").mock(return_value=httpx.Response(401))
        s = _octopus_settings(dispatch_entity="binary_sensor.dispatch")
        async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
            await gather_inputs(s, rest)
        assert any(
            str(c.request.url).endswith("/states/binary_sensor.dispatch")
            for c in respx.calls
        )


async def _gather_with_soc(
    monkeypatch: pytest.MonkeyPatch, soc_response: httpx.Response, **overrides: object
) -> Any:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{BASE}/states/sensor.soc").mock(return_value=soc_response)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = _settings().model_copy(update=overrides)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _, _ = await gather_inputs(s, rest)
    return inputs


@respx.mock
async def test_gather_inputs_rejects_stale_soc_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    inputs = await _gather_with_soc(
        monkeypatch, _state("sensor.soc", "30", last_reported=stale)
    )

    assert inputs.soc.status is SocStatus.STALE
    assert inputs.soc_now == 0.0  # the stale 30% must not reach the planner
    assert inputs.soc.value == 30.0  # ...but it is kept as evidence


@respx.mock
async def test_gather_inputs_honours_configured_max_report_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    inputs = await _gather_with_soc(
        monkeypatch,
        _state("sensor.soc", "30", last_reported=reported),
        soc_max_report_age_minutes=30.0,
    )

    assert inputs.soc.ok is True
    assert inputs.soc_now == 30.0


@respx.mock
async def test_gather_inputs_rejects_soc_without_last_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = await _gather_with_soc(
        monkeypatch, _state("sensor.soc", "30", last_reported=None)
    )

    assert inputs.soc.status is SocStatus.REPORT_TIME_UNUSABLE
    assert inputs.soc_now == 0.0


# --- #140/#143 §3: hold trust — `()` must stop meaning both "none" and "unreadable" ---


async def _gather_with_dispatch(
    monkeypatch: pytest.MonkeyPatch, dispatch: httpx.Response, **kw: Any
) -> Any:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    with respx.mock:
        respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(return_value=dispatch)
        respx.route(method="GET").mock(return_value=httpx.Response(404))
        s = _settings().model_copy(update=kw)
        async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
            inputs, _cfg, _src = await gather_inputs(s, rest)
    return inputs


async def test_a_readable_dispatch_entity_with_no_dispatches_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = await _gather_with_dispatch(
        monkeypatch,
        _state("binary_sensor.dispatch", "off", {"planned_dispatches": []}),
    )

    assert inputs.dispatches == ()
    assert inputs.dispatches_trusted is True


async def test_a_missing_dispatch_entity_is_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = await _gather_with_dispatch(monkeypatch, httpx.Response(404))

    assert inputs.dispatches == ()
    assert inputs.dispatches_trusted is False


@pytest.mark.parametrize("unreadable", ["unavailable", "unknown"])
async def test_an_unavailable_dispatch_entity_is_untrusted(
    monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    inputs = await _gather_with_dispatch(
        monkeypatch, _state("binary_sensor.dispatch", unreadable)
    )

    assert inputs.dispatches_trusted is False


async def test_no_dispatch_entity_configured_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A household with no dispatch source has no holds, not unreadable ones.

    Every tariff provider folds dispatches into holds, so treating the unset
    entity as untrusted would refuse every Axle export for ever.
    """
    inputs = await _gather_with_dispatch(monkeypatch, httpx.Response(404), dispatch_entity="")

    assert inputs.dispatches_trusted is True


@respx.mock
async def test_a_failed_octopus_dispatch_fetch_is_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET", url__startswith="http://ha.test").mock(
        return_value=httpx.Response(404)
    )
    respx.route(url__startswith="http://octo.test").mock(return_value=httpx.Response(401))

    s = _octopus_settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, _cfg, _src = await gather_inputs(s, rest)

    assert inputs.dispatches == ()
    assert inputs.dispatches_trusted is False


@respx.mock
async def test_read_dispatches_parses_a_readable_entity_as_trusted() -> None:
    """The one definition of trust `gather_inputs` and the per-minute reconcile share."""
    respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
        return_value=_state(
            "binary_sensor.dispatch",
            "on",
            {"planned_dispatches": [{"start": "2026-06-10T22:00:00+01:00",
                                     "end": "2026-06-10T23:00:00+01:00"}]},
        )
    )
    s = _settings()
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        dispatches, trusted = await sources.read_dispatches(s, rest)

    assert [d.start.hour for d in dispatches] == [22]
    assert trusted is True


@respx.mock
async def test_read_dispatches_makes_no_read_for_an_unset_entity() -> None:
    any_get = respx.route(method="GET").mock(return_value=httpx.Response(404))
    s = _settings().model_copy(update={"dispatch_entity": ""})
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        assert await sources.read_dispatches(s, rest) == ((), True)

    assert not any_get.called


@pytest.mark.parametrize(
    "state", ["Charging", "cHaRgInG", "Boosting", "bOoStInG", "Delivering", "DELIVERING"]
)
@respx.mock
async def test_read_ev_hold_status_recognizes_charging_states(state: str) -> None:
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", state))
    settings = _settings()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert await sources.read_ev_hold_status(settings, rest) is True


@pytest.mark.parametrize("state", ["Diverting", "Idle", "Paused", "off"])
@respx.mock
async def test_read_ev_hold_status_ignores_non_charging_states(state: str) -> None:
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=_state("sensor.ev", state))
    settings = _settings()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert await sources.read_ev_hold_status(settings, rest) is False


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), _state("sensor.ev", "unavailable"), _state("sensor.ev", "unknown")],
    ids=["missing", "unavailable", "unknown"],
)
@respx.mock
async def test_read_ev_hold_status_treats_unreadable_state_as_no_evidence(
    response: httpx.Response,
) -> None:
    respx.get(f"{BASE}/states/sensor.ev").mock(return_value=response)
    settings = _settings()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert await sources.read_ev_hold_status(settings, rest) is None


@respx.mock
async def test_read_ev_hold_status_makes_no_read_when_entity_is_unset() -> None:
    any_get = respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings().model_copy(update={"ev_status_entity": ""})
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert await sources.read_ev_hold_status(settings, rest) is None

    assert not any_get.called


@respx.mock
async def test_read_ev_hold_status_treats_a_connection_error_as_no_evidence() -> None:
    respx.get(f"{BASE}/states/sensor.ev").mock(
        side_effect=httpx.ConnectError("Home Assistant is unreachable")
    )
    settings = _settings()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert await sources.read_ev_hold_status(settings, rest) is None


def _frozen_clock(frozen: datetime) -> type[datetime]:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Clock:
            at = frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)
            return cls.fromtimestamp(at.timestamp(), at.tzinfo)

    return _Clock


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 4, 21, 0, tzinfo=UTC),  # the evening before, 22:00 BST
        datetime(2026, 10, 5, 16, 0, tzinfo=UTC),  # the event day, 17:00 BST
    ],
    ids=["evening-before", "event-day"],
)
@respx.mock
async def test_an_evening_axle_event_is_planned_on_its_own_day(
    monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    """From local midnight the horizon starts at tonight's window, after the event (#198)."""
    event = FlexibilityEvent(
        start=datetime(2026, 10, 5, 19, 0, tzinfo=UTC),  # 20:00-21:00 BST
        end=datetime(2026, 10, 5, 20, 0, tzinfo=UTC),
        direction="export",
        updated_at=now,
        rate_gbp_kwh=1.0,
    )

    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=12.0, slots=(0.25,) * 48, source="test")

    async def fake_event(_s: Settings, _rest: HomeAssistantRest) -> FlexibilityEvent:
        return event

    monkeypatch.setattr(sources, "datetime", _frozen_clock(now))
    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(sources, "read_axle_event", fake_event)
    respx.route(method="GET", url__startswith=BASE).mock(return_value=httpx.Response(404))
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        tariff_provider="axle",
        axle_api_url="http://axle.test",
        axle_api_key="k",
        latitude=51.5,
        longitude=-0.1,
    )
    soc = SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=90.0,
        raw_state="90",
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )

    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        inputs, cfg, _ = await gather_inputs(s, rest, soc=soc)
    plan = compute_plan(inputs, cfg, build_schedule(s, inputs, cfg))

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert (export.window_start, export.window_end) == (event.start, event.end)
