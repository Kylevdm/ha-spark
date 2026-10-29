"""Tests for the pure charge-planner math."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.evidence_freshness import EvidenceFreshness
from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import DispatchSlot, FlexibilityEvent, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.tariff import TariffSchedule, fixed_schedule


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


def _plan(inp: PlannerInputs, cfg: PlannerConfig) -> Any:
    """compute_plan under the fixed two-rate schedule — the planner-math default.

    These tests exercise sizing/cost math, not tariff selection, so they pin the
    fixed schedule explicitly now that ``compute_plan`` requires one (#91).
    """
    return compute_plan(inp, cfg, fixed_schedule(inp, cfg))


# An owner request above the driver ceiling; event funding must now respect
# the effective 62.5 A limit even when the owner asks for more.
_FILLS_THE_BATTERY_A = 200.0


def cfg(**kw: Any) -> PlannerConfig:
    base: dict[str, Any] = dict(
        capacity_kwh=26.88,
        voltage_v=51.0,
        min_soc=20.0,
        target_cap=90.0,
        max_current_a=62.5,
        solar_haircut_k=1.0,
        window_start=time(23, 30),
        window_end=time(5, 30),
        buffer_pct=0.0,
        charge_efficiency=1.0,
    )
    base.update(kw)
    return PlannerConfig(**base)


def test_window_hours_wraps_midnight() -> None:
    assert cfg().window_hours == 6.0


def test_basic_required_and_current() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    plan = _plan(inp, cfg())
    # deficit 15.45 - usable 2.688 = 12.76 kWh; target 30 + 12.76/26.88*100 ~ 77%
    assert round(plan.required_kwh, 2) == 12.76
    assert 77 <= plan.target_soc <= 78
    # 12.76 / (6h * 51V/1000) ~ 41.7 A -> charge_intent carries the SOC target,
    # not amps; rate is derived per-inverter from target_soc_pct.
    assert plan.charge_intent.target_soc_pct == pytest.approx(plan.target_soc)


def test_zero_need_when_full_and_sunny() -> None:
    inp = PlannerInputs(soc=_soc(90), solar_tomorrow_kwh=30, predicted_home_load_kwh=10)
    plan = _plan(inp, cfg())
    assert plan.required_kwh == 0
    assert plan.charge_intent.target_soc_pct == pytest.approx(plan.soc_now)


def test_plan_carries_the_exact_checked_measurement() -> None:
    """Identity, not equality: one read must never certify a different read."""
    measurement = _soc(37.5)
    inp = PlannerInputs(
        soc=measurement, solar_tomorrow_kwh=3, predicted_home_load_kwh=10
    )
    plan = _plan(inp, cfg())

    assert plan.soc is measurement
    assert plan.charge_intent.soc is measurement
    assert plan.soc_now == 37.5
    assert plan.charge_intent.soc_now == 37.5


def test_plan_carries_an_untrusted_axle_event_read() -> None:
    inputs = PlannerInputs(
        soc=_soc(37.5),
        solar_tomorrow_kwh=3,
        predicted_home_load_kwh=10,
        flexibility_event_trusted=False,
    )

    plan = _plan(inputs, cfg())

    assert plan.charge_intent.export_trusted is False


def test_failed_measurement_gives_the_planner_no_soc_value() -> None:
    """A failed measurement's SoC is 0 everywhere, and stays flagged."""
    inp = PlannerInputs(
        soc=_soc_unavailable(), solar_tomorrow_kwh=3, predicted_home_load_kwh=10
    )
    plan = _plan(inp, cfg())

    assert plan.soc_now == 0.0
    assert plan.charge_intent.soc_now == 0.0
    assert plan.charge_intent.soc.ok is False


def test_soc_validity_passes_through_to_plan() -> None:
    inp = PlannerInputs(
        soc=_soc_unavailable(), solar_tomorrow_kwh=3, predicted_home_load_kwh=10
    )
    assert _plan(inp, cfg()).soc.ok is False
    valid = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=3, predicted_home_load_kwh=10)
    assert _plan(valid, cfg()).soc.ok is True


def test_buffer_inflates_required_within_battery_room() -> None:
    inp = PlannerInputs(soc=_soc(20), solar_tomorrow_kwh=3, predicted_home_load_kwh=10)
    plan = _plan(inp, cfg(buffer_pct=20.0))
    # deficit = 10 - 3 = 7; usable_now = 0 (soc at min); buffered = 7 * 1.2 = 8.4.
    assert plan.deficit_kwh == pytest.approx(7.0)
    assert plan.buffer_pct == pytest.approx(20.0)
    assert plan.required_kwh == pytest.approx(8.4)


def test_battery_room_caps_required() -> None:
    inp = PlannerInputs(soc=_soc(85), solar_tomorrow_kwh=0, predicted_home_load_kwh=50)
    plan = _plan(inp, cfg())
    battery_room = 26.88 * (90 - 85) / 100  # 1.344
    assert round(plan.required_kwh, 3) == round(battery_room, 3)
    assert plan.target_soc <= 90 + 1e-9


def _slot(hh: int, mm: int) -> DispatchSlot:
    start = datetime(2026, 6, 8, hh, mm, tzinfo=UTC)
    return DispatchSlot(start, start + timedelta(minutes=30), -2.0, "SMART")


def test_daytime_dispatch_emits_stop_discharge() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        dispatches=(_slot(13, 0),),
    )
    plan = _plan(inp, cfg())
    assert len(plan.charge_intent.holds) == 1
    assert plan.cheap_covered_kwh > 0


def test_overnight_dispatch_does_not_stop_discharge() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        dispatches=(_slot(2, 0),),
    )
    plan = _plan(inp, cfg())
    assert len(plan.charge_intent.holds) == 0
    assert plan.cheap_covered_kwh == 0


def test_plan_emits_charge_intent() -> None:
    inp = PlannerInputs(soc=_soc(50.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=20.0)
    plan = _plan(inp, cfg())
    intent = plan.charge_intent
    assert intent.target_soc_pct == plan.target_soc
    assert intent.soc_now == plan.soc_now
    # default window is 23:30 -> 05:30
    assert (intent.window_start.hour, intent.window_start.minute) == (23, 30)
    assert (intent.window_end.hour, intent.window_end.minute) == (5, 30)


def test_daytime_dispatch_becomes_a_hold() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        dispatches=(_slot(13, 0),),
    )
    plan = _plan(inp, cfg())
    assert len(plan.charge_intent.holds) == 1


@pytest.mark.parametrize("trusted", [True, False])
def test_hold_trust_travels_from_inputs_onto_the_intent(trusted: bool) -> None:
    """The reconcile and export gate see the read's verdict, not just its holds (#143 §3)."""
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        dispatches_trusted=trusted,
    )
    assert _plan(inp, cfg()).charge_intent.hold_trusted is trusted


def test_the_kraken_read_time_travels_onto_the_intent() -> None:
    """The reconcile re-checks the plan's Kraken age, so the intent carries the own report time."""
    read_at = datetime(2026, 10, 3, 13, 50, tzinfo=UTC)
    freshness = EvidenceFreshness("fresh", read_at, 0.0, 2700, read_at)
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        evidence_freshness={"octopus_dispatch": freshness},
    )
    assert _plan(inp, cfg()).charge_intent.octopus_dispatch_reported_at == read_at
    bare = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    assert _plan(bare, cfg()).charge_intent.octopus_dispatch_reported_at is None


def test_axle_export_reserves_a_full_event_and_the_post_event_cheap_slot() -> None:
    """A funded event uses whole slots at the DNO ceiling and holds back after it."""
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
        rate_gbp_kwh=1.0,
    )
    inputs = PlannerInputs(
        soc=_soc(20.0),
        solar_tomorrow_kwh=1.0,
        predicted_home_load_kwh=24.0,
        load_slots=(0.5,) * 48,  # 1 kW forecast house load
        solar_slots=(0.0,) * 35 + (0.5, 0.5) + (0.0,) * 11,
        horizon_start=_HORIZON_START,
        flexibility_event=event,
    )

    plan = _plan(
        inputs,
        cfg(
            capacity_kwh=40.0,
            battery_discharge_ceiling_kw=3.2,
            dno_export_limit_kw=7.36,
            supply_max_current_a=75.0,
            supply_voltage_v=240.0,
            max_current_a=_FILLS_THE_BATTERY_A,
        ),
    )

    export = plan.charge_intent.export
    assert export is not None
    assert export.event_identity == ("export", event.start, event.end)
    assert (export.window_start, export.window_end) == (event.start, event.end)
    # min(7.36 DNO, 3.2 battery + 1.0 solar - 1.0 house) = 3.2 kW.
    assert export.planned_export_kw == pytest.approx(3.2)
    assert export.dno_export_limit_kw == pytest.approx(7.36)
    assert export.selected_slots == (event.start, event.start + timedelta(minutes=30))

    event_reservation, post_event_reservation = plan.reservations
    assert event_reservation.name == "axle-export-event"
    # 11.5 kWh of net house load through the event (the event's solar serves
    # its 1 kWh house load) plus 3.2 kWh of
    # selected export must survive until the event end.
    assert event_reservation.energy_kwh == pytest.approx(14.7)
    assert post_event_reservation.name == "reach-next-cheap-slot"
    # The 62.5 A window adds only 19.125 kWh: 1.075 kWh of the
    # post-event reserve funds export and is bought back profitably.
    assert post_event_reservation.energy_kwh == pytest.approx(4.425)
    assert plan.export_revenue == pytest.approx(3.2)


def test_axle_export_after_midnight_uses_the_in_progress_window_horizon() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 20, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 21, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 0, 0, tzinfo=UTC),
        rate_gbp_kwh=1.0,
    )
    inputs = PlannerInputs(
        soc=_soc(20.0),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=24.0,
        load_slots=(0.5,) * 48,
        solar_slots=(0.0,) * 48,
        horizon_start=datetime(2026, 6, 8, 23, 30, tzinfo=UTC),
        flexibility_event=event,
        now=datetime(2026, 6, 9, 0, 30, tzinfo=UTC),
    )

    plan = _plan(
        inputs,
        cfg(
            capacity_kwh=40.0,
            battery_discharge_ceiling_kw=3.2,
            dno_export_limit_kw=7.36,
            supply_max_current_a=75.0,
            supply_voltage_v=240.0,
            max_current_a=_FILLS_THE_BATTERY_A,
        ),
    )

    # Only five hours at 62.5 A remain: 15.9375 kWh cannot fund the
    # house through this late event, even spending the post-event reserve.
    assert plan.charge_intent.export is None
    assert plan.required_kwh == pytest.approx(15.9375)
    assert plan.export_revenue is None
    assert "reserved house or post-event energy" in plan.export_skips[-1].reason
    assert plan.target_soc > plan.soc_now


def test_axle_export_underfunding_selects_a_contiguous_suffix_of_full_slots() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 16, 30, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
        rate_gbp_kwh=1.0,
    )
    plan = _plan(
        PlannerInputs(
            soc=_soc(20.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
            load_slots=(0.0,) * 48, solar_slots=(0.0,) * 48,
            horizon_start=_HORIZON_START, flexibility_event=event,
        ),
        cfg(
            capacity_kwh=5.0, target_cap=90.0, min_soc=20.0,
            battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36,
        ),
    )

    export = plan.charge_intent.export
    assert export is not None
    # 3.5 kWh usable capacity funds only two 1.6 kWh slots. Working backwards
    # keeps the selected window contiguous and nearest the event end.
    assert export.selected_slots == (
        datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        datetime(2026, 6, 9, 17, 30, tzinfo=UTC),
    )
    assert export.window_end == event.end
    assert len(plan.export_skips) == 1
    assert "reserved house or post-event energy" in plan.export_skips[0].reason


def test_axle_export_skips_the_event_when_no_complete_slot_is_fundable() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC), direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC), rate_gbp_kwh=1.0,
    )
    plan = _plan(
        PlannerInputs(
            soc=_soc(20.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
            load_slots=(0.0,) * 48, solar_slots=(0.0,) * 48,
            horizon_start=_HORIZON_START, flexibility_event=event,
        ),
        cfg(capacity_kwh=2.0, battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36),
    )

    assert plan.charge_intent.export is None
    assert len(plan.export_skips) == 2
    assert any("reserved house or post-event energy" in skip.reason for skip in plan.export_skips)


def test_axle_export_does_not_overlap_a_dispatch_hold() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC), direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC), rate_gbp_kwh=1.0,
    )
    hold_start = datetime(2026, 6, 9, 17, 30, tzinfo=UTC)
    plan = _plan(
        PlannerInputs(
            soc=_soc(20.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
            load_slots=(0.0,) * 48, solar_slots=(0.0,) * 48,
            horizon_start=_HORIZON_START, flexibility_event=event,
            dispatches=(DispatchSlot(hold_start, hold_start + timedelta(minutes=30)),),
        ),
        cfg(battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36),
    )

    assert plan.charge_intent.export is None
    assert len(plan.charge_intent.holds) == 1
    assert any("dispatch hold" in skip.reason for skip in plan.export_skips)


def test_axle_export_overlapping_charge_window_suppresses_discretionary_charge() -> None:
    event = FlexibilityEvent(
        start=_HORIZON_START,
        end=_HORIZON_START + timedelta(minutes=30),
        direction="export",
        updated_at=_HORIZON_START - timedelta(minutes=1),
        rate_gbp_kwh=1.0,
    )
    plan = _plan(
        PlannerInputs(
            soc=_soc(50.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.5,
            load_slots=(0.5,) + (0.0,) * 47, solar_slots=(0.0,) * 48,
            horizon_start=_HORIZON_START, flexibility_event=event,
        ),
        cfg(battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36),
    )

    assert plan.charge_intent.export is not None
    assert plan.required_kwh == 0.0
    assert plan.charge_intent.target_soc_pct == plan.soc_now
    # 0.5 kWh house load plus 1.1 kWh export at the 2.2 kW safe ceiling.
    assert plan.reservations[0].energy_kwh == pytest.approx(1.6)


# --- same-day events: before tonight's horizon start (#198) ---

_SAME_DAY_EVENT = FlexibilityEvent(
    start=datetime(2026, 6, 8, 19, 0, tzinfo=UTC),
    end=datetime(2026, 6, 8, 20, 0, tzinfo=UTC),
    direction="export",
    updated_at=datetime(2026, 6, 8, 15, 55, tzinfo=UTC),
    rate_gbp_kwh=1.0,
)


def _same_day_inputs(
    *,
    soc_now: float,
    now: datetime,
    event: FlexibilityEvent | None = _SAME_DAY_EVENT,
) -> PlannerInputs:
    """0.5 kW house load all day; 16:00 to the 23:30 window drains 3.75 kWh."""
    return PlannerInputs(
        soc=_soc(soc_now),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=12.0,
        pre_window_drain_kwh=3.75,
        load_slots=(0.25,) * 48,
        solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START,
        flexibility_event=event,
        now=now,
    )


def _same_day_cfg() -> PlannerConfig:
    return cfg(battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=3.68)


def _same_day_plan(inputs: PlannerInputs, config: PlannerConfig) -> Any:
    """Exercise the former same-day cases through the live current-slot horizon."""
    return _plan(_current_event_inputs(inputs, config), config)


def test_same_day_event_reservations_are_not_charge_funded() -> None:
    """Tonight's charge funds only the post-window reservation, not the evening event."""
    from ha_spark.energy.report import format_plan

    plan = _same_day_plan(
        _same_day_inputs(soc_now=60.0, now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC)),
        _same_day_cfg(),
    )

    assert plan.charge_intent.export is not None
    assert "axle-export-event" in {r.name for r in plan.reservations}
    assert [r.name for r in plan.charge_funded_reservations] == ["reach-next-cheap-slot"]
    assert "(funds reach-next-cheap-slot)" in format_plan(plan, "test")


def test_same_day_export_is_funded_from_the_live_soc() -> None:
    """An event earlier than tonight's window is planned from the battery as it is now."""
    plan = _same_day_plan(
        _same_day_inputs(soc_now=60.0, now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC)),
        _same_day_cfg(),
    )

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert (export.window_start, export.window_end) == (
        _SAME_DAY_EVENT.start,
        _SAME_DAY_EVENT.end,
    )
    # 3.2 kW battery ceiling - 0.5 kW house load, under the 3.68 kW DNO limit.
    assert export.slot_export_kw == pytest.approx((2.7, 2.7))
    assert plan.export_revenue == pytest.approx(2.7)


def test_same_day_export_reports_soc_needed_for_the_funded_suffix() -> None:
    event = replace(_SAME_DAY_EVENT, rate_gbp_kwh=0.20)
    plan = _same_day_plan(
        _same_day_inputs(
            soc_now=43.0,
            now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC),
            event=event,
        ),
        replace(_same_day_cfg(), buffer_pct=20.0),
    )

    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == (event.start + timedelta(minutes=30),)
    assert "funding it would consume" in plan.export_skips[0].reason
    # Event house 0.50 + both exports 2.70 + buffered post-event load 2.10 kWh.
    assert plan.export_soc_needed_pct == pytest.approx(20.0 + 5.3 / 26.88 * 100.0)
    assert plan.export_soc_needed_at == event.start


def test_same_day_export_leaves_tonights_charge_to_refill_what_it_spent() -> None:
    """Tonight's charge is sized after the export, not zeroed by it."""
    now = datetime(2026, 6, 8, 16, 0, tzinfo=UTC)
    without = _same_day_plan(_same_day_inputs(soc_now=60.0, now=now, event=None), _same_day_cfg())
    with_export = _same_day_plan(_same_day_inputs(soc_now=60.0, now=now), _same_day_cfg())

    assert with_export.charge_intent.export is not None
    assert without.required_kwh > 0.0
    # The 2.7 kWh exported is gone from the battery by the time the window opens.
    assert with_export.required_kwh == pytest.approx(without.required_kwh + 2.7)


def test_same_day_export_keeps_the_slot_in_progress() -> None:
    """A replan during the event must not shorten or clear the running window."""
    plan = _same_day_plan(
        _same_day_inputs(soc_now=60.0, now=datetime(2026, 6, 8, 19, 0, 30, tzinfo=UTC)),
        _same_day_cfg(),
    )

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert export.window_start == _SAME_DAY_EVENT.start
    assert export.window_end == _SAME_DAY_EVENT.end


def test_same_day_export_keeps_its_window_once_a_slot_has_finished() -> None:
    """At 19:30 the window stays 19:00-20:00, so Slot 1 isn't rewritten mid-event."""
    plan = _same_day_plan(
        _same_day_inputs(soc_now=60.0, now=datetime(2026, 6, 8, 19, 30, 30, tzinfo=UTC)),
        _same_day_cfg(),
    )

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert (export.window_start, export.window_end) == (
        _SAME_DAY_EVENT.start,
        _SAME_DAY_EVENT.end,
    )
    # Only the slot still to come is planned and earns revenue.
    assert export.selected_slots == (datetime(2026, 6, 8, 19, 30, tzinfo=UTC),)


def test_same_day_export_counts_only_what_is_left_of_the_slot_in_progress() -> None:
    """A mid-slot replan doesn't charge the elapsed part again against the live SoC."""
    # At 19:15 the 2.7 kW export and 0.5 kW house load have already spent
    # 0.8 kWh of the 19:00 slot. The remaining need is 0.675 + 0.125 (rest of
    # the 19:00 slot) + 1.35 + 0.25 (19:30) + 1.75 (20:00-23:30) = 4.15 kWh.
    # Counting the 19:00 slot in full would need 4.95 kWh and drop it.
    soc = 20.0 + 4.5 / 26.88 * 100.0  # 4.5 kWh usable
    plan = _same_day_plan(
        _same_day_inputs(soc_now=soc, now=datetime(2026, 6, 8, 19, 15, tzinfo=UTC)),
        _same_day_cfg(),
    )

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert export.window_start == _SAME_DAY_EVENT.start


def test_same_day_export_refuses_a_slot_with_forecast_daylight() -> None:
    """Today's solar isn't forecast, so a daylight slot can't be checked against the DNO limit."""
    daylight_at_19_30 = (0.0,) * 40 + (0.5,) + (0.0,) * 7  # horizon slot 40 is 19:30
    inputs = replace(
        _same_day_inputs(soc_now=90.0, now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC)),
        solar_slots=daylight_at_19_30,
    )

    plan = _same_day_plan(inputs, _same_day_cfg())

    assert plan.charge_intent.export is None
    assert any("daylight" in skip.reason for skip in plan.export_skips)


def test_same_day_export_frees_battery_room_for_tonights_charge() -> None:
    """At the cap, a fill-to-cap night still refills what the event exports."""
    now = datetime(2026, 6, 8, 16, 0, tzinfo=UTC)
    fill = replace(_same_day_cfg(), strategy="fill")
    without = _same_day_plan(_same_day_inputs(soc_now=90.0, now=now, event=None), fill)
    with_export = _same_day_plan(_same_day_inputs(soc_now=90.0, now=now), fill)

    assert with_export.charge_intent.export is not None
    assert with_export.required_kwh == pytest.approx(without.required_kwh + 2.7)


def test_same_day_export_skips_when_the_live_soc_cannot_fund_it() -> None:
    """At 30% only 2.69 kWh is usable: less than the 3.75 kWh house load to 23:30."""
    plan = _same_day_plan(
        _same_day_inputs(soc_now=30.0, now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC)),
        _same_day_cfg(),
    )

    assert plan.charge_intent.export is None
    assert plan.export_skips
    assert all(
        "reserved house or post-event energy" in skip.reason
        or "a later slot is unavailable" in skip.reason
        for skip in plan.export_skips
    )


# --- current-slot same-day events (#403): the legacy #198 contract ---


def _current_event_inputs(legacy: PlannerInputs, config: PlannerConfig) -> PlannerInputs:
    assert legacy.now is not None
    geometry = compute_horizon_geometry(
        legacy.now, config.window_start, config.window_end, legacy.now.tzinfo or UTC
    )
    assert legacy.load_slots is not None
    assert legacy.horizon_start is not None
    # Lay the legacy daily profile onto absolute half-hours, including DST folds.
    indices = [
        int((start - legacy.horizon_start).total_seconds() / 1800) % 48
        for start in geometry.slot_starts
    ]
    return replace(
        legacy, geometry=geometry, horizon_start=geometry.horizon_start,
        pre_window_drain_kwh=0.0,
        load_slots=tuple(legacy.load_slots[i] for i in indices),
        solar_slots=tuple((legacy.solar_slots or (0.0,) * 48)[i] for i in indices),
    )


@pytest.mark.parametrize("guard", ["daylight", "dno", "supply", "hold", "capacity"])
def test_current_event_preserves_hard_refusals_and_suffix_termination(guard: str) -> None:
    config = _same_day_cfg()
    legacy = _same_day_inputs(
        soc_now=90.0, now=datetime(2026, 6, 8, 19, 15, tzinfo=UTC)
    )
    reasons = {
        "daylight": "Skipped paid slot: for a same-day slot in daylight the fixed discharge "
        "command isn't checked against the DNO limit, even with today's solar forecast.",
        "dno": "Skipped paid slot: the fixed discharge command would exceed "
        "the DNO or supply limit.",
        "supply": "Skipped paid slot: the fixed discharge command would exceed "
        "the DNO or supply limit.",
        "hold": "Skipped paid slot: it overlaps an Octopus dispatch hold.",
        "capacity": "Skipped paid slot: forecast house load leaves no export capacity.",
    }
    if guard == "daylight":
        legacy = replace(legacy, solar_slots=(0.0,) * 40 + (0.5,) + (0.0,) * 7)
    elif guard == "dno":
        config = replace(config, dno_export_limit_kw=2.0)
    elif guard == "supply":
        config = replace(config, supply_max_current_a=8.0, supply_voltage_v=240.0)
    elif guard == "hold":
        start = _SAME_DAY_EVENT.start + timedelta(minutes=30)
        legacy = replace(legacy, dispatches=(DispatchSlot(start, _SAME_DAY_EVENT.end),))
    else:
        legacy = replace(legacy, load_slots=(0.25,) * 40 + (2.0,) + (0.25,) * 7)

    actual = _plan(_current_event_inputs(legacy, config), config)

    assert actual.charge_intent.export is None
    assert len(actual.export_skips) == 2
    assert (
        actual.export_skips[0].reason == reasons[guard]
        if guard in {"dno", "supply"}
        else "a later slot is unavailable" in actual.export_skips[0].reason
    )
    assert actual.export_skips[-1].reason == reasons[guard]
    assert actual.export_soc_needed_pct is None  # more SoC cannot repair a hard refusal
    assert actual.export_funding is None


@pytest.mark.parametrize("discount", ["solar", "dispatch_before", "dispatch_after"])
def test_current_event_strict_protection_ignores_solar_and_dispatch(discount: str) -> None:
    config = replace(_same_day_cfg(), buffer_pct=20.0)
    legacy = _same_day_inputs(
        soc_now=43.0, now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC),
        event=replace(_SAME_DAY_EVENT, rate_gbp_kwh=0.20),
    )
    inputs = _current_event_inputs(legacy, config)
    if discount == "solar":
        inputs = replace(
            inputs, solar_slots=(2.0,) * 6 + (0.0,) * (len(inputs.load_slots or ()) - 6)
        )
    else:
        start = datetime(2026, 6, 8, 17 if discount == "dispatch_before" else 21, tzinfo=UTC)
        inputs = replace(inputs, dispatches=(DispatchSlot(start, start + timedelta(hours=1)),))

    plan = _plan(inputs, config)

    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == (_SAME_DAY_EVENT.start + timedelta(minutes=30),)
    assert plan.export_skips[0].reason == (
        "Skipped paid slot: funding it would consume reserved house or post-event energy."
    )
    assert plan.export_funding is None
    assert plan.export_soc_needed_pct == pytest.approx(20 + 5.3 / 26.88 * 100)


@pytest.mark.parametrize(
    ("dear_hour", "dear_minute", "price", "funded"),
    [(20, 0, 2.0, True), (22, 30, 2.0, False), (23, 0, 1.0, False)],
)
def test_current_event_buy_back_uses_worst_marginal_live_price_strictly(
    dear_hour: int, dear_minute: int, price: float, funded: bool
) -> None:
    config = _same_day_cfg()
    inputs = _current_event_inputs(
        _same_day_inputs(
            soc_now=20 + 5.6 / 26.88 * 100,
            now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC),
        ), config,
    )
    assert inputs.geometry is not None
    dear_slot = inputs.geometry.slot_starts.index(
        datetime(2026, 6, 8, dear_hour, dear_minute, tzinfo=UTC)
    )
    schedule = fixed_schedule(inputs, config)
    schedule = replace(schedule, prices=tuple(
        price if i == dear_slot else p for i, p in enumerate(schedule.prices)
    ))

    plan = compute_plan(inputs, config, schedule)

    export = plan.charge_intent.export
    assert export is not None
    assert len(export.selected_slots) == (2 if funded else 1)
    assert plan.export_funding == (
        "Funded 0.85 kWh from the post-event reserve: £1.00 event vs £0.30 buy-back."
        if funded else None
    )


@pytest.mark.parametrize(("month", "day", "event_hour", "count"), [(3, 29, 1, 2), (10, 25, 0, 4)])
def test_current_event_selects_real_half_hours_across_dst(
    month: int, day: int, event_hour: int, count: int
) -> None:
    config = _same_day_cfg()
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, month, day, 0, 0, tzinfo=tz)
    start = datetime(2026, month, day, event_hour, tzinfo=UTC)
    event = replace(_SAME_DAY_EVENT, start=start, end=start + timedelta(minutes=30 * count))
    inputs = _current_event_inputs(_same_day_inputs(soc_now=90, now=now, event=event), config)

    plan = _plan(inputs, config)

    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == tuple(start + timedelta(minutes=30 * i) for i in range(count))
    assert export.event_identity == event.identity
    assert plan.export_revenue == pytest.approx(2.7 * 0.5 * count)
    assert plan.required_kwh == 0.0


@pytest.mark.parametrize(("strategy", "soc"), [("reserve", 60.0), ("fill", 90.0)])
def test_current_event_is_refilled_by_the_next_charge(strategy: str, soc: float) -> None:
    config = replace(_same_day_cfg(), strategy=strategy)
    inputs = _current_event_inputs(
        _same_day_inputs(soc_now=soc, now=datetime(2026, 6, 8, 16, tzinfo=UTC)), config
    )

    without = _plan(replace(inputs, flexibility_event=None), config)
    with_export = _plan(inputs, config)

    assert with_export.charge_intent.export is not None
    assert with_export.required_kwh == pytest.approx(without.required_kwh + 2.7)
    assert with_export.pre_window_drain_kwh == pytest.approx(3.75 + 2.7)


def test_current_pre_window_export_overlapping_charge_keeps_zero_purchase() -> None:
    config = _same_day_cfg()
    event = replace(
        _SAME_DAY_EVENT, start=datetime(2026, 6, 8, 23, tzinfo=UTC),
        end=datetime(2026, 6, 9, 0, tzinfo=UTC),
    )
    inputs = _current_event_inputs(
        _same_day_inputs(
            soc_now=60, now=datetime(2026, 6, 8, 22, 30, tzinfo=UTC), event=event
        ), config,
    )

    plan = _plan(inputs, config)

    export = plan.charge_intent.export
    assert export is not None
    assert (export.window_start, export.window_end) == (event.start, event.end)
    assert plan.required_kwh == 0.0
    # Export wins the window: the target is the expected window-start SoC, not
    # decision B's min_soc + O (20 + 9.0/26.88), so a rate-based charger sizes
    # 0 A instead of charging into the export window.
    assert plan.charge_intent.expected_window_start_soc is not None
    assert plan.target_soc == plan.charge_intent.expected_window_start_soc
    assert plan.target_soc < 20 + 9.0 / 26.88 * 100
    assert plan.soc_path == ()


def test_current_post_window_event_charge_cap_uses_energy_at_window_start() -> None:
    config = replace(_same_day_cfg(), max_current_a=1000 / (51 * 6))  # 1 kWh charge limit
    event = replace(
        _SAME_DAY_EVENT,
        start=datetime(2026, 6, 9, 19, tzinfo=UTC),
        end=datetime(2026, 6, 9, 19, 30, tzinfo=UTC),
    )
    inputs = _current_event_inputs(
        _same_day_inputs(
            soc_now=20 + 2 / 26.88 * 100,
            now=datetime(2026, 6, 8, 16, tzinfo=UTC), event=event,
        ), config,
    )
    assert inputs.geometry is not None
    inputs = replace(inputs, load_slots=tuple(
        2.0 if i == 0 else 0.0 for i in range(len(inputs.geometry.slot_starts))
    ))

    plan = _plan(inputs, config)

    # The 2 kWh present now is spent before charging. Only 1 kWh can be
    # added, less than the 1.6 kWh needed for the post-window export slot.
    assert plan.charge_intent.export is None
    assert plan.overnight_charge_capped is True
    assert plan.export_skips[0].reason == (
        "Skipped paid slot: funding it would consume reserved house or post-event energy."
    )
    assert plan.export_soc_needed_pct == pytest.approx(20 + 1.6 / 26.88 * 100)


# --- funding an event from the post-event reserve (#207) ---


def _reserve_trade_plan(*, rate: float, prices: dict[int, float] | None = None) -> Any:
    """A 17:00-18:00 event 0.6 kWh short of funding its 17:00 slot.

    1 kW house load all day. At the 90% cap 19.6 kWh is usable: 12.5 kWh of house
    load through the event, 5.5 kWh reserved 18:00-23:30, and 1.6 kWh for two
    2.2 kW export slots that need 2.2 kWh. Import is £0.30 at 90% round trip.
    """
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
        rate_gbp_kwh=rate,
    )
    inputs = PlannerInputs(
        soc=_soc(20.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=(0.5,) * 48, solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START, flexibility_event=event,
    )
    config = cfg(
        capacity_kwh=28.0, battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36,
        supply_max_current_a=75.0, supply_voltage_v=240.0, charge_efficiency=0.9,
        max_current_a=_FILLS_THE_BATTERY_A,
    )
    schedule = fixed_schedule(inputs, config)
    if prices:
        schedule = replace(
            schedule,
            prices=tuple(prices.get(i, p) for i, p in enumerate(schedule.prices)),
        )
    return compute_plan(inputs, config, schedule)


def test_axle_export_spends_the_post_event_reserve_when_buying_it_back_costs_less() -> None:
    """£1.00 event vs £0.30 / 0.9 = £0.33 buy-back: the 17:00 slot is worth funding."""
    plan = _reserve_trade_plan(rate=1.0)

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert export.selected_slots == (
        datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        datetime(2026, 6, 9, 17, 30, tzinfo=UTC),
    )
    assert plan.export_skips == ()
    _, post_event_reservation = plan.reservations
    # With the 62.5 A ceiling, 1.075 kWh of the 5.5 kWh reserve
    # now goes to export instead of 0.6 kWh.
    assert post_event_reservation.energy_kwh == pytest.approx(4.425)
    assert plan.export_funding is not None
    assert "post-event reserve: £1.00 event vs £0.33 buy-back" in plan.export_funding


def test_axle_export_keeps_the_post_event_reserve_when_buying_it_back_costs_more() -> None:
    """£0.30 event vs £0.33 buy-back: the reserve stays put and the slot is skipped."""
    plan = _reserve_trade_plan(rate=0.30)

    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == (datetime(2026, 6, 9, 17, 30, tzinfo=UTC),)
    assert "reserved house or post-event energy" in plan.export_skips[0].reason
    assert plan.reservations[1].energy_kwh == pytest.approx(5.5)
    assert plan.export_funding is None


def test_axle_export_reports_soc_needed_for_the_funded_suffix() -> None:
    """At event start, include both export slots and the buffered post-event load."""
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
        rate_gbp_kwh=0.30,
    )
    inputs = PlannerInputs(
        soc=_soc(56.0),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=24.0,
        load_slots=(0.5,) * 48,
        solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START,
        flexibility_event=event,
    )
    config = cfg(
        capacity_kwh=28.0,
        target_cap=96.0,
        buffer_pct=20.0,
        battery_discharge_ceiling_kw=3.2,
        dno_export_limit_kw=7.36,
        supply_max_current_a=75.0,
    )

    plan = _plan(inputs, config)

    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == (event.start + timedelta(minutes=30),)
    assert "funding it would consume" in plan.export_skips[0].reason
    assert plan.export_soc_needed_pct == pytest.approx(55.0)
    assert plan.export_soc_needed_at == event.start


def test_axle_export_counts_earlier_slots_made_unavailable_by_a_funding_skip() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 30, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
        rate_gbp_kwh=0.30,
    )
    inputs = PlannerInputs(
        soc=_soc(56.0),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=12.0,
        load_slots=(0.25,) * 48,
        solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START,
        flexibility_event=event,
    )
    config = cfg(
        capacity_kwh=28.0,
        target_cap=60.0,
        buffer_pct=20.0,
        battery_discharge_ceiling_kw=3.2,
        dno_export_limit_kw=7.36,
        supply_max_current_a=75.0,
    )

    plan = _plan(inputs, config)

    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == (event.end - timedelta(minutes=30),)
    assert "a later slot is unavailable" in plan.export_skips[0].reason
    assert "funding it would consume" in plan.export_skips[1].reason
    # All three event slots are recoverable with more SoC: 0.75 house, 4.05 export,
    # and 3.00 buffered post-event kWh.
    assert plan.export_soc_needed_pct == pytest.approx(20.0 + 7.8 / 28.0 * 100.0)
    assert plan.export_soc_needed_at == event.start


@pytest.mark.parametrize(
    ("dear_slot", "funded"),
    [
        (37, True),  # 18:00: the battery still covers it, so its price is never paid
        (46, False),  # 22:30: the 0.6 kWh shortfall lands in the last two slots
    ],
)
def test_axle_export_prices_the_buy_back_where_the_shortfall_lands(
    dear_slot: int, funded: bool
) -> None:
    """The battery runs dry at the end of the stretch, so only the tail is bought back."""
    plan = _reserve_trade_plan(rate=1.0, prices={dear_slot: 2.0})

    export = plan.charge_intent.export
    assert export is not None
    assert (len(export.selected_slots) == 2) is funded


def test_same_day_export_spends_the_post_event_reserve_at_the_standard_rate() -> None:
    """Before the horizon there are no slot prices, so buy-back is the standard rate."""
    # 5.6 kWh usable: 2.0 kWh of house load to 20:00, 1.75 kWh reserved to the
    # 23:30 window, then 1.85 kWh for two 1.35 kWh slots. 0.85 kWh is short.
    soc = 20.0 + 5.6 / 26.88 * 100.0
    plan = _same_day_plan(
        _same_day_inputs(soc_now=soc, now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC)),
        _same_day_cfg(),
    )

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert len(export.selected_slots) == 2
    assert plan.export_funding is not None
    assert "£1.00 event vs £0.30 buy-back" in plan.export_funding


def test_same_day_export_never_spends_more_than_the_post_event_reserve() -> None:
    """The reserve is the only extra: house load before and during the event stays put."""
    # 4.5 kWh usable leaves 0.75 kWh after house load and the reserve: the
    # 1.75 kWh reserve can fund one 1.35 kWh slot but not both (2.7 > 2.5).
    soc = 20.0 + 4.5 / 26.88 * 100.0
    plan = _same_day_plan(
        _same_day_inputs(soc_now=soc, now=datetime(2026, 6, 8, 16, 0, tzinfo=UTC)),
        _same_day_cfg(),
    )

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert export.selected_slots == (datetime(2026, 6, 8, 19, 30, tzinfo=UTC),)


def test_axle_export_skips_a_slot_the_fixed_current_would_push_over_the_dno_limit() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 17, 30, tzinfo=UTC), direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC), rate_gbp_kwh=1.0,
    )
    plan = _plan(
        PlannerInputs(
            soc=_soc(90.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
            load_slots=(0.0,) * 48, solar_slots=(0.0,) * 48,
            horizon_start=_HORIZON_START, flexibility_event=event,
        ),
        cfg(battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=2.0),
    )

    assert plan.charge_intent.export is None
    assert "fixed discharge command would exceed the DNO" in plan.export_skips[0].reason
    assert plan.export_soc_needed_pct is None
    assert plan.export_soc_needed_at is None


def test_axle_soc_needed_excludes_a_dno_refusal_before_the_funded_suffix() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 19, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
        rate_gbp_kwh=0.30,
    )
    loads = [0.5] * 48
    loads[35] = 0.0  # 17:00 would exceed the DNO ceiling at the fixed discharge current.
    inputs = PlannerInputs(
        soc=_soc(56.0),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=sum(loads),
        load_slots=tuple(loads),
        solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START,
        flexibility_event=event,
    )
    config = cfg(
        capacity_kwh=28.0,
        target_cap=94.0,
        battery_discharge_ceiling_kw=3.2,
        dno_export_limit_kw=2.5,
        supply_max_current_a=75.0,
    )

    plan = _plan(inputs, config)

    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == (
        event.start + timedelta(hours=1),
        event.start + timedelta(hours=1, minutes=30),
    )
    assert "DNO or supply limit" in plan.export_skips[0].reason
    assert "funding it would consume" in plan.export_skips[1].reason
    # Count only 17:30 onward: 1.50 house, 3.30 export, and 4.50 post-event kWh.
    assert plan.export_soc_needed_pct == pytest.approx(20.0 + 9.3 / 28.0 * 100.0)
    assert plan.export_soc_needed_at == event.start + timedelta(minutes=30)


def test_axle_export_reports_when_more_than_a_full_battery_is_needed() -> None:
    event = FlexibilityEvent(
        start=_HORIZON_START + timedelta(hours=23),
        end=_HORIZON_START + timedelta(days=1),
        direction="export",
        updated_at=_HORIZON_START + timedelta(hours=22),
        rate_gbp_kwh=0.30,
    )
    inputs = PlannerInputs(
        soc=_soc(50.0),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=12.0,
        load_slots=(0.25,) * 48,
        solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START,
        flexibility_event=event,
    )
    config = cfg(
        capacity_kwh=1.5,
        battery_discharge_ceiling_kw=3.2,
        dno_export_limit_kw=3.68,
    )

    plan = _plan(inputs, config)

    assert plan.charge_intent.export is None
    assert "funding it would consume" in plan.export_skips[-1].reason
    assert plan.export_soc_needed_pct == pytest.approx(20.0 + 3.2 / 1.5 * 100.0)
    assert plan.export_soc_needed_pct > 100.0
    assert plan.export_soc_needed_at == event.start


def test_axle_export_skips_a_slot_the_fixed_current_would_push_over_the_supply_limit() -> None:
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 17, 30, tzinfo=UTC), direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC), rate_gbp_kwh=1.0,
    )
    plan = _plan(
        PlannerInputs(
            soc=_soc(90.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
            load_slots=(0.0,) * 48, solar_slots=(0.0,) * 48,
            horizon_start=_HORIZON_START, flexibility_event=event,
        ),
        cfg(
            battery_discharge_ceiling_kw=3.2,
            dno_export_limit_kw=7.36,
            supply_max_current_a=10.0,
            supply_voltage_v=240.0,
        ),
    )

    assert plan.charge_intent.export is None
    assert "DNO or supply limit" in plan.export_skips[0].reason


# --- v2 per-slot model ---

# Horizon origin: tonight 23:30 (the window start).
_HORIZON_START = datetime(2026, 6, 8, 23, 30, tzinfo=UTC)


def _slot_inputs(
    load: float = 0.5,
    solar_slots: tuple[float, ...] | None = None,
    dispatches: tuple[DispatchSlot, ...] = (),
    soc_now: float = 20.0,
) -> PlannerInputs:
    load_slots = (load,) * 48
    return PlannerInputs(soc=_soc(soc_now),
        solar_tomorrow_kwh=sum(solar_slots) if solar_slots else 0.0,
        predicted_home_load_kwh=sum(load_slots),
        dispatches=dispatches,
        load_slots=load_slots,
        solar_slots=solar_slots,
        horizon_start=_HORIZON_START,
    )


def test_slot_model_charges_for_expensive_slots_only() -> None:
    # 0.5 kWh per slot; 12 window slots are cheap -> 36 expensive slots = 18 kWh.
    plan = _plan(_slot_inputs(load=0.5, soc_now=20.0), cfg())
    assert plan.model == "slots"
    assert plan.expensive_load_kwh == pytest.approx(18.0)
    # SoC at min -> usable 0; required = 18 kWh within battery room (18.816).
    assert plan.required_kwh == pytest.approx(18.0)


def test_slot_model_reservation_drives_required_charge() -> None:
    # The reservation carries the existing buffer, while required_kwh subtracts
    # the energy already available at the window start.
    plan = _plan(_slot_inputs(load=0.2, soc_now=50.0), cfg(buffer_pct=20.0))
    assert len(plan.reservations) == 1
    reservation = plan.reservations[0]
    assert reservation.name == "reach-next-cheap-slot"
    assert reservation.obligation_kind == "reach-next-cheap-slot"
    assert reservation.target_slot == 48
    assert reservation.energy_kwh == pytest.approx(8.64)
    assert plan.required_kwh == pytest.approx(8.64 - plan.usable_now_kwh)


def test_slot_model_reservation_targets_a_cheap_slot_mid_horizon() -> None:
    load_slots = (1.0,) * 8
    inputs = PlannerInputs(
        soc=_soc(20.0),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=8.0,
        load_slots=load_slots,
        solar_slots=(0.0,) * 8,
        horizon_start=_HORIZON_START,
    )
    schedule = TariffSchedule(
        cheap_rate=0.069,
        standard_rate=0.30,
        export_rate=0.0,
        window_hours=0.5,
        prices=(0.30, 0.30, 0.30, 0.30, 0.069, 0.30, 0.30, 0.30),
        cheap_fracs=(0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
    )
    plan = compute_plan(inputs, cfg(buffer_pct=0.0), schedule)

    reservation = plan.reservations[0]
    assert reservation.target_slot == 4
    assert reservation.target_time == _HORIZON_START + timedelta(hours=2)
    assert reservation.energy_kwh == pytest.approx(4.0)
    assert "cheap slot" in reservation.reason


def test_slot_model_reservation_cap_names_shortfall() -> None:
    plan = _plan(_slot_inputs(load=2.0, soc_now=20.0), cfg(buffer_pct=20.0))

    reservation = plan.reservations[0]
    assert reservation.energy_kwh == pytest.approx(26.88 * 0.70)
    assert "capped" in reservation.reason
    assert "shortfall" in reservation.reason


def test_slot_model_subtracts_solar_per_slot() -> None:
    # 2 kWh of solar in a 0.5 kWh slot only removes that slot's load — per-slot
    # clamping means excess solar can't offset other slots.
    solar = [0.0] * 48
    solar[24] = 2.0  # 11:30 next day
    solar[26] = 2.0
    plan = _plan(_slot_inputs(load=0.5, solar_slots=tuple(solar)), cfg())
    assert plan.expensive_load_kwh == pytest.approx(17.0)  # 18 - 2 * 0.5
    assert plan.required_kwh == pytest.approx(17.0)


def test_slot_model_dispatch_overlap_is_fractional() -> None:
    # A dispatch covering one full expensive slot removes 0.5 kWh from the need
    # and still emits stop_discharge.
    d_start = _HORIZON_START + timedelta(hours=14)  # 13:30 next day
    dispatch = DispatchSlot(d_start, d_start + timedelta(minutes=30), -2.0, "SMART")
    plan = _plan(_slot_inputs(load=0.5, dispatches=(dispatch,)), cfg())
    assert plan.expensive_load_kwh == pytest.approx(17.5)
    assert len(plan.charge_intent.holds) == 1
    assert plan.cheap_covered_kwh == pytest.approx(0.5)


def test_slot_model_costs() -> None:
    plan = _plan(_slot_inputs(load=0.5, soc_now=20.0), cfg())
    # Baseline: 6 cheap kWh at 0.069 + 18 expensive kWh at 0.30.
    assert plan.baseline_cost == pytest.approx(6 * 0.069 + 18 * 0.30)
    # Planned: cheap load + the 18 kWh charge all at off-peak, nothing uncovered.
    assert plan.planned_cost == pytest.approx((6 + 18) * 0.069)
    assert plan.planned_cost < plan.baseline_cost


def test_daily_model_also_reports_costs() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    plan = _plan(inp, cfg())
    assert plan.model == "daily"
    assert plan.expensive_load_kwh is None
    assert plan.baseline_cost is not None and plan.planned_cost is not None
    assert plan.planned_cost <= plan.baseline_cost + 1e-9


def test_slot_model_respects_battery_room_and_max_current() -> None:
    plan = _plan(_slot_inputs(load=2.0, soc_now=85.0), cfg())
    battery_room = 26.88 * (90 - 85) / 100
    assert plan.required_kwh == pytest.approx(battery_room)
    assert plan.charge_intent.target_soc_pct <= 90.0 + 1e-9


def test_fill_strategy_charges_to_cap() -> None:
    inp = PlannerInputs(soc=_soc(69), solar_tomorrow_kwh=3.4, predicted_home_load_kwh=17.7)
    plan = _plan(inp, cfg(strategy="fill"))
    battery_room = 26.88 * (90 - 69) / 100
    assert plan.required_kwh == pytest.approx(battery_room)
    assert plan.target_soc == pytest.approx(90.0)
    assert plan.strategy == "fill"
    # Fill ignores need entirely: it buys more than the deficit strategy would.
    assert plan.required_kwh > _plan(inp, cfg()).required_kwh


def test_fill_strategy_zero_at_cap() -> None:
    inp = PlannerInputs(soc=_soc(90), solar_tomorrow_kwh=3.4, predicted_home_load_kwh=17.7)
    plan = _plan(inp, cfg(strategy="fill"))
    assert plan.required_kwh == 0.0
    assert plan.charge_intent.target_soc_pct == pytest.approx(plan.soc_now)


def test_pre_window_drain_reduces_usable() -> None:
    base = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    drained = PlannerInputs(soc=_soc(30),
        solar_tomorrow_kwh=8.75,
        predicted_home_load_kwh=24.2,
        pre_window_drain_kwh=1.0,
    )
    plan_base = _plan(base, cfg())
    plan_drained = _plan(drained, cfg())
    # The kWh drained before the window must be bought back on top.
    assert plan_drained.required_kwh == pytest.approx(plan_base.required_kwh + 1.0)
    assert plan_drained.pre_window_drain_kwh == 1.0
    # Raw usable_now is reported unchanged; only sizing uses the window value.
    assert plan_drained.usable_now_kwh == pytest.approx(plan_base.usable_now_kwh)


def test_charge_efficiency_inflates_purchase_and_current() -> None:
    inp = PlannerInputs(soc=_soc(20), solar_tomorrow_kwh=3, predicted_home_load_kwh=10)
    lossless = _plan(inp, cfg())
    lossy = _plan(inp, cfg(charge_efficiency=0.9))
    # Stored energy target is unchanged; the AC purchase and current grow by 1/0.9.
    assert lossy.required_kwh == pytest.approx(lossless.required_kwh)
    assert lossy.charge_intent.target_soc_pct == pytest.approx(
        lossless.charge_intent.target_soc_pct
    )
    assert lossy.charge_efficiency == pytest.approx(0.9)
    assert lossless.planned_cost is not None and lossy.planned_cost is not None
    extra_buy = lossless.required_kwh / 0.9 - lossless.required_kwh
    assert lossy.planned_cost == pytest.approx(lossless.planned_cost + extra_buy * 0.069)


def test_export_revenue_adjusts_both_costs_equally() -> None:
    # Daily model: 10 kWh solar vs 6 kWh load -> 4 kWh exported.
    inp = PlannerInputs(soc=_soc(50), solar_tomorrow_kwh=10, predicted_home_load_kwh=6)
    without = _plan(inp, cfg())
    with_export = _plan(inp, cfg(rate_export=0.15))
    assert without.export_revenue is None
    assert with_export.export_revenue == pytest.approx(4 * 0.15)
    assert without.baseline_cost is not None and with_export.baseline_cost is not None
    assert without.planned_cost is not None and with_export.planned_cost is not None
    assert with_export.baseline_cost == pytest.approx(without.baseline_cost - 0.6)
    assert with_export.planned_cost == pytest.approx(without.planned_cost - 0.6)
    # The decision (required charge) is unchanged.
    assert with_export.required_kwh == pytest.approx(without.required_kwh)


def test_slot_model_export_revenue_sums_per_slot_surplus() -> None:
    solar = [0.0] * 48
    solar[24] = 2.0  # 0.5 kWh load slots -> 1.5 kWh surplus in this slot
    plan = _plan(
        _slot_inputs(load=0.5, solar_slots=tuple(solar)), cfg(rate_export=0.10)
    )
    assert plan.export_revenue == pytest.approx(1.5 * 0.10)


def test_dispatch_ev_kwh_sums_magnitudes() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        dispatches=(_slot(13, 0), _slot(2, 0)),
    )
    # each fixture dispatch plans -2.0 kWh into the car
    assert _plan(inp, cfg()).dispatch_ev_kwh == pytest.approx(4.0)


def test_dispatch_ev_kwh_none_without_dispatches() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    assert _plan(inp, cfg()).dispatch_ev_kwh is None


# --- the charge window's current ceiling and the expected SoC path (#208) ---


def _rate_capped_plan(soc: float = 20.0, **kw: Any) -> Any:
    """A 17:00-18:00 event the charge window can't fund at 62.5 A.

    40 kWh at 20%: 28 kWh fills to the 90% cap, but 6 h x 62.5 A x 51 V adds
    only 19.125 kWh. 1 kW house load all day, no solar.
    """
    event = FlexibilityEvent(
        start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
        rate_gbp_kwh=0.30,
    )
    inputs = PlannerInputs(
        soc=_soc(soc), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=(0.5,) * 48, solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START, flexibility_event=event, **kw,
    )
    return _plan(
        inputs,
        cfg(capacity_kwh=40.0, battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36),
    )


def test_axle_export_is_funded_only_from_what_the_charge_window_can_add() -> None:
    plan = _rate_capped_plan()

    # 19.125 kWh reachable less 12.5 kWh of house load to the event end and
    # 5.5 kWh reserved after it leaves 1.125 kWh: one 1.1 kWh slot, not two.
    export = plan.charge_intent.export
    assert export is not None
    assert export.selected_slots == (datetime(2026, 6, 9, 17, 30, tzinfo=UTC),)
    assert plan.overnight_charge_capped is True
    assert plan.required_kwh <= 19.125 + 1e-9


def test_overnight_charge_is_capped_at_what_the_window_can_add() -> None:
    inp = PlannerInputs(soc=_soc(20.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=40.0)

    plan = _plan(inp, cfg(capacity_kwh=40.0))

    # 40 kWh deficit, 28 kWh of battery room, but 6 h x 62.5 A x 51 V = 19.125 kWh.
    assert plan.required_kwh == pytest.approx(19.125)


def test_inside_the_window_only_the_rest_of_it_counts() -> None:
    plan = _rate_capped_plan(now=_HORIZON_START + timedelta(hours=3))

    # Three hours left: 9.5625 kWh can't fund either slot.
    assert plan.required_kwh == pytest.approx(9.5625)
    assert plan.charge_intent.export is None
    assert plan.overnight_charge_capped is True


@pytest.mark.parametrize(
    ("hours_after_start", "expected_kwh"),
    [(-1.0, 19.1), (3.0, 9.5625), (6.0, 0.0)],
)
def test_recovery_plan_charge_limit_uses_remaining_wrapping_window(
    hours_after_start: float, expected_kwh: float
) -> None:
    plan = _rate_capped_plan(now=_HORIZON_START + timedelta(hours=hours_after_start))
    assert plan.required_kwh == pytest.approx(expected_kwh)


@pytest.mark.parametrize(
    ("hours_after_start", "expected_kwh"),
    [(-1.0, 12.75), (2.0, 6.375), (4.0, 0.0)],
)
def test_recovery_plan_charge_limit_uses_remaining_daytime_window(
    hours_after_start: float, expected_kwh: float
) -> None:
    start = datetime(2026, 6, 10, 1, 0, tzinfo=UTC)
    inputs = PlannerInputs(
        soc=_soc(20.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=40.0,
        horizon_start=start, now=start + timedelta(hours=hours_after_start),
    )
    plan = _plan(inputs, cfg(capacity_kwh=40.0, window_start=time(1), window_end=time(5)))
    assert plan.required_kwh == pytest.approx(expected_kwh)


def test_a_full_battery_within_reach_is_not_capped() -> None:
    # From 50%, 12 kWh usable plus 19.125 kWh more passes the 28 kWh cap.
    plan = _rate_capped_plan(soc=50.0)

    assert plan.overnight_charge_capped is False


def test_plan_carries_the_expected_soc_path_from_the_window_end() -> None:
    plan = _rate_capped_plan()

    window_end = _HORIZON_START + timedelta(hours=6)
    assert plan.soc_path[0] == (window_end, pytest.approx(plan.target_soc))
    by_time = dict(plan.soc_path)
    # 1 kW of house load drains 1.25 points a half hour of a 40 kWh battery.
    assert by_time[window_end + timedelta(minutes=30)] == pytest.approx(plan.target_soc - 1.25)
    # The 17:30 export slot also drains 1.1 kWh of export: 4 points in all.
    at_1730 = by_time[datetime(2026, 6, 9, 17, 30, tzinfo=UTC)]
    at_1800 = by_time[datetime(2026, 6, 9, 18, 0, tzinfo=UTC)]
    assert at_1730 - at_1800 == pytest.approx(1.25 + 2.75)
    assert len(plan.soc_path) == 48 - 12


def test_daily_model_has_no_soc_path() -> None:
    inp = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)

    assert _plan(inp, cfg()).soc_path == ()


def test_winter_heavy_day_v2l_refills_house_battery() -> None:
    """A 30 kWh day (10 base + 20 heat pump) exceeds the 18.8 kWh battery."""
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36,
        horizon_start=datetime(2026, 1, 10, 23, 30, tzinfo=UTC),
        car_plugged_in=True,
    )
    plan = _plan(inp, cfg(v2l_budget_kwh=15))
    assert sum(action.kwh for action in plan.v2l_refills) == pytest.approx(11.184)
    assert all(action.rate_kw == 1.5 for action in plan.v2l_refills)
    assert all('reach-next-cheap-slot' in action.reason for action in plan.v2l_refills)
    assert all(12 <= action.slot < 47 for action in plan.v2l_refills)
    assert not plan.v2l_wanted_but_unavailable


@pytest.mark.parametrize(('budget', 'plugged', 'grid', 'efficiency', 'wanted'), [
    (0, True, 0.30, 0.85, False),
    (15, False, 0.30, 0.85, True),
    (15, True, 0.06, 0.85, False),
    (15, True, 0.10, 0.50, False),
])
def test_v2l_availability_and_loss_adjusted_price(
    budget: float, plugged: bool, grid: float, efficiency: float, wanted: bool,
) -> None:
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36, car_plugged_in=plugged,
    )
    plan = _plan(inp, cfg(v2l_budget_kwh=budget, rate_peak=grid,
                          v2l_round_trip_efficiency=efficiency))
    assert plan.v2l_refills == ()
    assert plan.v2l_wanted_but_unavailable is wanted


def test_v2l_budget_and_rate_bound_refills() -> None:
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36, car_plugged_in=True,
    )
    plan = _plan(inp, cfg(v2l_budget_kwh=2, v2l_max_rate_kw=1,
                          v2l_preferred_rate_kw=1.5))
    assert sum(a.kwh for a in plan.v2l_refills) == pytest.approx(2)
    assert all(a.rate_kw == 1 for a in plan.v2l_refills)
    for slot in {a.slot for a in plan.v2l_refills}:
        assert sum(a.kwh for a in plan.v2l_refills if a.slot == slot) <= 0.5


def test_v2l_not_needed_when_solar_surplus_refills_battery() -> None:
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=15, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (1.0,) * 12 + (0.0,) * 3 + (1.0,) * 18 + (0.0,) * 3,
        solar_slots=(0.0,) * 24 + (5.0,) * 3 + (0.0,) * 21,
        car_plugged_in=True,
    )
    assert _plan(inp, cfg(v2l_budget_kwh=15)).v2l_refills == ()


def test_v2l_refills_appear_in_expected_soc_path_and_cost() -> None:
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36, car_plugged_in=True,
        horizon_start=datetime(2026, 1, 10, 23, 30, tzinfo=UTC),
    )
    without = _plan(inp, cfg())
    with_v2l = _plan(inp, cfg(v2l_budget_kwh=15))
    assert with_v2l.soc_path[-1][1] > without.soc_path[-1][1]
    assert with_v2l.planned_cost == pytest.approx(
        without.planned_cost - 11.184 * (0.30 - 0.07 / 0.85)
    )


def test_v2l_does_not_refill_a_funded_reservation_or_assume_car_is_present() -> None:
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=10,
        load_slots=(0.0,) * 12 + (10 / 36,) * 36,
    )
    assert not inp.car_plugged_in
    assert _plan(inp, cfg(v2l_budget_kwh=15)).v2l_refills == ()
    assert not _plan(inp, cfg(v2l_budget_kwh=15)).v2l_wanted_but_unavailable


def test_v2l_cannot_be_retroactively_stored_for_an_earlier_shortfall() -> None:
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30.0,) + (0.0,) * 35, car_plugged_in=True,
    )
    plan = _plan(inp, cfg(v2l_budget_kwh=15))
    # Overnight already fills the battery: no battery room before the one heavy slot.
    assert plan.v2l_refills == ()


def test_v2l_prices_the_shortfall_slot_not_the_representative_peak_rate() -> None:
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36, car_plugged_in=True,
    )
    config = cfg(v2l_budget_kwh=15)
    schedule = fixed_schedule(inp, config)
    schedule = replace(schedule, prices=(0.01,) * 48)
    assert compute_plan(inp, config, schedule).v2l_refills == ()


def test_v2l_soc_path_starts_after_pre_window_drain() -> None:
    inp = PlannerInputs(
        soc=_soc(90), solar_tomorrow_kwh=0, predicted_home_load_kwh=20,
        pre_window_drain_kwh=5,
        load_slots=(0.0,) * 12 + (20 / 36,) * 36, car_plugged_in=True,
        horizon_start=datetime(2026, 1, 10, 23, 30, tzinfo=UTC),
    )
    plan = _plan(inp, cfg(v2l_budget_kwh=15))
    assert sum(action.kwh for action in plan.v2l_refills) == pytest.approx(6.184)
    assert plan.soc_path[0][1] == pytest.approx(71.3988095238)
    # At the final slot start only its 20/36 kWh remains above the 20% floor.
    assert plan.soc_path[-1][1] == pytest.approx(22.0667989418)


@pytest.mark.parametrize(
    ("day", "slots", "hours"),
    [(datetime(2026, 3, 28), 46, 5), (datetime(2026, 10, 24), 50, 7)],
)
def test_geometry_dst_window_limit_and_soc_path(day: datetime, slots: int, hours: int) -> None:
    tz = ZoneInfo("Europe/London")
    now = day.replace(hour=23, minute=30, tzinfo=tz)
    config = cfg(strategy="fill", capacity_kwh=100)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.0,) * len(geom.slot_starts),
    )
    plan = _plan(inp, config)
    assert len(plan.slot_prices or ()) == slots
    assert plan.required_kwh == pytest.approx(62.5 * 51 * hours / 1000)
    assert plan.soc_path[0] == (geom.window_end, plan.target_soc)
    assert len(plan.soc_path) == slots - hours * 2


def test_geometry_partial_window_coverage_and_soc_path() -> None:
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 10, 23, 20, tzinfo=tz)
    config = cfg(window_start=time(23, 20), window_end=time(5, 20), strategy="fill",
                 capacity_kwh=100)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.0,) * len(geom.slot_starts),
    )
    plan = _plan(inp, config)
    assert len(plan.slot_prices or ()) == 49
    assert plan.slot_prices is not None
    # Only 23:20-23:30 of slot 0 is still to come, all of it in the window.
    assert plan.slot_prices[0] == pytest.approx(config.rate_offpeak)
    assert plan.required_kwh == pytest.approx(62.5 * 51 * 6 / 1000)
    assert plan.soc_path[0] == (geom.window_end, plan.target_soc)


@pytest.mark.parametrize(
    ("now", "window_start", "window_end", "event_start", "event_end", "conflict"),
    [
        (datetime(2026, 3, 28, 23, 30), time(23, 30), time(5, 30),
         datetime(2026, 3, 29, 5, 30), datetime(2026, 3, 29, 6, 45), False),
        (datetime(2026, 10, 24, 23, 30), time(23, 30), time(5, 30),
         datetime(2026, 10, 25, 5), datetime(2026, 10, 25, 5, 45), True),
        (datetime(2026, 1, 10, 23, 20), time(23, 20), time(5, 20),
         datetime(2026, 1, 11, 5), datetime(2026, 1, 11, 5, 45), True),
    ],
)
def test_geometry_event_containment_and_charge_conflict(
    now: datetime, window_start: time, window_end: time,
    event_start: datetime, event_end: datetime, conflict: bool,
) -> None:
    tz = ZoneInfo("Europe/London")
    now = now.replace(tzinfo=tz)
    config = cfg(window_start=window_start, window_end=window_end,
                 capacity_kwh=100, strategy="fill")
    geom = compute_horizon_geometry(now, window_start, window_end, tz)
    event = FlexibilityEvent(
        start=event_start.replace(tzinfo=tz), end=event_end.replace(tzinfo=tz),
        direction="export", rate_gbp_kwh=1, updated_at=now,
    )
    inp = PlannerInputs(
        soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        geometry=geom, horizon_start=geom.horizon_start, now=now,
        load_slots=(0.0,) * len(geom.slot_starts), flexibility_event=event,
    )
    plan = _plan(inp, config)
    export = plan.charge_intent.export
    assert export is not None
    expected = tuple(
        start for start in geom.slot_starts
        if event.start <= start and start + timedelta(minutes=30) <= event.end
    )
    assert export.selected_slots == expected
    assert export.window_end == expected[-1] + timedelta(minutes=30)
    assert (plan.required_kwh == 0) is conflict
    assert plan.export_revenue == pytest.approx(
        sum(export.slot_export_kw) * 0.5 * event.rate_gbp_kwh
    )


def test_geometry_v2l_refill_uses_only_the_part_after_the_window() -> None:
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 10, 23, 30, tzinfo=tz)
    config = cfg(window_start=time(23, 30), window_end=time(5, 20),
                 max_current_a=0, v2l_budget_kwh=5)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    loads = [0.0] * len(geom.slot_starts)
    first_full_post_window = geom.window_slot_range[1]
    loads[first_full_post_window] = 1
    inp = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=1,
        geometry=geom, horizon_start=geom.horizon_start, now=now,
        load_slots=tuple(loads), car_plugged_in=True,
    )
    plan = _plan(inp, config)
    assert len(plan.v2l_refills) == 1
    refill = plan.v2l_refills[0]
    assert refill.slot == first_full_post_window - 1
    assert refill.kwh == pytest.approx(1.5 / 6)  # ten minutes after 05:20
    assert plan.soc_path[0] == (geom.window_end, config.min_soc)
    assert plan.soc_path[1][1] == pytest.approx(config.min_soc + 0.25 / 26.88 * 100)
    assert _plan(replace(inp, car_plugged_in=False), config).v2l_refills == ()


@pytest.mark.parametrize("solar_source", ["today", "served", "missing"])
def test_geometry_today_event_daylight_refusal(solar_source: str) -> None:
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 5, 1, tzinfo=tz)
    config = cfg(capacity_kwh=100, strategy="fill")
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    start = datetime(2026, 1, 5, 17, tzinfo=tz)
    solar = tuple(
        1.0 if slot.hour == 17 and slot.minute == 0 and solar_source != "missing" else 0.0
        for slot in geom.slot_starts
    )
    # A served-day profile can show daylight at the same clock time even
    # when today's forecast has no solar in the event slot.
    if solar_source == "served":
        geom = compute_horizon_geometry(
            datetime(2026, 1, 5, 6, tzinfo=tz),
            config.window_start, config.window_end, tz,
        )
        solar = tuple(
            1.0 if slot.hour == 17 and slot.minute == 0 and slot.date() == geom.served_day
            else 0.0 for slot in geom.slot_starts
        )
    inp = PlannerInputs(
        soc=_soc(50), solar_tomorrow_kwh=1, predicted_home_load_kwh=0,
        geometry=geom, horizon_start=geom.horizon_start, now=now,
        load_slots=(0.0,) * len(geom.slot_starts), solar_slots=solar,
        flexibility_event=FlexibilityEvent(
            start=start, end=start + timedelta(minutes=30), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
    )
    plan = _plan(inp, config)
    if solar_source == "missing":
        assert plan.charge_intent.export is not None
    else:
        assert plan.charge_intent.export is None
        assert any("in daylight the fixed discharge" in skip.reason for skip in plan.export_skips)


def test_served_day_solar_keeps_legacy_input_keyword() -> None:
    inp = PlannerInputs(soc=_soc(50), solar_tomorrow_kwh=3.5, predicted_home_load_kwh=10)
    plan = _plan(inp, cfg())
    assert inp.served_day_solar_kwh == 3.5
    assert plan.served_day_solar_kwh == 3.5
    assert plan.solar_kwh == 3.5


def test_today_solar_does_not_fund_event_house_load() -> None:
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 5, 1, tzinfo=tz)
    config = cfg(capacity_kwh=10, buffer_pct=0)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    start = datetime(2026, 1, 5, 17, tzinfo=tz)
    loads = tuple(0.5 if slot.hour == 16 else 0.0 for slot in geom.slot_starts)
    inp = PlannerInputs(
        soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=sum(loads),
        geometry=geom, horizon_start=geom.horizon_start, now=now, load_slots=loads,
        flexibility_event=FlexibilityEvent(
            start=start, end=start + timedelta(minutes=30), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
    )
    without_solar = _plan(inp, config)
    with_solar = _plan(replace(inp, solar_slots=loads), config)
    assert with_solar.charge_intent.export == without_solar.charge_intent.export
    assert with_solar.export_soc_needed_pct == without_solar.export_soc_needed_pct
    assert with_solar.export_skips == without_solar.export_skips


@pytest.mark.parametrize("legacy", [True, False])
def test_today_daylight_guard_leaves_legacy_or_undated_inputs_unchanged(legacy: bool) -> None:
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 5, 1, tzinfo=tz)
    config = cfg(capacity_kwh=100, strategy="fill")
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    start = datetime(2026, 1, 5, 17, tzinfo=tz)
    inp = PlannerInputs(
        soc=_soc(50), solar_tomorrow_kwh=0.1, predicted_home_load_kwh=0,
        geometry=None if legacy else geom,
        horizon_start=geom.horizon_start, now=now if legacy else None,
        load_slots=(0.0,) * len(geom.slot_starts),
        solar_slots=tuple(0.1 if slot == start else 0.0 for slot in geom.slot_starts),
        flexibility_event=FlexibilityEvent(
            start=start, end=start + timedelta(minutes=30), direction="export",
            rate_gbp_kwh=1, updated_at=now,
        ),
    )
    assert _plan(inp, config).charge_intent.export is not None
# --- slot 0: remaining energy vs full-slot power (#400) ---


def test_midslot_replan_counts_only_the_rest_of_slot_0_in_energy_totals() -> None:
    """A 17:40 replan carries 2/3 of the 17:30 slot's 0.3 kWh load as energy."""
    tz = ZoneInfo("Europe/London")
    config = cfg()
    loads = [0.0] * 60  # 17:30 Monday to 23:30 Tuesday
    loads[0] = 0.3
    mid = datetime(2026, 1, 5, 17, 40, tzinfo=tz)
    geom = compute_horizon_geometry(mid, config.window_start, config.window_end, tz)
    assert geom.slot0_remaining_frac == pytest.approx(2 / 3)
    plan = _plan(
        PlannerInputs(
            soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
            now=mid, geometry=geom, horizon_start=geom.horizon_start,
            load_slots=tuple(loads),
        ),
        config,
    )

    # Only the 20 minutes left of slot 0 are carried: 0.3 kWh x 2/3.
    assert plan.expensive_load_kwh == pytest.approx(0.2)
    assert plan.required_kwh == 0
    assert plan.pre_window_drain_kwh == pytest.approx(0.2)
    assert plan.reservations[0].name == "reach-charge-window"
    assert plan.reservations[0].energy_kwh == pytest.approx(0.2)

    # The whole-slot replan at 17:30 still counts the full 0.3 kWh.
    whole = datetime(2026, 1, 5, 17, 30, tzinfo=tz)
    whole_geom = compute_horizon_geometry(whole, config.window_start, config.window_end, tz)
    whole_plan = _plan(
        PlannerInputs(
            soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
            now=whole, geometry=whole_geom, horizon_start=whole_geom.horizon_start,
            load_slots=tuple(loads),
        ),
        config,
    )
    assert whole_plan.expensive_load_kwh == pytest.approx(0.3)
    assert whole_plan.pre_window_drain_kwh == pytest.approx(0.3)
    assert whole_plan.required_kwh == 0


def _midslot_export_inputs(
    now: datetime, config: PlannerConfig, tz: ZoneInfo,
    *, solar_slots: tuple[float, ...] | None = None,
) -> PlannerInputs:
    """A 17:30-18:30 export event, replanned 10 minutes in (2/3 of slot 0 left)."""
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    event = FlexibilityEvent(
        start=datetime(2026, 1, 5, 17, 30, tzinfo=tz),
        end=datetime(2026, 1, 5, 18, 30, tzinfo=tz),
        direction="export", updated_at=now, rate_gbp_kwh=1.0,
    )
    return PlannerInputs(
        soc=_soc(90), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
        now=now, geometry=geom, horizon_start=geom.horizon_start,
        load_slots=(0.0,) * len(geom.slot_starts), solar_slots=solar_slots,
        flexibility_event=event,
    )


def test_midslot_export_carries_remaining_energy_at_full_power() -> None:
    """Energy and revenue carry the 1/3 h left of slot 0; its power stays 3.2 kW."""
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 5, 17, 40, tzinfo=tz)
    config = cfg()
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    plan = _plan(_midslot_export_inputs(now, config, tz), config)

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    # Power is full-slot: 3.2 kW for the slot in progress too.
    assert export.selected_slots == (geom.slot_starts[0], geom.slot_starts[1])
    assert export.slot_export_kw == pytest.approx((3.2, 3.2))
    # Energy: the 1/3 h left of slot 0 plus the whole 18:00 slot.
    assert plan.export_revenue == pytest.approx(3.2 * (1 / 3 + 0.5))
    reservation = next(r for r in plan.reservations if r.name == "axle-export-event")
    assert reservation.energy_kwh == pytest.approx(3.2 * (1 / 3 + 0.5))


def test_midslot_slot_with_solar_is_refused_not_capped() -> None:
    """1 kW of forecast solar in slot 0 (full 4.2 kW, over the 3.68 kW DNO limit).

    The 20 minutes left of the slot don't shrink the slot to fit: it is refused
    outright, never capped, and the clean 18:00 slot still exports at its full
    3.2 kW. Slot 0 is dated today, so the daylight refusal (#398) claims it
    before the power check.
    """
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 5, 17, 40, tzinfo=tz)
    config = cfg()
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    solar = [0.0] * len(geom.slot_starts)
    solar[0] = 0.5  # 1 kW of full-slot solar power
    plan = _plan(_midslot_export_inputs(now, config, tz, solar_slots=tuple(solar)), config)

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    assert export.selected_slots == (geom.slot_starts[1],)
    assert export.slot_export_kw == pytest.approx((3.2,))
    assert [skip.start for skip in plan.export_skips] == [geom.slot_starts[0]]
    assert "daylight" in plan.export_skips[0].reason
    assert plan.export_revenue == pytest.approx(3.2 * 0.5)


def test_midslot_soc_path_drains_only_the_rest_of_slot_0() -> None:
    """With the window ending 05:20, the 05:00-05:30 slot is 2/3 left, 2/3 window."""
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 6, 5, 10, tzinfo=tz)
    config = cfg(window_end=time(5, 20))
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    assert geom.slot0_remaining_frac == pytest.approx(2 / 3)
    loads = [0.0] * len(geom.slot_starts)
    loads[0] = 0.15
    plan = _plan(
        PlannerInputs(
            soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
            now=now, geometry=geom, horizon_start=geom.horizon_start,
            load_slots=tuple(loads),
        ),
        config,
    )

    assert plan.soc_path[0] == (geom.window_end, 50.0)
    # 05:10-05:30 is still to come; its last ten minutes, after the window
    # ends, drain 0.15 / 3 = 0.05 kWh (#394 review).
    assert plan.soc_path[1][1] == pytest.approx(50.0 - 0.05 / 26.88 * 100)
    assert plan.expensive_load_kwh == pytest.approx(0.05)


def test_window_opening_inside_a_slot_drains_its_first_part_before_the_window() -> None:
    """A 23:45 window splits the 23:30 slot: its first half is pre-window drain."""
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 10, 23, 30, tzinfo=tz)
    config = cfg(window_start=time(23, 45), window_end=time(5, 45), capacity_kwh=20)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    loads = [0.0] * len(geom.slot_starts)
    loads[0] = 1.0
    loads[20] = 2.0  # post-window peak load stays in the deficit
    plan = _plan(
        PlannerInputs(
            soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
            now=now, geometry=geom, horizon_start=geom.horizon_start,
            load_slots=tuple(loads),
        ),
        config,
    )

    assert plan.pre_window_drain_kwh == pytest.approx(0.5)
    assert plan.deficit_kwh == pytest.approx(2.0)
    assert plan.charge_intent.expected_window_start_soc == pytest.approx(47.5)
    assert plan.reservations[0].obligation_kind == "reach-charge-window"
    assert plan.reservations[0].energy_kwh == pytest.approx(0.5)


def test_midslot_window_start_counts_only_the_rest_before_the_window() -> None:
    """At 23:35 with a 23:45 window, 10 of the 25 minutes left are pre-window."""
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 10, 23, 35, tzinfo=tz)
    config = cfg(window_start=time(23, 45), window_end=time(5, 45), capacity_kwh=20)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    assert geom.window_fracs[0] == pytest.approx(15 / 25)
    loads = [0.0] * len(geom.slot_starts)
    loads[0] = 1.5
    plan = _plan(
        PlannerInputs(
            soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
            now=now, geometry=geom, horizon_start=geom.horizon_start,
            load_slots=tuple(loads),
        ),
        config,
    )

    assert plan.pre_window_drain_kwh == pytest.approx(1.5 * 10 / 30)
    assert plan.deficit_kwh == pytest.approx(0.0)


def test_midslot_v2l_ledger_counts_only_the_rest_of_an_export_slot_0() -> None:
    """An export overlapping the window's last partial slot drains what is left.

    The window ends 05:20, so slot 0 (05:00-05:30) is both 2/3 covered and 2/3
    left. The 05:00-06:00 event exports from it; the V2L ledger that sizes the
    refill for the 06:30 shortfall sees only the 1/3 h still to come.
    """
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 1, 6, 5, 10, tzinfo=tz)
    config = cfg(window_end=time(5, 20), v2l_budget_kwh=15)
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, tz)
    event = FlexibilityEvent(
        start=datetime(2026, 1, 6, 5, 0, tzinfo=tz),
        end=datetime(2026, 1, 6, 6, 0, tzinfo=tz),
        direction="export", updated_at=now, rate_gbp_kwh=1.0,
    )
    loads = [0.0] * len(geom.slot_starts)
    loads[2] = 3.0
    loads[4] = 3.0
    plan = _plan(
        PlannerInputs(
            soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
            now=now, geometry=geom, horizon_start=geom.horizon_start,
            load_slots=tuple(loads), flexibility_event=event, car_plugged_in=True,
        ),
        config,
    )

    # 8.064 usable - 1.0667 exported from slot 0 - 1.6 from slot 1 - 3 at 05:30
    # leaves 2.397 kWh: one 0.603 kWh refill covers the 06:30 slot's shortfall.
    # Charging the elapsed 2/3 of slot 0 would need 1.136 kWh and two refills.
    assert [(action.slot, action.kwh) for action in plan.v2l_refills] == [
        (3, pytest.approx(3.0 - (26.88 * 0.30 - 3.2 / 3 - 1.6 - 3.0)))
    ]


def test_midslot_same_day_export_spends_only_the_rest_of_the_slot_in_progress() -> None:
    """At 19:15 only 45 min of the 2.7 kW export drains before tonight's window."""
    now = datetime(2026, 6, 8, 19, 15, tzinfo=UTC)
    without = _same_day_plan(_same_day_inputs(soc_now=60.0, now=now, event=None), _same_day_cfg())
    plan = _same_day_plan(_same_day_inputs(soc_now=60.0, now=now), _same_day_cfg())

    export = plan.charge_intent.export
    assert export is not None, [skip.reason for skip in plan.export_skips]
    # Power unchanged: the full 2.7 kW for both slots, elapsed part included.
    assert export.slot_export_kw == pytest.approx((2.7, 2.7))
    assert plan.export_revenue == pytest.approx(2.7 * 0.75)
    assert plan.required_kwh == pytest.approx(without.required_kwh + 2.7 * 0.75)


def _pre_window_inputs(now: datetime, soc: float, config: PlannerConfig) -> PlannerInputs:
    geometry = compute_horizon_geometry(
        now, config.window_start, config.window_end, ZoneInfo("UTC")
    )
    first, end = geometry.window_slot_range
    loads = [0.0] * len(geometry.slot_starts)
    loads[:first] = [0.5] * first
    loads[end:] = [0.25] * (len(loads) - end)
    return PlannerInputs(
        soc=_soc(soc), solar_tomorrow_kwh=0, predicted_home_load_kwh=sum(loads),
        now=now, horizon_start=geometry.horizon_start, geometry=geometry,
        load_slots=tuple(loads),
    )


@pytest.mark.parametrize("soc, required", [(40.0, 7.8), (80.0, 0.0)])
def test_pre_window_ledger_target_is_exactly_stable(
    soc: float, required: float,
) -> None:
    config = cfg(capacity_kwh=20, buffer_pct=20)
    now = datetime(2026, 1, 5, 22, 30, tzinfo=UTC)
    first = _plan(_pre_window_inputs(now, soc, config), config)
    second = _plan(_pre_window_inputs(now + timedelta(minutes=30), soc - 2.5, config), config)

    # P = 1, E_ws = 3 (or 11), O = 36 * 0.25 * 1.2 = 10.8.
    assert first.required_kwh == pytest.approx(required)
    assert first.required_kwh < 14 - (soc - 25) / 5  # battery room at window start
    assert first.required_kwh < config.max_current_a * config.voltage_v / 1000 * 6
    assert first.target_soc == pytest.approx(74)
    assert not (first.target_soc != second.target_soc)
    assert first.charge_intent.expected_window_start_soc == (
        second.charge_intent.expected_window_start_soc
    )
    assert first.charge_intent.expected_window_start_soc == soc - 5
    assert first.pre_window_drain_kwh == 1
    assert second.pre_window_drain_kwh == 0.5
    assert first.charge_intent.soc is first.soc


def test_legacy_inputs_carry_no_expected_window_start_soc() -> None:
    # A legacy target (soc_now + required) already prices in the drain, so a
    # rate-based charger must keep sizing from the observed SoC (#402).
    plan = _plan(
        PlannerInputs(
            soc=_soc(50), solar_tomorrow_kwh=0, predicted_home_load_kwh=0,
            pre_window_drain_kwh=2.0, load_slots=(0.25,) * 48, solar_slots=(0.0,) * 48,
            horizon_start=_HORIZON_START,
        ),
        cfg(),
    )
    assert plan.charge_intent.expected_window_start_soc is None


def test_expected_window_start_soc_never_exceeds_the_observed_soc() -> None:
    # Already below min_soc: E_ws is 0, but the battery holds 15%, not 20%.
    config = cfg(capacity_kwh=20)
    plan = _plan(_pre_window_inputs(datetime(2026, 1, 5, 22, 30, tzinfo=UTC), 15, config), config)
    assert plan.charge_intent.expected_window_start_soc == 15


@pytest.mark.parametrize("soc, shortfall", [
    (40, ""),
    (22.5, " A 0.50 kWh shortfall will be imported before the charge window."),
])
def test_reach_charge_window_reservation_reason(soc: float, shortfall: str) -> None:
    config = cfg(capacity_kwh=20)
    inputs = _pre_window_inputs(datetime(2026, 1, 5, 22, 30, tzinfo=UTC), soc, config)
    plan = _plan(inputs, config)
    reach = next(r for r in plan.reservations if r.name == "reach-charge-window")
    assert reach.energy_kwh == 1
    assert reach.target_slot == 2
    assert inputs.geometry is not None
    assert reach.target_time == inputs.geometry.window_start
    assert reach.reason == (
        "Reserving 1.00 kWh to cover forecast house load until the 23:30 charge window."
        + shortfall
    )
    assert plan.required_kwh == pytest.approx(9 - max(0, (soc - 20) / 5 - 1))


def test_window_start_omits_evening_reservation_and_keeps_zero_charge_target() -> None:
    config = cfg(capacity_kwh=20)
    inputs = _pre_window_inputs(datetime(2026, 1, 5, 23, 30, tzinfo=UTC), 80, config)
    plan = _plan(inputs, config)
    assert plan.required_kwh == 0
    assert plan.target_soc == 80
    assert all(r.name != "reach-charge-window" for r in plan.reservations)


def test_pre_window_slot_outflow_adds_legacy_drain_and_discounts_solar_and_dispatch() -> None:
    config = cfg(capacity_kwh=20)
    now = datetime(2026, 1, 5, 22, 30, tzinfo=UTC)
    inputs = _pre_window_inputs(now, 40, config)
    assert inputs.geometry is not None
    solar = [0.0] * len(inputs.load_slots or ())
    solar[0] = 0.25
    inputs = replace(
        inputs, pre_window_drain_kwh=0.5, solar_slots=tuple(solar),
        dispatches=(DispatchSlot(
            start=now + timedelta(minutes=30), end=now + timedelta(hours=1),
        ),),
    )
    plan = _plan(inputs, config)

    # First slot: 0.5 - 0.25 solar; second: grid-covered; legacy drain: 0.5.
    assert plan.pre_window_drain_kwh == 0.75
    assert plan.charge_intent.expected_window_start_soc == 36.25
    assert plan.required_kwh == 5.75  # O = 9, E_ws = 3.25 (not P + O)
    assert plan.target_soc == 65
    evening, daytime = plan.reservations
    assert evening.name == "reach-charge-window"
    assert evening.energy_kwh == 0.75
    assert daytime.name == "reach-next-cheap-slot"
    assert daytime.energy_kwh == 9


@pytest.mark.parametrize("strategy, required", [("deficit", 9), ("fill", 14)])
def test_pre_window_shortfall_does_not_become_a_charge_obligation(
    strategy: str, required: float,
) -> None:
    config = cfg(capacity_kwh=20, strategy=strategy)
    inputs = _pre_window_inputs(datetime(2026, 1, 5, 22, 30, tzinfo=UTC), 20, config)
    plan = _plan(inputs, config)

    assert plan.required_kwh == required
    assert plan.charge_intent.expected_window_start_soc == 20
    assert plan.target_soc == 20 + required * 5
    assert plan.pre_window_drain_kwh == 1


@pytest.mark.parametrize("soc", [10, 80])
def test_running_window_zero_charge_target_keeps_observed_soc(soc: float) -> None:
    config = cfg(capacity_kwh=20)
    now = datetime(2026, 1, 6, 1, tzinfo=UTC)
    inputs = _pre_window_inputs(now, soc, config)
    inputs = replace(inputs, load_slots=(0.0,) * len(inputs.load_slots or ()))
    plan = _plan(inputs, config)

    assert plan.required_kwh == 0
    assert plan.target_soc == soc
    assert plan.pre_window_drain_kwh == 0
    assert all(r.name != "reach-charge-window" for r in plan.reservations)


@pytest.mark.parametrize("elapsed_slots", [0, 3])
def test_window_start_and_running_window_keep_the_dispatch_deficit_floor(
    elapsed_slots: int,
) -> None:
    config = cfg(buffer_pct=20, solar_haircut_k=0.85, charge_efficiency=0.9)
    window_start = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    now = window_start + timedelta(minutes=30 * elapsed_slots)
    geometry = compute_horizon_geometry(
        now, config.window_start, config.window_end, ZoneInfo("UTC")
    )
    loads = tuple(0.4 if i < 12 else 0.5 for i in range(elapsed_slots, 48))
    solar = tuple(1.0 if 20 <= i < 28 else 0.0 for i in range(elapsed_slots, 48))
    inputs = PlannerInputs(
        soc=_soc(55), solar_tomorrow_kwh=8, predicted_home_load_kwh=sum(loads),
        load_slots=loads, solar_slots=solar, now=now, geometry=geometry,
        horizon_start=geometry.horizon_start,
        dispatches=(DispatchSlot(
            start=datetime(2026, 1, 16, 13, 30, tzinfo=UTC),
            end=datetime(2026, 1, 16, 15, tzinfo=UTC),
        ),),
    )
    plan = _plan(inputs, config)

    assert plan.pre_window_drain_kwh == 0
    assert plan.expensive_load_kwh == 12.5
    assert plan.required_kwh == pytest.approx(5.592)
    assert plan.target_soc == pytest.approx(75.803571, abs=1e-6)
    assert plan.charge_intent.expected_window_start_soc == 55
    assert len(plan.reservations) == 1
    assert plan.reservations[0].energy_kwh == pytest.approx(4.8)
    assert plan.soc_path[0][0] == geometry.window_end


@pytest.mark.parametrize("strategy", ["deficit", "fill"])
def test_running_window_ledger_caps_purchase_at_remaining_charge_delivery(strategy: str) -> None:
    config = cfg(capacity_kwh=100, strategy=strategy)
    inputs = _pre_window_inputs(datetime(2026, 1, 6, 1, tzinfo=UTC), 20, config)
    inputs = replace(inputs, load_slots=tuple(10 * e for e in inputs.load_slots or ()))
    plan = _plan(inputs, config)

    assert plan.required_kwh == pytest.approx(4.5 * 62.5 * 51 / 1000)
    assert plan.target_soc == pytest.approx(20 + plan.required_kwh)
    assert plan.charge_intent.expected_window_start_soc == 20
    assert plan.pre_window_drain_kwh == 0


def test_pre_window_outflow_counts_only_selected_export_slots_before_the_window() -> None:
    config = cfg(capacity_kwh=100)
    now = datetime(2026, 1, 5, 22, 30, tzinfo=UTC)
    inputs = _pre_window_inputs(now, 80, config)
    inputs = replace(inputs, flexibility_event=FlexibilityEvent(
        start=now, end=now + timedelta(hours=2), direction="export",
        rate_gbp_kwh=1, updated_at=now,
    ))
    plan = _plan(inputs, config)

    export = plan.charge_intent.export
    assert export is not None
    assert len(export.selected_slots) == 4
    # Fixed output serves 1 kW of house load, leaving 2.2 kW exported in each
    # pre-window slot: P = 1 kWh house load + 2.2 kWh exported.
    assert export.slot_export_kw[:2] == pytest.approx((2.2, 2.2))
    assert plan.pre_window_drain_kwh == pytest.approx(3.2)
    assert plan.reservations[0].energy_kwh == pytest.approx(3.2)
    assert plan.charge_intent.expected_window_start_soc == pytest.approx(76.8)
    assert plan.required_kwh == 0  # The other two slots conflict with charging.


def test_v2l_never_funds_the_reach_charge_window_reservation() -> None:
    config = cfg(capacity_kwh=20, max_current_a=0, v2l_budget_kwh=5)
    inputs = _pre_window_inputs(datetime(2026, 1, 5, 22, 30, tzinfo=UTC), 20, config)
    assert inputs.geometry is not None
    loads = (0.5,) * inputs.geometry.window_slot_range[0]
    loads += (0.0,) * (len(inputs.load_slots or ()) - len(loads))
    inputs = replace(inputs, load_slots=loads, car_plugged_in=True)
    plan = _plan(inputs, config)

    assert plan.reservations[0].name == "reach-charge-window"
    assert plan.reservations[0].energy_kwh == 1
    assert plan.required_kwh == 0
    assert plan.v2l_refills == ()
    assert plan.soc_path[0] == (inputs.geometry.window_end, 20)


def test_pre_window_zero_charge_soc_path_starts_with_energy_actually_carried() -> None:
    config = cfg(capacity_kwh=20)
    inputs = _pre_window_inputs(datetime(2026, 1, 5, 22, 30, tzinfo=UTC), 80, config)
    plan = _plan(inputs, config)

    assert plan.required_kwh == 0
    assert plan.target_soc == 65  # Decision B: target is the obligation, not surplus.
    assert inputs.geometry is not None
    assert plan.soc_path[0] == (inputs.geometry.window_end, 75)


_LONDON = ZoneInfo("Europe/London")


def _live_inputs(
    now: datetime, soc: float, *, solar: dict[int, float] | None = None, **kw: Any
) -> PlannerInputs:
    config = cfg()
    geom = compute_horizon_geometry(now, config.window_start, config.window_end, _LONDON)
    n = len(geom.slot_starts)
    return PlannerInputs(
        soc=_soc(soc), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.25 * n,
        geometry=geom, horizon_start=geom.horizon_start, now=now,
        load_slots=(0.25,) * n,
        solar_slots=tuple((solar or {}).get(i, 0.0) for i in range(n)),
        **kw,
    )


@pytest.mark.parametrize(
    ("usable_now", "required", "uncovered"),
    [
        (13.75, 0.0, 0.0),  # covers 4.75 kWh before the window and 9 kWh after
        (2.0, 9.0, 2.75),  # 2.75 kWh of the evening load is imported at peak
    ],
)
def test_projected_cost_counts_the_evening_load_once(
    usable_now: float, required: float, uncovered: float
) -> None:
    """At 14:00 BST, 19 slots before 23:30 and 36 after 05:30 carry 0.25 kWh each."""
    config = cfg()
    inp = _live_inputs(
        datetime(2026, 6, 8, 14, 0, tzinfo=_LONDON), 20.0 + usable_now / 26.88 * 100.0
    )
    schedule = fixed_schedule(inp, config)
    plan = compute_plan(inp, config, schedule)

    assert plan.deficit_kwh == pytest.approx(9.0)
    assert plan.required_kwh == pytest.approx(required)
    assert plan.planned_cost == pytest.approx(
        (3.0 + required) * schedule.cheap_rate + uncovered * schedule.standard_rate
    )


def test_plan_times_read_on_the_household_clock() -> None:
    """A 17:00-18:00 BST event is described as 17:00, not its 16:00 UTC instant."""
    from ha_spark.energy.report import format_plan

    now = datetime(2026, 6, 8, 12, 0, tzinfo=_LONDON)
    start = datetime(2026, 6, 8, 17, 0, tzinfo=_LONDON)
    inp = _live_inputs(
        now, 90.0,
        flexibility_event=FlexibilityEvent(
            start=start, end=start + timedelta(hours=1), direction="export",
            rate_gbp_kwh=1.0, updated_at=now,
        ),
    )
    plan = _plan(inp, cfg())

    assert plan.charge_intent.export is not None, [s.reason for s in plan.export_skips]
    event = next(r for r in plan.reservations if r.name == "axle-export-event")
    assert "from 17:00 to 18:00" in event.reason
    assert "Paid export       17:00-18:00" in format_plan(plan, "test", tz=_LONDON)


def test_solar_export_counts_only_the_rest_of_slot_0() -> None:
    """At 14:15 only half of slot 0's 1 kWh surplus is still to come."""
    config = cfg(rate_export=0.15)
    at_start = _plan(
        _live_inputs(datetime(2026, 6, 8, 14, 0, tzinfo=_LONDON), 50.0, solar={0: 1.25}), config
    )
    midslot = _plan(
        _live_inputs(datetime(2026, 6, 8, 14, 15, tzinfo=_LONDON), 50.0, solar={0: 1.25}), config
    )

    assert at_start.export_revenue == pytest.approx(1.0 * 0.15)
    assert midslot.export_revenue == pytest.approx(0.5 * 0.15)


@pytest.mark.parametrize("slots", [False, True])
def test_plan_view_headline_costs_and_model(slots: bool) -> None:
    inputs = PlannerInputs(
        soc=_soc(40), solar_tomorrow_kwh=4, predicted_home_load_kwh=12,
        load_slots=(0.25,) * 48 if slots else None,
    )
    plan = _plan(inputs, cfg())
    view = plan.view
    assert view is not None
    assert view.headline.soc_now == plan.soc_now
    assert view.headline.target_soc == plan.target_soc
    assert view.headline.required_kwh == plan.required_kwh
    assert view.headline.min_soc == cfg().min_soc
    assert view.headline.capacity_kwh == plan.capacity_kwh
    assert view.costs.planned == plan.planned_cost
    assert view.costs.baseline == plan.baseline_cost
    assert view.costs.export_revenue == plan.export_revenue
    assert any(line.kind == "charge" and line.kwh == plan.required_kwh for line in view.lines)
    assert all(line.start is None and line.end is None for line in view.lines)
    assert view.slots == (() if slots else None)
    assert view.soc == (() if slots else None)


def test_plan_view_untrusted_soc_hides_derived_values() -> None:
    plan = _plan(PlannerInputs(
        soc=_soc_unavailable(), solar_tomorrow_kwh=4, predicted_home_load_kwh=12,
    ), cfg())
    view = plan.view
    assert view is not None
    assert (view.headline.soc_now, view.headline.target_soc,
            view.headline.required_kwh) == (None, None, None)
    assert view.headline.min_soc == cfg().min_soc
    assert view.headline.capacity_kwh == cfg().capacity_kwh
    assert (view.costs.planned, view.costs.baseline, view.costs.export_revenue) == (None,) * 3
    assert len(view.lines) == 1
    warning = view.lines[0]
    assert warning.kind == "warning"
    assert warning.start is warning.end is None
    assert warning.text == "SoC sensor unavailable"


@pytest.mark.parametrize("status", [status for status in SocStatus if status is not SocStatus.OK])
def test_plan_view_soc_warning_never_echoes_upstream_detail(status: SocStatus) -> None:
    measurement = replace(_soc_unavailable(), status=status,
                          raw_state="SECRET-VIEW-CANARY", energy_reason="SECRET-VIEW-CANARY")
    plan = _plan(PlannerInputs(
        soc=measurement, solar_tomorrow_kwh=4, predicted_home_load_kwh=12,
        load_slots=(0.25,) * 48,
    ), cfg())
    assert plan.view is not None
    assert "SECRET-VIEW-CANARY" not in repr(plan.view)
    assert plan.view.headline.soc_now is None
    assert plan.view.lines[0].kind == "warning"
