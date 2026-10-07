"""The dispatch evidence ladder is a pure planner input rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ha_spark.energy.dispatch_evidence import (
    DispatchEvidence,
    DispatchRating,
    partition_dispatches,
    rate_dispatch,
)
from ha_spark.energy.models import DispatchSlot

NOW = datetime(2026, 10, 3, 14, 15, tzinfo=UTC)
SLOT = DispatchSlot(NOW - timedelta(minutes=15), NOW + timedelta(minutes=15))


@pytest.mark.parametrize(
    "evidence",
    [
        DispatchEvidence(live_dispatch=True),
        DispatchEvidence(rate_adjusted=True),
        DispatchEvidence(ev_status_value="Delivering"),
        DispatchEvidence(ev_status_value="Boosting"),
        DispatchEvidence(ev_status_value="Charging"),
    ],
    ids=["live", "adjusted", "delivering", "boosting", "charging"],
)
def test_confirmation_evidence_beats_a_disconnected_plug(
    evidence: DispatchEvidence,
) -> None:
    evidence = DispatchEvidence(
        live_dispatch=evidence.live_dispatch,
        rate_adjusted=evidence.rate_adjusted,
        ev_status_value=evidence.ev_status_value,
        plug_value=" EV Disconnected ",
    )

    assert rate_dispatch(SLOT, evidence, NOW) is DispatchRating.CONFIRMED


def test_car_drawing_confirms_only_a_dispatch_planned_now() -> None:
    future_slot = DispatchSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=2))

    assert rate_dispatch(
        future_slot, DispatchEvidence(ev_status_value="Charging"), NOW
    ) is DispatchRating.UNCORROBORATED


@pytest.mark.parametrize("plug", ["EV Connected", "Charging", "Waiting for EV"])
def test_connected_plug_corroborates_a_planned_dispatch(plug: str) -> None:
    evidence = DispatchEvidence(plug_value=f" {plug} ")
    assert rate_dispatch(SLOT, evidence, NOW) is DispatchRating.CORROBORATED
    assert partition_dispatches((SLOT,), evidence, NOW)[0] == (SLOT,)


@pytest.mark.parametrize(
    "evidence",
    [
        DispatchEvidence(),
        DispatchEvidence(plug_value=None, ev_status_value=None),
        DispatchEvidence(plug_value="unknown", ev_status_value="unavailable"),
        DispatchEvidence(plug_value="Plugged in", ev_status_value="Paused"),
        DispatchEvidence(plug_value="EV Disconnected", ev_status_value="Paused"),
        DispatchEvidence(plug_value="EV Disconnected", rate_adjusted=False),
    ],
    ids=["unset", "unset-explicit", "unreadable", "unrecognised-plug", "issue-shape",
         "not-adjusted"],
)
def test_unconfirmed_or_unreadable_dispatch_is_kept_unless_contradicted(
    evidence: DispatchEvidence,
) -> None:
    rating = rate_dispatch(SLOT, evidence, NOW)

    if evidence.plug_value == "EV Disconnected":
        assert rating is DispatchRating.CONTRADICTED
        assert partition_dispatches((SLOT,), evidence, NOW)[0] == ()
    else:
        assert rating is DispatchRating.UNCORROBORATED
        assert partition_dispatches((SLOT,), evidence, NOW)[0] == (SLOT,)


def test_live_and_adjusted_rate_are_whole_picture_confirmations() -> None:
    future_slot = DispatchSlot(NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1))

    assert rate_dispatch(
        future_slot,
        DispatchEvidence(live_dispatch=True, plug_value="EV Disconnected"),
        NOW,
    ) is DispatchRating.CONFIRMED
    assert rate_dispatch(
        future_slot,
        DispatchEvidence(rate_adjusted=True, plug_value="EV Disconnected"),
        NOW,
    ) is DispatchRating.CONFIRMED


def test_unrecognised_and_readable_disconnected_plugs_have_distinct_ratings() -> None:
    assert rate_dispatch(SLOT, DispatchEvidence(plug_value="EV Disconnected"), NOW) is (
        DispatchRating.CONTRADICTED
    )
    assert rate_dispatch(SLOT, DispatchEvidence(plug_value="Maybe connected"), NOW) is (
        DispatchRating.UNCORROBORATED
    )


def test_2026_10_03_dispatch_shape_is_contradicted_and_dropped() -> None:
    evidence = DispatchEvidence(
        live_dispatch=False,
        rate_adjusted=False,
        plug_value="EV Disconnected",
        ev_status_value="Paused",
    )

    assert rate_dispatch(SLOT, evidence, NOW) is DispatchRating.CONTRADICTED
    assert partition_dispatches((SLOT,), evidence, NOW)[0] == ()


def test_car_draw_uses_household_wall_time_for_naive_dispatch_bounds() -> None:
    naive_slot = DispatchSlot(SLOT.start.replace(tzinfo=None), SLOT.end.replace(tzinfo=None))

    assert rate_dispatch(
        naive_slot,
        DispatchEvidence(ev_status_value="Charging", plug_value="EV Disconnected"),
        NOW,
    ) is DispatchRating.CONFIRMED


def test_drawing_confirms_when_now_is_inside_this_planned_slot() -> None:
    assert rate_dispatch(SLOT, DispatchEvidence(ev_status_value="Charging"), NOW) is (
        DispatchRating.CONFIRMED
    )


def test_only_contradicted_dispatches_are_dropped() -> None:
    active = SLOT
    future = DispatchSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    slots = (active, future)
    evidence = DispatchEvidence(plug_value="EV Disconnected", ev_status_value="Charging")

    assert partition_dispatches(slots, evidence, NOW)[0] == (active,)


@pytest.mark.parametrize(
    ("car", "grid", "house", "rating"),
    [
        (1.4, None, None, DispatchRating.CONFIRMED),
        (None, 4.0, 1.0, DispatchRating.CONFIRMED),
        (0.0, 3.999, 1.0, DispatchRating.CONTRADICTED),
        (0.0, None, 1.0, DispatchRating.UNCORROBORATED),
        (None, 0.0, 1.0, DispatchRating.UNCORROBORATED),
        (float("nan"), 0.0, 1.0, DispatchRating.UNCORROBORATED),
        (-1.0, 0.0, 1.0, DispatchRating.UNCORROBORATED),
        (0.0, float("inf"), 1.0, DispatchRating.UNCORROBORATED),
    ],
)
def test_measured_power_confirms_or_conservatively_releases(
    car: float | None,
    grid: float | None,
    house: float | None,
    rating: DispatchRating,
) -> None:
    evidence = DispatchEvidence(car_power_kw=car, grid_import_kw=grid, forecast_house_kw=house)
    assert rate_dispatch(SLOT, evidence, NOW) is rating


@pytest.mark.parametrize("minutes", [0, 9, 10])
def test_no_draw_release_waits_ten_minutes(minutes: int) -> None:
    evidence = DispatchEvidence(car_power_kw=0, grid_import_kw=1, forecast_house_kw=1)
    now = SLOT.start + timedelta(minutes=minutes)
    expected = DispatchRating.CONTRADICTED if minutes == 10 else DispatchRating.UNCORROBORATED
    assert rate_dispatch(SLOT, evidence, now) is expected


@pytest.mark.parametrize(
    "stronger",
    [
        {"live_dispatch": True},
        {"rate_adjusted": True},
        {"ev_status_value": "Charging"},
        {"plug_value": "EV Connected"},
    ],
)
def test_no_draw_never_overrides_stronger_evidence(stronger: dict[str, object]) -> None:
    evidence = DispatchEvidence(car_power_kw=0, grid_import_kw=0, forecast_house_kw=1)
    from dataclasses import replace

    evidence = replace(evidence, **stronger)
    assert rate_dispatch(SLOT, evidence, NOW) in (
        DispatchRating.CONFIRMED,
        DispatchRating.CORROBORATED,
    )


def test_power_never_rates_a_future_dispatch() -> None:
    future = DispatchSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    assert rate_dispatch(future, DispatchEvidence(car_power_kw=7), NOW) is (
        DispatchRating.UNCORROBORATED
    )
