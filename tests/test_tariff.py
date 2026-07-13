"""Tariff schedule contract tests (P8.2 #36, P8.4 #38).

Two seams: the provider boundary (config in, schedule out) and the planner
boundary (inputs + a synthetic schedule in, plan choice + costs out). The
byte-identical fixed-provider behaviour is pinned separately in
test_golden_baseline.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from ha_spark.config import (
    ConfigError,
    Settings,
    validate_dynamic_tariff,
    validate_fixed_tariff,
    validate_octopus_intelligent_tariff,
)
from ha_spark.energy.models import DispatchSlot, PlannerConfig, PlannerInputs, PricePoint
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import (
    DynamicTariffProvider,
    FixedTariffProvider,
    OctopusIntelligentProvider,
    TariffSchedule,
    fixed_schedule,
)

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


# --- dynamic provider boundary (P8.4, #38) ---


def _points(prices: list[float]) -> tuple[PricePoint, ...]:
    """One PricePoint per half-hour slot from ``HORIZON``, in order."""
    return tuple(
        PricePoint(
            start=HORIZON + timedelta(minutes=30 * i),
            end=HORIZON + timedelta(minutes=30 * (i + 1)),
            price=p,
        )
        for i, p in enumerate(prices)
    )


def _dynamic_inputs(prices: list[float], *, load_slots: bool = True) -> PlannerInputs:
    return PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)) if load_slots else None,
        horizon_start=HORIZON,
        dynamic_prices=_points(prices),
    )


def _dynamic() -> DynamicTariffProvider:
    return DynamicTariffProvider(fallback=FixedTariffProvider(0.069, 0.30, 0.0))


def test_dynamic_provider_falls_back_without_load_slots() -> None:
    """No v2 slot horizon -> identical to the fixed schedule (daily model)."""
    inputs = _dynamic_inputs([0.1] * 48, load_slots=False)
    assert _dynamic().schedule(inputs, cfg()) == fixed_schedule(inputs, cfg())


def test_dynamic_provider_still_honours_daytime_dispatches() -> None:
    """A daytime Octopus dispatch becomes a controlled window under `dynamic` too."""
    day = DispatchSlot(
        start=datetime(2026, 1, 16, 13, 0, tzinfo=UTC),
        end=datetime(2026, 1, 16, 14, 30, tzinfo=UTC),
    )
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dynamic_prices=_points([0.1] * 48), dispatches=(day,),
    )
    sched = _dynamic().schedule(inputs, cfg())
    assert sched.controlled_windows == ((day.start, day.end),)


def test_dynamic_provider_falls_back_without_prices() -> None:
    """No live prices at all (unread/empty sensor) -> falls back to fixed."""
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
    )
    assert _dynamic().schedule(inputs, cfg()) == fixed_schedule(inputs, cfg())


def test_dynamic_provider_marks_cheapest_slots_as_cheap() -> None:
    """The window_hours-worth of cheapest live-priced slots become cheap_frac=1.0,
    wherever they fall — not just the first 12 slots (the old fixed window)."""
    prices = [0.30] * 48
    cheap_indices = range(20, 32)  # 12 slots (6h window) away from slot 0
    for i in cheap_indices:
        prices[i] = 0.05
    sched = _dynamic().schedule(_dynamic_inputs(prices), cfg())
    assert sched.prices == tuple(prices)
    for i in range(48):
        expected = 1.0 if i in cheap_indices else 0.0
        assert sched.cheap_fracs[i] == expected, i
    # The old fixed-window slots (0-11) are no longer flagged cheap.
    assert sched.cheap_fracs[:12] == (0.0,) * 12


def test_dynamic_provider_uncovered_slot_costs_standard_rate() -> None:
    """A slot with no live price (partial sensor read) costs at the standard rate."""
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dynamic_prices=_points([0.05] * 10),  # only the first 10 slots are covered
    )
    sched = _dynamic().schedule(inputs, cfg())
    assert sched.prices[10] == pytest.approx(0.30, abs=APPROX)
    assert sched.prices[0] == pytest.approx(0.05, abs=APPROX)


def test_dynamic_schedule_places_charge_in_the_cheapest_slots() -> None:
    """Planner integration: load sitting on the genuinely cheapest slots is
    cheap-covered, even though those slots aren't the (old) fixed window."""
    prices = [0.30] * 48
    cheap_indices = range(20, 32)
    for i in cheap_indices:
        prices[i] = 0.05
    inputs = _dynamic_inputs(prices)
    sched = _dynamic().schedule(inputs, cfg())
    plan = compute_plan(inputs, cfg(), sched)
    # Net load in the 12 genuinely-cheapest slots (0.5 kWh * 12) is cheap-covered;
    # the rest (36 slots) is expensive.
    assert plan.expensive_load_kwh == pytest.approx(0.5 * 36, abs=APPROX)
    assert plan.baseline_cost == pytest.approx(0.5 * 12 * 0.05 + 0.5 * 36 * 0.30, abs=APPROX)


# --- dynamic startup validation (P8.4, #38) ---


def test_validate_dynamic_noop_for_fixed_provider() -> None:
    validate_dynamic_tariff(_settings())  # tariff_provider defaults to "fixed"; no raise


def test_validate_dynamic_rejects_missing_entity() -> None:
    with pytest.raises(ConfigError) as exc:
        validate_dynamic_tariff(_settings(tariff_provider="dynamic"))
    assert "dynamic_rates_entity" in str(exc.value)


def test_validate_dynamic_accepts_configured_entity() -> None:
    validate_dynamic_tariff(
        _settings(tariff_provider="dynamic", dynamic_rates_entity="event.rates")
    )  # no raise


# --- octopus_intelligent provider boundary (P8.5, #39) ---


def _octopus() -> OctopusIntelligentProvider:
    return OctopusIntelligentProvider(fallback=FixedTariffProvider(0.069, 0.30, 0.0))


def test_octopus_intelligent_matches_fixed_dispatch_handling_without_live_prices() -> None:
    """No live API prices (e.g. an unconfigured/failed fetch) -> byte-identical
    to fixed, including dispatch-overlap cheap coverage and controlled windows
    — the P8.1 golden dispatch scenarios' guarantee."""
    dispatch = DispatchSlot(
        start=datetime(2026, 1, 16, 13, 30, tzinfo=UTC),
        end=datetime(2026, 1, 16, 15, 0, tzinfo=UTC),
        charge_in_kwh=-5.2,
        source="octopus",
    )
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=8.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dispatches=(dispatch,),
    )
    a = compute_plan(inputs, cfg())
    b = compute_plan(inputs, cfg(), _octopus().schedule(inputs, cfg()))
    assert a == b


def test_octopus_intelligent_overlays_live_prices_without_changing_dispatch_handling() -> None:
    """Live API prices replace `prices`, but cheap_fracs/controlled_windows
    (the dispatch-overlap math) stay identical to the fixed schedule."""
    dispatch = DispatchSlot(
        start=datetime(2026, 1, 16, 13, 30, tzinfo=UTC),
        end=datetime(2026, 1, 16, 15, 0, tzinfo=UTC),
    )
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dispatches=(dispatch,), dynamic_prices=_points([0.08] * 48),
    )
    base = fixed_schedule(inputs, cfg())
    sched = _octopus().schedule(inputs, cfg())
    assert sched.cheap_fracs == base.cheap_fracs
    assert sched.controlled_windows == base.controlled_windows
    assert sched.prices == (0.08,) * 48  # live prices, not the fixed frac*rate blend


def test_octopus_intelligent_falls_back_without_load_slots() -> None:
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        dynamic_prices=_points([0.08] * 48),
    )
    assert _octopus().schedule(inputs, cfg()) == fixed_schedule(inputs, cfg())


def test_octopus_intelligent_uncovered_slot_costs_standard_rate() -> None:
    inputs = PlannerInputs(
        soc_now=55, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dynamic_prices=_points([0.05] * 10),
    )
    sched = _octopus().schedule(inputs, cfg())
    assert sched.prices[0] == pytest.approx(0.05, abs=APPROX)
    assert sched.prices[10] == pytest.approx(0.30, abs=APPROX)


# --- octopus_intelligent startup validation (P8.5, #39) ---


def test_validate_octopus_intelligent_noop_for_fixed_provider() -> None:
    validate_octopus_intelligent_tariff(_settings())  # no raise


def test_validate_octopus_intelligent_rejects_missing_config() -> None:
    with pytest.raises(ConfigError) as exc:
        validate_octopus_intelligent_tariff(_settings(tariff_provider="octopus_intelligent"))
    assert "octopus_" in str(exc.value)


def test_validate_octopus_intelligent_accepts_full_config() -> None:
    validate_octopus_intelligent_tariff(
        _settings(
            tariff_provider="octopus_intelligent",
            octopus_api_key="sk_test",
            octopus_account_number="A-1234ABCD",
            octopus_product_code="INTELLI-VAR-22-10-14",
            octopus_tariff_code="E-1R-INTELLI-VAR-22-10-14-A",
        )
    )  # no raise
