"""Tests for the half-hourly scheduled plan/apply loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.api.server import AppState
from ha_spark.authority import HoldDevice, HoldRecord
from ha_spark.config import DeviceConfig
from ha_spark.config import Settings as ConfigSettings
from ha_spark.devices import Capability, inverter_device
from ha_spark.devices.execution import execution_context
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.energy import scheduler, sources
from ha_spark.energy.evidence_freshness import EvidenceFreshness
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import (
    SLOTS_PER_DAY,
    ChargeIntent,
    ChargePlan,
    LoadForecast,
)
from ha_spark.energy.scheduler import (
    SIGNAL_SAMPLE_INTERVAL,
    charge_current_changed,
    guard_tick,
    run_once,
    sample_signals,
    setpoint_changed,
    should_run,
)
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.soc_monitor import SocMonitor
from ha_spark.ha.rest import HomeAssistantRest
from tests.authority_helpers import legacy_settings as Settings
from tests.authority_helpers import run_daemon as run_forever
from tests.authority_helpers import seed_authority


@respx.mock
@pytest.mark.parametrize("initial_soc", [45.0, 80.0])
@pytest.mark.parametrize("new_input", ["dispatch", "event"])
async def test_live_current_horizon_ticks_skip_writes_then_absorb_new_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_soc: float, new_input: str,
) -> None:
    """Real gather/planner/Solis sizing and read-back, seeded by a verified apply."""
    from ha_spark.devices.inverters import solis
    from ha_spark.energy import axle

    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 5, 18, 0, tzinfo=tz)
    profile = tuple(0.2 + i * 0.001 for i in range(48))
    dispatches: list[dict[str, object]] = []
    event: dict[str, str] = {}
    states = {
        "select.power": "On",
        "sensor.solis_control_work_mode_bitfield": "35",
        "sensor.solis_control_timed_charge_current": "0",
        "sensor.solis_control_timed_discharge_current": "0",
    }
    fields = (
        "timed_charge_start_hours", "timed_charge_start_minutes",
        "timed_charge_end_hours", "timed_charge_end_minutes",
        "timed_discharge_start_hours", "timed_discharge_start_minutes",
        "timed_discharge_end_hours", "timed_discharge_end_minutes",
    )
    for suffix in ("", "_2", "_3"):
        for field in fields:
            states[f"sensor.solis_control_{field}{suffix}"] = "0"

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return cls.fromtimestamp(now.timestamp(), tz or UTC)

    async def fake_load(_s: Settings, **kw: object) -> LoadForecast:
        return LoadForecast(
            total_kwh=sum(profile), slots=profile, source="slot profile (14d hourly house stats)",
            target_date=kw["target"],
        )

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    def get_state(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vpp/home-assistant/event":
            return httpx.Response(200, json=event)
        entity = request.url.path.rsplit("/", 1)[-1]
        if entity == "sensor.solis_control_inverter_clock":
            states[entity] = now.strftime("%y,%m,%d,%H,%M,%S")
        attrs = {"planned_dispatches": dispatches} if entity == "binary_sensor.dispatch" else {}
        state = "off" if entity == "binary_sensor.dispatch" else states.get(entity)
        if state is None:
            return httpx.Response(404)
        return httpx.Response(200, json={
            "entity_id": entity, "state": state, "attributes": attrs,
            "last_reported": now.isoformat(), "last_updated": now.isoformat(),
        })

    def write_register(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/services/modbus/write_register":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        address, value = payload["address"], payload["value"]
        if address == 43141:
            states["sensor.solis_control_timed_charge_current"] = str(value / 10)
        elif address == 43142:
            states["sensor.solis_control_timed_discharge_current"] = str(value / 10)
        elif address in (43143, 43153, 43163):
            suffix = {43143: "", 43153: "_2", 43163: "_3"}[address]
            for field, item in zip(fields, value, strict=True):
                states[f"sensor.solis_control_{field}{suffix}"] = str(item)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(sources, "datetime", FixedDatetime)
    monkeypatch.setattr(scheduler, "datetime", FixedDatetime)
    monkeypatch.setattr(solis, "datetime", FixedDatetime)
    monkeypatch.setattr(axle, "datetime", FixedDatetime)
    monkeypatch.setattr(solis, "_READ_BACK_DELAY_SECONDS", 0)
    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", noop)
    respx.route(method="GET").mock(side_effect=get_state)
    respx.route(method="POST").mock(side_effect=write_register)
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="optimise", timezone="Europe/London",
        db_path=str(tmp_path / "ledger.db"), v2l_power_entity="",
        inverter_power_switch_entity="select.power", dispatch_entity="binary_sensor.dispatch",
        latitude=0.0, longitude=0.0, charge_buffer_pct=0.0,
        export_event_provider="axle", axle_api_url="http://axle.test", axle_api_key="test-key",
    )

    def checked_soc(value: float) -> SocMeasurement:
        return SocMeasurement(
            status=SocStatus.OK, observed_at=now, value=value, reported_at=now,
            raw_state=str(value), age_s=0.0, max_age_s=600.0,
        )

    baseline = await run_once(settings, soc=checked_soc(initial_soc))
    assert baseline.applied_plan is baseline.plan
    if initial_soc == 45.0:
        assert baseline.plan.required_kwh > 0
    else:
        assert baseline.plan.required_kwh == 0
    target = baseline.plan.target_soc
    intent = baseline.plan.charge_intent
    assert intent is not None
    rate = _planned_w(settings, intent)  # real Solis current, not a sizing stub
    baseline_calls = len(respx.calls)
    observed_soc = initial_soc
    previous = baseline.applied_plan
    for slot in (36, 37):
        observed_soc -= profile[slot] / settings.battery_capacity_kwh * 100
        now += timedelta(minutes=30)
        result = await run_once(settings, soc=checked_soc(observed_soc), previous_plan=previous)
        assert result.plan.target_soc == pytest.approx(target)
        assert result.plan.charge_intent is not None
        assert _planned_w(settings, result.plan.charge_intent) == rate
        assert result.applied_plan is baseline.plan
        assert result.apply_lines == ()
        previous = result.applied_plan
    assert not [
        call for call in list(respx.calls)[baseline_calls:]
        if call.request.url.path.startswith("/api/services/")
    ]

    # New inputs are read on the next ordinary slot tick, without an extra trigger.
    if new_input == "dispatch":
        dispatches.append({
            "start": "2026-01-05T20:00:00+00:00", "end": "2026-01-05T20:30:00+00:00",
            "charge_in_kwh": -2.0, "source": "SMART",
        })
    else:
        event.update({
            "start_time": "2026-01-05T21:00:00+00:00",
            "end_time": "2026-01-05T21:30:00+00:00",
            "import_export": "export", "updated_at": now.isoformat(),
        })
    observed_soc -= profile[38] / settings.battery_capacity_kwh * 100
    now += timedelta(minutes=30)
    changed = await run_once(settings, soc=checked_soc(observed_soc), previous_plan=previous)
    assert changed.plan.charge_intent is not None
    if new_input == "dispatch":
        assert changed.plan.charge_intent.holds == ((
            datetime(2026, 1, 5, 20, 0, tzinfo=tz), datetime(2026, 1, 5, 20, 30, tzinfo=tz),
        ),)
    else:
        assert changed.plan.charge_intent.export is not None
        assert states["sensor.solis_control_timed_discharge_start_hours"] == "21"
        assert states["sensor.solis_control_timed_discharge_end_minutes"] == "30"
    assert changed.apply_lines  # a normal tick reached apply, with no extra trigger
    assert changed.applied_plan is changed.plan
    if new_input == "event":
        # Once armed, even an identical replan must still take apply (#148).
        repeated = await run_once(
            settings, soc=checked_soc(observed_soc), previous_plan=changed.applied_plan
        )
        assert repeated.plan.charge_intent == changed.plan.charge_intent
        assert repeated.apply_lines
        assert repeated.applied_plan is repeated.plan
    async with ForecastLedger(settings.db_path) as ledger:
        rows = await ledger.forecasts_since(date(2026, 1, 5))
    assert rows
    assert all(row.target_date == date(2026, 1, 6) and row.slots == profile for row in rows)


def _soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )


# A concrete intent so plan.charge_intent drives a real planned charge rate (W).
_INTENT = ChargeIntent(
    target_soc_pct=77.0, soc=_soc(30.0), window_start=time(23, 30), window_end=time(5, 30)
)


def _fake_solis_device(
    device_type: type[SolisDevice], settings: Settings, rest: HomeAssistantRest
) -> SolisDevice:
    """Build a subclass test double with the same valid constructor inputs."""
    config = DeviceConfig(id="test_solis", driver="solis")
    return device_type(config, settings, rest)


def _plan(intent: ChargeIntent = _INTENT) -> ChargePlan:
    return ChargePlan(
        soc=_soc(30), capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
        load_kwh=24.2, cheap_covered_kwh=0.0, usable_now_kwh=2.69,
        deficit_kwh=12.8, buffer_pct=0.0, required_kwh=12.8,
        target_soc=77, window_hours=6.0,
        ev_charging=False, ha_template_needed=None,
        charge_intent=intent,
    )


def _planned_w(settings: Settings, intent: ChargeIntent) -> float:
    """The watts the active charger plans for ``intent`` (pure)."""
    rest = HomeAssistantRest(settings.ha_rest_url, settings.auth_token)
    return inverter_device(settings, rest).planned_rate_w(intent)


def test_should_run_first_half_hour_slot_immediately() -> None:
    now = datetime(2026, 6, 10, 12, 17)
    assert should_run(now, None) is True


def test_should_run_false_until_the_next_half_hour_slot() -> None:
    now = datetime(2026, 6, 10, 12, 17)
    assert should_run(now, datetime(2026, 6, 10, 12, 0)) is False
    assert should_run(datetime(2026, 6, 10, 12, 30), datetime(2026, 6, 10, 12, 0)) is True


def test_should_run_runs_after_a_missed_slot_without_catching_up() -> None:
    now = datetime(2026, 6, 10, 13, 17)
    assert should_run(now, datetime(2026, 6, 10, 12, 0)) is True


def test_should_run_uses_local_slot_start() -> None:
    assert should_run(datetime(2026, 6, 11, 0, 0), datetime(2026, 6, 10, 23, 30)) is True


def test_setpoint_change_ignores_fresh_soc_observation() -> None:
    from ha_spark.energy.scheduler import setpoint_changed

    previous = _INTENT
    current = replace(_INTENT, soc=_soc(31.0))
    assert setpoint_changed(previous, current) is False


def test_setpoint_change_detects_target_window_and_hold_changes() -> None:
    from ha_spark.energy.scheduler import setpoint_changed

    assert setpoint_changed(_INTENT, replace(_INTENT, target_soc_pct=78.0)) is True
    assert setpoint_changed(_INTENT, replace(_INTENT, window_start=time(0, 0))) is True
    assert setpoint_changed(
        _INTENT,
        replace(_INTENT, holds=((datetime(2026, 6, 10, 12, 0), datetime(2026, 6, 10, 12, 30)),)),
    ) is True


def test_setpoint_change_retries_an_untrusted_axle_event_read() -> None:
    assert setpoint_changed(_INTENT, replace(_INTENT, export_trusted=False)) is True


@pytest.mark.parametrize(
    ("applied_a", "planned_a", "changed"),
    [
        (38, 38, False),
        (38, 39, True),  # any 1 A rise re-applies
        (38, 38.4, False),  # rounds to the same register value
        (38, 38.6, True),
        (38, 34, False),  # a 4 A fall is inside the deadband
        (38, 33, True),  # a 5 A fall re-applies
        (38, 33.4, True),  # rounds to 33
    ],
)
def test_charge_current_deadband_is_asymmetric(
    applied_a: float, planned_a: float, changed: bool
) -> None:
    # #173: rise >= 1 A or fall >= 5 A (whole register amps) re-applies.
    assert charge_current_changed(applied_a, planned_a) is changed


class _AmpsDevice:
    """Fake device whose planned rate is a fixed amps-per-SoC table x voltage."""

    def __init__(self, amps_by_soc: dict[float, float], voltage_v: float) -> None:
        self.amps_by_soc = amps_by_soc
        self.voltage_v = voltage_v
        self.applied: list[ChargeIntent] = []
        # Lines each apply returns, in order; then "[APPLIED] test".
        self.results: list[list[str]] = []

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return self.amps_by_soc[intent.soc_now] * self.voltage_v

    async def apply(self, intent: ChargeIntent) -> list[str]:
        self.applied.append(intent)
        return self.results.pop(0) if self.results else ["[APPLIED] test"]

    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]:
        return []


def _plan_at(soc: float) -> ChargePlan:
    intent = replace(_INTENT, soc=_soc(soc))
    return replace(_plan(), soc=intent.soc, charge_intent=intent)


def _patch_run_once_io(
    monkeypatch: pytest.MonkeyPatch,
    current: ChargePlan | list[ChargePlan | Exception],
    device: object,
) -> None:
    """Stub ``run_once``'s I/O. A list is computed in order; an exception there raises."""

    async def fake_current_plan(_s: Settings, _rest: object, **_kw: object) -> object:
        plan = current.pop(0) if isinstance(current, list) else current
        if isinstance(plan, Exception):
            raise plan
        return SimpleNamespace(
            plan=plan, inputs=SimpleNamespace(ev_hold_charging=None), load_source="test",
            tz=None,
            forecast=LoadForecast(total_kwh=24.0, slots=None, source="test"),
        )

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(scheduler, "current_plan", fake_current_plan)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_args: device)
    monkeypatch.setattr(scheduler, "_record_forecast", noop)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", noop)


@pytest.mark.parametrize(
    ("current_soc", "planned_a", "applies"),
    [
        (34.0, 35.0, False),  # 2026-10-04 00:30: 38 -> 35 A stays inside the deadband
        (40.0, 33.0, True),  # a 5 A fall re-applies
        (29.0, 39.0, True),  # a 1 A rise re-applies
    ],
)
async def test_run_once_reapplies_an_unchanged_target_when_the_amps_move(
    monkeypatch: pytest.MonkeyPatch, current_soc: float, planned_a: float, applies: bool
) -> None:
    settings = Settings()
    device = _AmpsDevice({31.0: 38.0, current_soc: planned_a}, settings.battery_voltage_v)
    previous, current = _plan_at(31.0), _plan_at(current_soc)
    _patch_run_once_io(monkeypatch, current, device)

    await run_once(settings, soc=current.soc, previous_plan=previous)

    assert device.applied == ([current.charge_intent] if applies else [])


async def test_run_once_ignores_amps_sized_from_an_untrusted_soc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untrusted SoC sizes from 0% (a huge rise); it must not force a write."""
    settings = Settings()
    failed = _failed_soc()
    current = replace(_plan(), soc=failed, charge_intent=replace(_INTENT, soc=failed))
    device = _AmpsDevice({31.0: 38.0, 0.0: 62.5}, settings.battery_voltage_v)
    _patch_run_once_io(monkeypatch, current, device)

    await run_once(settings, soc=failed, previous_plan=_plan_at(31.0))

    assert device.applied == []


async def test_run_once_keeps_the_fallback_program_when_monitor_says_active(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings()
    current = _plan_at(31.0)
    device = _AmpsDevice({31.0: 38.0}, settings.battery_voltage_v)
    _patch_run_once_io(monkeypatch, current, device)

    caplog.set_level("INFO")
    result = await run_once(
        settings, soc=current.soc, previous_plan=_plan_at(30.0), fallback_active=True
    )

    assert device.applied == []
    assert result.applied_plan is None
    assert "normal programming held while Solis fallback is active" in caplog.text


@pytest.mark.parametrize(
    ("lines", "resident"),
    [
        (["[APPLIED] set charge window"], True),
        (["[SIMULATE] [BLOCKED] set charge window: SoC unavailable"], True),
        (["[APPLIED] deactivate slot 1", "[FAILED] set charge window"], False),
        (["[BLOCKED] set charge window: SoC unavailable"], False),
    ],
)
async def test_run_once_reports_whether_the_apply_left_the_plan_resident(
    monkeypatch: pytest.MonkeyPatch, lines: list[str], resident: bool
) -> None:
    """A failed or blocked apply leaves the device unknown: no baseline (#168)."""
    settings = Settings()
    device = _AmpsDevice({31.0: 38.0}, settings.battery_voltage_v)
    device.results = [lines]
    current = _plan_at(31.0)
    _patch_run_once_io(monkeypatch, current, device)

    result = await run_once(settings, soc=current.soc, previous_plan=None)

    assert result.plan is current
    assert result.applied_plan is (current if resident else None)


def _run_forever_through(
    monkeypatch: pytest.MonkeyPatch,
    plans: list[ChargePlan | Exception],
    device: _AmpsDevice,
    ticks: list[datetime],
) -> type[Exception]:
    """Drive the real ``run_once`` inside ``run_forever``, one tick per entry in ``ticks``."""

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    _patch_run_once_io(monkeypatch, plans, device)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    # The loop and the real `run_once` each read the clock once per replan.
    return _patch_loop(monkeypatch, [t for tick in ticks for t in (tick, tick)])


async def test_run_forever_compares_against_the_last_applied_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipped plans must not become the baseline, or 4 A falls never add up (#173)."""
    plans = [_plan_at(31.0), _plan_at(34.0), _plan_at(40.0), _plan_at(42.0)]
    device = _AmpsDevice(
        {31.0: 38.0, 34.0: 35.0, 40.0: 33.0, 42.0: 31.0}, Settings().battery_voltage_v
    )
    stop = _run_forever_through(
        monkeypatch,
        list(plans),
        device,
        [
            datetime(2026, 6, 10, 23, 0),  # 38 A applied
            datetime(2026, 6, 10, 23, 30),  # 35 A: skipped
            datetime(2026, 6, 11, 0, 0),  # 33 A: 5 A below the applied 38 A
            datetime(2026, 6, 11, 0, 30),  # 31 A: compared with the applied 33 A
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="23:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert device.applied == [plans[0].charge_intent, plans[2].charge_intent]


async def test_run_forever_retries_an_unchanged_plan_after_a_failed_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-03: a falsely failed apply left Slot 1 zeroed and the next slot skipped (#168)."""
    plan = _plan_at(31.0)
    device = _AmpsDevice({31.0: 12.0}, Settings().battery_voltage_v)
    device.results = [["[APPLIED] deactivate slot 1", "[FAILED] set charge window"]]
    stop = _run_forever_through(
        monkeypatch,
        [plan, plan, plan],
        device,
        [
            datetime(2026, 10, 3, 12, 30),  # apply fails part-way
            datetime(2026, 10, 3, 13, 0),  # same plan: retried, lands
            datetime(2026, 10, 3, 13, 30),  # same plan, now resident: skipped
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert device.applied == [plan.charge_intent, plan.charge_intent]


async def test_run_forever_reapplies_after_a_replan_that_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replan that raises may have stopped mid-write: forget the baseline (#168)."""
    plan = _plan_at(31.0)
    device = _AmpsDevice({31.0: 12.0}, Settings().battery_voltage_v)
    stop = _run_forever_through(
        monkeypatch,
        [plan, RuntimeError("boom"), plan],
        device,
        [
            datetime(2026, 10, 3, 12, 30),  # applied
            datetime(2026, 10, 3, 13, 0),  # replan raises
            datetime(2026, 10, 3, 13, 1),  # same plan: re-applied, not skipped
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert device.applied == [plan.charge_intent, plan.charge_intent]


@respx.mock
async def test_run_once_computes_and_applies_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"),
    )

    with caplog.at_level("INFO"):
        plan = (await run_once(s)).plan

    assert plan.charge_intent.target_soc_pct >= 0
    assert any("Charge plan" in r.message for r in caplog.records)
    assert any("SIMULATE" in r.message for r in caplog.records)

    tomorrow = (datetime.now(load_timezone(s.timezone)) + timedelta(days=1)).date()
    async with ForecastLedger(s.db_path) as ledger:
        rows = await ledger.forecasts_since(tomorrow)
    assert len(rows) == 1
    assert rows[0].target_date == tomorrow
    assert rows[0].model == "baseline"
    assert rows[0].total_kwh == plan.load_kwh
    assert rows[0].source == "test"


@respx.mock
async def test_run_once_records_the_served_day_forecast_in_day_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 01:00 replan inside Sunday night's running window ledgers *Monday's*
    forecast (#200/#397): the served day, in slot-of-day order — never the
    horizon-order ``load_slots`` the plan itself is indexed by."""
    frozen = datetime(2026, 1, 12, 1, 0, tzinfo=ZoneInfo("Europe/London"))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FixedDatetime:
            return cls.fromtimestamp(frozen.timestamp(), tz or UTC)

    observed: list[date | None] = []

    async def fake_load(
        _s: Settings, *, target: date | None = None, **_kw: object
    ) -> LoadForecast:
        observed.append(target)
        return LoadForecast(
            total_kwh=12.0,
            slots=tuple(float(i) for i in range(SLOTS_PER_DAY)),
            source="slot profile (14d hourly house stats)",
            target_date=target,
        )

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(sources, "datetime", FixedDatetime)
    monkeypatch.setattr(scheduler, "datetime", FixedDatetime)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"),
        charge_window_start="23:30",
        charge_window_end="05:30",
        timezone="Europe/London",
        load_model="median",
    )
    plan = (await run_once(s)).plan

    assert observed == [date(2026, 1, 12)]  # Monday, not Tuesday (#200)
    async with ForecastLedger(s.db_path) as ledger:
        rows = await ledger.forecasts_since(date(2026, 1, 11))
    assert len(rows) == 1
    row = rows[0]
    assert row.target_date == date(2026, 1, 12)
    assert row.model == "slots"
    assert row.total_kwh == plan.load_kwh
    assert row.source == "slot profile (14d hourly house stats)"
    # Slot-of-day order (0.0 at 00:00), not the current-half-hour horizon order.
    assert row.slots == tuple(float(i) for i in range(SLOTS_PER_DAY))


@respx.mock
async def test_run_once_records_paired_forecast_horizon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_spark.energy import forecast, orchestrator

    async def stats(*args: object, **kwargs: object) -> list[dict[str, object]]:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            {"start": (midnight - timedelta(hours=h)).timestamp() * 1000, "change": 1.0}
            for h in range(1, 24 * 15 + 1)
        ]

    monkeypatch.setattr(forecast, "statistics_during_period", stats)
    monkeypatch.setattr(orchestrator, "statistics_during_period", stats)
    respx.route(method="POST", path__startswith="/api/states/").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        db_path=str(tmp_path / "paired.db"), load_model="median",
    )
    await run_once(settings)
    async with ForecastLedger(settings.db_path) as ledger:
        rows = await ledger.forecast_history(date(2000, 1, 1))
    assert len(rows) == 1
    row = rows[0]
    assert row.drove_plan
    assert row.horizon_start is not None
    assert row.horizon_end is not None
    assert row.horizon_end > row.horizon_start
    assert row.median_total_kwh == row.total_kwh == 24.0
    assert row.median_slots == row.slots


async def test_run_once_skips_unchanged_command_after_fresh_soc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[ChargeIntent] = []
    reconciled: list[ChargeIntent] = []
    previous = _plan()
    current_intent = replace(previous.charge_intent, soc=_soc(31.0))
    current = replace(previous, soc=current_intent.soc, charge_intent=current_intent)

    async def fake_current_plan(_s: Settings, _rest: object, **_kw: object) -> object:
        return SimpleNamespace(
            plan=current, inputs=SimpleNamespace(ev_hold_charging=None), load_source="test",
            tz=None,
            forecast=LoadForecast(total_kwh=24.0, slots=None, source="test"),
        )

    class FakeDevice:
        async def apply(self, intent: ChargeIntent) -> list[str]:
            applied.append(intent)
            return ["[APPLIED] test"]

        async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]:
            reconciled.append(intent)
            return ["[SKIP] set inverter power switch to On (already set)"]

        def planned_rate_w(self, intent: ChargeIntent) -> float:
            return 1000.0  # same command either side of the fresh SoC

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(scheduler, "current_plan", fake_current_plan)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_args: FakeDevice())
    monkeypatch.setattr(scheduler, "_record_forecast", noop)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", noop)

    result = await run_once(Settings(), soc=current.soc, previous_plan=previous)

    assert result.plan is current
    assert result.applied_plan is previous
    assert applied == []
    # A skipped setpoint must still serve the clock (#143): the reconcile is a
    # separate seam, so a slot with no plan change still converges the switch.
    assert reconciled == [current_intent]


async def test_run_once_retries_after_untrusted_previous_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[ChargeIntent] = []
    reconciled: list[ChargeIntent] = []
    failed = _failed_soc()
    previous = replace(_plan(), soc=failed, charge_intent=replace(_INTENT, soc=failed))
    current = _plan()

    async def fake_current_plan(_s: Settings, _rest: object, **_kw: object) -> object:
        return SimpleNamespace(
            plan=current, inputs=SimpleNamespace(ev_hold_charging=None), load_source="test",
            tz=None,
            forecast=LoadForecast(total_kwh=24.0, slots=None, source="test"),
        )

    class FakeDevice:
        async def apply(self, intent: ChargeIntent) -> list[str]:
            applied.append(intent)
            return ["[APPLIED] test"]

        async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]:
            reconciled.append(intent)
            return ["[SKIP] set inverter power switch to On (already set)"]

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(scheduler, "current_plan", fake_current_plan)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_args: FakeDevice())
    monkeypatch.setattr(scheduler, "_record_forecast", noop)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", noop)

    await run_once(Settings(), soc=current.soc, previous_plan=previous)

    assert applied == [current.charge_intent]
    assert reconciled == [current.charge_intent]


@respx.mock
async def test_dynamic_plan_parity_between_scheduler_and_agent_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#91: under ``tariff_provider="dynamic"`` the daily scheduler and the agent
    ``get_plan`` surface must cost against the *same* live prices. Before the fix
    ``get_plan`` silently used the fixed fallback, so its cost differed from the
    plan the daemon applied.

    The issue frames this as identical ``slot_prices``, but the agent payload
    never exposes ``slot_prices`` (see ``plan_to_payload``); ``baseline_cost``
    sums each slot at its live import price, so it is the faithful price-derived
    observable that stands in for the raw prices here."""
    from ha_spark.agent import tools

    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        # 48 slot-of-day kWh -> triggers the v2 slot horizon (and slot_prices).
        return LoadForecast(total_kwh=24.0, slots=tuple(0.5 for _ in range(48)), source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)

    # Pin the clock so both plan computations share the exact same ``now``.
    # A few minutes before a half-hour boundary makes a straddle regression
    # obvious: the two paths would then build horizons of different lengths.
    now = datetime(2026, 6, 10, 12, 25, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return cls.fromtimestamp(now.timestamp(), tz)

    monkeypatch.setattr(sources, "datetime", _FixedDatetime)
    monkeypatch.setattr(scheduler, "datetime", _FixedDatetime)
    monkeypatch.setattr("ha_spark.energy.soc_monitor.datetime", _FixedDatetime)

    # One wide live-rate point spanning the whole horizon at a distinctive price,
    # so every slot is priced 0.99 regardless of today's date — nothing like the
    # fixed 0.069/0.30, so a fixed-fallback bug would show up in the cost.
    rates = [{
        "start": (now - timedelta(days=1)).isoformat(),
        "end": (now + timedelta(days=3)).isoformat(),
        "value_inc_vat": 0.99,
    }]
    respx.get("http://ha.test/api/states/sensor.dynamic_rates").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.dynamic_rates", "state": "0.99",
                       "attributes": {"rates": rates}},
        )
    )
    # A trusted SoC: untrusted readings publish baseline_cost as unavailable (#174).
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=_soc_resp("50", now)
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        tariff_provider="dynamic", dynamic_rates_entity="sensor.dynamic_rates",
    )

    plan = (await run_once(s)).plan
    # The dynamic schedule actually flowed through the daemon's plan.
    assert plan.model == "slots"
    # The live horizon runs from the current half-hour, so its length follows the clock.
    assert plan.slot_prices and set(plan.slot_prices) == {0.99}

    # baseline_cost tallies each slot at its live import price, so it reflects the
    # 0.99 dynamic prices (unlike planned_cost, which costs at the representative
    # cheap/standard rates). Under the old bug get_plan would report the fixed
    # fallback's baseline here, diverging from the daemon's.
    result = await tools.get_plan(s)
    baseline_cost = next(
        e["state"] for e in result.plan if e["entity_id"] == "sensor.ha_spark_baseline_cost"
    )
    assert baseline_cost == f"{plan.baseline_cost:.2f}"


async def test_run_forever_runs_once_per_day_and_retries_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        calls.append("run")
        if len(calls) == 1:
            raise RuntimeError("boom")
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    class _StopLoop(Exception):
        pass

    # Tick sequence: fail at 22:00, retry succeeds in the same slot, then run
    # again at 22:30 and once more after midnight.
    ticks = iter(
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 30),
            datetime(2026, 6, 11, 0, 0),
        ]
    )

    class _FakeDatetime:
        @staticmethod
        def now(_tz: object) -> datetime:
            try:
                return next(ticks)
            except StopIteration as exc:
                raise _StopLoop from exc

    original_sleep = asyncio.sleep

    async def fake_sleep(_seconds: float) -> None:
        await original_sleep(0)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    async def noop_v2l_tick(_s: Settings, _now: datetime) -> None:
        return None

    def fake_make_server(_app: object, _host: str, _port: int) -> object:
        return object()  # never bound; serve_in_background is also stubbed

    async def fake_serve_in_background(_server: object) -> asyncio.Task[None]:
        return asyncio.ensure_future(asyncio.sleep(0))  # dummy completed task

    async def fake_stop_server(_server: object, task: asyncio.Task[None]) -> None:
        await task

    monkeypatch.setattr(scheduler, "datetime", _FakeDatetime)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(scheduler, "make_server", fake_make_server)
    monkeypatch.setattr(scheduler, "serve_in_background", fake_serve_in_background)
    monkeypatch.setattr(scheduler, "stop_server", fake_stop_server)
    monkeypatch.setattr(scheduler, "run_v2l_tick", noop_v2l_tick)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00", v2l_power_entity=""
    )
    with pytest.raises(_StopLoop):
        await run_forever(s, poll_seconds=0)

    # First tick fails (retry), second tick succeeds, then each new slot runs.
    assert calls == ["run", "run", "run", "run"]


def _patch_loop(
    monkeypatch: pytest.MonkeyPatch, ticks: list[datetime]
) -> type[Exception]:
    """Drive run_forever through ``ticks``, then raise to stop the loop."""
    it = iter(ticks)

    class _StopLoop(Exception):
        pass

    class _FakeDatetime:
        @staticmethod
        def now(_tz: object) -> datetime:
            try:
                return next(it)
            except StopIteration as exc:
                raise _StopLoop from exc

    original_sleep = asyncio.sleep

    async def fake_sleep(_seconds: float) -> None:
        await original_sleep(0)

    async def noop_v2l_tick(_s: Settings, _now: datetime) -> None:
        return None

    def fake_make_server(_app: object, _host: str, _port: int) -> object:
        return object()  # never bound; serve_in_background is also stubbed

    async def fake_serve_in_background(_server: object) -> asyncio.Task[None]:
        return asyncio.ensure_future(asyncio.sleep(0))  # dummy completed task

    async def fake_stop_server(_server: object, task: asyncio.Task[None]) -> None:
        await task

    monkeypatch.setattr(scheduler, "datetime", _FakeDatetime)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler, "make_server", fake_make_server)
    monkeypatch.setattr(scheduler, "serve_in_background", fake_serve_in_background)
    monkeypatch.setattr(scheduler, "stop_server", fake_stop_server)
    monkeypatch.setattr(scheduler, "run_v2l_tick", noop_v2l_tick)
    return _StopLoop


async def test_run_forever_records_control_loop_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[AppState] = []

    def capture_build_app(state: AppState) -> object:
        assert state.loop_tick is None
        captured.append(state)
        return object()

    async def fake_run_once(_s: Settings, **_kwargs: object) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])
    monkeypatch.setattr(scheduler, "build_app", capture_build_app)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    before = datetime.now(UTC)
    with pytest.raises(stop):
        await run_forever(Settings(ha_url="http://ha.test", ha_token="t"), poll_seconds=0)
    tick = captured[0].loop_tick
    assert tick is not None
    assert tick.tzinfo == UTC
    assert before <= tick <= datetime.now(UTC)


async def test_run_forever_publishes_plan_to_api_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each computed plan is pushed into the AppState the HTTP API serves."""
    captured: dict[str, object] = {}

    def capture_build_app(state: object) -> object:
        captured["state"] = state
        return object()  # never actually served; make_server is stubbed too

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])
    monkeypatch.setattr(scheduler, "build_app", capture_build_app)  # capture the AppState

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)
    state = captured["state"]
    assert isinstance(state, AppState)
    assert state.plan is not None


async def test_run_forever_guard_ticks_only_inside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_targets: list[float | None] = []

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fake_guard_tick(
        _s: Settings, target_w: float | None, *,
        soc: SocMeasurement | None = None,
        fallback_active: bool = False,
        fallback_confirmed: bool = False,
    ) -> float:
        guard_targets.append(target_w)
        assert target_w is not None
        return target_w

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fake_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),  # plan runs; outside window -> no guard
            datetime(2026, 6, 10, 23, 45),  # inside window -> guard ticks
            datetime(2026, 6, 11, 4, 0),  # still inside (wraps midnight) -> guard ticks
            datetime(2026, 6, 11, 12, 0),  # outside window -> no guard
        ],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)
    # Restore target is the plan's planned charge rate in watts, not amps.
    planned = _planned_w(s, _INTENT)
    assert guard_targets == [planned, planned]


async def test_run_forever_no_guard_when_entity_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fail_guard_tick(
        _s: Settings, target_w: float | None, *,
        soc: SocMeasurement | None = None,
        fallback_active: bool = False,
        fallback_confirmed: bool = False,
    ) -> float:
        raise AssertionError("guard must not run when grid_power_entity is empty")

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fail_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 23, 45)])

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)


async def test_run_forever_no_guard_when_charger_has_no_live_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AlphaESS has no settable rate -> the guard branch never fires, even with
    grid_power_entity set."""

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fail_guard_tick(
        _s: Settings, target_w: float | None, *,
        soc: SocMeasurement | None = None,
        fallback_active: bool = False,
        fallback_confirmed: bool = False,
    ) -> float:
        raise AssertionError("guard must not run for an inverter without a live rate")

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fail_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 23, 45)])

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        inverter="alphaess", grid_power_entity="sensor.house_supply_power",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)


async def test_run_forever_guard_failure_does_not_kill_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[datetime] = []

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def boom_guard_tick(
        _s: Settings, target_w: float | None, *,
        soc: SocMeasurement | None = None,
        fallback_active: bool = False,
        fallback_confirmed: bool = False,
    ) -> float:
        attempts.append(datetime.now())
        raise RuntimeError("HA unreachable")

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "guard_tick", boom_guard_tick)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [datetime(2026, 6, 11, 0, 0), datetime(2026, 6, 11, 0, 1)],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
    )
    with caplog.at_level("ERROR"), pytest.raises(stop):
        await run_forever(s, poll_seconds=0)
    assert len(attempts) == 2  # the failure did not stop the next tick
    assert any("Supply guard tick failed" in r.message for r in caplog.records)


@respx.mock
async def test_guard_tick_adopts_setpoint_as_target_on_restart() -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        grid_power_entity="sensor.house_supply_power", battery_voltage_v=51.0,
    )
    for entity, state in (
        (s.grid_power_entity, "2000"),
        ("sensor.solis_control_timed_charge_current", "30"),
    ):
        respx.get(f"http://ha.test/api/states/{entity}").mock(
            return_value=httpx.Response(
                200, json={"entity_id": entity, "state": state, "attributes": {}}
            )
        )
    # Mid-window restart: no plan target yet -> adopt the live setpoint in watts
    # (30 A * 51 V = 1530 W) via the charger's read_charge_rate.
    assert await guard_tick(s, None) == 30.0 * 51.0


@respx.mock
async def test_guard_tick_uses_configured_fallback_as_restore_target() -> None:
    s = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        authority_mode="observe",
        grid_power_entity="sensor.house_supply_power",
        battery_voltage_v=51.0,
        solis_fallback_current_a=15.0,
    )
    for entity, state in (
        (s.grid_power_entity, "3000"),
        ("sensor.solis_control_timed_charge_current", "5"),
    ):
        respx.get(f"http://ha.test/api/states/{entity}").mock(
            return_value=httpx.Response(
                200, json={"entity_id": entity, "state": state, "attributes": {}}
            )
        )

    target = await guard_tick(
        s,
        100.0,
        fallback_active=True,
        fallback_confirmed=True,
    )

    assert target == 15.0 * s.battery_voltage_v


@respx.mock
async def test_sample_signals_records_occupancy_heatpump_and_temperature(
    tmp_path: Path,
) -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        person_entities="person.alice, person.bob",
        heatpump_energy_entity="sensor.heatpump_energy",
        outdoor_weather_entity="weather.home",
    )
    respx.get("http://ha.test/api/states/person.alice").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "person.alice", "state": "home", "attributes": {}}
        )
    )
    respx.get("http://ha.test/api/states/person.bob").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "person.bob", "state": "not_home", "attributes": {}}
        )
    )
    respx.get("http://ha.test/api/states/sensor.heatpump_energy").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.heatpump_energy", "state": "1.5", "attributes": {}}
        )
    )
    respx.get("http://ha.test/api/states/weather.home").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "weather.home",
                "state": "cloudy",
                "attributes": {"temperature": 12.5},
            },
        )
    )

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await sample_signals(s, now)

    async with ForecastLedger(s.db_path) as ledger:
        since = datetime(2026, 1, 1, tzinfo=UTC)
        assert await ledger.signal_history("occupancy_home_frac", since) == [(now, 0.5)]
        assert await ledger.signal_history("heatpump_kwh", since) == [(now, 1.5)]
        assert await ledger.signal_history("temp_out_c", since) == [(now, 12.5)]


@respx.mock
async def test_sample_signals_skips_disabled_signals(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        person_entities="",
        heatpump_energy_entity="",
        outdoor_weather_entity="",
    )
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await sample_signals(s, now)

    async with ForecastLedger(s.db_path) as ledger:
        since = datetime(2026, 1, 1, tzinfo=UTC)
        assert await ledger.signal_history("occupancy_home_frac", since) == []
        assert await ledger.signal_history("heatpump_kwh", since) == []
        assert await ledger.signal_history("temp_out_c", since) == []


@respx.mock
async def test_sample_signals_tolerates_unreadable_entity(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        person_entities="person.alice",
        heatpump_energy_entity="",
        outdoor_weather_entity="",
    )
    respx.get("http://ha.test/api/states/person.alice").mock(return_value=httpx.Response(500))

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await sample_signals(s, now)  # must not raise

    async with ForecastLedger(s.db_path) as ledger:
        since = datetime(2026, 1, 1, tzinfo=UTC)
        assert await ledger.signal_history("occupancy_home_frac", since) == [(now, 0.0)]


async def test_run_forever_samples_signals_every_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled: list[datetime] = []

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fake_sample_signals(_s: Settings, now: datetime) -> None:
        sampled.append(now)

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", fake_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 0) + SIGNAL_SAMPLE_INTERVAL - timedelta(minutes=1),
            datetime(2026, 6, 10, 22, 0) + SIGNAL_SAMPLE_INTERVAL,
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "ledger.db"))
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    # First tick samples immediately; the next sample is skipped until the
    # interval elapses, then samples again on the third tick.
    assert sampled == [
        datetime(2026, 6, 10, 22, 0),
        datetime(2026, 6, 10, 22, 0) + SIGNAL_SAMPLE_INTERVAL,
    ]


@respx.mock
async def test_agent_predictions_logs_proactive_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"), timezone="UTC",
    )
    # Seed low occupancy so a suggestion fires, and no away context.
    async with ForecastLedger(s.db_path) as ledger:
        for d in range(14, 0, -1):
            day = datetime.now(UTC) - timedelta(days=d)
            await ledger.record_signal(
                day.replace(hour=12, minute=0, second=0, microsecond=0),
                "occupancy_home_frac", 0.05,
            )

    from ha_spark.agent.tools import get_predictions

    with caplog.at_level("INFO"):
        result = await get_predictions(s)

    assert result.decisions[0]["action"] == "suggest_away_context"
    assert any("Proactive decision" in r.message for r in caplog.records)


@respx.mock
async def test_run_once_triggers_derived_rerive_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scheduled plan run also re-derives trailing base-load history when configured."""
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    rerive_calls: list[object] = []

    async def fake_rerive(
        settings: Settings,
        specs: dict[str, object],
        *,
        statistic_id: str,
        statistic_name: str,
        window_hours: int = 48,
    ) -> object:
        rerive_calls.append(specs)
        from datetime import UTC, datetime

        from ha_spark.energy.derived_base_load import DerivedBackfillResult
        return DerivedBackfillResult(
            rows_imported=10,
            span="2026-06-11 00:00 .. 2026-06-11 09:00 UTC",
            degradation=[],
            negative_clamped=0,
            coverage={"grid_import": (datetime(2026, 6, 10, tzinfo=UTC),
                                      datetime(2026, 6, 11, tzinfo=UTC))},
        )

    monkeypatch.setattr(scheduler, "rerive_trailing_window", fake_rerive)

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"),
        derive_grid_import_entity="sensor.grid_import",
    )
    with caplog.at_level("INFO"):
        await run_once(s)
    assert len(rerive_calls) == 1
    assert "10 rows upserted" in caplog.text


@respx.mock
async def test_run_once_skips_derived_rerive_without_grid_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No grid import configured -> rerive is a no-op (source-entity path still works)."""
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    rerive_calls: list[object] = []

    async def fake_rerive(*args: object, **kwargs: object) -> object:
        rerive_calls.append(args)
        raise AssertionError("should not be called without grid import")

    monkeypatch.setattr(scheduler, "rerive_trailing_window", fake_rerive)

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"),
    )
    await run_once(s)
    assert rerive_calls == []


@respx.mock
async def test_run_once_rerive_failure_does_not_block_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rerive failure is logged but never aborts the plan run."""
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    async def boom_rerive(*args: object, **kwargs: object) -> object:
        raise RuntimeError("ws down")

    monkeypatch.setattr(scheduler, "rerive_trailing_window", boom_rerive)

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"),
        derive_grid_import_entity="sensor.grid_import",
    )
    with caplog.at_level("INFO"):
        await run_once(s)
    assert any("Derived base-load rerive failed" in r.message for r in caplog.records)
    # The plan still ran successfully.
    assert any("Charge plan" in r.message for r in caplog.records)


def test_derive_specs_for_scheduler_uses_shared_helper() -> None:
    """The scheduler reads the shared derive_specs_from_settings helper.

    A duplicate helper here would let the CLI and scheduler drift apart
    (different defaults for the same Settings options); the test pins
    that both code paths read from the same module-level mapper.
    """
    from ha_spark.energy import scheduler
    from ha_spark.energy.derived_base_load import derive_specs_from_settings

    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        derive_grid_import_entity="sensor.gi",
        derive_solar_generation_entity="sensor.sol",
        derive_invert_grid_export=True,
    )
    # The scheduler module exposes the same helper, not its own copy.
    assert scheduler.derive_specs_from_settings is derive_specs_from_settings
    specs = derive_specs_from_settings(s)
    assert specs["grid_import"].entity_id == "sensor.gi"
    assert specs["solar_generation"].entity_id == "sensor.sol"
    # Grid export is not configured (no entity) but the invert flag is set;
    # the helper must not include it when no entity id is present.
    assert "grid_export" not in specs


# --- SoC integrity monitoring in the one-minute loop (#114) ---


def _soc_resp(state: str, reported: datetime) -> httpx.Response:
    """One HA SoC-entity response; ``reported`` drives the freshness check."""
    return httpx.Response(
        200,
        json={
            "entity_id": "sensor.soc",
            "state": state,
            "attributes": {},
            "last_reported": reported.isoformat(),
        },
    )


def _soc_get(states: list[str], reported: list[datetime]) -> None:
    """Mock the SoC entity read with one response per daemon tick."""
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        side_effect=[
            _soc_resp(state, rep) for state, rep in zip(states, reported, strict=True)
        ]
    )


def _integrity_posts() -> list[tuple[str, dict[str, object]]]:
    """Monitor results captured by the fixture, no longer REST sensor bodies."""
    raise AssertionError("monitor result fixture not installed")


@pytest.fixture(autouse=True)
def monitor_results(monkeypatch):
    """Observe public monitor results at each tick and recovery boundary."""
    from dataclasses import fields

    results = []
    latest = None
    for name in ("record", "complete_fallback", "complete_recovery"):
        original = getattr(SocMonitor, name)

        def capture(self, *args, _original=original, **kwargs):
            nonlocal latest
            latest = _original(self, *args, **kwargs)
            return latest

        monkeypatch.setattr(SocMonitor, name, capture)

    for name in ("soc_monitor_tick", "finish_recovery"):
        original_tick = getattr(scheduler, name)

        async def tick(*args, _original=original_tick, **kwargs):
            nonlocal latest
            latest = None
            value = await _original(*args, **kwargs)
            if latest is not None:
                results.append((latest.state.value, {
                    field.name: getattr(latest, field.name) for field in fields(latest)
                }))
            return value

        monkeypatch.setattr(scheduler, name, tick)
    monkeypatch.setattr(__import__(__name__, fromlist=["_integrity_posts"]),
                        "_integrity_posts", lambda: results)


def _patch_monitor_loop(
    monkeypatch: pytest.MonkeyPatch,
    ticks: list[datetime],
    *,
    run_once_socs: list[SocMeasurement | None] | None = None,
    guard_socs: list[SocMeasurement | None] | None = None,
    fallback_states: list[bool] | None = None,
) -> type[Exception]:
    """Patch the loop like ``_patch_loop``, optionally capturing tick SoCs."""
    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    if run_once_socs is not None:
        async def fake_run_once(
            _s: Settings,
            *,
            soc: SocMeasurement | None = None,
            previous_plan: ChargePlan | None = None,
            trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
            ev_hold_state: scheduler.EvHoldState | None = None,
            fallback_active: bool = False,
        ) -> scheduler.RunResult:
            run_once_socs.append(soc)
            if fallback_states is not None:
                fallback_states.append(fallback_active)
            plan = _plan()
            return scheduler.RunResult(plan, plan)
    else:
        async def fake_run_once(
            _s: Settings,
            *,
            soc: SocMeasurement | None = None,
            previous_plan: ChargePlan | None = None,
            trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
            ev_hold_state: scheduler.EvHoldState | None = None,
            fallback_active: bool = False,
        ) -> scheduler.RunResult:
            if fallback_states is not None:
                fallback_states.append(fallback_active)
            plan = _plan()
            return scheduler.RunResult(plan, plan)

    if guard_socs is not None:
        async def fake_guard_tick(
            _s: Settings, target_w: float | None, *,
            soc: SocMeasurement | None = None,
            fallback_active: bool = False,
            fallback_confirmed: bool = False,
        ) -> float:
            guard_socs.append(soc)
            assert target_w is not None
            return target_w
    else:
        async def fake_guard_tick(
            _s: Settings, target_w: float | None, *,
            soc: SocMeasurement | None = None,
            fallback_active: bool = False,
            fallback_confirmed: bool = False,
        ) -> float:
            assert target_w is not None
            return target_w

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fake_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    return _patch_loop(monkeypatch, ticks)


@respx.mock
async def test_loop_isolated_soc_failure_then_pass_resets_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad minute is tolerated: pending failure, then a passing observation
    resets the count and returns to normal operation."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    now = datetime.now(UTC)
    _soc_get(["unavailable", "55"], [now, now])
    run_socs: list[SocMeasurement | None] = []
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 1)],
        run_once_socs=run_socs,
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    # The plan run consumed the tick's failed measurement (blocked at the
    # charger gate), and the passing tick reset the count.
    assert run_socs[0] is not None and not run_socs[0].ok
    assert run_socs[0].status is SocStatus.UNAVAILABLE
    published = _integrity_posts()
    assert [state for state, _ in published] == ["pending_failure", "normal"]
    assert published[0][1]["consecutive_failures"] == 1
    assert published[1][1]["consecutive_failures"] == 0
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 0,
        "fallback_status": "normal",
    }


@respx.mock
async def test_loop_third_consecutive_failure_reaches_fallback_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a configured current, threshold entry leaves the program untouched."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50", "50"], [stale, stale, stale])
    stop = _patch_monitor_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 1),
            datetime(2026, 6, 10, 22, 2),
        ],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        v2l_power_entity="",
    )
    with caplog.at_level("WARNING"), pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    published = _integrity_posts()
    assert [state for state, _ in published] == [
        "pending_failure",
        "pending_failure",
        "fallback_threshold",
    ]
    assert published[-1][1]["consecutive_failures"] == 3
    assert published[-1][1]["failure_threshold"] == 3
    assert published[-1][1]["fallback_confirmed"] is False
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 3,
        "fallback_status": "fallback_threshold",
    }
    assert "fallback-entry threshold reached" in caplog.text


@respx.mock
async def test_soc_monitor_tick_skips_fallback_when_factory_returns_non_solis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50"], [stale])

    class OtherDevice:
        pass

    monkeypatch.setattr(scheduler, "inverter_device", lambda *_args: OtherDevice())
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_failure_threshold=1,
        inverter="solis",
        solis_fallback_current_a=42.0,
        v2l_power_entity="",
    )

    seed_authority(settings, "optimise")
    with caplog.at_level("ERROR"):
        await scheduler.soc_monitor_tick(
            settings, SocMonitor(path=tmp_path / "ha_spark_soc_monitor.json")
        )

    state, attrs = _integrity_posts()[0]
    assert state == "fallback_requested"
    assert attrs["fallback_confirmed"] is False
    assert attrs["fallback_action"] == (
        "[SKIP] Solis fallback skipped: inverter factory returned non-Solis device "
        "(OtherDevice)"
    )
    assert "Solis fallback skipped: inverter factory returned OtherDevice" in caplog.text


@respx.mock
async def test_loop_enters_configured_fallback_at_threshold_and_publishes_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50", "50"], [stale, stale, stale])
    fallback_calls: list[SocMeasurement] = []

    class FallbackDevice(SolisDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            fallback_calls.append(soc)
            return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

        async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        async def write_safe_state(self) -> list[str]:
            return []

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(FallbackDevice, settings, rest),
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_failure_threshold=2,
        solis_fallback_current_a=42.0,
        v2l_power_entity="",
    )
    seed_authority(settings, "optimise")
    fallback_states: list[bool] = []
    # Preserve the loop harness's run_once stub while capturing its fallback gate.
    stop = _patch_monitor_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 1),
            datetime(2026, 6, 10, 22, 30),
        ],
        fallback_states=fallback_states,
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert len(fallback_calls) == 1 and not fallback_calls[0].ok
    assert fallback_states == [False, True]
    published = _integrity_posts()
    assert [state for state, _ in published] == [
        "pending_failure",
        "fallback_verified",
        "fallback_verified",
    ]
    state, attrs = published[1]
    assert state == "fallback_verified"
    assert attrs["fallback_confirmed"] is True
    assert attrs["fallback_action"] == (
        "[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"
    )
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 3,
        "fallback_status": "fallback_verified",
    }


@respx.mock
async def test_loop_retries_unconfirmed_fallback_once_on_next_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50"], [stale, stale])
    fallback_calls: list[SocMeasurement] = []

    class RetryingFallbackDevice(SolisDevice):
        outcomes = [
            [
                "[BLOCKED] Solis fallback held: live verified export slot 1 is unreadable"
            ],
            ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"],
        ]

        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            fallback_calls.append(soc)
            return self.outcomes.pop(0)

        async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        async def write_safe_state(self) -> list[str]:
            return []

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(
            RetryingFallbackDevice, settings, rest
        ),
    )
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 1)],
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_failure_threshold=1,
        solis_fallback_current_a=42.0,
        v2l_power_entity="",
    )

    seed_authority(settings, "optimise")
    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert len(fallback_calls) == 2
    assert [state for state, _ in _integrity_posts()] == [
        "fallback_failed",
        "fallback_verified",
    ]
    assert _integrity_posts()[0][1]["fallback_confirmed"] is False
    first_action = _integrity_posts()[0][1]["fallback_action"]
    assert first_action.startswith("[FAILED] Solis fallback unconfirmed: [BLOCKED]")
    assert "live verified export slot 1 is unreadable" in first_action
    assert _integrity_posts()[1][1]["fallback_confirmed"] is True


@respx.mock
async def test_loop_keeps_simulated_fallback_unconfirmed_without_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50"], [stale])

    class SimulatedFallbackDevice(SolisDevice):
        async def apply_fallback(self, _soc: SocMeasurement) -> list[str]:
            pytest.fail("Observe must never request fallback programming")

        def planned_rate_w(self, _intent: object) -> float:
            return 0.0

        async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        async def write_safe_state(self) -> list[str]:
            return []

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(SimulatedFallbackDevice, settings, rest),
    )
    stop = _patch_monitor_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        plan_run_time="22:00",
        authority_mode="observe",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_failure_threshold=1,
        solis_fallback_current_a=42.0,
        v2l_power_entity="",
    )

    with caplog.at_level("WARNING"), pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    state, attrs = _integrity_posts()[0]
    assert state == "fallback_threshold"
    assert attrs["fallback_confirmed"] is False
    assert attrs["fallback_action"] is None
    assert not any(
        record.levelname == "WARNING"
        and record.message.startswith(("[FAILED] Solis fallback", "[SIMULATE] Solis fallback"))
        for record in caplog.records
    )


@respx.mock
async def test_loop_reconciles_persisted_fallback_even_when_soc_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted confirmation is untrusted until this process reads back Solis."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    now = datetime.now(UTC)
    _soc_get(["55"], [now])
    monitor_path = tmp_path / "ha_spark_soc_monitor.json"
    monitor_path.write_text(
        json.dumps({"consecutive_failures": 2, "fallback_status": "fallback_verified"}),
        encoding="utf-8",
    )
    fallback_calls: list[SocMeasurement] = []

    class ReconcileFallbackDevice(SolisDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            fallback_calls.append(soc)
            return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

        def planned_rate_w(self, _intent: object) -> float:
            return 0.0

        async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        async def write_safe_state(self) -> list[str]:
            return []

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(ReconcileFallbackDevice, settings, rest),
    )
    stop = _patch_monitor_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_failure_threshold=2,
        solis_fallback_current_a=42.0,
        v2l_power_entity="",
    )

    seed_authority(settings, "optimise")
    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert len(fallback_calls) == 1 and fallback_calls[0].ok
    assert _integrity_posts()[0][0] == "fallback_verified"
    assert _integrity_posts()[0][1]["fallback_confirmed"] is True


# --- #422: the authority store owns fallback active and its causes ---


def _fallback_notices(title: str) -> list[dict[str, object]]:
    """The persistent-notification bodies with ``title`` this test saw."""
    out: list[dict[str, object]] = []
    for call in respx.calls:
        if call.request.url.path != "/api/services/persistent_notification/create":
            continue
        body = json.loads(call.request.content)
        if body.get("title") == title:
            out.append(body)
    return out


def _hardware_service_calls() -> list[str]:
    """Hardware-actuating service calls (modbus/select/switch), never notices."""
    return [
        call.request.url.path
        for call in respx.calls
        if call.request.method == "POST"
        and any(
            call.request.url.path.startswith(f"/api/services/{domain}/")
            for domain in ("modbus", "select", "switch", "alphaess")
        )
    ]


@respx.mock
async def test_loop_fallback_entry_saves_the_store_before_the_write_and_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#422) Entry: the store (active + cause + bumped epoch) is saved before
    the fallback write is requested, the apply plans under the new epoch, and
    one entry notice fires across the ticks."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50", "50"], [stale, stale, stale])
    apply_state: list[tuple[bool, int, int]] = []

    class StoreWitnessDevice(SolisDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            from ha_spark.devices.execution import execution_context

            context = execution_context()
            store = context.authority
            assert store is not None and store.state is not None
            apply_state.append((
                store.state.fallback.active,
                store.state.epoch,
                context.planned_epoch.get(),
            ))
            return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

        async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        async def write_safe_state(self) -> list[str]:
            return []

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(StoreWitnessDevice, settings, rest),
    )
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        soc_failure_threshold=2, solis_fallback_current_a=42.0, v2l_power_entity="",
    )
    store = seed_authority(settings, "optimise")
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 1),
         datetime(2026, 6, 10, 22, 30)],
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    from ha_spark.authority import load_authority

    # Saved active + cause with the epoch bumped (start_process + entry), and
    # the fallback apply itself ran under the bumped epoch, not refused stale.
    assert store.state is not None
    assert apply_state == [(True, store.state.epoch + 2, store.state.epoch + 2)]
    result = load_authority(settings)
    assert result.state is not None
    assert result.state.fallback.active is True
    assert result.state.fallback.causes == ("soc_integrity",)
    assert result.state.epoch == store.state.epoch + 2
    assert len(_fallback_notices("ha-spark fallback entered")) == 1
    assert _fallback_notices("ha-spark fallback left") == []


@respx.mock
async def test_loop_fallback_exit_clears_the_store_and_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#422) Exit: the store's fallback active + cause clear and the epoch
    bumps only after the recovered plan applied and read back, with one exit
    notice."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, _runs, settings = _patch_recovery_loop(
        tmp_path, monkeypatch, [0, 1, 2, 3],
        [["[APPLIED] set timed charge current to 20 A over 3.5 h of the window"]],
    )
    from ha_spark.authority import load_authority

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    result = load_authority(settings)
    assert result.state is not None
    assert result.state.fallback.active is False
    assert result.state.fallback.causes == ()
    # Seed + start_process + first-boot reconciliation entry + exit.
    assert result.state.epoch == 4
    assert len(_fallback_notices("ha-spark fallback entered")) == 1
    assert len(_fallback_notices("ha-spark fallback left")) == 1


@pytest.mark.parametrize("mode", ["observe", "protect"])
@respx.mock
async def test_loop_sustained_failure_without_charging_authority_never_requests_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """(#422) Observe/Protect: pending failure and the threshold crossing are
    counted, persisted and published, but `request_fallback`/`apply_fallback`
    are never called and zero fallback writes happen."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50", "50"], [stale, stale, stale])
    requests: list[object] = []
    applies: list[object] = []

    def forbid_request(self: SocMonitor) -> object:
        requests.append(self)
        raise AssertionError(f"{mode} requested a fallback")

    async def forbid_apply(self: SolisDevice, soc: SocMeasurement) -> list[str]:
        applies.append(soc)
        raise AssertionError(f"{mode} applied a fallback")

    monkeypatch.setattr(SocMonitor, "request_fallback", forbid_request)
    monkeypatch.setattr(SolisDevice, "apply_fallback", forbid_apply)
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        authority_mode=mode,  # type: ignore[arg-type]
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        soc_failure_threshold=2, solis_fallback_current_a=42.0, v2l_power_entity="",
    )
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 1),
         datetime(2026, 6, 10, 22, 2)],
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    from ha_spark.authority import load_authority

    assert requests == [] and applies == []
    # Counted, persisted and published — but never written.
    assert [state for state, _ in _integrity_posts()] == [
        "pending_failure", "fallback_threshold", "fallback_threshold",
    ]
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 3, "fallback_status": "fallback_threshold",
    }
    result = load_authority(settings)
    assert result.state is not None
    assert result.state.fallback.active is False
    assert result.state.epoch == 2  # only start_process; entry never happened
    assert _fallback_notices("ha-spark fallback entered") == []
    assert _hardware_service_calls() == []


@respx.mock
async def test_first_boot_reconciles_monitor_fallback_into_the_store_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#422) First boot of this version: a monitor-persisted fallback status
    is reconciled into the store as an active cause that is not confirmed (the
    monitor's confirmation stays false until read-back), and a restart does
    not notify again."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    now = datetime.now(UTC)
    _soc_get(["55"], [now])
    (tmp_path / "ha_spark_soc_monitor.json").write_text(
        json.dumps({"consecutive_failures": 2, "fallback_status": "fallback_verified"}),
        encoding="utf-8",
    )

    class UnconfirmedFallbackDevice(SolisDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            return ["[FAILED] Solis fallback at 42 A in 23:30-05:30 unconfirmed: read-back"]

        def planned_rate_w(self, _intent: object) -> float:
            return 0.0

        async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        async def write_safe_state(self) -> list[str]:
            return []

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(UnconfirmedFallbackDevice, settings, rest),
    )
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        authority_mode="optimise",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        soc_failure_threshold=2, solis_fallback_current_a=42.0, v2l_power_entity="",
    )
    stop = _patch_monitor_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    from ha_spark.authority import load_authority

    result = load_authority(settings)
    assert result.state is not None
    assert result.state.fallback.active is True
    assert result.state.fallback.causes == ("soc_integrity",)
    assert len(_fallback_notices("ha-spark fallback entered")) == 1
    # Unconfirmed: the reconcile attempt failed, so the cause is active but the
    # fallback is not hardware-confirmed; it retries next minute.
    state, attrs = _integrity_posts()[0]
    assert state == "fallback_failed"
    assert attrs["fallback_confirmed"] is False
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 2, "fallback_status": "fallback_failed",
    }

    # A restart: the store already says active, so no second entry or notice.
    _soc_get(["55"], [now])
    stop = _patch_monitor_loop(monkeypatch, [datetime(2026, 6, 10, 22, 1)])
    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert len(_fallback_notices("ha-spark fallback entered")) == 1
    assert load_authority(settings).state is not None
    assert load_authority(settings).state.fallback.active is True


@respx.mock
async def test_fallback_entry_save_failure_refuses_the_fallback_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(#422) If the entry save fails, no fallback write runs: `save_failed`
    makes the gate refuse, and the attempt stays retryable next minute."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50"], [stale])
    settings = Settings(
        ha_url="http://ha.test", ha_token="t",
        authority_mode="optimise",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        soc_failure_threshold=1, solis_fallback_current_a=42.0, v2l_power_entity="",
    )

    def boom(path: object, payload: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ha_spark.authority._durable_write", boom)
    monitor = SocMonitor(path=tmp_path / "ha_spark_soc_monitor.json")
    with caplog.at_level("WARNING"):
        await scheduler.soc_monitor_tick(settings, monitor)

    from ha_spark.authority import load_authority

    assert _hardware_service_calls() == []
    assert "fallback entry not persisted" in caplog.text
    state, attrs = _integrity_posts()[0]
    assert state == "fallback_failed"
    assert attrs["fallback_confirmed"] is False
    assert attrs["fallback_action"].startswith("[FAILED] Solis fallback unconfirmed: [REFUSED]")
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 1, "fallback_status": "fallback_failed",
    }
    result = load_authority(settings)
    assert result.state is not None
    assert result.state.fallback.active is False  # the entry never persisted


@respx.mock
async def test_loop_caps_verified_fallback_guard_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50"], [stale])
    stop = _patch_monitor_loop(monkeypatch, [datetime(2026, 6, 10, 23, 30)])
    guard_calls: list[tuple[float | None, bool, bool]] = []

    class FallbackDevice(SolisDevice):
        capabilities = frozenset({Capability.CHARGE_RATE})

        async def apply_fallback(self, _soc: SocMeasurement) -> list[str]:
            return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

        async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        async def write_safe_state(self) -> list[str]:
            return []

        def planned_rate_w(self, _intent: ChargeIntent) -> float:
            # This is the rejected plan target. Guard restoration must instead
            # use the explicitly configured fallback ceiling.
            return 900.0

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(FallbackDevice, settings, rest),
    )

    async def capture_guard(
        _settings: Settings,
        target_w: float | None,
        *,
        soc: SocMeasurement | None = None,
        fallback_active: bool = False,
        fallback_confirmed: bool = False,
    ) -> float:
        guard_calls.append((target_w, fallback_active, fallback_confirmed))
        return target_w or 0.0

    monkeypatch.setattr(scheduler, "guard_tick", capture_guard)
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        plan_run_time="23:30",
        charge_window_start="23:30",
        charge_window_end="05:30",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_failure_threshold=1,
        solis_fallback_current_a=42.0,
        grid_power_entity="sensor.house_supply_power",
        v2l_power_entity="",
    )

    seed_authority(settings, "optimise")
    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    # The loop forwards plan state unchanged; guard_tick owns fallback target selection.
    assert guard_calls == [(900.0, True, True)]


@respx.mock
async def test_loop_never_requests_fallback_for_alphaess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50"], [stale])
    fallback_states: list[bool] = []
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0)],
        fallback_states=fallback_states,
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_failure_threshold=1,
        inverter="alphaess",
        solis_fallback_current_a=42.0,
        v2l_power_entity="",
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert fallback_states == [True]  # threshold blocks normal programming
    assert _integrity_posts()[-1][0] == "fallback_threshold"
    assert _integrity_posts()[-1][1]["fallback_action"] is None
    assert not any(call.request.url.path.startswith("/api/services/") for call in respx.calls)


@respx.mock
async def test_loop_custom_threshold_behaves_equivalently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50"], [stale, stale])
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 1)],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        soc_failure_threshold=2, v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    published = _integrity_posts()
    assert [state for state, _ in published] == ["pending_failure", "fallback_threshold"]
    assert published[-1][1]["failure_threshold"] == 2


@respx.mock
async def test_loop_observes_once_per_tick_and_reuses_the_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observation identity: one failed tick where the plan run, the guard, and
    publication all consume the same observation increments the count once."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    soc_get = respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=_soc_resp("50", stale)
    )
    run_socs: list[SocMeasurement | None] = []
    guard_socs: list[SocMeasurement | None] = []
    # 23:30: plan run time AND inside the charge window -> both fire this tick.
    stop = _patch_monitor_loop(
        monkeypatch, [datetime(2026, 6, 10, 23, 30)],
        run_once_socs=run_socs, guard_socs=guard_socs,
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="23:30",
        charge_window_start="23:30", charge_window_end="05:30",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    # Exactly one HA read for the SoC this tick, and the very same measurement
    # object reached the plan run and the guard.
    assert soc_get.call_count == 1
    assert run_socs[0] is guard_socs[0]
    assert run_socs[0] is not None and not run_socs[0].ok
    # Counted once, not once per consumer.
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 1,
        "fallback_status": "pending_failure",
    }
    published = _integrity_posts()
    assert [state for state, _ in published] == ["pending_failure"]
    assert published[0][1]["measurement"] is run_socs[0]
    assert run_socs[0].status is SocStatus.STALE


@respx.mock
async def test_loop_skips_monitoring_without_soc_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No configured soc_entity -> nothing to observe: no reads, no publishes,
    no persisted monitor state (the plan run observes through its own path)."""
    gets = respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop = _patch_monitor_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert gets.call_count == 0
    assert _integrity_posts() == []
    assert not (tmp_path / "ha_spark_soc_monitor.json").exists()


def _failed_soc() -> SocMeasurement:
    """One stale checked measurement: parseable value, unusably old report."""
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.STALE,
        observed_at=now,
        value=30.0,
        raw_state="30",
        reported_at=now - timedelta(hours=1),
        age_s=3600.0,
        max_age_s=600.0,
    )


async def _fake_load(_s: Settings, **_kw: object) -> LoadForecast:
    return LoadForecast(total_kwh=24.0, slots=None, source="test")


@respx.mock
async def test_run_once_failed_soc_leaves_solis_resident_program_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """First-failure policy on Solis: the plan run consumes the tick's failed
    measurement, no register is written (resident program untouched), and the
    blocked action line names the concrete integrity reason."""
    monkeypatch.setattr(sources, "predict_home_load", _fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="optimise",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
    )
    failed = _failed_soc()
    with caplog.at_level("WARNING"):
        plan = (await run_once(s, soc=failed)).plan

    # The plan carries the exact tick measurement (no independent reread).
    assert plan.soc is failed
    assert any("[BLOCKED]" in r.message and "not charging to" in r.message
               for r in caplog.records)
    assert any("over the 600s maximum" in r.message for r in caplog.records)
    # No service call left the process: the resident program is untouched
    # (only reads and sensor publishes happened).
    for call in respx.calls:
        assert not call.request.url.path.startswith("/api/services/")


@respx.mock
async def test_run_once_failed_soc_blocks_alphaess_programming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """First-failure policy on AlphaESS: any failed integrity observation
    blocks new charge programming (no fallback, no service call)."""
    monkeypatch.setattr(sources, "predict_home_load", _fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", authority_mode="optimise",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        inverter="alphaess", alphaess_serial="SN1",
    )
    with caplog.at_level("INFO"):
        plan = (await run_once(s, soc=_failed_soc())).plan

    assert not plan.soc.ok
    assert any("[BLOCKED]" in r.message and "not charge to" in r.message
               for r in caplog.records)
    for call in respx.calls:
        assert not call.request.url.path.startswith("/api/services/")


@respx.mock
async def test_loop_blocked_plan_rate_never_becomes_guard_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan blocked on an untrusted SoC was sized from soc_now == 0 (likely
    max current): its rate must not become the supply guard's restore target.
    The guard adopts the live setpoint instead — reductions only."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50"], [stale])

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        # A plan computed from the tick's failed measurement: blocked at the
        # charger gate, but still a plan object (existence != applied).
        failed = soc if soc is not None and not soc.ok else _failed_soc()
        plan = replace(
            _plan(ChargeIntent(
                target_soc_pct=90.0, soc=failed,
                window_start=time(23, 30), window_end=time(5, 30),
            )),
            soc=failed,  # compute_plan carries the same measurement at both levels
        )
        return scheduler.RunResult(plan, None)

    guard_targets: list[float | None] = []

    async def capture_guard_tick(
        _s: Settings, target_w: float | None, *,
        soc: SocMeasurement | None = None,
        fallback_active: bool = False,
        fallback_confirmed: bool = False,
    ) -> float:
        guard_targets.append(target_w)
        # Like the real guard_tick: adopt (echo) the live setpoint when no
        # trusted target exists, so later ticks keep it as the ceiling.
        return target_w if target_w is not None else 2040.0

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    planned_calls: list[object] = []

    async def fake_planned_rate_w(_s: Settings, _plan: ChargePlan) -> float:
        planned_calls.append(_plan)
        return 4000.0

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", capture_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(scheduler, "_planned_rate_w", fake_planned_rate_w)
    stop = _patch_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 23, 30), datetime(2026, 6, 10, 23, 45)],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="23:30",
        charge_window_start="23:30", charge_window_end="05:30",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert planned_calls == []  # the blocked plan's rate was never consulted
    # First in-window tick: no trusted target (None) -> the guard adopts the
    # live setpoint; the adopted value, not the blocked plan's rate, is the
    # ceiling the second tick sees.
    assert guard_targets == [None, 2040.0]


def test_a_pending_export_event_always_re_applies(tmp_path) -> None:
    """The arming boundary is a clock event, not a plan change (#144).

    An Axle event announced a day ahead yields an equal `ExportIntent` on every
    tick. Comparing plans alone would report "unchanged" right through the tick
    that would have armed the window, so the event would never be programmed.
    """
    from datetime import UTC, datetime, time, timedelta
    from zoneinfo import ZoneInfo

    from ha_spark.energy.models import ChargeIntent, ExportIntent
    from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus

    london = ZoneInfo("Europe/London")
    observed = datetime.now(UTC)
    soc = SocMeasurement(
        status=SocStatus.OK,
        observed_at=observed,
        value=60.0,
        raw_state="60",
        reported_at=observed,
        age_s=0.0,
        max_age_s=600.0,
    )
    start = datetime(2026, 9, 15, 18, 30, tzinfo=london)
    end = start + timedelta(hours=1)
    export = ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )
    pending = ChargeIntent(77.0, soc, time(23, 30), time(5, 30), export=export)

    assert setpoint_changed(
        pending,
        pending,
    ) is True
    # Without an event the plan-value comparison still governs.
    plain = ChargeIntent(77.0, soc, time(23, 30), time(5, 30))
    assert setpoint_changed(plain, plain) is False


class _RecordingDevice:
    """A device that records every reconcile pass the loop drives."""

    def __init__(self, seen: list[datetime], *, boom: bool = False) -> None:
        self._seen = seen
        self._boom = boom

    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]:
        self._seen.append(now)
        if self._boom:
            raise RuntimeError("HA unreachable")
        return ["[SKIP] set inverter power switch to On (already set)"]


async def test_reconcile_tick_does_nothing_before_a_plan_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No picture yet is not evidence that no hold is active.

    Writing ``On`` merely because a plan has not loaded would release a live
    dispatch across every restart and every hot reload (the precedent is
    ``guard_tick``, which adopts rather than guesses).
    """
    seen: list[datetime] = []
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: _RecordingDevice(seen))

    await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"), None, datetime(2026, 6, 10, 22, 0)
    )

    assert seen == []


async def test_reconcile_tick_drives_the_device_with_the_ticks_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[datetime] = []
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: _RecordingDevice(seen))
    now = datetime(2026, 6, 10, 22, 1)

    await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"), _plan(), now
    )

    assert seen == [now]


async def test_reconcile_tick_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed pass is logged and retried next minute; it cannot abort the loop."""
    seen: list[datetime] = []
    monkeypatch.setattr(
        scheduler, "inverter_device", lambda *_a: _RecordingDevice(seen, boom=True)
    )

    await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"), _plan(), datetime(2026, 6, 10, 22, 0)
    )

    assert seen == [datetime(2026, 6, 10, 22, 0)]


async def test_run_forever_reconciles_every_minute_not_every_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconcile answers the clock, so it cannot ride the half-hourly replan.

    A hold that opens and closes between two slot runs (17:35-17:55) would
    otherwise never be applied at all, and a failed write would wait out the
    rest of the dispatch (#143 hole 2).
    """
    reconciled: list[datetime] = []
    plans: list[ChargePlan | None] = []

    async def fake_run_once(_s: Settings, **_kw: object) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        plans.append(plan)
        reconciled.append(now)
        return scheduler.ReconcileResult(
            ["[SKIP] set inverter power switch to On (already set)"], trusted_holds
        )

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    # One slot boundary, then three ticks inside the same slot: only the first
    # runs the planner, but every one of them reconciles.
    ticks = [
        datetime(2026, 6, 10, 22, 0),
        datetime(2026, 6, 10, 22, 1),
        datetime(2026, 6, 10, 22, 2),
        datetime(2026, 6, 10, 22, 3),
    ]
    stop = _patch_loop(monkeypatch, ticks)

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    # Every tick inside the slot reconciles; the slot boundary itself does not,
    # because `run_once` reconciles there with the plan it just computed.
    assert reconciled == ticks[1:]
    assert all(plan is not None for plan in plans)


async def test_run_forever_does_not_reconcile_twice_on_a_slot_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot tick must reconcile once, against the plan it just computed.

    Reconciling here as well would use the *stale* plan first: when a dispatch
    is announced or cancelled between slots, the switch is driven to the old
    plan's state and then immediately back — two writes and a momentarily wrong
    whole-inverter enable inside one tick.
    """
    reconciled: list[datetime] = []

    async def fake_run_once(_s: Settings, **_kw: object) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        reconciled.append(now)
        return scheduler.ReconcileResult([], trusted_holds)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch, [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 30)]
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert reconciled == []


async def test_reconcile_tick_logs_only_what_changed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """1440 identical lines a day would bury the plan and guard activity."""
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: _RecordingDevice([]))
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    now = datetime(2026, 6, 10, 22, 0)

    with caplog.at_level("INFO", logger="ha_spark.energy.scheduler"):
        first = await scheduler.reconcile_tick(settings, _plan(), now)
        repeat = await scheduler.reconcile_tick(settings, _plan(), now, previous=first.lines)

    assert first == repeat
    assert len(caplog.records) == 1


# --- #140/#143 §3: degraded hold data is ignored, not believed ---

_HOLD = (datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 23, 0))


def _untrusted_plan() -> ChargePlan:
    """A plan built from a failed dispatch read: its empty holds mean "unreadable"."""
    return _plan(replace(_INTENT, holds=(), hold_trusted=False))


class _IntentRecordingDevice:
    def __init__(self) -> None:
        self.intents: list[ChargeIntent] = []

    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]:
        self.intents.append(intent)
        return ["[SKIP] set inverter power switch to Off (already set)"]


async def test_an_untrusted_tick_reconciles_against_the_last_trusted_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the last trusted hold, a failed read must keep the inverter held ``Off``."""
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    inside = datetime(2026, 6, 10, 22, 15)

    await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"),
        _untrusted_plan(),
        inside,
        trusted_holds=(_HOLD,),
    )

    [intent] = device.intents
    assert intent.holds == (_HOLD,)
    assert intent.hold_active(inside) is True


async def test_a_trusted_hold_still_ends_on_its_own_end_time_while_reads_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No expiry timer and no stuck ``Off``: the known end time releases the hold.

    Past the last known end with reads still failing, the release is the
    relinquish path's safe state (#143 §5), which writes ``On`` without the
    reconcile's pre-read.
    """
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)

    await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"),
        _untrusted_plan(),
        datetime(2026, 6, 10, 23, 5),
        trusted_holds=(_HOLD,),
    )

    assert device.safe_states == 1


async def test_an_untrusted_tick_with_no_trusted_holds_yet_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Booting into a failed read is no picture, and no picture never writes ``On``."""
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)

    lines = await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"),
        _untrusted_plan(),
        datetime(2026, 6, 10, 22, 15),
        trusted_holds=None,
    )

    assert device.intents == []
    assert any("untrusted" in line for line in lines.lines)


async def test_a_trusted_tick_reconciles_against_its_own_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    plan = _plan(replace(_INTENT, holds=()))

    await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"),
        plan,
        datetime(2026, 6, 10, 22, 15),
        trusted_holds=(_HOLD,),
    )

    assert device.intents == [plan.charge_intent]


async def test_run_once_reconciles_an_untrusted_plan_against_the_trusted_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slot tick's own pass is the one a failed read reaches first."""
    reconciled: list[ChargeIntent] = []
    current = _untrusted_plan()

    async def fake_current_plan(_s: Settings, _rest: object, **_kw: object) -> object:
        return SimpleNamespace(
            plan=current, inputs=SimpleNamespace(ev_hold_charging=None), load_source="test",
            tz=None,
            forecast=LoadForecast(total_kwh=24.0, slots=None, source="test"),
        )

    class FakeDevice:
        async def apply(self, intent: ChargeIntent) -> list[str]:
            return ["[APPLIED] test"]

        async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]:
            reconciled.append(intent)
            return []

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(scheduler, "current_plan", fake_current_plan)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_args: FakeDevice())
    monkeypatch.setattr(scheduler, "_record_forecast", noop)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", noop)

    await run_once(Settings(), soc=current.soc, trusted_holds=(_HOLD,))
    await run_once(Settings(), soc=current.soc)

    # With a trusted set it substitutes; without one it does not reconcile at all.
    assert [intent.holds for intent in reconciled] == [(_HOLD,)]


async def test_run_forever_remembers_holds_only_from_trusted_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = _plan(replace(_INTENT, holds=(_HOLD,)))
    plans = iter([trusted, _untrusted_plan()])
    run_once_saw: list[object] = []
    reconcile_saw: list[object] = []

    async def fake_run_once(_s: Settings, **kw: object) -> scheduler.RunResult:
        run_once_saw.append(kw.get("trusted_holds"))
        plan = next(plans)
        return scheduler.RunResult(plan, plan)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        reconcile_saw.append(trusted_holds)
        return scheduler.ReconcileResult([], trusted_holds)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),  # trusted plan
            datetime(2026, 6, 10, 22, 1),
            datetime(2026, 6, 10, 22, 30),  # untrusted plan
            datetime(2026, 6, 10, 22, 31),
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert run_once_saw == [None, (_HOLD,)]
    assert reconcile_saw == [(_HOLD,), (_HOLD,)]


async def test_run_forever_keeps_ev_state_but_drops_dispatch_picture_on_hot_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload may change the dispatch source; the old picture is not evidence."""
    captured: list[AppState] = []
    reconcile_saw: list[object] = []
    ev_states: list[scheduler.EvHoldState] = []

    class _CapturingState(AppState):
        def __init__(self, **kw: object) -> None:
            super().__init__(**kw)  # type: ignore[arg-type]
            captured.append(self)

    async def fake_run_once(_s: Settings, **kw: object) -> scheduler.RunResult:
        state = kw["ev_hold_state"]
        assert isinstance(state, scheduler.EvHoldState)
        ev_states.append(state)
        state.active = True
        state.clear_count = 2
        state.last_clear_report_at = datetime(2026, 6, 10, 21, 55)
        plan = _plan(replace(_INTENT, holds=(_HOLD,)))
        return scheduler.RunResult(plan, plan)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        reconcile_saw.append(trusted_holds)
        assert ev_hold_state is not None
        ev_states.append(ev_hold_state)
        assert ev_hold_state.active is True
        assert ev_hold_state.clear_count == 2
        assert ev_hold_state.last_clear_report_at == datetime(2026, 6, 10, 21, 55)
        captured[0].settings = Settings(
            ha_url="http://ha.test", ha_token="t", plan_run_time="22:00"
        )
        return scheduler.ReconcileResult([], trusted_holds)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "AppState", _CapturingState)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 1),  # reconciles, then the options are rewritten
            datetime(2026, 6, 10, 22, 2),
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert reconcile_saw == [(_HOLD,), None]
    assert ev_states[0] is ev_states[1]
    assert ev_states[2] is ev_states[1]


# --- #140/#143 §4: the per-minute pass tracks Dave's poll on the HA-entity path ---

_DISPATCH_URL = "http://ha.test/api/states/binary_sensor.dispatch"
_EV_STATUS_URL = "http://ha.test/api/states/sensor.ev"
_EV_PLUG_URL = "http://ha.test/api/states/sensor.ev_plug"
_OCTOPUS_RATE_URL = "http://ha.test/api/states/sensor.octopus_rate"


def _dispatch_state(
    state: str,
    dispatches: list[dict[str, str]] | None = None,
    *,
    at: datetime | None = None,
) -> httpx.Response:
    """A dispatch entity payload; ``at`` is its top-level ``last_reported``.

    #420 made report times hold evidence: a payload without one reads as
    unknown freshness, which never starts or extends a hold.
    """
    body: dict[str, object] = {
        "entity_id": "binary_sensor.dispatch",
        "state": state,
        "attributes": {"planned_dispatches": dispatches or []},
    }
    if at is not None:
        body["last_reported"] = at.isoformat()
    return httpx.Response(200, json=body)


def _ev_status_state(state: str, *, at: datetime | None = None) -> httpx.Response:
    body: dict[str, object] = {"entity_id": "sensor.ev", "state": state, "attributes": {}}
    if at is not None:
        body["last_reported"] = at.isoformat()
    return httpx.Response(200, json=body)


def _dispatch_settings(**kw: object) -> Settings:
    base: dict[str, object] = dict(
        ha_url="http://ha.test", ha_token="t", dispatch_entity="binary_sensor.dispatch"
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _fresh(at: datetime, validity: str = "fresh") -> EvidenceFreshness:
    """Hold evidence as #419 reads it: a validity and an effective report time."""
    return EvidenceFreshness(
        validity=validity, reported_at=at, age_s=0.0, max_age_s=300.0, own_reported_at=at,
    )


@respx.mock
async def test_contradicted_dispatch_does_not_hold_during_per_minute_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(
        return_value=_dispatch_state(
            "off",
            [{"start": "2026-10-03T14:00:00", "end": "2026-10-03T15:00:00"}],
            at=now - timedelta(seconds=60),
        )
    )
    respx.get(_EV_PLUG_URL).mock(
        return_value=_ev_status_state("EV Disconnected", at=now - timedelta(seconds=60))
    )
    ev_status = respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    respx.get(_OCTOPUS_RATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.octopus_rate",
                "state": "0.20",
                "attributes": {"is_intelligent_adjusted": False},
            },
        )
    )
    settings = _dispatch_settings(
        ev_plug_entity="sensor.ev_plug",
        ev_status_entity="sensor.ev",
        octopus_rate_entity="sensor.octopus_rate",
    )
    caplog.set_level("INFO", logger="ha_spark.energy.sources")

    result = await scheduler.reconcile_tick(
        settings,
        _plan(replace(_INTENT, holds=(_HOLD,))),
        now,
        trusted_holds=(_HOLD,),
    )

    [intent] = device.intents
    assert intent.holds == ()
    assert intent.hold_active(now) is False
    assert intent.hold_trusted is True
    assert result.trusted_holds == ()
    assert ev_status.call_count == 1
    assert not any("Dropped contradicted dispatch" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("plug_at", [timedelta(seconds=301), None], ids=["stale", "missing"])
@respx.mock
async def test_a_stale_disconnected_plug_never_releases_a_per_minute_hold(
    monkeypatch: pytest.MonkeyPatch, plug_at: timedelta | None
) -> None:
    """A cached "EV Disconnected" is not a live picture, so it cannot contradict."""
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(
        return_value=_dispatch_state(
            "off",
            [{"start": "2026-10-03T14:00:00", "end": "2026-10-03T15:00:00"}],
            at=now - timedelta(seconds=60),
        )
    )
    respx.get(_EV_PLUG_URL).mock(
        return_value=_ev_status_state(
            "EV Disconnected", at=None if plug_at is None else now - plug_at
        )
    )
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    respx.get(_OCTOPUS_RATE_URL).mock(return_value=httpx.Response(404))
    settings = _dispatch_settings(
        ev_plug_entity="sensor.ev_plug",
        ev_status_entity="sensor.ev",
        octopus_rate_entity="sensor.octopus_rate",
    )

    result = await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=(_HOLD,))), now, trusted_holds=(_HOLD,)
    )

    [intent] = device.intents
    assert intent.hold_trusted is True
    assert intent.hold_active(now) is True
    assert result.trusted_holds == intent.holds != ()


@pytest.mark.parametrize(
    ("dispatch_state", "plug_state"),
    [("on", "EV Disconnected"), ("off", "unavailable")],
    ids=["live-beats-disconnected", "unreadable-plug-keeps-uncorroborated"],
)
@respx.mock
async def test_live_or_uncorroborated_dispatch_keeps_per_minute_hold(
    monkeypatch: pytest.MonkeyPatch, dispatch_state: str, plug_state: str
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(
        return_value=_dispatch_state(
            dispatch_state,
            [{"start": "2026-10-03T14:00:00", "end": "2026-10-03T15:00:00"}],
            at=now - timedelta(seconds=60),
        )
    )
    respx.get(_EV_PLUG_URL).mock(
        return_value=httpx.Response(
            200,
            json={"entity_id": "sensor.ev_plug", "state": plug_state, "attributes": {}},
        )
    )
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    respx.get(_OCTOPUS_RATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.octopus_rate",
                "state": "0.20",
                "attributes": {"is_intelligent_adjusted": False},
            },
        )
    )
    settings = _dispatch_settings(
        ev_plug_entity="sensor.ev_plug",
        ev_status_entity="sensor.ev",
        octopus_rate_entity="sensor.octopus_rate",
    )

    result = await scheduler.reconcile_tick(
        settings,
        _plan(replace(_INTENT, holds=())),
        now,
        trusted_holds=(),
    )

    [intent] = device.intents
    assert intent.hold_active(now) is True
    assert intent.hold_trusted is True
    assert result.trusted_holds == ((datetime(2026, 10, 3, 14, 0), datetime(2026, 10, 3, 15, 0)),)


@respx.mock
async def test_reconcile_skips_plug_and_rate_reads_without_planned_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state("off", []))
    ev_status = respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    plug = respx.get(_EV_PLUG_URL).mock(return_value=_ev_status_state("EV Connected"))
    rate = respx.get(_OCTOPUS_RATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.octopus_rate",
                "state": "0.20",
                "attributes": {"is_intelligent_adjusted": False},
            },
        )
    )

    await scheduler.reconcile_tick(
        _dispatch_settings(
            ev_plug_entity="sensor.ev_plug",
            ev_status_entity="sensor.ev",
            octopus_rate_entity="sensor.octopus_rate",
        ),
        _plan(replace(_INTENT, holds=())),
        datetime(2026, 10, 3, 14, 15),
        trusted_holds=(),
    )

    assert ev_status.call_count == 1
    assert plug.called is False
    assert rate.called is False


@respx.mock
async def test_a_dispatch_published_mid_slot_is_honoured_on_the_next_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published *and* started inside one slot: the next minute holds, without a replan."""
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    inside = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(
        return_value=_dispatch_state(
            "on",
            [{"start": "2026-06-10T22:00:00", "end": "2026-06-10T23:00:00"}],
            at=inside - timedelta(seconds=60),
        )
    )

    result = await scheduler.reconcile_tick(
        _dispatch_settings(), _plan(replace(_INTENT, holds=())), inside, trusted_holds=()
    )

    [intent] = device.intents
    assert intent.hold_active(inside) is True
    assert intent.hold_trusted is True
    assert result.trusted_holds == (_HOLD,)


@respx.mock
async def test_a_fresh_overnight_dispatch_is_not_a_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holds are controlled windows, not raw dispatches: switching the inverter
    ``Off`` during the cheap overnight charge would waste it."""
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    inside = datetime(2026, 6, 11, 1, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(
        return_value=_dispatch_state(
            "on",
            [{"start": "2026-06-11T01:00:00", "end": "2026-06-11T02:00:00"}],
            at=inside - timedelta(seconds=60),
        )
    )

    result = await scheduler.reconcile_tick(
        _dispatch_settings(), _plan(), inside, trusted_holds=()
    )

    [intent] = device.intents
    assert intent.holds == ()
    assert result.trusted_holds == ()


@pytest.mark.parametrize(
    "response", [httpx.Response(404), _dispatch_state("unavailable")], ids=["missing", "down"]
)
async def test_an_unreadable_dispatch_entity_per_minute_falls_back_to_the_trusted_holds(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    inside = datetime(2026, 6, 10, 22, 15)

    with respx.mock:
        respx.get(_DISPATCH_URL).mock(return_value=response)
        result = await scheduler.reconcile_tick(
            _dispatch_settings(), _plan(replace(_INTENT, holds=())), inside, trusted_holds=(_HOLD,)
        )

    [intent] = device.intents
    assert intent.hold_active(inside) is True
    assert result.trusted_holds == (_HOLD,)


@respx.mock
async def test_the_octopus_path_never_polls_kraken_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-minute Kraken polling would be 30x the rate Dave's own cap allows.

    The local HA dispatch entity may be read for its live state (#228); Kraken never is.
    """
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)

    async def forbidden_fetch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reconcile must not fetch planned dispatches from Kraken")

    monkeypatch.setattr(sources, "fetch_planned_dispatches", forbidden_fetch)
    route = respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state("off"))
    octopus = respx.route(url__startswith="http://octo.test").mock(
        return_value=httpx.Response(500)
    )
    now = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    plan = _plan(
        replace(
            _INTENT,
            holds=(_HOLD,),
            unrated_holds=(_HOLD,),
            octopus_dispatch_reported_at=now - timedelta(minutes=10),
        )
    )

    result = await scheduler.reconcile_tick(
        _dispatch_settings(
            tariff_provider="octopus_intelligent",
            octopus_api_url="http://octo.test/v1",
            octopus_api_key="sk_test",
            octopus_account_number="A-1234ABCD",
        ),
        plan,
        now,
        trusted_holds=(_HOLD,),
    )

    assert route.called
    assert not octopus.called
    assert device.intents == [plan.charge_intent]
    assert result.trusted_holds == (_HOLD,)


@respx.mock
async def test_octopus_rerates_a_dropped_dispatch_when_the_plug_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    start = datetime(2026, 10, 3, 14, 0, tzinfo=UTC)
    end = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state("off", []))
    respx.get(_EV_PLUG_URL).mock(return_value=_ev_status_state("EV Connected"))
    ev_status = respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    respx.get(_OCTOPUS_RATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.octopus_rate",
                "state": "0.20",
                "attributes": {"is_intelligent_adjusted": False},
            },
        )
    )
    intent = replace(
        _INTENT,
        holds=(),
        hold_trusted=True,
        unrated_holds=((start, end),),
        octopus_dispatch_reported_at=now - timedelta(minutes=10),
    )
    settings = _dispatch_settings(
        tariff_provider="octopus_intelligent",
        ev_plug_entity="sensor.ev_plug",
        ev_status_entity="sensor.ev",
        octopus_rate_entity="sensor.octopus_rate",
    )

    result = await scheduler.reconcile_tick(
        settings,
        _plan(intent),
        now,
        trusted_holds=(),
    )

    [fresh] = device.intents
    assert fresh.holds == ((start, end),)
    assert fresh.hold_active(now) is True
    assert result.trusted_holds == ((start, end),)
    assert ev_status.call_count == 1


@respx.mock
async def test_octopus_rerates_a_kept_dispatch_when_the_plug_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    start = datetime(2026, 10, 3, 14, 0, tzinfo=UTC)
    end = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state("off", []))
    plug = respx.get(_EV_PLUG_URL).mock(
        return_value=_ev_status_state(
            "EV Disconnected", at=datetime(2026, 10, 3, 14, 14, tzinfo=UTC)
        )
    )
    ev_status = respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    rate = respx.get(_OCTOPUS_RATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.octopus_rate",
                "state": "0.20",
                "attributes": {"is_intelligent_adjusted": False},
            },
        )
    )
    intent = replace(
        _INTENT,
        holds=((start, end),),
        hold_trusted=True,
        unrated_holds=((start, end),),
        octopus_dispatch_reported_at=now - timedelta(minutes=10),
    )
    settings = _dispatch_settings(
        tariff_provider="octopus_intelligent",
        ev_plug_entity="sensor.ev_plug",
        ev_status_entity="sensor.ev",
        octopus_rate_entity="sensor.octopus_rate",
    )

    result = await scheduler.reconcile_tick(
        settings,
        _plan(intent),
        now,
        trusted_holds=((start, end),),
    )

    [fresh] = device.intents
    assert fresh.holds == ()
    assert fresh.hold_active(now) is False
    assert result.trusted_holds == ()
    assert ev_status.call_count == 1
    assert plug.call_count == 1
    assert rate.call_count == 1


@pytest.mark.parametrize(
    "reported_ago", [timedelta(seconds=2701), None], ids=["stale", "missing"]
)
@respx.mock
async def test_octopus_does_not_rerate_a_plan_whose_kraken_read_is_not_fresh(
    monkeypatch: pytest.MonkeyPatch, reported_ago: timedelta | None
) -> None:
    """A trusted plan's Kraken picture expires; the reconcile never re-polls it.

    Without the age check an old plan's windows would keep being re-rated as
    trusted, so a dispatch Kraken later added could be released.
    """
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state("off", []))
    plug = respx.get(_EV_PLUG_URL).mock(return_value=_ev_status_state("EV Disconnected"))
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    rate = respx.get(_OCTOPUS_RATE_URL).mock(return_value=httpx.Response(404))
    start = datetime(2026, 10, 3, 14, 0, tzinfo=UTC)
    end = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    intent = replace(
        _INTENT,
        holds=(),
        hold_trusted=True,
        unrated_holds=(),
        octopus_dispatch_reported_at=None if reported_ago is None else now - reported_ago,
    )

    result = await scheduler.reconcile_tick(
        _dispatch_settings(
            tariff_provider="octopus_intelligent",
            ev_plug_entity="sensor.ev_plug",
            ev_status_entity="sensor.ev",
            octopus_rate_entity="sensor.octopus_rate",
        ),
        _plan(intent),
        now,
        trusted_holds=((start, end),),
    )

    [fresh] = device.intents
    assert fresh.hold_trusted is False
    assert fresh.hold_active(now) is True
    assert result.trusted_holds == ((start, end),)
    assert scheduler.STALE_DISPATCH_LINE in result.lines
    assert plug.called is False
    assert rate.called is False


@respx.mock
async def test_octopus_does_not_rerate_an_untrusted_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    plug = respx.get(_EV_PLUG_URL).mock(return_value=_ev_status_state("EV Connected"))
    rate = respx.get(_OCTOPUS_RATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.octopus_rate",
                "state": "0.20",
                "attributes": {"is_intelligent_adjusted": False},
            },
        )
    )
    ev_status = respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused"))
    start = datetime(2026, 10, 3, 14, 0)
    end = datetime(2026, 10, 3, 15, 0)
    plan = _plan(
        replace(
            _INTENT,
            holds=(),
            hold_trusted=False,
            unrated_holds=((start, end),),
        )
    )

    result = await scheduler.reconcile_tick(
        _dispatch_settings(
            tariff_provider="octopus_intelligent",
            ev_plug_entity="sensor.ev_plug",
            ev_status_entity="sensor.ev",
            octopus_rate_entity="sensor.octopus_rate",
        ),
        plan,
        datetime(2026, 10, 3, 14, 15),
        trusted_holds=(),
    )

    [fresh] = device.intents
    assert fresh.holds == ()
    assert fresh.hold_trusted is False
    assert result.trusted_holds == ()
    assert ev_status.call_count == 1
    assert plug.called is False
    assert rate.called is False


@respx.mock
async def test_an_unset_dispatch_entity_is_not_read_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    any_get = respx.route(method="GET").mock(return_value=httpx.Response(404))
    plan = _plan()

    result = await scheduler.reconcile_tick(
        _dispatch_settings(dispatch_entity=""), plan, datetime(2026, 6, 10, 22, 15)
    )

    assert not any_get.called
    assert device.intents == [plan.charge_intent]
    assert result.trusted_holds is None


@pytest.mark.parametrize("status", ["Charging", "Boosting", "Delivering"])
@respx.mock
async def test_active_ev_status_holds_the_inverter_without_a_dispatch(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state(status, at=now))

    await scheduler.reconcile_tick(
        _dispatch_settings(dispatch_entity="", ev_status_entity="sensor.ev"),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=scheduler.EvHoldState(),
    )

    [intent] = device.intents
    assert intent.hold_active(now) is True
    assert intent.ev_hold_active is True


@pytest.mark.parametrize("mode", ["on", "simulate"])
@respx.mock
async def test_ev_hold_uses_the_real_solis_switch_path(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    now = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Boosting", at=now))
    switch_url = "http://ha.test/api/states/select.solisac_power_switch"
    switch_reads = iter(("On", "Off"))
    respx.get(switch_url).mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={
                "entity_id": "select.solisac_power_switch",
                "state": next(switch_reads),
                "attributes": {},
            },
        )
    )
    switch_write = respx.post(
        "http://ha.test/api/services/select/select_option"
    ).mock(return_value=httpx.Response(200, json=[]))
    settings = _dispatch_settings(
        dispatch_entity="",
        ev_status_entity="sensor.ev",
        inverter_power_switch_entity="select.solisac_power_switch",
        authority_mode="optimise" if mode == "on" else "observe",
    )

    result = await scheduler.reconcile_tick(
        settings,
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=scheduler.EvHoldState(),
    )

    if mode == "on":
        assert switch_write.called is True
        assert json.loads(switch_write.calls[0].request.content) == {
            "entity_id": "select.solisac_power_switch",
            "option": "Off",
        }
    else:
        assert switch_write.called is False
        assert any(
            "[SIMULATE] would set inverter power switch to Off" in line
            for line in result.lines
        )


@respx.mock
async def test_ev_hold_does_not_invent_an_alphaess_hold_write() -> None:
    now = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    ev_status = respx.get(_EV_STATUS_URL).mock(
        return_value=_ev_status_state("Charging", at=now)
    )

    result = await scheduler.reconcile_tick(
        _dispatch_settings(
            inverter="alphaess",
            dispatch_entity="",
            ev_status_entity="sensor.ev",
        ),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=scheduler.EvHoldState(),
    )

    assert ev_status.called is True
    assert result.lines == []
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
async def test_run_once_applies_the_ev_hold_on_a_replan_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return cls(2026, 6, 10, 12, 0, tzinfo=tz)

    monkeypatch.setattr(scheduler, "datetime", _FrozenDatetime)
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    plan = _plan(replace(_INTENT, holds=()))

    async def fake_current_plan(_s: Settings, _rest: object, **_kw: object) -> object:
        return SimpleNamespace(
            plan=plan,
            inputs=SimpleNamespace(
                ev_hold_charging=True,
                evidence_freshness={"ev_status": _fresh(datetime(2026, 6, 10, 12, 0, tzinfo=UTC))},
                battery_direction="unknown",
            ),
            load_source="test",
            tz=None,
            forecast=LoadForecast(total_kwh=24.0, slots=None, source="test"),
        )

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(scheduler, "current_plan", fake_current_plan)
    monkeypatch.setattr(scheduler, "_record_forecast", noop)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", noop)
    switch_url = "http://ha.test/api/states/select.solisac_power_switch"
    switch_reads = iter(("On", "Off"))
    respx.get(switch_url).mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={
                "entity_id": "select.solisac_power_switch",
                "state": next(switch_reads),
                "attributes": {},
            },
        )
    )
    switch_write = respx.post(
        "http://ha.test/api/services/select/select_option"
    ).mock(return_value=httpx.Response(200, json=[]))
    settings = _dispatch_settings(
        dispatch_entity="",
        ev_status_entity="sensor.ev",
        inverter_power_switch_entity="select.solisac_power_switch",
        authority_mode="optimise",
    )

    await run_once(
        settings,
        previous_plan=plan,
        ev_hold_state=scheduler.EvHoldState(),
    )

    assert switch_write.called is True
    assert json.loads(switch_write.calls[0].request.content) == {
        "entity_id": "select.solisac_power_switch",
        "option": "Off",
    }


@respx.mock
async def test_ev_diversion_does_not_start_a_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Diverting"))
    now = datetime(2026, 6, 10, 22, 15)

    await scheduler.reconcile_tick(
        _dispatch_settings(dispatch_entity="", ev_status_entity="sensor.ev"),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=scheduler.EvHoldState(),
    )

    [intent] = device.intents
    assert intent.hold_active(now) is False


# --- #420: fresh-evidence hold policy and EV clearance (pure state machine) ---

def _fresh_hold(at: datetime, *, overnight_exception: bool = False) -> scheduler.EvHoldState:
    """An EV hold confirmed by fresh charging evidence at ``at``."""
    state = scheduler.next_ev_hold_state(
        scheduler.EvHoldState(), True, _fresh(at), overnight_exception=overnight_exception
    )
    assert state.active is True
    return state


def test_ev_hold_releases_on_the_fifth_advancing_clear_report() -> None:
    started = datetime(2026, 6, 10, 22, 0)
    state = _fresh_hold(started)

    for index in range(1, 5):
        state = scheduler.next_ev_hold_state(
            state, False, _fresh(started + timedelta(minutes=index))
        )
        assert state.active is True
        assert state.clear_count == index

    state = scheduler.next_ev_hold_state(state, False, _fresh(started + timedelta(minutes=5)))
    assert state.active is False
    assert state.clear_count == 0
    assert state.last_clear_report_at is None


def test_repeated_gets_of_one_clear_report_never_release() -> None:
    """Five reads of one cached report are one report, not five distinct ones."""
    started = datetime(2026, 6, 10, 22, 0)
    state = _fresh_hold(started)

    for _ in range(8):
        state = scheduler.next_ev_hold_state(state, False, _fresh(started + timedelta(minutes=1)))
        assert state.active is True
        assert state.clear_count == 1
        assert state.last_clear_report_at == started + timedelta(minutes=1)


def test_a_backwards_clear_report_resets_the_clearance_run() -> None:
    started = datetime(2026, 6, 10, 22, 0)
    state = _fresh_hold(started)
    state = scheduler.next_ev_hold_state(state, False, _fresh(started + timedelta(minutes=3)))
    assert state.clear_count == 1

    state = scheduler.next_ev_hold_state(state, False, _fresh(started + timedelta(minutes=2)))
    assert state.active is True
    assert state.clear_count == 0
    assert state.last_clear_report_at is None

    # The run restarts from the backwards reset, not from where it left off.
    for index in range(1, 5):
        state = scheduler.next_ev_hold_state(
            state, False, _fresh(started + timedelta(minutes=10 + index))
        )
        assert state.active is True
    state = scheduler.next_ev_hold_state(
        state, False, _fresh(started + timedelta(minutes=16))
    )
    assert state.active is False


@pytest.mark.parametrize("validity", ["stale", "future", "unknown"])
@pytest.mark.parametrize("charging", [True, False, None], ids=["charging", "clear", "unreadable"])
def test_bad_evidence_never_starts_and_never_releases_a_hold(
    validity: str, charging: bool | None
) -> None:
    """Unknown, stale or future evidence is no evidence (Q5): no start, no release."""
    at = datetime(2026, 6, 10, 22, 0)
    inactive = scheduler.next_ev_hold_state(
        scheduler.EvHoldState(), charging, _fresh(at, validity)
    )
    assert inactive.active is False
    assert inactive.clear_count == 0

    active = _fresh_hold(at - timedelta(minutes=30))
    for _ in range(3):  # a partial clearance run that bad evidence must reset
        active = scheduler.next_ev_hold_state(
            active, False, _fresh(at - timedelta(minutes=10))
        )
    assert active.clear_count == 1
    held = scheduler.next_ev_hold_state(active, charging, _fresh(at, validity))
    assert held.active is True
    assert held.clear_count == 0
    assert held.last_clear_report_at is None


def test_fresh_charging_resets_the_clearance_run() -> None:
    started = datetime(2026, 6, 10, 22, 0)
    state = _fresh_hold(started)
    state = scheduler.next_ev_hold_state(state, False, _fresh(started + timedelta(minutes=1)))
    state = scheduler.next_ev_hold_state(state, False, _fresh(started + timedelta(minutes=2)))
    assert state.clear_count == 2

    state = scheduler.next_ev_hold_state(state, True, _fresh(started + timedelta(minutes=3)))
    assert state.active is True
    assert state.clear_count == 0
    assert state.last_clear_report_at is None

    for index in range(4, 8):
        state = scheduler.next_ev_hold_state(
            state, False, _fresh(started + timedelta(minutes=index))
        )
        assert state.active is True
    state = scheduler.next_ev_hold_state(state, False, _fresh(started + timedelta(minutes=8)))
    assert state.active is False


def test_a_new_ev_hold_is_suppressed_by_the_overnight_exception() -> None:
    """Q4: inside a verified resident window with no discharge, no *new* hold."""
    state = scheduler.next_ev_hold_state(
        scheduler.EvHoldState(), True, _fresh(datetime(2026, 6, 11, 1, 15)),
        overnight_exception=True,
    )
    assert state.active is False
    assert state.clear_count == 0


def test_a_confirmed_ev_hold_survives_the_overnight_exception() -> None:
    """Q4: the exception stops only a *new* hold; a confirmed one clears only
    by five advancing clear reports or an explicit handover."""
    started = datetime(2026, 6, 10, 12, 0)
    state = _fresh_hold(started)
    state = scheduler.next_ev_hold_state(
        state, True, _fresh(started + timedelta(hours=13)), overnight_exception=True
    )
    assert state.active is True


@pytest.mark.parametrize(
    ("window", "at", "validity", "direction", "expected"),
    [
        ((time(23, 30), time(5, 30)), "01:15", "fresh", "idle", True),
        ((time(23, 30), time(5, 30)), "01:15", "fresh", "charging", True),
        ((time(23, 30), time(5, 30)), "01:15", "fresh", "discharging", False),
        ((time(23, 30), time(5, 30)), "01:15", "fresh", "unknown", False),
        ((time(23, 30), time(5, 30)), "01:15", "stale", "idle", False),
        ((time(23, 30), time(5, 30)), "01:15", "unknown", "idle", False),
        (None, "01:15", "fresh", "idle", False),
        ((time(23, 30), time(5, 30)), "12:15", "fresh", "idle", False),
        ((time(0, 0), time(0, 0)), "01:15", "fresh", "idle", False),
    ],
)
def test_ev_hold_overnight_exception_requires_a_resident_window_and_no_discharge(
    window: tuple[time, time] | None,
    at: str,
    validity: str,
    direction: str,
    expected: bool,
) -> None:
    """Unknown window or direction, or discharge, means the hold applies."""
    hour, minute = (int(part) for part in at.split(":"))
    now = datetime(2026, 6, 11, hour, minute, tzinfo=UTC)
    assert (
        scheduler.ev_hold_overnight_exception(now, window, validity, direction) is expected
    )


# --- #420: fresh-evidence hold policy, driven through the reconcile/plan seams ---

_SWITCH_URL = "http://ha.test/api/states/select.solisac_power_switch"
_SWITCH_WRITE_URL = "http://ha.test/api/services/select/select_option"
_CLOCK_URL = "http://ha.test/api/states/sensor.solis_control_inverter_clock"
_RESIDENT_FIELDS = (
    "timed_charge_start_hours",
    "timed_charge_start_minutes",
    "timed_charge_end_hours",
    "timed_charge_end_minutes",
)


def _mock_power_switch(monkeypatch: pytest.MonkeyPatch, initial: str = "On") -> dict[str, str]:
    """A Solis power switch that reads back what was last written to it."""
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    state = {"option": initial}

    def read(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "entity_id": "select.solisac_power_switch",
            "state": state["option"],
            "attributes": {},
        })

    def write(request: httpx.Request) -> httpx.Response:
        state["option"] = json.loads(request.content)["option"]
        return httpx.Response(200, json=[])

    respx.get(_SWITCH_URL).mock(side_effect=read)
    respx.post(_SWITCH_WRITE_URL).mock(side_effect=write)
    return state


def _restore_schedule_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the release's schedule check as passed (#424; tested in test_verified_hold_release)."""

    async def confirmed(*_args: object) -> tuple[list[object], None]:
        return [], None

    monkeypatch.setattr(SolisDevice, "_confirm_restore_schedule", confirmed)


def _switch_writes() -> list[httpx.Response]:
    return [call for call in respx.calls if call.request.method == "POST"]


def _mock_resident_window(
    start: time, end: time, *, at: datetime | None = None, clock_at: datetime | None = None,
) -> None:
    """The resident slot-1 charge window as #418 reads it back (GETs only).

    ``at`` is every field's ``last_reported``; ``clock_at`` mocks the hub's
    inverter-clock sibling (#444).
    """
    values = dict(zip(
        _RESIDENT_FIELDS, (start.hour, start.minute, end.hour, end.minute), strict=True
    ))
    for field, value in values.items():
        body: dict[str, object] = {
            "entity_id": f"sensor.solis_control_{field}",
            "state": str(value),
            "attributes": {},
        }
        if at is not None:
            body["last_reported"] = at.isoformat()
        respx.get(f"http://ha.test/api/states/sensor.solis_control_{field}").mock(
            return_value=httpx.Response(200, json=body)
        )
    if clock_at is not None:
        respx.get(_CLOCK_URL).mock(return_value=httpx.Response(200, json={
            "entity_id": "sensor.solis_control_inverter_clock",
            "state": "2026-06-11 01:15:00",
            "attributes": {},
            "last_reported": clock_at.isoformat(),
        }))


def _mock_battery_power(state: str, at: datetime) -> None:
    respx.get("http://ha.test/api/states/sensor.battery_power").mock(
        return_value=httpx.Response(200, json={
            "entity_id": "sensor.battery_power",
            "state": state,
            "attributes": {"unit_of_measurement": "W"},
            "last_reported": at.isoformat(),
        })
    )


def _ev_hold_settings(**kw: object) -> Settings:
    base: dict[str, object] = dict(
        dispatch_entity="",
        ev_status_entity="sensor.ev",
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    base.update(kw)
    return _dispatch_settings(**base)


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@respx.mock
async def test_fresh_ev_charging_report_holds_and_writes_the_switch_off(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    now = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Charging", at=now))

    result = await scheduler.reconcile_tick(
        _ev_hold_settings(authority_mode=mode),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=scheduler.EvHoldState(),
    )

    if mode == "observe":
        assert switch["option"] == "On"
        assert any(
            "[SIMULATE] would set inverter power switch to Off" in line for line in result.lines
        )
        assert all(call.request.method == "GET" for call in respx.calls)
    else:
        assert switch["option"] == "Off"
        assert any(
            "[APPLIED] set inverter power switch to Off" in line for line in result.lines
        )
        assert all(
            call.request.url.path != "/api/services/modbus/write_register"
            for call in respx.calls
        )


def _ev_report(reports: dict[str, object]) -> httpx.Response:
    """The EV status entity as its source last reported it (a cached GET)."""
    at = reports["at"]
    return _ev_status_state(str(reports["state"]), at=at if at else None)


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@respx.mock
async def test_five_advancing_clear_reports_release_the_ev_hold(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Reuse the store, not a newly seeded empty authority on every report.
    settings = _ev_hold_settings(authority_mode=mode)
    started = datetime(2026, 6, 10, 22, 0, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    _restore_schedule_confirmed(monkeypatch)
    reports: dict[str, object] = {"state": "Charging", "at": started}
    respx.get(_EV_STATUS_URL).mock(side_effect=lambda _r: _ev_report(reports))
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        settings,
        _plan(replace(_INTENT, holds=())),
        started,
        ev_hold_state=ev_state,
    )
    assert ev_state.active is True
    if mode != "observe":
        assert switch["option"] == "Off"

    for minute in range(1, 5):  # four advancing clear reports: not enough
        reports["state"] = "Paused"
        reports["at"] = started + timedelta(minutes=minute)
        await scheduler.reconcile_tick(
            settings,
            _plan(replace(_INTENT, holds=())),
            started + timedelta(minutes=minute),
            ev_hold_state=ev_state,
        )
        assert ev_state.active is True
        assert ev_state.clear_count == minute

    reports["at"] = started + timedelta(minutes=5)  # the fifth advancing report
    await scheduler.reconcile_tick(
        settings,
        _plan(replace(_INTENT, holds=())),
        started + timedelta(minutes=5),
        ev_hold_state=ev_state,
    )
    assert ev_state.active is False
    if mode == "observe":
        assert all(call.request.method == "GET" for call in respx.calls)
    else:
        assert switch["option"] == "On"
        assert [json.loads(w.request.content)["option"] for w in _switch_writes()] == [
            "Off", "On",
        ]


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@respx.mock
async def test_repeated_gets_of_one_clear_report_never_release_the_ev_hold(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """One cached report, however often it is read back, is one report (Q5)."""
    started = datetime(2026, 6, 10, 22, 0, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    reports: dict[str, object] = {"state": "Charging", "at": started}
    respx.get(_EV_STATUS_URL).mock(side_effect=lambda _r: _ev_report(reports))
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        _ev_hold_settings(authority_mode=mode),
        _plan(replace(_INTENT, holds=())),
        started,
        ev_hold_state=ev_state,
    )
    assert ev_state.active is True

    reports["state"] = "Paused"
    reports["at"] = started + timedelta(minutes=1)
    # Eight passes over one cached report, each still inside its max age:
    # equal report times advance nothing, so the hold never releases.
    for tick in range(8):
        await scheduler.reconcile_tick(
            _ev_hold_settings(authority_mode=mode),
            _plan(replace(_INTENT, holds=())),
            started + timedelta(minutes=1, seconds=10 * tick),
            ev_hold_state=ev_state,
        )
        assert ev_state.active is True
        assert ev_state.clear_count == 1

    assert all(call.request.method == "GET" for call in respx.calls) or switch["option"] == "Off"


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@pytest.mark.parametrize(
    "evidence", ["stale", "future", "unavailable"], ids=["stale", "future", "down"]
)
@respx.mock
async def test_bad_ev_evidence_starts_no_hold_and_writes_nothing_off(
    monkeypatch: pytest.MonkeyPatch, mode: str, evidence: str
) -> None:
    """Unknown, stale or future evidence never starts a hold (#409 11/27)."""
    now = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    if evidence == "stale":
        respx.get(_EV_STATUS_URL).mock(
            return_value=_ev_status_state("Charging", at=now - timedelta(minutes=10))
        )
    elif evidence == "future":
        respx.get(_EV_STATUS_URL).mock(
            return_value=_ev_status_state("Charging", at=now + timedelta(minutes=5))
        )
    else:
        respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("unavailable"))
    ev_state = scheduler.EvHoldState()

    result = await scheduler.reconcile_tick(
        _ev_hold_settings(authority_mode=mode),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=ev_state,
    )

    assert ev_state.active is False
    assert switch["option"] == "On"
    assert all(
        call.request.url.path != "/api/services/modbus/write_register" for call in respx.calls
    )
    assert not any("power switch to Off" in line for line in result.lines if "[SKIP]" not in line)


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@pytest.mark.parametrize(
    "evidence", ["stale", "future", "unavailable"], ids=["stale", "future", "down"]
)
@respx.mock
async def test_bad_ev_evidence_releases_nothing(
    monkeypatch: pytest.MonkeyPatch, mode: str, evidence: str
) -> None:
    """An active hold stays active on unknown, stale or future evidence (Q5)."""
    started = datetime(2026, 6, 10, 22, 0, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    reports: dict[str, object] = {"state": "Charging", "at": started}
    respx.get(_EV_STATUS_URL).mock(side_effect=lambda _r: _ev_report(reports))
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        _ev_hold_settings(authority_mode=mode),
        _plan(replace(_INTENT, holds=())),
        started,
        ev_hold_state=ev_state,
    )
    assert ev_state.active is True

    if evidence == "stale":
        # A clear report last made a minute ago: older than max age at every
        # later tick, so it is stale evidence and never a release.
        respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state(
            "Paused", at=started + timedelta(minutes=1)
        ))
        # ticks below start at +7 min: a 300 s max age is inclusive, so the
        # +6 min tick still reads the report as fresh.
    elif evidence == "future":
        respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state(
            "Paused", at=started + timedelta(hours=1)
        ))
    else:
        respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("unavailable"))

    for minute in range(7, 12):
        result = await scheduler.reconcile_tick(
            _ev_hold_settings(authority_mode=mode),
            _plan(replace(_INTENT, holds=())),
            started + timedelta(minutes=minute),
            ev_hold_state=ev_state,
        )
        assert ev_state.active is True
        assert ev_state.clear_count == 0

    assert not any("power switch to On" in line for line in result.lines)
    if mode == "observe":
        assert all(call.request.method == "GET" for call in respx.calls)
    else:
        assert switch["option"] == "Off"


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@respx.mock
async def test_overnight_exception_blocks_a_new_hold_inside_a_resident_window(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Q4: fresh charging inside a verified resident window, no discharge."""
    now = datetime(2026, 6, 11, 1, 15, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch, initial="Off")
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Charging", at=now))
    _mock_resident_window(time(23, 30), time(5, 30), at=now, clock_at=now)
    _mock_battery_power("-2851", now)
    ev_state = scheduler.EvHoldState()

    result = await scheduler.reconcile_tick(
        _ev_hold_settings(
            authority_mode=mode,
            battery_power_entity="sensor.battery_power",
            battery_power_discharge_sign="positive",
        ),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=ev_state,
    )

    assert ev_state.active is False
    if mode == "observe":
        assert all(call.request.method == "GET" for call in respx.calls)
        assert any(
            "[SIMULATE] would set inverter power switch to On" in line for line in result.lines
        )
    else:
        # No owned record: the overnight exception must not release owner-Off.
        assert switch["option"] == "Off"
        assert _switch_writes() == []


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@pytest.mark.parametrize(
    ("scenario", "discharging"),
    [
        ("discharging", "100"),
        ("idle-but-stale", "-2851"),
        ("direction-unavailable", "0.5"),
        ("no-direction-entity", None),
        ("window-unknown", "-2851"),
    ],
)
@respx.mock
async def test_overnight_exception_holds_when_direction_or_window_is_not_verified(
    monkeypatch: pytest.MonkeyPatch, mode: str, scenario: str, discharging: str | None
) -> None:
    """Missing, stale or unknown direction, or discharge: the hold applies (Q4)."""
    now = datetime(2026, 6, 11, 1, 15, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Charging", at=now))
    if scenario != "window-unknown":
        _mock_resident_window(time(23, 30), time(5, 30))
    else:
        for field in _RESIDENT_FIELDS:  # the resident read fails: not verified
            respx.get(f"http://ha.test/api/states/sensor.solis_control_{field}").mock(
                return_value=httpx.Response(404)
            )
    settings_kw: dict[str, object] = {"battery_power_discharge_sign": "positive"}
    if scenario == "discharging":
        _mock_battery_power(discharging, now)
        settings_kw["battery_power_entity"] = "sensor.battery_power"
    elif scenario == "idle-but-stale":
        _mock_battery_power(discharging, now - timedelta(minutes=10))
        settings_kw["battery_power_entity"] = "sensor.battery_power"
    elif scenario == "direction-unavailable":
        respx.get("http://ha.test/api/states/sensor.battery_power").mock(
            return_value=httpx.Response(200, json={
                "entity_id": "sensor.battery_power",
                "state": discharging,
                "attributes": {"unit_of_measurement": "kW"},
                "last_reported": now.isoformat(),
            })
        )
        settings_kw["battery_power_entity"] = "sensor.battery_power"
    elif scenario == "no-direction-entity":
        pass  # no battery_power_entity: the direction evidence is missing
    ev_state = scheduler.EvHoldState()

    result = await scheduler.reconcile_tick(
        _ev_hold_settings(authority_mode=mode, **settings_kw),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=ev_state,
    )

    assert ev_state.active is True
    if mode == "observe":
        assert all(call.request.method == "GET" for call in respx.calls)
        assert any(
            "[SIMULATE] would set inverter power switch to Off" in line for line in result.lines
        )
    else:
        assert switch["option"] == "Off"
        assert any(
            "[APPLIED] set inverter power switch to Off" in line for line in result.lines
        )


_WINDOW_NOW = datetime(2026, 6, 11, 1, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("window_at", "clock_at", "excepted"),
    [
        (_WINDOW_NOW - timedelta(minutes=4), None, True),  # own reports fresh
        (_WINDOW_NOW - timedelta(hours=5), _WINDOW_NOW - timedelta(seconds=11), True),
        (_WINDOW_NOW - timedelta(hours=5), _WINDOW_NOW - timedelta(minutes=10), False),
        (_WINDOW_NOW - timedelta(hours=5), None, False),  # no clock sibling
        (_WINDOW_NOW - timedelta(hours=13), _WINDOW_NOW - timedelta(seconds=11), False),
        (_WINDOW_NOW + timedelta(minutes=5), _WINDOW_NOW, False),  # future-dated
        (None, _WINDOW_NOW, False),  # no last_reported at all
    ],
)
@respx.mock
async def test_overnight_exception_needs_fresh_resident_window_reports(
    monkeypatch: pytest.MonkeyPatch,
    window_at: datetime | None, clock_at: datetime | None, excepted: bool,
) -> None:
    """#444: a cached window is not read back; the clock sibling may vouch up to 12 h."""
    now = _WINDOW_NOW
    _mock_power_switch(monkeypatch)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Charging", at=now))
    _mock_resident_window(time(23, 30), time(5, 30), at=window_at, clock_at=clock_at)
    if clock_at is None:
        respx.get(_CLOCK_URL).mock(return_value=httpx.Response(404))
    _mock_battery_power("-2851", now)
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        _ev_hold_settings(
            authority_mode="observe",
            battery_power_entity="sensor.battery_power",
            battery_power_discharge_sign="positive",
        ),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=ev_state,
    )

    assert ev_state.active is not excepted


@respx.mock
async def test_overnight_exception_caps_own_window_reports_at_12_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#444: a max-age above 12 h never makes a 13 h old window field fresh."""
    now = _WINDOW_NOW
    _mock_power_switch(monkeypatch)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Charging", at=now))
    _mock_resident_window(time(23, 30), time(5, 30), at=now - timedelta(hours=13), clock_at=now)
    _mock_battery_power("-2851", now)
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        _ev_hold_settings(
            authority_mode="observe",
            battery_power_entity="sensor.battery_power",
            battery_power_discharge_sign="positive",
            battery_power_max_age_seconds=86400,
        ),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=ev_state,
    )

    assert ev_state.active is True


@pytest.mark.parametrize("bad", ["unavailable", "future", "clock-unavailable"])
@respx.mock
async def test_overnight_exception_off_when_one_window_report_is_unusable(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """#444: one unavailable or future-dated field, or a dead clock, means no exception."""
    now = _WINDOW_NOW
    stale = now - timedelta(hours=2)
    _mock_power_switch(monkeypatch)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Charging", at=now))
    _mock_resident_window(time(23, 30), time(5, 30), at=stale, clock_at=now)
    field = f"http://ha.test/api/states/sensor.solis_control_{_RESIDENT_FIELDS[2]}"
    if bad == "unavailable":
        respx.get(field).mock(return_value=httpx.Response(200, json={
            "entity_id": "x", "state": "unavailable", "attributes": {},
            "last_reported": now.isoformat(),
        }))
    elif bad == "future":
        respx.get(field).mock(return_value=httpx.Response(200, json={
            "entity_id": "x", "state": "5", "attributes": {},
            "last_reported": (now + timedelta(minutes=2)).isoformat(),
        }))
    else:
        respx.get(_CLOCK_URL).mock(return_value=httpx.Response(200, json={
            "entity_id": "x", "state": "unavailable", "attributes": {},
            "last_reported": now.isoformat(),
        }))
    _mock_battery_power("-2851", now)
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        _ev_hold_settings(
            authority_mode="observe",
            battery_power_entity="sensor.battery_power",
            battery_power_discharge_sign="positive",
        ),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=ev_state,
    )

    assert ev_state.active is True


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@respx.mock
async def test_a_confirmed_ev_hold_persists_into_the_resident_window(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Q4: the exception stops only a *new* hold; a confirmed one keeps going."""
    midday = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    night = datetime(2026, 6, 11, 1, 15, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    reports: dict[str, object] = {"state": "Charging", "at": midday}
    respx.get(_EV_STATUS_URL).mock(side_effect=lambda _r: _ev_report(reports))
    _mock_resident_window(time(23, 30), time(5, 30))
    settings = _ev_hold_settings(
        authority_mode=mode,
        battery_power_entity="sensor.battery_power",
        battery_power_discharge_sign="positive",
    )
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=())), midday, ev_hold_state=ev_state
    )
    assert ev_state.active is True
    if mode != "observe":
        assert switch["option"] == "Off"

    reports["at"] = night
    _mock_battery_power("-2851", night)
    await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=())), night, ev_hold_state=ev_state
    )

    assert ev_state.active is True
    if mode == "observe":
        assert all(call.request.method == "GET" for call in respx.calls)
    else:
        assert switch["option"] == "Off"
        assert [json.loads(w.request.content)["option"] for w in _switch_writes()] == ["Off"]


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe"])
@respx.mock
async def test_invalid_soc_never_blocks_the_ev_hold_switch_write(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """An invalid SoC blocks charging writes, never the discharge-only hold."""
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    at = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    failed = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=at,
        value=30.0,
        raw_state="30",
        reported_at=at - timedelta(hours=1),
        age_s=3600.0,
        max_age_s=600.0,
    )
    plan = _plan(ChargeIntent(
        target_soc_pct=77.0, soc=failed, window_start=time(23, 30), window_end=time(5, 30),
    ))

    async def fake_current_plan(_s: Settings, _rest: object, **_kw: object) -> object:
        return SimpleNamespace(
            plan=plan,
            inputs=SimpleNamespace(
                ev_hold_charging=True,
                evidence_freshness={"ev_status": _fresh(at)},
                battery_direction="unknown",
            ),
            load_source="test",
            tz=None,
            forecast=LoadForecast(total_kwh=24.0, slots=None, source="test"),
        )

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(scheduler, "current_plan", fake_current_plan)
    monkeypatch.setattr(scheduler, "_record_forecast", noop)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", noop)
    switch = _mock_power_switch(monkeypatch)

    result = await run_once(
        _ev_hold_settings(authority_mode=mode),
        ev_hold_state=scheduler.EvHoldState(),
    )

    assert any("[BLOCKED]" in line and "not charging to" in line for line in result.action_lines)
    if mode == "observe":
        assert switch["option"] == "On"
        assert all(call.request.method == "GET" for call in respx.calls)
        assert any(
            "[SIMULATE] would set inverter power switch to Off" in line
            for line in result.action_lines
        )
    else:
        assert switch["option"] == "Off"
        assert any(
            "[APPLIED] set inverter power switch to Off" in line for line in result.action_lines
        )


@respx.mock
async def test_a_stale_dispatch_entity_read_falls_back_to_the_last_trusted_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#420: a cached dispatch picture is untrusted; the last trusted set stands."""
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    trusted = (
        (datetime(2026, 10, 3, 14, 0, tzinfo=UTC), datetime(2026, 10, 3, 15, 0, tzinfo=UTC)),
    )
    respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state(
        "off", [{"start": "2026-10-03T14:00:00", "end": "2026-10-03T15:00:00"}],
        at=now - timedelta(minutes=10),
    ))
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused", at=now))

    result = await scheduler.reconcile_tick(
        _dispatch_settings(ev_status_entity="sensor.ev"),
        _untrusted_plan(),
        now,
        trusted_holds=trusted,
    )

    [intent] = device.intents
    assert intent.holds == trusted
    assert intent.hold_active(now) is True
    assert result.trusted_holds == trusted
    assert any(scheduler.STALE_DISPATCH_LINE in line for line in result.lines)


@respx.mock
async def test_a_stale_dispatch_read_never_becomes_a_trusted_empty_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A readable-but-stale entity with no dispatches is not a no-holds picture."""
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
    trusted = (
        (datetime(2026, 10, 3, 14, 0, tzinfo=UTC), datetime(2026, 10, 3, 15, 0, tzinfo=UTC)),
    )
    respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state(
        "off", [], at=now - timedelta(minutes=10)
    ))
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused", at=now))

    result = await scheduler.reconcile_tick(
        _dispatch_settings(ev_status_entity="sensor.ev"),
        _untrusted_plan(),
        now,
        trusted_holds=trusted,
    )

    [intent] = device.intents
    assert intent.hold_active(now) is True
    assert result.trusted_holds == trusted


@respx.mock
async def test_advancing_sibling_reports_release_an_unchanged_clear_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling liveness proves the source is live; its reports advance the run."""
    started = datetime(2026, 6, 10, 22, 0, tzinfo=UTC)
    switch = _mock_power_switch(monkeypatch)
    _restore_schedule_confirmed(monkeypatch)
    ev: dict[str, object] = {"state": "Charging", "at": started - timedelta(minutes=10)}
    sibling: dict[str, datetime] = {"at": started - timedelta(seconds=60)}
    respx.get(_EV_STATUS_URL).mock(side_effect=lambda _r: _ev_report(ev))
    respx.get("http://ha.test/api/states/sensor.ev_live").mock(
        side_effect=lambda _r: httpx.Response(200, json={
            "entity_id": "sensor.ev_live",
            "state": "ok",
            "attributes": {},
            "last_reported": sibling["at"].isoformat(),
        })
    )
    settings = _ev_hold_settings(
        authority_mode="optimise", ev_liveness_entity="sensor.ev_live"
    )
    ev_state = scheduler.EvHoldState()

    await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=())), started, ev_hold_state=ev_state
    )
    assert ev_state.active is True
    assert switch["option"] == "Off"

    ev["state"] = "Paused"  # the EV status itself never reports again
    for second in range(50, 11, -10):  # five advancing sibling reports
        sibling["at"] = started - timedelta(seconds=second)
        await scheduler.reconcile_tick(
            settings, _plan(replace(_INTENT, holds=())), started, ev_hold_state=ev_state
        )
        assert ev_state.active is True

    sibling["at"] = started - timedelta(seconds=5)
    await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=())), started, ev_hold_state=ev_state
    )

    assert ev_state.active is False
    assert switch["option"] == "On"
    assert [json.loads(w.request.content)["option"] for w in _switch_writes()] == ["Off", "On"]


@respx.mock
async def test_trusted_ev_hold_prevents_relinquishing_after_dispatch_hold_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 6, 10, 23, 15, tzinfo=UTC)
    respx.get(_DISPATCH_URL).mock(return_value=httpx.Response(404))
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Boosting", at=now))

    result = await scheduler.reconcile_tick(
        _dispatch_settings(ev_status_entity="sensor.ev"),
        _untrusted_plan(),
        now,
        trusted_holds=(
            (datetime(2026, 6, 10, 22, 0, tzinfo=UTC), datetime(2026, 6, 10, 23, 0, tzinfo=UTC)),
        ),
        ev_hold_state=scheduler.EvHoldState(),
    )

    assert result.relinquished is False
    assert device.safe_states == 0
    assert len(device.reconciled) == 1
    assert device.reconciled[0].ev_hold_active is True
    assert result.trusted_holds == (
        (datetime(2026, 6, 10, 22, 0, tzinfo=UTC), datetime(2026, 6, 10, 23, 0, tzinfo=UTC)),
    )


@pytest.mark.parametrize("failure", ["404", "exception", "unavailable", "unknown"])
@respx.mock
async def test_untrusted_ev_status_never_relinquishes_after_dispatch_end(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    if failure == "exception":
        respx.get(_EV_STATUS_URL).mock(side_effect=httpx.ConnectError("offline"))
    elif failure in {"unavailable", "unknown"}:
        respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state(failure))
    else:
        respx.get(_EV_STATUS_URL).mock(return_value=httpx.Response(404))
    respx.get(_DISPATCH_URL).mock(return_value=httpx.Response(404))
    now = datetime(2026, 6, 10, 23, 15, tzinfo=UTC)
    ev_state = scheduler.EvHoldState(active=True)

    result = await scheduler.reconcile_tick(
        _dispatch_settings(ev_status_entity="sensor.ev"),
        _untrusted_plan(),
        now,
        trusted_holds=(
            (datetime(2026, 6, 10, 22, 0, tzinfo=UTC), datetime(2026, 6, 10, 23, 0, tzinfo=UTC)),
        ),
        ev_hold_state=ev_state,
    )

    assert result.trusted_holds == (
        (datetime(2026, 6, 10, 22, 0, tzinfo=UTC), datetime(2026, 6, 10, 23, 0, tzinfo=UTC)),
    )
    assert result.relinquished is False
    assert device.safe_states == 0
    assert device.reconciled == []
    # #420: unreadable EV evidence is no evidence, so an active hold stays
    # active instead of the old unreadable-status-clears-the-hold rule.
    assert ev_state.active is True


@respx.mock
async def test_octopus_tariff_still_reads_ev_status_each_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    dispatch = respx.get(_DISPATCH_URL).mock(return_value=_dispatch_state("off"))
    now = datetime(2026, 6, 10, 22, 15, tzinfo=UTC)
    ev_status = respx.get(_EV_STATUS_URL).mock(
        return_value=_ev_status_state("Charging", at=now)
    )

    await scheduler.reconcile_tick(
        _dispatch_settings(
            tariff_provider="octopus_intelligent",
            dispatch_entity="binary_sensor.dispatch",
            ev_status_entity="sensor.ev",
        ),
        _plan(replace(_INTENT, holds=())),
        now,
        ev_hold_state=scheduler.EvHoldState(),
    )

    assert dispatch.called is False
    assert ev_status.called is True
    assert device.intents[0].hold_active(now) is True


@respx.mock
async def test_reconcile_does_not_request_an_unset_ev_status_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    any_get = respx.route(method="GET").mock(return_value=httpx.Response(404))

    await scheduler.reconcile_tick(
        _dispatch_settings(dispatch_entity="", ev_status_entity=""),
        _plan(replace(_INTENT, holds=())),
        datetime(2026, 6, 10, 22, 15),
        ev_hold_state=scheduler.EvHoldState(),
    )

    assert not any_get.called


async def test_an_ev_status_entity_down_for_hours_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    now = datetime(2026, 6, 10, 22, 15)

    with respx.mock, caplog.at_level("INFO"):
        respx.get(_EV_STATUS_URL).mock(return_value=httpx.Response(404))
        first = await scheduler.reconcile_tick(
            _dispatch_settings(dispatch_entity="", ev_status_entity="sensor.ev"),
            _plan(replace(_INTENT, holds=())),
            now,
            ev_hold_state=scheduler.EvHoldState(),
        )
        await scheduler.reconcile_tick(
            _dispatch_settings(dispatch_entity="", ev_status_entity="sensor.ev"),
            _plan(replace(_INTENT, holds=())),
            now + timedelta(minutes=1),
            previous=first.lines,
            ev_hold_state=scheduler.EvHoldState(),
        )

    warnings = [
        record
        for record in caplog.records
        if record.name.startswith("ha_spark") and "EV status unreadable" in record.getMessage()
    ]
    assert len(warnings) == 1


@respx.mock
async def test_an_inactive_ev_status_does_not_cancel_a_dispatch_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _IntentRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Diverting"))
    now = datetime(2026, 6, 10, 22, 15)

    await scheduler.reconcile_tick(
        _dispatch_settings(dispatch_entity="", ev_status_entity="sensor.ev"),
        _plan(replace(_INTENT, holds=(_HOLD,))),
        now,
        trusted_holds=(_HOLD,),
        ev_hold_state=scheduler.EvHoldState(),
    )

    [intent] = device.intents
    assert intent.hold_active(now) is True
    assert intent.holds == (_HOLD,)


async def test_run_forever_keeps_the_holds_a_per_minute_read_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconcile_saw: list[object] = []

    async def fake_run_once(_s: Settings, **_kw: object) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        reconcile_saw.append(trusted_holds)
        return scheduler.ReconcileResult([], (_HOLD,))

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 1),  # the fresh read sees a dispatch
            datetime(2026, 6, 10, 22, 2),
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert reconcile_saw == [(), (_HOLD,)]


async def test_a_dispatch_entity_down_for_hours_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A renamed or removed entity must not put 1440 warnings a day into the log."""
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: _IntentRecordingDevice())
    now = datetime(2026, 6, 10, 22, 15)

    with respx.mock, caplog.at_level("INFO"):
        respx.get(_DISPATCH_URL).mock(return_value=httpx.Response(404))
        first = await scheduler.reconcile_tick(
            _dispatch_settings(), _plan(), now, trusted_holds=(_HOLD,)
        )
        await scheduler.reconcile_tick(
            _dispatch_settings(), _plan(), now, previous=first.lines, trusted_holds=(_HOLD,)
        )

    about_dispatch = [
        r for r in caplog.records
        if r.name.startswith("ha_spark") and "dispatch" in r.getMessage()
    ]
    assert len(about_dispatch) == 1


# --- #143 §5: relinquishing control writes the safe state, once ---


class _RelinquishRecordingDevice:
    def __init__(self) -> None:
        self.reconciled: list[ChargeIntent] = []
        self.safe_states = 0
        self.safe_windows = 0

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return 0.0

    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[str]:
        self.reconciled.append(intent)
        return ["[WARNING] set inverter power switch to On refused: state unreadable"]

    async def write_safe_state(self) -> list[str]:
        self.safe_states += 1
        return ["[APPLIED] set inverter power switch to On (relinquishing control)"]

    async def write_safe_window(self) -> list[str]:
        self.safe_windows += 1
        return ["[APPLIED] set charge window 23:30-05:30 (relinquishing control)"]


async def test_untrusted_reads_inside_a_known_hold_never_relinquish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)

    result = await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"),
        _untrusted_plan(),
        datetime(2026, 6, 10, 22, 15),
        trusted_holds=(_HOLD,),
    )

    assert device.safe_states == 0
    assert result.trusted_holds == (_HOLD,)


async def test_untrusted_past_every_known_hold_end_relinquishes_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    later_hold = (datetime(2026, 6, 10, 21, 0), datetime(2026, 6, 10, 21, 30))

    with caplog.at_level("WARNING", logger="ha_spark.energy.scheduler"):
        first = await scheduler.reconcile_tick(
            settings,
            _untrusted_plan(),
            datetime(2026, 6, 10, 23, 0),  # exactly the last known end
            trusted_holds=(later_hold, _HOLD),
        )
    second = await scheduler.reconcile_tick(
        settings,
        _untrusted_plan(),
        datetime(2026, 6, 10, 23, 1),
        previous=first.lines,
        trusted_holds=first.trusted_holds,
    )

    assert device.safe_states == 1
    assert first.trusted_holds == ()
    assert any("relinquishing control" in r.getMessage() for r in caplog.records)
    # Once relinquished there is nothing left to steer; the next pass is a plain one.
    assert second.trusted_holds == ()
    assert len(device.reconciled) == 1


async def test_untrusted_with_no_known_holds_never_relinquishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    settings = Settings(ha_url="http://ha.test", ha_token="t")

    for trusted in (None, ()):
        await scheduler.reconcile_tick(
            settings, _untrusted_plan(), datetime(2026, 6, 10, 23, 5), trusted_holds=trusted
        )

    assert device.safe_states == 0


async def test_a_trusted_tick_past_every_hold_end_never_relinquishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)

    result = await scheduler.reconcile_tick(
        Settings(ha_url="http://ha.test", ha_token="t"),
        _plan(replace(_INTENT, holds=(_HOLD,))),
        datetime(2026, 6, 10, 23, 5),
        trusted_holds=(_HOLD,),
    )

    assert device.safe_states == 0
    assert len(device.reconciled) == 1
    assert result.trusted_holds == (_HOLD,)


async def _run_forever_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    device: _RelinquishRecordingDevice,
    *,
    plan: ChargePlan | None,
    shutdown_at: datetime = datetime(2026, 6, 10, 23, 5),
    authority_mode: str = "optimise",
    holds: tuple[HoldRecord, ...] = (),
) -> list[tuple[object, ...]]:
    """One slot tick, then a cancel (as SIGTERM delivers it) with ``shutdown_at`` left
    on the clock for the ``finally``. Returns the notifications sent."""
    notices: list[tuple[object, ...]] = []

    async def notify(*args: object) -> None:
        notices.append(args)

    monkeypatch.setattr(scheduler, "send_notification", notify)

    async def fake_run_once(_s: Settings, **_kw: object) -> scheduler.RunResult:
        if plan is None:
            raise RuntimeError("no plan")
        if holds:
            # Recorded during the run, so the startup reconcile has not seen them,
            # on the configured identity (#425 refuses a remapped one).
            inverter = _s.devices[0]
            identity = HoldDevice(
                driver=inverter.driver, power_switch=inverter.entities.get("power_switch", ""),
                hub=_s.solis_control_hub, slave=_s.solis_modbus_slave,
            )
            execution_context().authority.save_holds(
                tuple(h.model_copy(update={"device": identity}) for h in holds)
            )
        return scheduler.RunResult(plan, plan)

    async def cancelled_sample_signals(_s: Settings, _now: datetime) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", cancelled_sample_signals)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    _patch_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0), shutdown_at])

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        authority_mode=authority_mode,
    )
    with pytest.raises(asyncio.CancelledError):
        await run_forever(s, poll_seconds=0)
    return notices


async def test_run_forever_writes_the_safe_state_once_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _RelinquishRecordingDevice()
    await _run_forever_until_cancelled(
        monkeypatch, device, plan=_plan(replace(_INTENT, holds=(_HOLD,)))
    )

    assert device.safe_states == 1


async def test_run_forever_without_a_plan_has_nothing_to_relinquish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _RelinquishRecordingDevice()
    await _run_forever_until_cancelled(monkeypatch, device, plan=None)

    assert device.safe_states == 0


async def test_shutdown_inside_a_known_hold_leaves_the_hold_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart mid-dispatch must not drain the battery into the car (#143 §1).

    Same rule as the per-minute relinquish: only past every known hold end. The
    next process picks the hold up within a minute.
    """
    device = _RelinquishRecordingDevice()
    notices = await _run_forever_until_cancelled(
        monkeypatch,
        device,
        plan=_plan(replace(_INTENT, holds=(_HOLD,))),
        shutdown_at=datetime(2026, 6, 10, 22, 20),
    )

    assert device.safe_states == 0
    assert device.safe_windows == 1
    assert len(notices) == 1 and "dispatch" in str(notices[0][3])


@pytest.mark.parametrize("pending_release", [False, True])
async def test_shutdown_with_an_ev_hold_keeps_the_switch_off_and_restores_the_window(
    monkeypatch: pytest.MonkeyPatch, pending_release: bool,
) -> None:
    """#426: Optimise + EV hold, no dispatch: window only, and a notice naming why.

    A release still pending has not verified, so its record holds too."""
    device = _RelinquishRecordingDevice()
    ev = HoldRecord(
        cause="ev", started_at=datetime(2026, 6, 10, 21, 0, tzinfo=UTC), confirmed=True,
        pending_release=pending_release,
        device=HoldDevice(driver="solis", power_switch="select.x", hub="h", slave=1),
    )
    notices = await _run_forever_until_cancelled(
        monkeypatch, device, plan=_plan(), holds=(ev,)
    )

    assert (device.safe_states, device.safe_windows) == (0, 1)
    assert len(notices) == 1 and "EV" in str(notices[0][3])


@pytest.mark.parametrize("mode", ["protect", "observe"])
async def test_shutdown_outside_optimise_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    device = _RelinquishRecordingDevice()
    notices = await _run_forever_until_cancelled(
        monkeypatch, device, plan=_plan(replace(_INTENT, holds=(_HOLD,))),
        authority_mode=mode,
    )

    assert (device.safe_states, device.safe_windows) == (0, 0)
    assert notices == []


async def test_a_relinquish_is_reported_so_the_loop_can_reapply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _RelinquishRecordingDevice()
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: device)
    settings = Settings(ha_url="http://ha.test", ha_token="t")

    fired = await scheduler.reconcile_tick(
        settings, _untrusted_plan(), datetime(2026, 6, 10, 23, 5), trusted_holds=(_HOLD,)
    )
    plain = await scheduler.reconcile_tick(
        settings, _untrusted_plan(), datetime(2026, 6, 10, 22, 15), trusted_holds=(_HOLD,)
    )

    assert fired.relinquished is True
    assert plain.relinquished is False


async def test_run_forever_reapplies_the_plan_after_a_relinquish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safe state overwrote Slot 1; an unchanged plan must not skip restoring it."""
    previous_seen: list[object] = []
    relinquish_at = datetime(2026, 6, 10, 22, 1)

    async def fake_run_once(_s: Settings, **kw: object) -> scheduler.RunResult:
        previous_seen.append(kw.get("previous_plan"))
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        return scheduler.ReconcileResult([], trusted_holds, relinquished=now == relinquish_at)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_a: _RelinquishRecordingDevice())
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            relinquish_at,
            datetime(2026, 6, 10, 22, 30),  # forced re-apply
            datetime(2026, 6, 10, 23, 0),  # back to the ordinary diff
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert previous_seen[0] is None
    assert previous_seen[1] is None
    assert previous_seen[2] is not None


# --- #147: a failing replan must not starve the per-minute reconcile ---


async def test_run_forever_reconciles_every_tick_while_the_replan_keeps_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing replan must not starve the clock seam.

    ``last_run_slot`` only advances on a successful run, so while ``run_once``
    keeps raising *every* later tick is a replan tick. Without a fallback pass
    neither path reconciles, and a dispatch hold that ends meanwhile leaves the
    inverter power switch ``Off`` — the house on grid import — for as long as
    the failures last (#147).
    """
    reconciled: list[datetime] = []

    async def fake_run_once(_s: Settings, **_kw: object) -> scheduler.RunResult:
        raise RuntimeError("HA unreachable")

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        reconciled.append(now)
        return scheduler.ReconcileResult([], trusted_holds)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    ticks = [
        datetime(2026, 6, 10, 22, 30),  # the replan raises
        datetime(2026, 6, 10, 22, 31),  # still a replan tick: the slot never completed
        datetime(2026, 6, 10, 22, 32),
    ]
    stop = _patch_loop(monkeypatch, ticks)

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert reconciled == ticks


async def test_a_failed_replans_fallback_pass_uses_the_last_plan_and_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback pass steers by the last good plan and its trusted holds.

    ``last_plan`` still carries the hold's known end time, so the clock is
    served even though a fresh plan could not be computed.
    """
    trusted = _plan(replace(_INTENT, holds=(_HOLD,)))
    outcomes: list[ChargePlan | Exception] = [trusted, RuntimeError("HA unreachable")]
    reconciled: list[tuple[ChargePlan | None, object]] = []

    async def fake_run_once(_s: Settings, **_kw: object) -> scheduler.RunResult:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return scheduler.RunResult(outcome, outcome)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        reconciled.append((plan, trusted_holds))
        return scheduler.ReconcileResult([], trusted_holds)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),  # the replan succeeds; `run_once` reconciles
            datetime(2026, 6, 10, 22, 30),  # the replan raises; the fallback pass runs
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert reconciled == [(trusted, (_HOLD,))]


async def test_run_forever_reapplies_when_a_fallback_pass_relinquishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relinquish is a relinquish whichever pass made it.

    The safe state overwrote Slot 1, so the next replan to succeed must apply
    even an unchanged setpoint (#143 §5) — the fallback pass cannot drop that.
    """
    previous_seen: list[object] = []
    fail_at = datetime(2026, 6, 10, 22, 30)
    outcomes: list[ChargePlan | Exception] = [
        _plan(), RuntimeError("HA unreachable"), _plan()
    ]

    async def fake_run_once(_s: Settings, **kw: object) -> scheduler.RunResult:
        previous_seen.append(kw.get("previous_plan"))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return scheduler.RunResult(outcome, outcome)

    async def fake_reconcile_tick(
        _s: Settings,
        plan: ChargePlan | None,
        now: datetime,
        *,
        previous: list[str] | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.ReconcileResult:
        return scheduler.ReconcileResult([], trusted_holds, relinquished=now == fail_at)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "reconcile_tick", fake_reconcile_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),  # succeeds
            fail_at,  # raises; the fallback pass relinquishes
            datetime(2026, 6, 10, 23, 0),  # forced re-apply
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert previous_seen[0] is None
    assert previous_seen[2] is None


@pytest.mark.parametrize(("enabled", "expected"), [(True, 1), (False, 0)])
async def test_run_forever_syncs_the_inverter_clock_at_a_clock_change_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, expected: int
) -> None:
    """#161: the first pass after the household offset changes runs the sync."""
    from zoneinfo import ZoneInfo

    from ha_spark.energy.dst_clock_sync import DstClockSync

    london = ZoneInfo("Europe/London")
    runs: list[datetime] = []

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def record_run(self: DstClockSync, _s: Settings, _rest: object, now: datetime) -> None:
        runs.append(now)
        self.handled = self.due(now)

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(DstClockSync, "run", record_run)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 10, 25, 0, 59, tzinfo=UTC).astimezone(london),  # BST
            datetime(2026, 10, 25, 1, 0, tzinfo=UTC).astimezone(london),  # GMT
            datetime(2026, 10, 25, 1, 1, tzinfo=UTC).astimezone(london),
        ],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", inverter_clock_dst_sync=enabled
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert len(runs) == expected


# --- recovery from an active Solis fallback (#117) ---


@pytest.mark.parametrize("inverter", ["solis", "alphaess"])
@respx.mock
async def test_restored_pending_failure_blocks_normal_run_until_checked_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inverter: str
) -> None:
    (tmp_path / "ha_spark_soc_monitor.json").write_text(
        '{"consecutive_failures": 1, "fallback_status": "pending_failure"}',
        encoding="utf-8",
    )
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    checked = _passing_at(0)
    observations = iter([None, checked])

    async def fake_monitor_tick(
        _settings: Settings, monitor: SocMonitor, _on_observed: object = None,
    ) -> SocMeasurement | None:
        measurement = next(observations)
        if measurement is not None:
            monitor.record(measurement, failure_threshold=3)
        return measurement

    monkeypatch.setattr(scheduler, "soc_monitor_tick", fake_monitor_tick)
    states: list[bool] = []
    socs: list[SocMeasurement | None] = []
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 30)],
        run_once_socs=socs,
        fallback_states=states,
    )
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc", inverter=inverter, v2l_power_entity="",
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert states == [True, False]
    assert socs == [None, checked]


_REC_T0 = datetime(2026, 6, 10, 1, 0, tzinfo=UTC)


def _passing_at(minute: int) -> SocMeasurement:
    """A passing measurement observed and reported ``minute`` min after 01:00."""
    at = _REC_T0 + timedelta(minutes=minute)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=at,
        value=40.0 + minute,
        raw_state=str(40.0 + minute),
        reported_at=at,
        age_s=0.0,
        max_age_s=600.0,
    )


class _RecoveryFallbackDevice(SolisDevice):
    async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
        return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

    async def reconcile_holds(self, *_args: object, **_kwargs: object) -> list[str]:
        return []

    async def write_safe_state(self) -> list[str]:
        return []


def _patch_recovery_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minutes: list[int],
    apply_results: list[list[str] | Exception],
    *,
    authority_mode: str = "optimise",
) -> tuple[type[Exception], list[dict[str, object]], Settings]:
    """A loop resumed in persisted verified fallback, one passing SoC per tick.

    ``apply_results`` is consumed only by runs that apply (fallback not held).
    """
    (tmp_path / "ha_spark_soc_monitor.json").write_text(
        json.dumps({"consecutive_failures": 3, "fallback_status": "fallback_verified"}),
        encoding="utf-8",
    )
    measurements = iter([_passing_at(m) for m in minutes])

    async def fake_observe(
        _s: Settings, _rest: HomeAssistantRest, *, monitor: SocMonitor | None = None
    ) -> SocMeasurement:
        return next(measurements)

    runs: list[dict[str, object]] = []
    results = iter(apply_results)

    async def fake_run_once(
        _s: Settings,
        *,
        soc: SocMeasurement | None = None,
        previous_plan: ChargePlan | None = None,
        trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
        ev_hold_state: scheduler.EvHoldState | None = None,
        fallback_active: bool = False,
    ) -> scheduler.RunResult:
        runs.append({"soc": soc, "previous": previous_plan, "held": fallback_active})
        plan = _plan(replace(_INTENT, soc=soc or _INTENT.soc))
        if fallback_active:
            return scheduler.RunResult(plan, None)
        result = next(results)
        if isinstance(result, Exception):
            raise result
        failed = any(line.startswith(("[FAILED]", "[BLOCKED]")) for line in result)
        from ha_spark.devices.outcomes import legacy_action_line

        return scheduler.RunResult(
            plan, None if failed else plan, tuple(legacy_action_line(line) for line in result)
        )

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "observe_soc", fake_observe)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(_RecoveryFallbackDevice, settings, rest),
    )
    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 2, m) for m in minutes])
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc",
        soc_recovery_minutes=2,
        solis_fallback_current_a=42.0,
        v2l_power_entity="",
    )
    seed_authority(settings, authority_mode)
    return stop, runs, settings


def _monitor_file(tmp_path: Path) -> object:
    return json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text())


@respx.mock
async def test_alphaess_threshold_recovers_only_with_fresh_verified_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path, monkeypatch, [0, 1, 2, 3],
        [["[APPLIED] AlphaESS charge plan read-back verified"]],
    )
    (tmp_path / "ha_spark_soc_monitor.json").write_text(
        json.dumps({"consecutive_failures": 3, "fallback_status": "fallback_threshold"})
    )
    settings = ConfigSettings.model_validate(
        {**settings.model_dump(), "inverter": "alphaess", "devices": []}
    )

    def forbid_fallback(_monitor: SocMonitor) -> None:
        pytest.fail("AlphaESS requested fallback")

    monkeypatch.setattr(SocMonitor, "request_fallback", forbid_fallback)

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert [run["held"] for run in runs] == [True, False]
    assert runs[1]["previous"] is None
    assert runs[1]["soc"] is not runs[0]["soc"]
    assert runs[1]["soc"] == _passing_at(2)
    assert [state for state, _ in _integrity_posts()] == [
        "fallback_threshold", "fallback_threshold", "fallback_threshold", "normal", "normal"
    ]
    assert _integrity_posts()[2][1]["recovery_ready"] is True
    assert _monitor_file(tmp_path) == {"consecutive_failures": 0, "fallback_status": "normal"}
    assert not any("fallback" in call.request.url.path for call in respx.calls)


@respx.mock
async def test_alphaess_never_requests_fallback_from_failure_through_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path, monkeypatch, [0, 1, 2, 3], [["[APPLIED] AlphaESS charge plan"]]
    )
    (tmp_path / "ha_spark_soc_monitor.json").unlink()
    settings = ConfigSettings.model_validate({
        **settings.model_dump(), "inverter": "alphaess", "devices": [],
        "soc_failure_threshold": 1,
    })
    observations = iter([
        replace(_passing_at(0), status=SocStatus.STALE),
        _passing_at(1), _passing_at(2), _passing_at(3),
    ])

    async def observe(
        _settings: Settings, _rest: HomeAssistantRest, *, monitor: SocMonitor | None = None
    ) -> SocMeasurement:
        return next(observations)

    def forbid_fallback(_monitor: SocMonitor) -> None:
        pytest.fail("AlphaESS requested fallback")

    monkeypatch.setattr(scheduler, "observe_soc", observe)
    monkeypatch.setattr(SocMonitor, "request_fallback", forbid_fallback)
    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert [run["held"] for run in runs] == [True, False]
    assert runs[1]["soc"] is not runs[0]["soc"]
    assert _monitor_file(tmp_path) == {"consecutive_failures": 0, "fallback_status": "normal"}


@respx.mock
async def test_alphaess_simulated_recovery_does_not_claim_hardware_was_programmed(
    tmp_path: Path,
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    monitor = SocMonitor(path=tmp_path / "ha_spark_soc_monitor.json")
    monitor.record(replace(_passing_at(0), status=SocStatus.STALE), failure_threshold=1)
    for minute in (1, 2, 3):
        monitor.record(
            _passing_at(minute), failure_threshold=1,
            recovery_duration=timedelta(minutes=2), recover_without_fallback=True,
        )
    assert monitor.recovery_ready
    settings = Settings(ha_url="http://ha.test", ha_token="t", inverter="alphaess")

    await scheduler.finish_recovery(
        settings, monitor, ("[SIMULATE] would charge to 80%",)
    )

    action = _integrity_posts()[-1][1]["recovery_action"]
    assert action.startswith("[RECOVERED] plan computed without hardware programming:")


@respx.mock
async def test_loop_recovers_with_a_fresh_plan_only_after_stable_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path,
        monkeypatch,
        [0, 1, 2, 3],
        [["[APPLIED] set timed charge current to 20 A over 3.5 h of the window"]],
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    # 02:00 is a slot run held by the fallback; 02:02 is the recovery run; 02:03
    # is back to normal with no slot due.
    assert [run["held"] for run in runs] == [True, False]
    recovery = runs[1]
    assert recovery["previous"] is None  # never compared against a rejected plan
    soc = recovery["soc"]
    assert isinstance(soc, SocMeasurement) and soc.observed_at == _passing_at(2).observed_at
    states = [state for state, _ in _integrity_posts()]
    assert states == ["fallback_verified"] * 3 + ["normal", "normal"]
    restored_attrs = _integrity_posts()[0][1]
    assert restored_attrs["restored_state"] is True
    assert restored_attrs["fresh_observation"] is True
    assert restored_attrs["fallback_confirmed"] is True
    _, attrs = _integrity_posts()[3]
    assert attrs["recovery_action"] == (
        "[RECOVERED] normal programming resumed: "
        "[APPLIED] set timed charge current to 20 A over 3.5 h of the window"
    )
    assert attrs["consecutive_failures"] == 0
    assert _integrity_posts()[2][1]["recovery_ready"] is True
    assert _monitor_file(tmp_path) == {"consecutive_failures": 0, "fallback_status": "normal"}


@respx.mock
async def test_loop_keeps_fallback_until_the_recovered_apply_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[FAILED], a [WARNING] mismatch, and a raise all keep the fallback.

    (#423) Each failed recovered apply re-applies the fallback on the same
    tick and starts the five-minute backoff, so the next recovery attempt
    waits instead of retrying every minute; the fourth attempt verifies and
    leaves the fallback.
    """
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path,
        monkeypatch,
        [0, 1, 2, 7, 12, 17],
        [
            ["[FAILED] set timed charge current to 20 A"],
            ["[WARNING] set timed charge current to 20 A, but read back 18 A"],
            RuntimeError("modbus timeout"),
            ["[APPLIED] set timed charge current to 21 A over 3.4 h of the window"],
        ],
    )
    applied_at: list[str] = []

    class ReapplyWitnessDevice(_RecoveryFallbackDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            applied_at.append(soc.observed_at.isoformat())
            return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(ReapplyWitnessDevice, settings, rest),
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    # 02:00 is a slot run held by the fallback; each recovery attempt that
    # fails (02:02, 02:07, 02:12) re-applies the fallback on the same tick and
    # pushes the next attempt five minutes out; 02:17 verifies and exits.
    assert [run["held"] for run in runs] == [True, False, False, False, False]
    socs = [run["soc"] for run in runs[1:]]
    assert [s.observed_at for s in socs if isinstance(s, SocMeasurement)] == [
        _passing_at(m).observed_at for m in (2, 7, 12, 17)
    ]
    # The restored fallback is confirmed once (02:00), then re-applied on
    # every failed recovered apply's own tick; the verified attempt applies none.
    assert applied_at == [_passing_at(m).observed_at.isoformat() for m in (0, 2, 7, 12)]
    posts = _integrity_posts()
    states = [state for state, _ in posts]
    # One post per tick, plus the re-apply verdict post on each failed
    # attempt's tick (02:02, 02:07, 02:12) and the transition post (02:17).
    assert states == ["fallback_verified"] * 9 + ["normal"]
    reapply_posts = [posts[3], posts[5], posts[7]]
    assert all(attrs["fallback_confirmed"] is True for _, attrs in reapply_posts)
    assert all(
        str(attrs["fallback_action"]).startswith("[FALLBACK]") for _, attrs in reapply_posts
    )
    assert posts[-1][1]["recovery_action"].startswith("[RECOVERED] normal programming resumed")
    assert _monitor_file(tmp_path) == {"consecutive_failures": 0, "fallback_status": "normal"}


@respx.mock
async def test_partial_recovered_apply_reapplies_fallback_same_tick_then_waits_five_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#423) A partly-failed recovered apply re-applies and re-verifies the
    fallback on the same tick, publishes the verdict, and no recovery attempt
    runs before +5 minutes, where exactly one runs and completes recovery."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path,
        monkeypatch,
        [0, 1, 2, 3, 4, 5, 6, 7],
        [
            ["[FAILED] set timed charge current to 20 A"],
            ["[APPLIED] set timed charge current to 21 A over 3.4 h of the window"],
        ],
    )
    applied_at: list[str] = []

    class ReapplyWitnessDevice(_RecoveryFallbackDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            applied_at.append(soc.observed_at.isoformat())
            return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(ReapplyWitnessDevice, settings, rest),
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    # 02:00 held by the fallback; 02:02 is the failed recovery attempt; no
    # attempt at +1..+4 minutes (02:03-02:06); exactly one at +5 (02:07).
    assert [run["held"] for run in runs] == [True, False, False]
    socs = [run["soc"] for run in runs[1:]]
    assert [s.observed_at for s in socs if isinstance(s, SocMeasurement)] == [
        _passing_at(m).observed_at for m in (2, 7)
    ]
    # The fallback was re-applied on the failed attempt's own tick (02:02),
    # through the same read-back-verified device action, and not again after.
    assert applied_at == [_passing_at(m).observed_at.isoformat() for m in (0, 2)]
    posts = _integrity_posts()
    states = [state for state, _ in posts]
    # One post per tick plus the same-tick re-apply verdict (02:02) and the
    # recovery transition (02:07).
    assert states == ["fallback_verified"] * 9 + ["normal"]
    # 02:02's second post is the re-apply: verified again, published at once.
    state, attrs = posts[3]
    assert state == "fallback_verified"
    assert attrs["fallback_confirmed"] is True
    assert attrs["fresh_observation"] is True
    assert str(attrs["fallback_action"]).startswith("[FALLBACK]")
    assert posts[-1][1]["recovery_action"].startswith("[RECOVERED] normal programming resumed")
    # The fallback cleared only after the verified recovered apply: store
    # cleared, epoch bumped, exactly one exit notice.
    from ha_spark.authority import load_authority

    result = load_authority(settings)
    assert result.state is not None
    assert result.state.fallback.active is False
    assert result.state.fallback.causes == ()
    assert result.state.epoch == 4
    assert len(_fallback_notices("ha-spark fallback entered")) == 1
    assert len(_fallback_notices("ha-spark fallback left")) == 1
    assert _monitor_file(tmp_path) == {"consecutive_failures": 0, "fallback_status": "normal"}


@respx.mock
async def test_failed_fallback_reapply_stays_active_unconfirmed_and_retries_every_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#423) A re-apply that fails leaves the fallback active and unconfirmed
    in the monitor and the store, publishes the [FAILED] verdict, and is
    retried every minute of the backoff; no recovery attempt runs meanwhile."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path,
        monkeypatch,
        [0, 1, 2, 3, 4],
        [["[FAILED] set timed charge current to 20 A"]],
    )
    applied_at: list[str] = []
    results = [
        "[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified",
        "[FAILED] Solis fallback at 42 A unconfirmed: read back 40 A",
    ]

    class FailingReapplyDevice(_RecoveryFallbackDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            applied_at.append(soc.observed_at.isoformat())
            return [results.pop(0)] if len(results) > 1 else [results[0]]

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(FailingReapplyDevice, settings, rest),
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    # 02:00 confirms the restored fallback; 02:02's failed recovered apply
    # re-applies on the same tick and fails; the backoff retries it every
    # minute (02:03, 02:04) while no recovery attempt runs.
    assert [run["held"] for run in runs] == [True, False]
    assert applied_at == [_passing_at(m).observed_at.isoformat() for m in (0, 2, 3, 4)]
    posts = _integrity_posts()
    # 02:02's second post publishes the failed re-apply: unconfirmed, [FAILED].
    state, attrs = posts[3]
    assert state == "fallback_failed"
    assert attrs["fallback_confirmed"] is False
    assert attrs["fresh_observation"] is True
    assert str(attrs["fallback_action"]).startswith("[FAILED]")
    # Every later post stays in the failed, unconfirmed fallback.
    for post_state, post_attrs in posts[4:]:
        assert post_state == "fallback_failed"
        assert post_attrs["fallback_confirmed"] is False
    assert _monitor_file(tmp_path) == {
        "consecutive_failures": 3, "fallback_status": "fallback_failed"
    }
    from ha_spark.authority import load_authority

    result = load_authority(settings)
    assert result.state is not None
    assert result.state.fallback.active is True
    assert result.state.fallback.causes == ("soc_integrity",)
    assert _fallback_notices("ha-spark fallback left") == []


@pytest.mark.parametrize("authority_mode", ["observe", "protect"])
@respx.mock
async def test_reapply_during_recovery_never_actuates_without_charging_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority_mode: str
) -> None:
    """(#423) Observe and Protect never request the fallback re-apply write:
    a failed recovered apply there keeps today's retry-next-minute behaviour
    with zero hardware-actuating calls."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path,
        monkeypatch,
        [0, 1, 2, 3],
        [
            ["[FAILED] set timed charge current to 20 A"],
            ["[FAILED] set timed charge current to 20 A"],
        ],
        authority_mode=authority_mode,
    )
    applied_at: list[str] = []

    class NeverReapplyDevice(_RecoveryFallbackDevice):
        async def apply_fallback(self, soc: SocMeasurement) -> list[str]:
            applied_at.append(soc.observed_at.isoformat())
            return ["[FALLBACK] Solis fallback at 42 A in 23:30-05:30 read-back verified"]

    monkeypatch.setattr(
        scheduler,
        "inverter_device",
        lambda settings, rest: _fake_solis_device(NeverReapplyDevice, settings, rest),
    )

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    # No charging authority: the fallback write is never requested, and the
    # failed recovered apply retries the next minute as before.
    assert applied_at == []
    assert [run["held"] for run in runs] == [True, False, False]
    assert _hardware_service_calls() == []
    assert all(state == "fallback_verified" for state, _ in _integrity_posts())
    assert _monitor_file(tmp_path)["fallback_status"] == "fallback_verified"


@respx.mock
async def test_loop_completes_recovery_in_simulate_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate claims nothing on hardware, so there is no fallback to retain."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(
        tmp_path,
        monkeypatch,
        [0, 1, 2],
        [["[SIMULATE] would set timed charge current to 20 A"]],
        authority_mode="observe",
    )
    from ha_spark.devices.execution import execution_context

    assert execution_context().authority.state.committed_mode == "observe"

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert [run["held"] for run in runs] == [True, False]
    assert _monitor_file(tmp_path) == {"consecutive_failures": 0, "fallback_status": "normal"}


@respx.mock
async def test_loop_failure_during_recovery_restarts_the_stability_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop, runs, settings = _patch_recovery_loop(tmp_path, monkeypatch, [0, 1, 2], [])
    stale = replace(_passing_at(1), status=SocStatus.STALE)
    measurements = iter([_passing_at(0), stale, _passing_at(2)])

    async def fake_observe(
        _s: Settings, _rest: HomeAssistantRest, *, monitor: SocMonitor | None = None
    ) -> SocMeasurement:
        return next(measurements)

    monkeypatch.setattr(scheduler, "observe_soc", fake_observe)

    with pytest.raises(stop):
        await run_forever(settings, poll_seconds=0)

    assert [run["held"] for run in runs] == [True]
    assert all(state == "fallback_verified" for state, _ in _integrity_posts())
    assert _integrity_posts()[-1][1]["recovery_ready"] is False


@pytest.mark.parametrize(
    ("settings_kw", "host", "peer"),
    [
        ({"supervisor_token": "x"}, "0.0.0.0", "172.30.32.2"),  # add-on: proxy-only
        ({"ha_url": "http://ha.test", "ha_token": "t"}, "127.0.0.1", None),  # dev: loopback
    ],
)
async def test_run_forever_restricts_the_ingress_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    settings_kw: dict[str, str],
    host: str,
    peer: str | None,
) -> None:
    """Add-on mode answers only the ingress proxy; standalone binds loopback only."""
    for module in ("ha_spark.authority", "ha_spark.daemon_channel", "ha_spark.settings_service"):
        monkeypatch.setattr(f"{module}.resolve_data_dir", lambda _: tmp_path)
    captured: dict[str, object] = {}

    class _Bound(Exception):
        pass

    def capture_build_app(_state: object, **kwargs: object) -> object:
        captured["peer"] = kwargs.get("ingress_peer")
        return object()

    def capture_make_server(_app: object, bind_host: str, port: int) -> object:
        captured["host"], captured["port"] = bind_host, port
        raise _Bound  # stop before the loop touches HA

    monkeypatch.setattr(scheduler, "build_app", capture_build_app)
    monkeypatch.setattr(scheduler, "make_server", capture_make_server)

    with pytest.raises(_Bound):
        await run_forever(Settings(**settings_kw), poll_seconds=0)  # type: ignore[arg-type]
    assert captured == {"peer": peer, "host": host, "port": scheduler.INGRESS_PORT}


@respx.mock
async def test_soc_verdict_is_published_before_the_tick_awaits_its_side_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(#481) The display verdict changes as soon as the observation is checked,
    not after the fallback mirroring and integrity publishing that follow it."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    _soc_get(["50"], [datetime.now(UTC) - timedelta(hours=1)])
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "ledger.db"),
        soc_entity="sensor.soc", v2l_power_entity="",
    )
    seen: list[bool] = []
    verdicts_at_mirror: list[list[bool]] = []

    async def mirror(*args: object, **kwargs: object) -> None:
        verdicts_at_mirror.append(list(seen))

    monkeypatch.setattr(scheduler, "_mirror_fallback_authority", mirror)
    monitor = SocMonitor(path=tmp_path / "ha_spark_soc_monitor.json")
    measurement = await scheduler.soc_monitor_tick(
        settings, monitor, lambda observed: seen.append(observed.ok),
    )
    assert measurement is not None and not measurement.ok
    assert verdicts_at_mirror == [[False]]
