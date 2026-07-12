"""Tariff schedule contract tests (P8.2, #36).

Two seams: the provider boundary (config in, schedule out) and the planner
boundary (inputs + a synthetic schedule in, plan choice + costs out). The
byte-identical fixed-provider behaviour is pinned separately in
test_golden_baseline.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from ha_spark.config import ConfigError, Settings, validate_fixed_tariff
from ha_spark.energy.models import DispatchSlot, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import FixedTariffProvider, TariffSchedule, fixed_schedule

APPROX = 1e-9
HORIZON = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)


def cfg(**kw: object) -> PlannerConfig:
    base = dict(
        capacity_kwh=26.88,
        voltage_v=51.0,
        min_soc=20.0,
        target_cap=90.0,
        max_current_a=62.5,
        solar_haircut_k=0.85,
        window_start=time(23, 30),
        window_end=time(5, 30),
        rate_offpeak=0.069,
        rate_peak=0.30,
        buffer_pct=20.0,
        charge_efficiency=0.90,
    )
    base.update(kw)
    return PlannerConfig(**base)  # type: ignore[arg-type]


# --- provider boundary ---


def test_fixed_provider_prices_and_window_from_config() -> None:
    """Slots inside the window are cheap-rate; outside are standard-rate."""
    load = tuple(0.5 for _ in range(48))
    sched = fixed_schedule(
        PlannerInputs(
            soc_now=50, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
            load_slots=load, horizon_start=HORIZON,
        ),
        cfg(),
    )
    assert len(sched.prices) == 48
    # 23:30–05:30 window = 6 h = 12 half-hour slots at the head of the horizon.
    assert sched.cheap_fracs[:12] == (1.0,) * 12
    assert sched.cheap_fracs[12] == 0.0
    assert sched.prices[0] == pytest.approx(0.069, abs=APPROX)
    assert sched.prices[12] == pytest.approx(0.30, abs=APPROX)


def test_fixed_provider_daytime_dispatch_becomes_controlled_window() -> None:
    """A dispatch outside the window is a controlled (held) window; a night one isn't."""
    day = DispatchSlot(
        start=datetime(2026, 1, 16, 13, 0, tzinfo=UTC),
        end=datetime(2026, 1, 16, 14, 30, tzinfo=UTC),
    )
    night = DispatchSlot(
        start=datetime(2026, 1, 16, 2, 0, tzinfo=UTC),
        end=datetime(2026, 1, 16, 4, 0, tzinfo=UTC),
    )
    sched = fixed_schedule(
        PlannerInputs(
            soc_now=50, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
            dispatches=(day, night),
        ),
        cfg(),
    )
    assert sched.controlled_windows == ((day.start, day.end),)


def test_fixed_schedule_equals_default_costing() -> None:
    """compute_plan with an explicit fixed schedule matches the None default."""
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=8.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
    )
    a = compute_plan(inputs, cfg())
    b = compute_plan(inputs, cfg(), fixed_schedule(inputs, cfg()))
    assert a == b


# --- planner boundary: synthetic schedules drive plan choice + costs ---


def _slot_inputs() -> PlannerInputs:
    return PlannerInputs(
        soc_now=55,
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)),
        horizon_start=HORIZON,
    )


def test_synthetic_all_cheap_schedule_needs_no_charge() -> None:
    """Every slot supplier-cheap -> no expensive load -> nothing to buy."""
    sched = TariffSchedule(
        cheap_rate=0.069, standard_rate=0.30, export_rate=0.0, window_hours=6.0,
        prices=(0.069,) * 48, cheap_fracs=(1.0,) * 48,
    )
    plan = compute_plan(_slot_inputs(), cfg(), sched)
    assert plan.expensive_load_kwh == pytest.approx(0.0, abs=APPROX)
    assert plan.required_kwh == pytest.approx(0.0, abs=APPROX)
    assert plan.baseline_cost == pytest.approx(24.0 * 0.069, abs=APPROX)


def test_synthetic_all_expensive_schedule_drives_charge_and_cost() -> None:
    """No cheap slots -> full net load is expensive; costed at the standard rate."""
    sched = TariffSchedule(
        cheap_rate=0.069, standard_rate=0.30, export_rate=0.0, window_hours=6.0,
        prices=(0.30,) * 48, cheap_fracs=(0.0,) * 48,
    )
    plan = compute_plan(_slot_inputs(), cfg(), sched)
    assert plan.expensive_load_kwh == pytest.approx(24.0, abs=APPROX)
    assert plan.required_kwh > 0.0
    assert plan.baseline_cost == pytest.approx(24.0 * 0.30, abs=APPROX)
    assert plan.slot_prices == (0.30,) * 48


def test_provider_satisfies_protocol() -> None:
    prov: object = FixedTariffProvider(0.069, 0.30, 0.0)
    assert hasattr(prov, "schedule")


# --- startup validation names the bad field ---


def _settings(**kw: object) -> Settings:
    base = dict(ha_url="http://ha", ha_token="t")  # standalone: skips credential gate
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_validate_rejects_negative_rate_naming_field() -> None:
    with pytest.raises(ConfigError) as exc:
        validate_fixed_tariff(_settings(rate_peak_gbp_kwh=-0.1))
    assert "rate_peak" in str(exc.value)


def test_validate_rejects_bad_window_naming_field() -> None:
    with pytest.raises(ConfigError) as exc:
        validate_fixed_tariff(_settings(charge_window_start="25:99"))
    assert "window_start" in str(exc.value)


def test_validate_accepts_defaults() -> None:
    validate_fixed_tariff(_settings())  # no raise
