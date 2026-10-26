"""Planner-owned reasons and ingress-only observed hold wording."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ha_spark.api.timeline import timeline_payload
from ha_spark.energy.models import DispatchSlot, FlexibilityEvent, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import fixed_schedule
from tests.test_plan_timeline import stored_run
from tests.test_planner import _HORIZON_START, _reserve_trade_plan, _soc, _soc_unavailable, cfg
from tests.test_planner_slots_view import slot_inputs


def test_charge_reservation_and_dispatch_lines():
    config = cfg(strategy="fill")
    now = datetime(2026, 1, 12, 2, 17, tzinfo=UTC)
    inputs = slot_inputs(now, config, dispatches=(
        DispatchSlot(now.replace(hour=12, minute=0), now.replace(hour=13, minute=0)),
    ))
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    charge = next(line for line in plan.view.lines if line.kind == "charge")
    assert (charge.start, charge.end, charge.kwh) == (
        now, inputs.geometry.window_end, plan.required_kwh,
    )
    assert charge.text == (
        f"Charge the house battery with {plan.required_kwh:.2f} kWh "
        f"to reach {plan.target_soc:.0f}% SoC."
    )
    hold = next(line for line in plan.view.lines if line.kind == "hold")
    assert (hold.start, hold.end) == (
        now.replace(hour=12, minute=0), now.replace(hour=13, minute=0),
    )
    assert hold.text == "Hold the house battery during a smart-charge dispatch."
    reserves = [line for line in plan.view.lines if line.kind == "reserve"]
    assert [line.text for line in reserves] == [r.reason for r in plan.reservations]
    assert [line.kwh for line in reserves] == [r.energy_kwh for r in plan.reservations]


@pytest.mark.parametrize("trusted", [True, False])
def test_ev_hold_and_untrusted_lines(trusted):
    record = stored_run(measurement=_soc(40) if trusted else _soc_unavailable())
    payload = timeline_payload(replace(record, ev_hold_active=True), "observe",
                               datetime.now(UTC))
    ev = payload["lines"][-1]
    assert ev == {"kind": "hold", "start": payload["now"], "end": None,
                  "kwh": None, "text": "Hold the house battery while an EV hold is active."}
    if not trusted:
        assert {line["kind"] for line in payload["lines"]} == {"warning", "hold"}


def test_daily_charge_has_no_span():
    config = cfg(strategy="fill")
    inputs = PlannerInputs(soc=_soc(30), solar_tomorrow_kwh=0, predicted_home_load_kwh=20)
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    line = next(line for line in plan.view.lines if line.kind == "charge")
    assert line.start is None and line.end is None
    assert line.kwh == plan.required_kwh


def test_export_lines_sum_selected_energy():
    plan = _reserve_trade_plan(rate=1.0)
    export = plan.charge_intent.export
    lines = [line for line in plan.view.lines if line.kind == "export"]
    assert lines
    assert sum(line.kwh for line in lines) == pytest.approx(sum(export.slot_export_kw) * 0.5)
    for line in lines:
        assert line.text == f"Export {line.kwh:.2f} kWh from the house battery."
        assert line.start < line.end


@pytest.mark.parametrize("interleaved", [False, True])
def test_v2l_lines_fold_and_preserve_original_reasons(interleaved):
    config = cfg(v2l_budget_kwh=15)
    inputs = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36, car_plugged_in=True,
        horizon_start=datetime(2026, 1, 10, 23, 30, tzinfo=UTC),
    )
    if interleaved:
        # An unfundable export creates a skip inside the refill run, not a
        # boundary at which otherwise adjacent V2L reasons should stop folding.
        inputs = replace(inputs, flexibility_event=FlexibilityEvent(
            start=datetime(2026, 1, 11, 20, tzinfo=UTC),
            end=datetime(2026, 1, 11, 20, 30, tzinfo=UTC), direction="export",
            updated_at=datetime(2026, 1, 10, 23, tzinfo=UTC), rate_gbp_kwh=0.01,
        ))
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    lines = [line for line in plan.view.lines if line.kind == "v2l"]
    if interleaved:
        skip = next(line for line in plan.view.lines if line.kind == "skip")
        assert any(line.start < skip.start < line.end for line in lines)
    assert lines and len(lines) < len(plan.v2l_refills)
    assert sum(line.kwh for line in lines) == pytest.approx(sum(r.kwh for r in plan.v2l_refills))
    for refill in plan.v2l_refills:
        assert refill.reason == (
            f"Refill the house battery with {refill.kwh:.2f} kWh from V2L for "
            f"the {refill.reservation} reservation."
        )
    for line in lines:
        assert f"{line.kwh:.2f} kWh" in line.text


def test_adjacent_skips_with_different_reasons_do_not_fold():
    inputs = PlannerInputs(
        soc=_soc(56), solar_tomorrow_kwh=0, predicted_home_load_kwh=12,
        load_slots=(0.25,) * 48, solar_slots=(0.0,) * 48,
        horizon_start=_HORIZON_START,
        flexibility_event=FlexibilityEvent(
            start=datetime(2026, 6, 9, 17, tzinfo=UTC),
            end=datetime(2026, 6, 9, 18, 30, tzinfo=UTC), direction="export",
            updated_at=datetime(2026, 6, 9, 16, tzinfo=UTC), rate_gbp_kwh=0.30,
        ),
    )
    config = cfg(capacity_kwh=28, target_cap=60, buffer_pct=20,
                 battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36,
                 supply_max_current_a=75)
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    skips = [line for line in plan.view.lines if line.kind == "skip"]
    assert len(skips) == len(plan.export_skips) == 2
    assert skips[0].end == skips[1].start
    assert [line.text for line in skips] == [skip.reason for skip in plan.export_skips]
    assert skips[0].text != skips[1].text


@pytest.mark.parametrize("trusted", [True, False])
def test_dispatch_line_clips_now_and_survives_untrusted_soc(trusted):
    config = cfg()
    now = datetime(2026, 1, 12, 12, 17, tzinfo=UTC)
    inputs = slot_inputs(now, config, dispatches=(
        DispatchSlot(now.replace(minute=0), now.replace(hour=13, minute=0)),
    ))
    if not trusted:
        inputs = replace(inputs, soc=_soc_unavailable())
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    hold = next(line for line in plan.view.lines if line.kind == "hold")
    assert hold.start == now
    assert hold.end == now.replace(hour=13, minute=0)
    if not trusted:
        assert {line.kind for line in plan.view.lines} == {"hold", "warning"}


def test_elapsed_reservation_has_no_line():
    from tests.test_planner import _pre_window_inputs

    config = cfg(capacity_kwh=20)
    inputs = replace(_pre_window_inputs(datetime(2026, 1, 6, 1, 0, tzinfo=UTC), 80, config),
                     pre_window_drain_kwh=1.0)
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    assert any(r.name == "reach-charge-window" for r in plan.reservations)
    lines = [line for line in plan.view.lines if line.kind == "reserve"]
    assert all(line.start is None or line.start < line.end for line in lines)
    assert not any(line.end is not None and line.end <= inputs.now.astimezone(UTC)
                   for line in lines)
