"""Readiness evidence is immutable, time-indexed and read-only to hardware."""
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

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
    geometry = a.run.inputs.geometry_for(a.run.cfg)
    inputs = replace(a.run.inputs, car_plugged_in=True, flexibility_event=event,
                     geometry=replace(geometry, window_fracs=(0.5,) + (1.0,) * 47))
    a = replace(a, run=replace(a.run, inputs=inputs,
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
