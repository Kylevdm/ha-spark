"""The day CLI renders the pure cost result and its audit inputs."""

import asyncio
from dataclasses import replace
from datetime import UTC

import respx

from ha_spark import cli
from ha_spark.energy.energy_ledger import EnergyLedger, capture_rates
from ha_spark.energy.models import FreeSession
from ha_spark.energy.readiness_measurements import day_bounds
from ha_spark.energy.stats import compute_stats, cost_stats
from ha_spark.energy.tariff import apply_import_overlay
from tests.test_energy_ledger import CLOSE, counter, settings_for
from tests.test_readiness_ledger import START, revision


@respx.mock
def test_stats_day_renders_costs_and_session_evidence(tmp_path, monkeypatch, capsys):
    settings = settings_for(tmp_path, timezone="UTC")
    rev = revision()
    inputs = replace(rev.run.inputs, ev_hold_charging=True,
                     free_sessions=(FreeSession("free_electricity", "booking", START, CLOSE),))
    schedule = apply_import_overlay(rev.run.schedule, inputs, rev.run.cfg)
    rev = replace(rev, run=replace(rev.run, inputs=inputs, schedule=schedule))
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), ("13", CLOSE))
    day_start, day_end = day_bounds(START.date(), settings.timezone)

    async def record():
        await ledger.record_boundary(settings, START, capture_rates(settings, START, None))
        await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, rev))
        rows = await ledger.slots(day_start, day_end)
        return cost_stats(compute_stats(rows, day_start, day_end, UTC))

    result = asyncio.run(record())
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    assert cli.main(["stats", "--range", "day", "--at", START.date().isoformat()]) == 0
    out = capsys.readouterr().out
    cost = result.slots[0]
    inputs = cost.inputs
    assert f"import={inputs.grid_import_kwh:g} kWh" in out
    assert f"house={inputs.house_load_kwh:g} kWh" in out
    assert f"import_rate={inputs.rates.import_rate:g} GBP/kWh" in out
    assert "Free Electricity (hold)" in out
    assert "Axle earnings (estimated)=0 GBP" in out
    assert f"Actual cost={cost.actual_cost:.2f} GBP" in out
    assert f"No-battery cost={cost.no_battery_cost:.2f} GBP" in out
    assert f"Battery savings={cost.battery_savings:.2f} GBP" in out
    assert "unpriced (missing_slot)" in out
    assert "Cost coverage: 1/48 slots" in out
    assert "max(0, house - solar)" in out
    assert "Standing charges excluded" in out
    assert "the battery and ha-spark together" in out
