"""Native P90 is same-day admission evidence, never export energy."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import FlexibilityEvent, PlannerInputs
from tests.test_planner import _plan, _soc, cfg


@pytest.mark.parametrize("now", [
    datetime(2026, 1, 5, 1), datetime(2026, 1, 5, 17, 40),
    datetime(2026, 3, 29, 0), datetime(2026, 10, 25, 0),
])
@pytest.mark.parametrize("ceiling", ["dno", "supply"])
@pytest.mark.parametrize("p90,admitted", [(1.0, True), (1.01, False)])
def test_same_day_p90_ceiling(now: datetime, ceiling: str, p90: float, admitted: bool) -> None:
    tz = ZoneInfo("Europe/London")
    now = now.replace(tzinfo=tz)
    config = cfg(capacity_kwh=100, battery_discharge_ceiling_kw=3,
                 dno_export_limit_kw=4 if ceiling == "dno" else 10,
                 supply_max_current_a=40 if ceiling == "dno" else 20,
                 supply_voltage_v=200)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    start = now.replace(hour=17, minute=30).astimezone(UTC)
    event = FlexibilityEvent(start=start, end=start + timedelta(minutes=30),
                             direction="export", rate_gbp_kwh=1, updated_at=now)
    loads = tuple(0.5 if slot == start else 0.0 for slot in geom.slot_starts)
    solar = tuple(0.5 if slot == start else 0.0 for slot in geom.slot_starts)
    inp = PlannerInputs(
        soc=_soc(90), solar_tomorrow_kwh=0, predicted_home_load_kwh=sum(loads),
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=loads, solar_slots=solar, flexibility_event=event,
        export_p90_kw=tuple(p90 if slot == start else None for slot in geom.slot_starts),
    )
    plan = _plan(inp, config)
    assert (plan.charge_intent.export is not None) is admitted
    if not admitted:
        # Subtracting the 1 kW forecast load would incorrectly admit this slot.
        assert any("DNO or supply limit" in skip.reason for skip in plan.export_skips)
    else:
        baseline = _plan(replace(inp, solar_slots=None, export_p90_kw=None), config)
        assert plan.charge_intent.export == baseline.charge_intent.export
        assert plan.export_revenue == baseline.export_revenue
        assert plan.export_soc_needed_pct == baseline.export_soc_needed_pct
        assert plan.export_funding == baseline.export_funding
        lower_p90 = _plan(replace(inp, export_p90_kw=tuple(
            0.0 if value is not None else None for value in inp.export_p90_kw or ()
        )), config)
        assert plan == lower_p90
        # Evidence lapsing during the event restores the daylight refusal.
        assert _plan(replace(inp, export_p90_kw=None), config).charge_intent.export is None


def test_p90_refusal_breaks_suffix() -> None:
    now = datetime(2026, 1, 5, 17, tzinfo=UTC)
    config = cfg(capacity_kwh=100, battery_discharge_ceiling_kw=3, dno_export_limit_kw=4)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end,
                                    ZoneInfo("Europe/London"))
    inp = PlannerInputs(
        soc=_soc(90), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.0,) * len(geom.slot_starts),
        flexibility_event=FlexibilityEvent(
            start=now, end=now + timedelta(minutes=90), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
        export_p90_kw=(0.0, 2.0, 0.0) + (None,) * (len(geom.slot_starts) - 3),
    )
    plan = _plan(inp, config)
    assert plan.charge_intent.export is not None
    assert plan.charge_intent.export.selected_slots == (now + timedelta(hours=1),)
    assert "later slot" in plan.export_skips[0].reason
    assert "DNO or supply limit" in plan.export_skips[1].reason


@pytest.mark.parametrize("soc", [21, 90])
def test_p90_never_funds_export(soc: int) -> None:
    now = datetime(2026, 1, 5, 16, tzinfo=UTC)
    config = cfg(capacity_kwh=10, battery_discharge_ceiling_kw=3, dno_export_limit_kw=10)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end,
                                    ZoneInfo("Europe/London"))
    start = now + timedelta(hours=1)
    inp = PlannerInputs(
        soc=_soc(soc), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.1,) * len(geom.slot_starts),
        flexibility_event=FlexibilityEvent(
            start=start, end=start + timedelta(hours=1), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
    )
    baseline = _plan(inp, config)
    native = _plan(replace(inp, export_p90_kw=(2.0,) * len(geom.slot_starts)), config)
    assert native == baseline
    if soc == 21:
        assert native.charge_intent.export is None
        assert native.export_soc_needed_pct is not None


@pytest.mark.parametrize("now", [
    datetime(2026, 3, 29, 0, 40, tzinfo=UTC),
    datetime(2026, 10, 25, 0, 40, tzinfo=UTC),
    datetime(2026, 10, 25, 1, 40, tzinfo=UTC),
])
def test_mid_event_evidence_lapses_across_clock_changes(now: datetime) -> None:
    tz = ZoneInfo("Europe/London")
    config = cfg(capacity_kwh=100, battery_discharge_ceiling_kw=3, dno_export_limit_kw=4)
    geom = compute_horizon_geometry(now.astimezone(tz), config.window_start,
                                    config.window_end, tz)
    start = now.replace(minute=0)
    inp = PlannerInputs(
        soc=_soc(90), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now.astimezone(tz), geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.0,) * len(geom.slot_starts),
        solar_slots=(0.5,) * len(geom.slot_starts),
        export_p90_kw=(1.0,) * len(geom.slot_starts),
        flexibility_event=FlexibilityEvent(
            start=start, end=start + timedelta(hours=1), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
    )
    active = _plan(inp, config).charge_intent.export
    assert active is not None
    assert active.selected_slots == (geom.slot_starts[0],)
    assert _plan(replace(inp, export_p90_kw=None), config).charge_intent.export is None


def test_tomorrow_ignores_p90() -> None:
    now = datetime(2026, 1, 5, 23, 30, tzinfo=UTC)
    config = cfg(capacity_kwh=100)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end,
                                    ZoneInfo("Europe/London"))
    start = now.replace(day=6, hour=17, minute=0)
    inp = PlannerInputs(
        soc=_soc(90), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.0,) * len(geom.slot_starts),
        flexibility_event=FlexibilityEvent(
            start=start, end=start + timedelta(minutes=30), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
    )
    baseline = _plan(inp, config)
    assert baseline.charge_intent.export is not None
    assert _plan(replace(inp, export_p90_kw=(100.0,) * len(geom.slot_starts)), config) == baseline



def test_short_evidence_tuple_is_no_evidence() -> None:
    """Inputs built outside gather_inputs may carry a shorter tuple: never a crash."""
    now = datetime(2026, 1, 5, 17, tzinfo=UTC)
    config = cfg(capacity_kwh=100, battery_discharge_ceiling_kw=3, dno_export_limit_kw=4)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end,
                                    ZoneInfo("Europe/London"))
    inp = PlannerInputs(
        soc=_soc(90), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.0,) * len(geom.slot_starts),
        flexibility_event=FlexibilityEvent(
            start=now, end=now + timedelta(minutes=90), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
        export_p90_kw=(0.0,),
    )
    assert _plan(inp, config) == _plan(replace(inp, export_p90_kw=(0.0,) + (None,) * (
        len(geom.slot_starts) - 1)), config)
