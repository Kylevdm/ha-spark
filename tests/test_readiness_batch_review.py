"""Defensive readiness evaluation of persisted, potentially incomplete evidence."""
from dataclasses import replace
from datetime import date, timedelta

import aiosqlite
import pytest

from ha_spark.config import Settings
from ha_spark.energy.readiness import evaluate_readiness, load_readiness, readiness_tick
from ha_spark.energy.readiness_measurements import ReadinessDays, day_bounds
from tests.authority_helpers import seed_authority
from tests.test_readiness import fixture


@pytest.mark.parametrize("problem", ["slots", "median", "baseline", "change", "paired", "zero"])
def test_forecast_insufficiency_never_compares_margin(problem):
    day, forecast, guards = fixture()
    forecast = replace(forecast, total_kwh=10)
    if problem in {"slots", "median", "baseline"}:
        forecast = replace(forecast, model=problem)
    elif problem == "change":
        slot = day.slots[-1]
        day = day.model_copy(update={"slots": (*day.slots[:-1], slot.model_copy(update={
            "intent": slot.intent.model_copy(update={"forecast_model": "other"}),
        }))})
    elif problem == "paired":
        forecast = replace(forecast, median_total_kwh=None)
    else:
        forecast = replace(forecast, median_total_kwh=0)
    result = evaluate_readiness([day], guards, [forecast], Settings(readiness_window_days=1))
    assert result.verdict == "insufficient_evidence"
    assert result.mae_improvement_pct is None
    assert "MAE improvement below threshold" not in result.reasons
    if problem == "change":
        assert "model change" in result.reasons
    if problem in {"slots", "median", "baseline"}:
        assert "median active" in result.reasons


def test_zero_guard_samples_insufficient_even_with_zero_coverage_threshold():
    day, forecast, _ = fixture()
    result = evaluate_readiness([day], [], [forecast], Settings(
        readiness_window_days=1, readiness_min_guard_coverage_pct=0,
    ))
    assert result.verdict == "insufficient_evidence"
    assert "guard evidence missing" in result.reasons


@pytest.mark.parametrize("problem", ["gap", "duplicate", "mode", "intent", "substituted", "absent"])
def test_invalid_day_excluded_without_trusting_reasons(problem):
    day, forecast, guards = fixture()
    slots = list(day.slots)
    if problem == "gap":
        slots.pop()
    elif problem == "duplicate":
        slots[-1] = slots[0]
    elif problem == "mode":
        slots[0] = slots[0].model_copy(update={
            "intent": slots[0].intent.model_copy(update={"operating_mode": "protect"}),
        })
    elif problem == "intent":
        slots[0] = slots[0].model_copy(update={"intent": None})
    else:
        components = dict(slots[0].components)
        components["grid_import"] = components["grid_import"].model_copy(update={
            "provenance": problem,
        })
        slots[0] = slots[0].model_copy(update={"components": components})
    day = day.model_copy(update={"slots": tuple(slots)})
    result = evaluate_readiness([day], guards, [forecast], Settings(readiness_window_days=1))
    assert result.verdict == "insufficient_evidence"
    assert result.qualifying_days == ()
    assert result.excluded_days[day.local_date]


@pytest.mark.parametrize("local_date,count", [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
def test_complete_dst_days_qualify(local_date, count):
    day, forecast, guards = fixture()
    start, _ = day_bounds(local_date, day.timezone)
    slots = []
    for i in range(count):
        slot = day.slots[0 if i == 0 else 1]
        instant = start + timedelta(minutes=30 * i)
        slots.append(slot.model_copy(update={
            "slot_start": instant,
            "intent": slot.intent.model_copy(update={"slot_start": instant}),
        }))
    day = day.model_copy(update={"local_date": local_date, "slots": tuple(slots)})
    forecast = replace(forecast, target_date=local_date, made_at=start - timedelta(days=1))
    guards = [g.model_copy(update={"slot_start": start,
                                  "observed_at": start + timedelta(minutes=i)})
              for i, g in enumerate(guards)]
    result = evaluate_readiness([day], guards, [forecast], Settings(readiness_window_days=1))
    assert result.verdict == "pass"
    assert result.qualifying_days == (local_date,)


async def test_legacy_forecasts_read_as_empty_without_migration(tmp_path):
    settings = Settings(db_path=str(tmp_path / "ledger.db"))
    day, _, _ = fixture()
    await ReadinessDays(settings.db_path).record(day)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY, model TEXT)")
        await db.commit()
    before = (tmp_path / "ledger.db").read_bytes()
    result = await load_readiness(settings)
    assert result.verdict == "insufficient_evidence"
    assert result.excluded_days[day.local_date] == ("forecast missing",)
    assert (tmp_path / "ledger.db").read_bytes() == before


async def test_invalid_timezone_readiness_tick_uses_utc(tmp_path):
    settings = Settings(db_path=str(tmp_path / "ledger.db"), timezone="Invalid/Zone")
    authority = seed_authority(settings, "observe")
    day, _, _ = fixture()
    assert await readiness_tick(settings, day.slots[0].slot_start, None, authority) is None
