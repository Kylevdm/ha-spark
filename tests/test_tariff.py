"""Tariff schedule contract tests (P8.2 #36, P8.4 #38).

Two seams: the provider boundary (config in, schedule out) and the planner
boundary (inputs + a synthetic schedule in, plan choice + costs out). The
byte-identical fixed-provider behaviour is pinned separately in
test_golden_baseline.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import (
    ConfigError,
    Settings,
    validate_dynamic_tariff,
    validate_fixed_tariff,
    validate_octopus_intelligent_tariff,
)
from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import DispatchSlot, PlannerConfig, PlannerInputs, PricePoint
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.tariff import (
    DynamicTariffProvider,
    FixedTariffProvider,
    OctopusIntelligentProvider,
    TariffSchedule,
    fixed_schedule,
)

APPROX = 1e-9
HORIZON = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)


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


def _soc_unavailable() -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.UNAVAILABLE,
        observed_at=now,
        raw_state="unavailable",
        max_age_s=600.0,
    )


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
        PlannerInputs(soc=_soc(50), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
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
        PlannerInputs(soc=_soc(50), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
            dispatches=(day, night),
        ),
        cfg(),
    )
    assert sched.controlled_windows == ((day.start, day.end),)


# --- planner boundary: synthetic schedules drive plan choice + costs ---


def _slot_inputs() -> PlannerInputs:
    return PlannerInputs(soc=_soc(55),
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
    return PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
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
    inputs = PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dynamic_prices=_points([0.1] * 48), dispatches=(day,),
    )
    sched = _dynamic().schedule(inputs, cfg())
    assert sched.controlled_windows == ((day.start, day.end),)


def test_dynamic_provider_falls_back_without_prices() -> None:
    """No live prices at all (unread/empty sensor) -> falls back to fixed."""
    inputs = PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
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
    inputs = PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
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


# --- charge-cycle selection over a mid-day horizon (#399) ---

LONDON = ZoneInfo("Europe/London")
# 2026-01-11 is a Sunday, 2026-01-12 a Monday (GMT: no DST in January).
SUN = date(2026, 1, 11)
MON = date(2026, 1, 12)
TUE = date(2026, 1, 13)


def at(day: date, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=LONDON)


def _cycle_points(start: datetime, prices: list[float]) -> tuple[PricePoint, ...]:
    """One PricePoint per real half-hour from ``start`` (UTC), in order."""
    start = start.astimezone(UTC)
    return tuple(
        PricePoint(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
            price=p,
        )
        for i, p in enumerate(prices)
    )


def _geom_inputs(
    now: datetime,
    points: tuple[PricePoint, ...] = (),
    *,
    dispatches: tuple[DispatchSlot, ...] = (),
    window: tuple[time, time] = (time(23, 30), time(5, 30)),
) -> PlannerInputs:
    """Inputs on a real horizon geometry: slot 0 is the current half-hour."""
    geom = compute_horizon_geometry(now, *window, LONDON)
    return PlannerInputs(
        soc=_soc(50), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        now=now, horizon_start=geom.horizon_start, geometry=geom,
        load_slots=tuple(0.5 for _ in geom.slot_starts),
        dynamic_prices=points, dispatches=dispatches,
    )


def test_dynamic_midday_selection_equals_window_start_selection() -> None:
    """A 12:00 replan selects the same cheap slots over the charge cycle (the
    running-or-next window start to the next window start) as a window-start
    replan; the stretch before the window is priced live, never cheap."""
    cycle_prices = [0.30] * 48
    for i in (3, 4, 20, 21, 22, 23, 30, 31, 32, 40, 41, 42):
        cycle_prices[i] = 0.05
    points = _cycle_points(at(MON, 23, 30), cycle_prices)
    midday = _dynamic().schedule(_geom_inputs(at(MON, 12, 0), points), cfg())
    at_start = _dynamic().schedule(_geom_inputs(at(MON, 23, 30), points), cfg())
    # The selection over the cycle is identical, so the marks carried onto the
    # mid-day horizon line up 23 slots in (the pre-window stretch).
    assert midday.cycle_cheap_slots == at_start.cycle_cheap_slots
    assert midday.cheap_fracs[:23] == (0.0,) * 23  # before the window: never cheap
    assert midday.cheap_fracs[23:] == at_start.cheap_fracs  # marks carry over
    # Pre-window slots are priced live (standard rate where the feed misses).
    assert midday.prices[:23] == (0.30,) * 23
    assert midday.prices[23:] == at_start.prices


def test_dynamic_in_window_selection_keeps_elapsed_cheapest_slots() -> None:
    """A 01:00 replan inside the running window ranks the elapsed cycle slots
    from the feed too, so the selection equals the window-start one; the
    elapsed marks fall off the already-past head of the horizon."""
    cycle_prices = [0.30] * 48
    for i in (1, 2, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19):
        cycle_prices[i] = 0.05
    points = _cycle_points(at(SUN, 23, 30), cycle_prices)
    in_window = _dynamic().schedule(_geom_inputs(at(MON, 1, 0), points), cfg())
    at_start = _dynamic().schedule(_geom_inputs(at(SUN, 23, 30), points), cfg())
    assert in_window.cycle_cheap_slots == at_start.cycle_cheap_slots
    # Horizon slot i (from 01:00) is cycle slot i+3: the marks shift by the
    # three elapsed slots, which are already past and carry nothing.
    assert in_window.cheap_fracs == at_start.cheap_fracs[3:]
    assert in_window.prices == at_start.prices[3:]


def test_dynamic_missing_elapsed_prices_degrade_to_standard_rate() -> None:
    """Elapsed cycle slots the feed no longer covers price at the standard
    rate (degradation case 2), so they stop outranking the live-priced slots."""
    points = _cycle_points(at(MON, 1, 0), [0.05] * 12 + [0.30] * 33)
    sched = _dynamic().schedule(_geom_inputs(at(MON, 1, 0), points), cfg())
    marked = [start for start, frac in sched.cycle_cheap_slots if frac == 1.0]
    assert marked == [at(MON, 1, 0) + timedelta(minutes=30 * i) for i in range(12)]
    assert sched.cheap_fracs == (1.0,) * 12 + (0.0,) * 33
    assert sched.prices == (0.05,) * 12 + (0.30,) * 33


@pytest.mark.parametrize(("month", "day", "n_cycle"), [(3, 28, 46), (10, 24, 50)])
def test_dynamic_cycle_spans_real_half_hours_across_dst(
    month: int, day: int, n_cycle: int,
) -> None:
    """The cheap-set cycle runs from the window start to the next one in real
    half-hours — 46 slots on the spring-forward night, 50 on the fall-back one
    (decision C) — and the window_hours-worth of cheapest marks carry onto the
    horizon one-for-one from the window start."""
    now = datetime(2026, month, day, 23, 30, tzinfo=LONDON)
    prices = [0.30] * n_cycle
    for i in range(5, 17):
        prices[i] = 0.05
    sched = _dynamic().schedule(_geom_inputs(now, _cycle_points(now, prices)), cfg())
    assert len(sched.cycle_cheap_slots) == n_cycle
    assert sched.cheap_fracs == tuple(frac for _, frac in sched.cycle_cheap_slots)
    assert sum(1 for _, frac in sched.cycle_cheap_slots if frac == 1.0) == 12


def test_dynamic_carries_marks_by_real_overlap_off_the_half_hour() -> None:
    """A window start off the half-hour: the cycle runs from 23:45 in real
    half-hours and a marked cycle slot splits its mark across the two horizon
    slots it straddles, exactly as window coverage splits."""
    window = (time(23, 45), time(5, 45))
    now = at(MON, 12, 0)
    geom = compute_horizon_geometry(now, *window, LONDON)
    # All-equal prices: the stable sort marks the first 12 cycle slots, so
    # cycle slot 0 (23:45-00:15) straddles two horizon slots.
    points = _cycle_points(at(MON, 23, 45), [0.30] * 48)
    sched = _dynamic().schedule(
        _geom_inputs(now, points, window=window), cfg(window_start=window[0], window_end=window[1])
    )
    assert len(sched.cycle_cheap_slots) == 48
    idx = geom.slot_starts.index(at(MON, 23, 30).astimezone(UTC))
    assert sched.cheap_fracs[: idx] == (0.0,) * idx  # wholly pre-window: never cheap
    assert sched.cheap_fracs[idx] == pytest.approx(0.5, abs=APPROX)
    assert sched.cheap_fracs[idx + 1] == pytest.approx(1.0, abs=APPROX)


def test_fixed_cycle_folds_window_and_dispatch_like_the_horizon() -> None:
    """The fixed/Octopus cycle view folds window coverage and dispatch overlap
    with the same 1.0 cap as the horizon (a partial slot never double-
    discounts a dispatch/window); a daytime dispatch cheapens its cycle slot."""
    now = at(MON, 12, 0)
    dispatches = (
        DispatchSlot(start=at(TUE, 13, 0), end=at(TUE, 13, 30)),  # daytime: no window
        DispatchSlot(start=at(TUE, 14, 0), end=at(TUE, 14, 15)),  # partial slot
        DispatchSlot(start=at(TUE, 5, 0), end=at(TUE, 5, 45)),  # overlaps the window
    )
    sched = fixed_schedule(_geom_inputs(now, dispatches=dispatches), cfg())
    by_clock = {
        start.astimezone(LONDON).time(): frac for start, frac in sched.cycle_cheap_slots
    }
    assert by_clock[time(13, 0)] == pytest.approx(1.0, abs=APPROX)  # dispatch
    assert by_clock[time(14, 0)] == pytest.approx(0.5, abs=APPROX)  # 15 of 30 minutes
    assert by_clock[time(5, 0)] == pytest.approx(1.0, abs=APPROX)  # window + dispatch, capped
    assert by_clock[time(5, 30)] == pytest.approx(0.5, abs=APPROX)  # dispatch only
    assert by_clock[time(23, 30)] == pytest.approx(1.0, abs=APPROX)  # window
    assert by_clock[time(12, 30)] == 0.0  # pre-window
    # The horizon view (#396) and the cycle view agree slot for slot.
    geom = compute_horizon_geometry(now, time(23, 30), time(5, 30), LONDON)
    for clock in (time(13, 0), time(14, 0), time(5, 0), time(5, 30)):
        idx = geom.slot_starts.index(at(TUE, clock.hour, clock.minute).astimezone(UTC))
        assert sched.cheap_fracs[idx] == pytest.approx(by_clock[clock], abs=APPROX)


def test_dynamic_geometry_falls_back_to_fixed_without_live_prices() -> None:
    """No usable live price over a mid-day horizon -> the fixed schedule
    verbatim (degradation case 1), cycle view included."""
    inputs = _geom_inputs(at(MON, 12, 0))
    assert _dynamic().schedule(inputs, cfg()) == fixed_schedule(inputs, cfg())


def test_octopus_intelligent_carries_the_fixed_cycle_over_a_geometry() -> None:
    """Intelligent overlays live prices on the fixed folding; the cycle view
    rides along from the fixed base so the backtest stays window-anchored."""
    now = at(MON, 12, 0)
    dispatch = DispatchSlot(start=at(TUE, 13, 0), end=at(TUE, 13, 30))
    points = _cycle_points(at(MON, 12, 0), [0.08] * 71)
    inputs = _geom_inputs(now, points, dispatches=(dispatch,))
    base = fixed_schedule(inputs, cfg())
    sched = _octopus().schedule(inputs, cfg())
    assert sched.cycle_cheap_slots == base.cycle_cheap_slots
    assert sched.cheap_fracs == base.cheap_fracs
    assert sched.prices == (0.08,) * 71


def test_legacy_inputs_keep_today_s_schedule_without_a_cycle() -> None:
    """Hand-built, window-anchored inputs expose no separate cycle: their
    horizon already starts at the window, exactly as before #399."""
    prices = [0.30] * 48
    for i in range(20, 32):
        prices[i] = 0.05
    inputs = _dynamic_inputs(prices)
    assert _dynamic().schedule(inputs, cfg()).cycle_cheap_slots == ()
    assert fixed_schedule(inputs, cfg()).cycle_cheap_slots == ()


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
    inputs = PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=8.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dispatches=(dispatch,),
    )
    a = compute_plan(inputs, cfg(), fixed_schedule(inputs, cfg()))
    b = compute_plan(inputs, cfg(), _octopus().schedule(inputs, cfg()))
    assert a == b


def test_octopus_intelligent_overlays_live_prices_without_changing_dispatch_handling() -> None:
    """Live API prices replace `prices`, but cheap_fracs/controlled_windows
    (the dispatch-overlap math) stay identical to the fixed schedule."""
    dispatch = DispatchSlot(
        start=datetime(2026, 1, 16, 13, 30, tzinfo=UTC),
        end=datetime(2026, 1, 16, 15, 0, tzinfo=UTC),
    )
    inputs = PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=HORIZON,
        dispatches=(dispatch,), dynamic_prices=_points([0.08] * 48),
    )
    base = fixed_schedule(inputs, cfg())
    sched = _octopus().schedule(inputs, cfg())
    assert sched.cheap_fracs == base.cheap_fracs
    assert sched.controlled_windows == base.controlled_windows
    assert sched.prices == (0.08,) * 48  # live prices, not the fixed frac*rate blend


def test_octopus_intelligent_falls_back_without_load_slots() -> None:
    inputs = PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        dynamic_prices=_points([0.08] * 48),
    )
    assert _octopus().schedule(inputs, cfg()) == fixed_schedule(inputs, cfg())


def test_octopus_intelligent_uncovered_slot_costs_standard_rate() -> None:
    inputs = PlannerInputs(soc=_soc(55), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
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


@pytest.mark.parametrize(
    ("product_code", "tariff_code"),
    [("", ""), ("INTELLI-VAR-22-10-14", ""), ("", "E-1R-INTELLI-VAR-22-10-14-A")],
)
def test_validate_octopus_intelligent_allows_missing_rate_codes_with_dispatch_auth(
    product_code: str, tariff_code: str
) -> None:
    """#176: missing rate codes only lose live rates; the fixed schedule covers them."""
    validate_octopus_intelligent_tariff(
        _settings(
            tariff_provider="octopus_intelligent",
            octopus_api_key="sk_test",
            octopus_account_number="A-1234ABCD",
            octopus_product_code=product_code,
            octopus_tariff_code=tariff_code,
        )
    )  # no raise


@pytest.mark.parametrize("month, day", [(3, 28), (10, 24)])
def test_geometry_tariffs_keep_naive_dispatches_on_the_household_clock(
    month: int, day: int,
) -> None:
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, month, day, 23, 30, tzinfo=tz)
    config = cfg()
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    # Naive supplier timestamps still mean the household clock, not UTC.
    start = datetime(2026, month, day + 1, 12)
    end = start + timedelta(minutes=30)
    inp = PlannerInputs(
        soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, horizon_start=geom.horizon_start, geometry=geom,
        load_slots=(0.0,) * len(geom.slot_starts),
        dispatches=(DispatchSlot(start=start, end=end),),
        dynamic_prices=(PricePoint(start=start, end=end, price=0.123),),
    )
    slot = geom.slot_starts.index(start.replace(tzinfo=tz).astimezone(UTC))
    fixed = FixedTariffProvider(0.069, 0.30, 0)
    schedule = fixed.schedule(inp, config)
    assert schedule.cheap_fracs[slot - 1:slot + 2] == (0, 1, 0)
    assert schedule.controlled_windows == ((start, end),)
    for provider in (DynamicTariffProvider(fixed), OctopusIntelligentProvider(fixed)):
        live = provider.schedule(inp, config)
        assert live.prices[slot - 1:slot + 2] == (0.30, 0.123, 0.30)


def test_validate_octopus_intelligent_still_requires_dispatch_auth() -> None:
    with pytest.raises(ConfigError):
        validate_octopus_intelligent_tariff(
            _settings(
                tariff_provider="octopus_intelligent",
                octopus_api_key="sk_test",
                octopus_product_code="INTELLI-VAR-22-10-14",
                octopus_tariff_code="E-1R-INTELLI-VAR-22-10-14-A",
            )
        )
