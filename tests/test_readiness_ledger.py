"""Readiness evidence is immutable, time-indexed and read-only to hardware."""
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import (
    ExportIntent,
    FlexibilityEvent,
    LoadForecast,
    PlannerConfig,
    PlannerInputs,
)
from ha_spark.energy.plan_run import PlanRun
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.readiness_ledger import PlanRevision, ReadinessLedger, record_slot
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.tariff import FixedTariffProvider

START = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)


def revision(name: str = "A", price: float = 0.1) -> PlanRevision:
    cfg = PlannerConfig(10, 50, 10, 100, 60, 1, time(0), time(6))
    soc = SocMeasurement(status=SocStatus.OK, observed_at=START, value=50, age_s=0)
    inputs = PlannerInputs(soc, 0, 10, load_slots=(0.2,) * 48,
                           horizon_start=START, now=START)
    schedule = FixedTariffProvider(price, 0.3, 0.05).schedule(inputs, cfg)
    run = PlanRun(compute_plan(inputs, cfg, schedule), inputs, cfg, schedule,
                  "median of 21 days", LoadForecast(10, None, "median of 21 days"))
    return PlanRevision(name, START, run, 1000, 3)


async def test_slot_revision_and_rates_are_write_once(tmp_path):
    path = str(tmp_path / "ledger.db")
    a, b = revision(), revision("B", 0.2)
    await record_slot(path, START, a, "observe", a.run.inputs.soc)
    await record_slot(path, START + timedelta(minutes=10), b, "observe", b.run.inputs.soc)
    await record_slot(path, START, b, "observe", b.run.inputs.soc)
    await record_slot(path, START + timedelta(minutes=30), b, "protect", b.run.inputs.soc)
    rows = await ReadinessLedger(path).slots(START, START + timedelta(hours=1))
    assert [r.revision_id for r in rows] == ["A", "B"]
    assert [r.import_rate for r in rows] == [0.1, 0.2]
    assert rows[0].start_energy_kwh == 5
    assert rows[1].operating_mode == "protect"


async def test_previous_revision_uses_matching_instant_and_missing_horizon_is_gap(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    schedule = replace(a.run.schedule, prices=(0.1, 0.25) + (0.3,) * 46)
    a = replace(a, run=replace(a.run, schedule=schedule))
    await record_slot(
        path, START + timedelta(minutes=30), a, "observe",
        SocMeasurement(SocStatus.READ_FAILED, START),
    )
    await record_slot(path, START + timedelta(days=2), a, "observe", a.run.inputs.soc)
    rows = await ReadinessLedger(path).slots(START, START + timedelta(days=3))
    assert len(rows) == 1
    assert rows[0].import_rate == 0.25
    assert rows[0].start_energy_kwh is None


async def test_slot_captures_replay_parameters_export_hold_and_provenance(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    end = START + timedelta(minutes=30)
    export = ExportIntent(("export", START, end), START, end, 2, 3, (START,), (2,))
    intent = replace(a.run.plan.charge_intent, holds=((START, end),), export=export)
    event = FlexibilityEvent(START, end, "export", START, 1.25)
    inputs = replace(a.run.inputs, car_plugged_in=True, flexibility_event=event)
    a = replace(a, run=replace(a.run, inputs=inputs,
                               cfg=replace(a.run.cfg, window_end=time(0, 15)),
                               plan=replace(a.run.plan, charge_intent=intent)))
    await record_slot(path, START, a, "observe", a.run.inputs.soc)
    row = (await ReadinessLedger(path).slots(START, end))[0]
    assert row.charge_window_fraction == 0.5
    assert row.charge_current_a == 20
    assert row.export_kw == 2
    assert row.hold_state == "held"
    assert row.v2l_available
    assert row.event_rate == 1.25
    assert row.export_rate == 0.05
    assert row.forecast_model == "median"
    assert row.config_generation == 3
    cfg = a.run.cfg
    for name in ("capacity_kwh", "min_soc", "target_cap", "voltage_v", "charge_efficiency",
                 "max_current_a", "battery_discharge_ceiling_kw"):
        assert getattr(row, name) == getattr(cfg, name)


@pytest.mark.parametrize("window_start", [time(0), time(23, 30)])
async def test_mid_slot_plan_records_full_half_hour_window_fraction(tmp_path, window_start):
    path = str(tmp_path / "ledger.db")
    a = revision()
    tz = ZoneInfo("Europe/London")
    now = (START + timedelta(hours=4, minutes=32)).astimezone(tz)  # 05:32 household clock
    cfg = replace(a.run.cfg, window_start=window_start, window_end=time(5, 40))
    geometry = compute_horizon_geometry(now, cfg.window_start, cfg.window_end, tz)
    inputs = replace(a.run.inputs, now=now, horizon_start=geometry.horizon_start, geometry=geometry)
    a = replace(a, issued_at=now, run=replace(a.run, cfg=cfg, inputs=inputs, tz=tz))
    await record_slot(path, now, a, "observe", inputs.soc)
    (row,) = await ReadinessLedger(path).slots(START, START + timedelta(days=1))
    assert row.charge_window_fraction == pytest.approx(10 / 30)


async def test_event_price_is_separate_from_ordinary_export_rate(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    event = FlexibilityEvent(START, START + timedelta(minutes=30), "export", START, 1.25)
    a = replace(a, run=replace(
        a.run, inputs=replace(a.run.inputs, flexibility_event=event),
        schedule=replace(a.run.schedule, export_prices=(1.25, 0.08) + (0.05,) * 46),
    ))
    for minutes in (0, 30):
        await record_slot(path, START + timedelta(minutes=minutes), a, "observe", a.run.inputs.soc)
    rows = await ReadinessLedger(path).slots(START, START + timedelta(hours=1))
    assert [r.export_rate for r in rows] == [0.05, 0.08]
    assert [r.event_rate for r in rows] == [1.25, None]


async def test_untrusted_hold_is_unknown_and_ev_hold_is_captured(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    untrusted = replace(a, run=replace(a.run, plan=replace(a.run.plan, charge_intent=replace(
        a.run.plan.charge_intent, hold_trusted=False,
    ))))
    await record_slot(path, START, untrusted, "observe", a.run.inputs.soc, ev_hold_active=True)
    await record_slot(path, START + timedelta(minutes=30), a, "observe", a.run.inputs.soc,
                      ev_hold_active=True)
    await record_slot(path, START + timedelta(hours=1), a, "observe", a.run.inputs.soc)
    rows = await ReadinessLedger(path).slots(START, START + timedelta(hours=2))
    assert [r.hold_state for r in rows] == ["unknown", "held", "not_held"]
    assert rows[2].event_rate is None
    assert rows[2].export_kw == 0


async def test_missed_boundary_never_backfills_even_with_a_plan(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    await record_slot(path, START, None, "observe", a.run.inputs.soc)
    await record_slot(path, START + timedelta(minutes=6), a, "observe", a.run.inputs.soc)
    assert await ReadinessLedger(path).slots(START, START + timedelta(hours=1)) == []


async def test_a_drifted_first_tick_still_captures_its_slot(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    await record_slot(path, START + timedelta(minutes=2), a, "observe", a.run.inputs.soc)
    (row,) = await ReadinessLedger(path).slots(START, START + timedelta(hours=1))
    assert row.slot_start == START


async def test_recording_failure_is_isolated(tmp_path, caplog):
    a = revision()
    await record_slot(str(tmp_path), START, a, "observe", a.run.inputs.soc)
    assert "Readiness slot recording failed" in caplog.text


async def test_missing_database_diagnostic_does_not_create_it(tmp_path):
    path = tmp_path / "missing.db"
    assert await ReadinessLedger(str(path)).slots(START, START + timedelta(days=1)) == []
    assert not path.exists()


async def test_partial_slot_export_event_is_recorded(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    event = FlexibilityEvent(START + timedelta(minutes=15), START + timedelta(minutes=20),
                             "export", START, 1.25)
    a = replace(a, run=replace(a.run, inputs=replace(a.run.inputs, flexibility_event=event),
                               schedule=replace(a.run.schedule, export_prices=(1.25,) * 48)))
    await record_slot(path, START, a, "observe", a.run.inputs.soc)
    (row,) = await ReadinessLedger(path).slots(START, START + timedelta(minutes=30))
    assert row.event_rate == 1.25
    assert row.export_rate == 0.05


async def test_kathmandu_capture_uses_household_half_hour_grid(tmp_path):
    path = str(tmp_path / "ledger.db")
    tz = ZoneInfo("Asia/Kathmandu")
    now = datetime(2026, 10, 20, 0, 2, tzinfo=tz)
    a = revision()
    geometry = compute_horizon_geometry(now, a.run.cfg.window_start, a.run.cfg.window_end, tz)
    inputs = replace(a.run.inputs, now=now, horizon_start=geometry.horizon_start, geometry=geometry)
    a = replace(a, issued_at=now, run=replace(a.run, inputs=inputs, tz=tz))
    await record_slot(path, now, a, "observe", inputs.soc)
    rows = await ReadinessLedger(path).slots(now - timedelta(hours=1), now + timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].slot_start == now.replace(minute=0).astimezone(UTC)


async def test_slot_records_effective_limits_not_later_flat_settings(tmp_path):
    path = str(tmp_path / "ledger.db")
    a = revision()
    cfg = replace(a.run.cfg, max_current_a=200)
    plan = compute_plan(a.run.inputs, cfg, a.run.schedule)
    # A later settings edit cannot change what this revision used.
    a = replace(a, run=replace(a.run, plan=plan, cfg=replace(cfg, max_current_a=1)))
    await record_slot(path, START, a, "observe", a.run.inputs.soc)
    (row,) = await ReadinessLedger(path).slots(START, START + timedelta(minutes=30))
    assert row.max_current_a == 62.5
    assert row.limits_source == "legacy"
    assert row.effective_limits == plan.effective_limits


async def test_old_slot_json_loads_and_replays_identically(tmp_path):
    from ha_spark.energy.readiness_ledger import SlotRecord
    from ha_spark.energy.readiness_replay import replay_day
    from tests.test_readiness_replay import FIXTURE_BLOCKS, day_from_blocks

    day = day_from_blocks(FIXTURE_BLOCKS)
    slots = []
    for slot in day.slots:
        assert slot.intent is not None
        old_json = slot.intent.model_dump_json(exclude={"limits_source", "effective_limits"})
        loaded = SlotRecord.model_validate_json(old_json)
        assert loaded.limits_source is None
        assert loaded.effective_limits is None
        slots.append(slot.model_copy(update={"intent": loaded}))
    assert replay_day(day.model_copy(update={"slots": tuple(slots)})) == replay_day(day)
