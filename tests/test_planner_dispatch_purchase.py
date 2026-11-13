"""Started dispatch purchases and the coarse-hold refusal (#697)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ha_spark.energy import sources
from ha_spark.energy.models import DispatchSlot, FreeSession, LoadForecast
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from tests.solar_source_helpers import entities_source
from tests.test_energy_sources import BASE, _settings, _state
from tests.test_planner import cfg
from tests.test_planner_daytime import planned
from tests.test_planner_limits import native
from tests.test_planner_slots_view import LONDON, slot_inputs

NOW = datetime(2026, 1, 12, 12, tzinfo=LONDON)
SLOT = DispatchSlot(NOW, NOW + timedelta(minutes=30))


def _inputs(config, *, proof=True, ev_hold=False, sessions=()):
    inputs = slot_inputs(NOW, config)
    n = len(inputs.load_slots)
    return replace(
        inputs,
        load_slots=(0.0,) * n,
        solar_slots=(0.0,) * n,
        dispatches=(SLOT,),
        dispatch_purchase_slots=(SLOT,) if proof else (),
        ev_hold_charging=ev_hold,
        free_sessions=sessions,
    )


def test_drawing_dispatch_buys_at_offpeak_and_shrinks_overnight_on_no_hold_driver():
    # AlphaESS has no coarse hold, so the same dispatch is admitted.
    config = cfg(strategy="fill", buffer_pct=0, driver_kind="alphaess")
    topology = native(config)
    with_hold = _inputs(config, proof=False)
    ordinary = planned(with_hold, config, topology=topology)
    plan = planned(_inputs(config), config, topology=topology)

    purchase, = plan.daytime_purchases
    assert not purchase.refused
    assert purchase.overlay == "dispatch"
    assert purchase.effective_price == pytest.approx(config.rate_offpeak)
    assert "car drawing" in purchase.reason
    assert plan.required_kwh == pytest.approx(ordinary.required_kwh - purchase.energy_kwh)
    window, = plan.charge_intent.daytime_windows
    assert (window.start, window.end) == (purchase.start, purchase.end)
    assert "charge" in plan.view.slots[0].actions


@pytest.mark.parametrize(
    ("config_kind", "topology_kind", "refused"),
    [
        ("solis", "alphaess", False),  # the effective native inverter admits
        ("alphaess", "solis", True),  # the legacy config cannot override it
    ],
    ids=["native-alphaess-admits", "native-solis-refuses"],
)
def test_coarse_hold_follows_the_effective_native_inverter_not_the_legacy_config(
    config_kind, topology_kind, refused,
):
    config = cfg(strategy="fill", buffer_pct=0, driver_kind=config_kind)
    topology = native(replace(config, driver_kind=topology_kind))
    plan = planned(_inputs(config), config, topology=topology)
    purchase, = plan.daytime_purchases
    assert purchase.refused is refused
    assert (plan.charge_intent.daytime_windows == ()) is refused


def test_coarse_hold_keeps_the_dispatch_purchase_as_a_refused_diagnostic():
    config = cfg(strategy="fill", buffer_pct=0)  # Solis is coarse.
    topology = native(config)
    with_hold = _inputs(config, proof=False)
    ordinary = planned(with_hold, config, topology=topology)
    plan = planned(_inputs(config), config, topology=topology)

    purchase, = plan.daytime_purchases
    assert purchase.refused is True
    assert purchase.energy_kwh > 0
    assert "dispatch hold" in purchase.reason
    assert purchase.refusal_reason == purchase.reason
    # Diagnostic only: no dated window, no overnight shrink, no cost or SoC help.
    assert plan.charge_intent.daytime_windows == ()
    assert plan.required_kwh == ordinary.required_kwh
    assert plan.target_soc == ordinary.target_soc
    assert plan.planned_cost == ordinary.planned_cost
    assert plan.soc_path == ordinary.soc_path
    assert "charge" not in plan.view.slots[0].actions
    warning = next(line for line in plan.view.lines if line.text == purchase.reason)
    assert warning.kind == "warning"


def test_a_future_purchase_proof_slot_never_buys():
    # The planner trusts the supplied proof only while that dispatch has started.
    config = cfg(strategy="fill", buffer_pct=0, driver_kind="alphaess")
    future = DispatchSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=1, minutes=30))
    inputs = replace(
        _inputs(config, proof=False), dispatches=(future,),
        dispatch_purchase_slots=(future,),
    )
    plan = planned(inputs, config, topology=native(config))
    assert plan.daytime_purchases == ()
    assert plan.charge_intent.daytime_windows == ()


def test_planned_or_forecast_dispatch_without_draw_proof_is_only_a_hold():
    config = cfg(strategy="fill", buffer_pct=0, driver_kind="alphaess")
    plan = planned(_inputs(config, proof=False), config, topology=native(config))
    assert plan.daytime_purchases == ()
    assert plan.charge_intent.daytime_windows == ()
    assert plan.charge_intent.holds == ((SLOT.start, SLOT.end),)


def test_happy_hour_inside_an_ev_hold_is_refused_with_the_hold_named():
    config = cfg(strategy="fill", buffer_pct=0)
    happy = FreeSession("happy_hour", "hh", NOW, NOW + timedelta(minutes=30))
    inputs = _inputs(config, proof=False, ev_hold=True, sessions=(happy,))
    # A pure EV hold, without the separate dispatch hold.
    inputs = replace(inputs, dispatches=(), happy_hour_remaining_kwh={"hh": 14.4})
    ordinary = planned(replace(inputs, free_sessions=()), config)
    plan = planned(inputs, config)

    purchase, = plan.daytime_purchases
    assert purchase.refused is True
    assert purchase.overlay == "happy_hour"
    assert "EV hold" in purchase.reason
    assert plan.charge_intent.daytime_windows == ()
    assert plan.required_kwh == ordinary.required_kwh
    assert plan.target_soc == ordinary.target_soc
    assert plan.planned_cost == ordinary.planned_cost
    assert "charge" not in plan.view.slots[0].actions


def test_untrusted_dispatch_proof_produces_no_purchase():
    config = cfg(strategy="fill", buffer_pct=0, driver_kind="alphaess")
    inputs = replace(_inputs(config, proof=False), dispatch_purchase_slots=())
    plan = planned(inputs, config, topology=native(config))
    assert plan.daytime_purchases == ()


async def _gather_dispatch_proof(
    monkeypatch: pytest.MonkeyPatch, *, ev_state: str, dispatch_last_reported: str | None,
    duration_minutes: int = 20,
) -> tuple[object, datetime, datetime]:
    async def fake_load(_settings: object, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    now = datetime.now(UTC)
    start, end = now - timedelta(minutes=10), now + timedelta(minutes=duration_minutes)
    dispatch = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "charge_in_kwh": -2.0,
        "source": "SMART",
    }
    with respx.mock:
        respx.get(f"{BASE}/states/binary_sensor.dispatch").mock(
            return_value=_state(
                "binary_sensor.dispatch", "on", {"planned_dispatches": [dispatch]},
                last_reported=dispatch_last_reported,
            )
        )
        respx.get(f"{BASE}/states/sensor.ev").mock(
            return_value=_state("sensor.ev", ev_state)
        )
        respx.route(method="GET").mock(return_value=httpx.Response(404))
        settings = _settings()
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            inputs, _cfg, _source = await gather_inputs(
                settings, rest, source=entities_source(settings)
            )
    return inputs, start, end


async def test_gather_carries_only_fresh_trusted_draw_as_purchase_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, start, end = await _gather_dispatch_proof(
        monkeypatch, ev_state="Charging", dispatch_last_reported="now",
    )
    slot, = inputs.dispatch_purchase_slots
    now = inputs.now
    assert now is not None
    half_hour_end = now.replace(minute=30, second=0, microsecond=0)
    assert slot.start == now
    assert slot.end == min(end, half_hour_end)


async def test_gather_clips_a_long_dispatch_proof_to_the_current_half_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, _start, end = await _gather_dispatch_proof(
        monkeypatch, ev_state="Charging", dispatch_last_reported="now",
        duration_minutes=150,
    )
    slot, = inputs.dispatch_purchase_slots
    now = inputs.now
    assert now is not None
    half_hour_end = now.replace(
        minute=0 if now.minute < 30 else 30, second=0, microsecond=0
    ) + timedelta(minutes=30)
    assert slot.start == now
    assert slot.end == min(end, half_hour_end)


@pytest.mark.parametrize(
    ("ev_state", "last_reported"),
    [
        ("EV Connected", "now"),  # a live/adjusted flag is not actual draw
        ("Charging", (datetime.now(UTC) - timedelta(seconds=301)).isoformat()),
    ],
    ids=["no-draw", "untrusted-dispatch-source"],
)
async def test_gather_never_buys_without_fresh_trusted_draw(
    monkeypatch: pytest.MonkeyPatch, ev_state: str, last_reported: str,
) -> None:
    inputs, _start, _end = await _gather_dispatch_proof(
        monkeypatch, ev_state=ev_state, dispatch_last_reported=last_reported,
    )
    assert inputs.dispatch_purchase_slots == ()


def _long_dispatch_inputs(now, config, *, proof=True):
    start = now.replace(hour=12, minute=0, second=0, microsecond=0)
    long = DispatchSlot(start, start + timedelta(hours=3))
    inputs = slot_inputs(now, config)
    n = len(inputs.load_slots)
    return replace(
        inputs,
        load_slots=(0.0,) * n,
        solar_slots=(0.0,) * n,
        dispatches=(long,),
        dispatch_purchase_slots=(long,) if proof else (),
    ), long


@pytest.mark.parametrize(
    ("now", "hours"),
    [
        (datetime(2026, 1, 12, 12, tzinfo=LONDON), 0.5),
        (datetime(2026, 1, 12, 12, 10, tzinfo=LONDON), 1 / 3),
    ],
    ids=["aligned-1200", "mid-slot-1210"],
)
def test_a_long_active_dispatch_buys_only_its_current_started_half_hour(now, hours):
    # A three-hour dispatch live now must not look like three hours of purchase.
    config = cfg(strategy="fill", buffer_pct=0, driver_kind="alphaess")
    inputs, long = _long_dispatch_inputs(now, config)
    ordinary_inputs, _ = _long_dispatch_inputs(now, config, proof=False)
    ordinary = planned(ordinary_inputs, config, topology=native(config))
    plan = planned(inputs, config, topology=native(config))

    purchase, = plan.daytime_purchases
    half_hour_end = now.replace(minute=30, second=0, microsecond=0)
    assert purchase.start == now.astimezone(UTC)
    assert purchase.end <= half_hour_end.astimezone(UTC)
    rate_kw = config.voltage_v * config.max_current_a / 1000
    assert purchase.energy_kwh == pytest.approx(rate_kw * hours)
    assert ordinary.required_kwh - plan.required_kwh == pytest.approx(
        purchase.energy_kwh
    )
    # The hold keeps the whole dispatch window; only purchase proof is clipped.
    assert plan.charge_intent.holds == ((long.start, long.end),)


def test_a_free_session_inside_an_ev_hold_is_a_refused_diagnostic_only():
    config = cfg(strategy="fill", buffer_pct=0)
    free = FreeSession("free_electricity", "free", NOW, NOW + timedelta(minutes=30))
    inputs = replace(
        _inputs(config, proof=False, ev_hold=True, sessions=(free,)), dispatches=(),
    )
    ordinary = planned(replace(inputs, free_sessions=()), config)
    plan = planned(inputs, config)

    purchase, = plan.daytime_purchases
    assert purchase.refused is True
    assert purchase.overlay == "free_electricity"
    assert "EV hold" in purchase.reason
    assert plan.charge_intent.daytime_windows == ()
    assert plan.required_kwh == ordinary.required_kwh
    assert plan.target_soc == ordinary.target_soc
    assert plan.planned_cost == ordinary.planned_cost
    assert plan.soc_path == ordinary.soc_path


def test_an_explicit_cheap_price_inside_an_ev_hold_is_a_refused_diagnostic_only():
    from tests.test_planner_daytime import _dynamic_inputs, _dynamic_schedule

    config = cfg(strategy="fill", buffer_pct=0)
    inputs = _dynamic_inputs(NOW, config, [0.05] + [0.30] * 70)
    inputs = replace(inputs, ev_hold_charging=True)
    schedule = _dynamic_schedule(inputs, config)
    ordinary_schedule = replace(
        schedule, explicit_import_prices=(None,) * len(schedule.prices),
    )
    ordinary = compute_plan(inputs, config, ordinary_schedule)
    plan = compute_plan(inputs, config, schedule)

    purchase, = plan.daytime_purchases
    assert purchase.refused is True
    assert purchase.overlay == "dynamic_price"
    assert purchase.effective_price == pytest.approx(0.05)
    assert "EV hold" in purchase.reason
    assert plan.charge_intent.daytime_windows == ()
    assert plan.required_kwh == ordinary.required_kwh
    assert plan.target_soc == ordinary.target_soc
    assert plan.planned_cost == ordinary.planned_cost
    assert plan.soc_path == ordinary.soc_path


def test_a_low_remaining_happy_hour_refusal_uses_the_admitted_allowance_sizing():
    config = cfg(strategy="fill", buffer_pct=0, charge_efficiency=0.8)
    happy = FreeSession("happy_hour", "hh", NOW, NOW + timedelta(minutes=30))
    inputs = replace(
        _inputs(config, proof=False, ev_hold=True, sessions=(happy,)),
        dispatches=(),
        happy_hour_remaining_kwh={"hh": 1.7},
    )
    ordinary = planned(replace(inputs, free_sessions=()), config)
    refused_plan = planned(inputs, config)
    admitted_plan = planned(replace(inputs, ev_hold_charging=False), config)

    refused, = refused_plan.daytime_purchases
    admitted, = admitted_plan.daytime_purchases
    assert refused.refused is True
    assert admitted.refused is False
    # 1.7 kWh less the fixed 1.6 kWh margin leaves 0.1 kWh AC for the battery.
    assert refused.energy_kwh == pytest.approx(0.1 * config.charge_efficiency)
    assert refused.energy_kwh == pytest.approx(admitted.energy_kwh)
    assert refused_plan.charge_intent.daytime_windows == ()
    assert refused_plan.required_kwh == ordinary.required_kwh
    assert refused_plan.planned_cost == ordinary.planned_cost
    assert refused_plan.soc_path == ordinary.soc_path
