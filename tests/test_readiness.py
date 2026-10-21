"""Readiness is a recommendation from matched closed-day evidence, never authority."""
from dataclasses import replace
from datetime import timedelta

import pytest

from ha_spark.config import Settings
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ForecastHistoryRecord
from ha_spark.energy.readiness import evaluate_readiness, load_readiness
from ha_spark.energy.readiness_ledger import GuardRecord, ReadinessLedger
from ha_spark.energy.readiness_measurements import ReadinessDays
from tests.test_readiness_replay import single as replay_single


def single(**kwargs):
    """A complete Observe day with one nonempty slot and a consistent ML model."""
    day = replay_single(**kwargs)
    empty = replay_single(held=True).slots[0]
    slots = [day.slots[0]]
    for i in range(1, 48):
        start = day.slots[0].slot_start + timedelta(minutes=30 * i)
        slots.append(empty.model_copy(update={
            "slot_start": start,
            "intent": empty.intent.model_copy(update={"slot_start": start}),
        }))
    return day.model_copy(update={"slots": tuple(s.model_copy(update={
        "intent": s.intent.model_copy(update={"forecast_model": "ml"}),
    }) for s in slots)})


def fixture():
    day = single(fraction=1, current=0, held=True)
    forecast = ForecastHistoryRecord(
        made_at=day.slots[0].slot_start - timedelta(days=1), target_date=day.local_date,
        model="ml", total_kwh=0, slots=None, source="test", drove_plan=True,
        median_total_kwh=1,
    )
    guards = [GuardRecord(observed_at=day.slots[0].slot_start + timedelta(minutes=i),
                          slot_start=day.slots[0].slot_start, revision_id="test",
                          sample_valid=True, measured_import_w=0, setpoint_w=0,
                          target_w=0, throttle_w=0, breach=False) for i in range(30)]
    return day, forecast, guards


def test_clean_fixture_passes():
    day, forecast, guards = fixture()
    result = evaluate_readiness([day], guards, [forecast], Settings(readiness_window_days=1))
    assert result.verdict == "pass"
    assert result.guard_coverage_pct == 100
    assert result.cost_delta == 0
    assert result.model_mae == 0
    assert result.median_mae == 1


@pytest.mark.parametrize("failure", ["cost", "breach", "coverage", "mae"])
def test_each_measurement_can_fail(failure):
    day, forecast, guards = fixture()
    settings = Settings(readiness_window_days=1,
                        readiness_max_cost_delta=-1 if failure == "cost" else 0)
    if failure == "breach":
        guards[0] = guards[0].model_copy(update={"breach": True})
    if failure == "coverage":
        guards = guards[:26]
    if failure == "mae":
        forecast = replace(forecast, total_kwh=1)
    result = evaluate_readiness([day], guards, [forecast], settings)
    assert result.verdict == "fail"


@pytest.mark.parametrize("problem", ["few", "median", "zero", "paired", "missing"])
def test_insufficient_evidence(problem):
    day, forecast, guards = fixture()
    if problem == "median":
        forecast = replace(forecast, model="median")
    if problem == "zero":
        forecast = replace(forecast, median_total_kwh=0)
    if problem == "paired":
        forecast = replace(forecast, median_total_kwh=None)
    result = evaluate_readiness([day], guards, [] if problem == "missing" else [forecast],
                                Settings(readiness_window_days=2 if problem == "few" else 1))
    assert result.verdict == "insufficient_evidence"
    if problem == "missing":
        assert result.excluded_days[day.local_date] == ("forecast missing",)


def shifted(day, days):
    delta = timedelta(days=days)
    return day.model_copy(update={
        "local_date": day.local_date + delta,
        "slots": tuple(s.model_copy(update={
            "slot_start": s.slot_start + delta,
            "intent": s.intent.model_copy(update={"slot_start": s.slot_start + delta}),
        }) for s in day.slots),
    })


def test_latest_qualifying_days_and_model_change():
    day, forecast, guards = fixture()
    older = shifted(day, -2)
    excluded = shifted(day, -1).model_copy(update={"reasons": ("substituted load",)})
    old_forecast = replace(forecast, target_date=older.local_date,
                           made_at=forecast.made_at - timedelta(days=2), model="old")
    older_guards = [g.model_copy(update={
        "observed_at": g.observed_at - timedelta(days=2),
        "slot_start": g.slot_start - timedelta(days=2),
    }) for g in guards]
    days = [day, older, excluded]
    result = evaluate_readiness(days, guards + older_guards, [forecast, old_forecast],
                                Settings(readiness_window_days=1))
    assert result.verdict == "pass"
    assert result.qualifying_days == (day.local_date,)
    assert result.excluded_days == {excluded.local_date: ("substituted load",)}
    result = evaluate_readiness(days, guards + older_guards, [forecast, old_forecast],
                                Settings(readiness_window_days=2))
    assert result.verdict == "insufficient_evidence"
    assert "model change" in result.reasons


def test_forecast_mae_uses_base_load_without_v2l():
    day, forecast, guards = fixture()
    day = single(fraction=1, current=0, held=True, load=1, v2l=0.5, imp=1)
    forecast = replace(forecast, total_kwh=0.5, median_total_kwh=1.5)
    result = evaluate_readiness([day], guards, [forecast],
                                Settings(readiness_window_days=1, readiness_max_cost_delta=100))
    assert result.verdict == "pass"
    assert result.model_mae == 0
    assert result.median_mae == 1


def test_non_replayable_day_is_excluded():
    day, forecast, guards = fixture()
    day = day.model_copy(update={"start_energy_kwh": None})
    result = evaluate_readiness([day], guards, [forecast], Settings(readiness_window_days=1))
    assert result.verdict == "insufficient_evidence"
    assert result.qualifying_days == ()
    assert day.local_date in result.excluded_days


async def test_loader_reads_persisted_evidence_without_writing(tmp_path):
    settings = Settings(db_path=str(tmp_path / "ledger.db"), readiness_window_days=1)
    day, forecast, guards = fixture()
    await ReadinessDays(settings.db_path).record(day)
    for guard in guards:
        await ReadinessLedger(settings.db_path).record(guard)
    async with ForecastLedger(settings.db_path) as ledger:
        await ledger.record_forecast(
            forecast.made_at, forecast.target_date, forecast.model, forecast.total_kwh,
            None, "test", drove_plan=True, median_total_kwh=forecast.median_total_kwh,
        )
        # A later revision issued during the day must not rewrite its driving forecast.
        await ledger.record_forecast(
            day.slots[0].slot_start, day.local_date, "wrong", 100, None, "test",
            drove_plan=True, median_total_kwh=0,
        )
    before = (tmp_path / "ledger.db").read_bytes()
    result = await load_readiness(settings)
    assert result.verdict == "pass"
    assert result.model_mae == 0
    assert (tmp_path / "ledger.db").read_bytes() == before
