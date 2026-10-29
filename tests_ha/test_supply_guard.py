"""Supply guard decisions use explicit time, measurements and authority."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.ha_spark.supply_guard import (
    AuthorityMirror,
    GuardInputs,
    GuardReading,
    GuardState,
    InverterMeasurement,
    step,
)

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
MIRROR = AuthorityMirror("optimise", 7, False, False, None)


def inputs(at: datetime, watts: float | None = 11000) -> GuardInputs:
    return GuardInputs(
        limit_kw=10, requested_kw=4, reserve_kw=2,
        reading=GuardReading(watts, at),
        measurements=(InverterMeasurement("inv", 4000, at),),
        mirror=MIRROR,
    )


def test_two_readings_cut_and_restore_in_one_step() -> None:
    state = GuardState(restore_day=NOW.date())
    state, decision = step(state, inputs(NOW), NOW)
    assert not decision.write
    state, decision = step(state, inputs(NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1))
    assert decision.write and decision.allowance_kw == 3
    for seconds in range(2, 303, 30):
        at = NOW + timedelta(seconds=seconds)
        state, decision = step(state, inputs(at, 5000), at)
    assert decision.write and decision.allowance_kw == 4
    assert state.cut_kw is None and state.restores_used == 1


def test_single_spike_and_deadband_do_not_cut() -> None:
    state = GuardState(restore_day=NOW.date())
    for seconds, watts in enumerate([11000, 10200, 11000, 10000]):
        at = NOW + timedelta(seconds=seconds)
        state, decision = step(state, inputs(at, watts), at)
        assert not decision.write and state.cut_kw is None


def test_missing_grace_from_report_and_latch_survives_fresh_reading() -> None:
    state, _ = step(GuardState(restore_day=NOW.date()), inputs(NOW, 9000), NOW)
    for seconds in (60, 61, 119):
        state, decision = step(state, inputs(NOW, 9000), NOW + timedelta(seconds=seconds))
        assert not decision.write
    state, decision = step(state, inputs(NOW, None), NOW + timedelta(seconds=120))
    assert decision.write and decision.allowance_kw == 2 and state.latched
    at = NOW + timedelta(seconds=121)
    state, _ = step(state, inputs(at, 9000), at)
    state, decision = step(state, inputs(at, None), at + timedelta(seconds=120))
    assert state.latched and decision.allowance_kw == 2 and not decision.write


def test_budget_epoch_and_warn_only() -> None:
    state = GuardState(restore_day=NOW.date(), cut_kw=2, epoch=7, restored=True)
    for mirror in (None, replace(MIRROR, mode="observe"), replace(MIRROR, epoch=None)):
        _, decision = step(state, replace(inputs(NOW), mirror=mirror), NOW)
        assert not decision.write
    _, decision = step(state, inputs(NOW), NOW)
    assert decision.write
    state = replace(state, restored=False, restores_used=48)
    for seconds in range(0, 361, 30):
        at = NOW + timedelta(seconds=seconds)
        state, decision = step(state, inputs(at, 5000), at)
    assert not decision.write and state.restores_left == 0
    tomorrow = NOW + timedelta(days=1)
    state, _ = step(state, inputs(tomorrow, 5000), tomorrow)
    assert state.restores_left == 48


@pytest.mark.parametrize("mode", ["protect", "optimise", "observe", "unknown"])
@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("failed,transition", [(False, None), (True, None), (False, "observe")])
def test_mirror_matrix(mode: str, manual: bool, failed: bool, transition: str | None) -> None:
    mirror = AuthorityMirror(mode, 7, manual, failed, transition)
    state = GuardState(restore_day=NOW.date())
    for second in range(2):
        at = NOW + timedelta(seconds=second)
        state, decision = step(state, replace(inputs(at), mirror=mirror), at)
    assert decision.write == (mode in {"protect", "optimise"} and not failed
                              and transition != "observe")
    assert decision.allowance_kw == 3  # manual owns only the ceiling, not the target


@pytest.mark.parametrize("watts", [None, float("nan"), float("inf")])
def test_invalid_reading_is_missing(watts: float | None) -> None:
    state, _ = step(GuardState(restore_day=NOW.date()), inputs(NOW, 9000), NOW)
    at = NOW + timedelta(seconds=120)
    state, decision = step(state, inputs(at, watts), at)
    assert state.latched and decision.allowance_kw == 2


@pytest.mark.parametrize("headroom,reserve,cut", [(2, 2, False), (3, 2, False), (1, 2, True)])
def test_missing_only_cuts_below_reserve(headroom: float, reserve: float, cut: bool) -> None:
    state, _ = step(GuardState(restore_day=NOW.date()), inputs(NOW, (10 - headroom) * 1000), NOW)
    at = NOW + timedelta(seconds=120)
    state, decision = step(state, replace(inputs(at, None), reserve_kw=reserve), at)
    assert decision.write == cut
    assert state.latched == cut


def test_stale_inverter_counts_as_missing_and_future_report_is_not_evidence() -> None:
    state, _ = step(GuardState(restore_day=NOW.date()), inputs(NOW, 9000), NOW)
    at = NOW + timedelta(seconds=120)
    for report in (NOW, at + timedelta(seconds=1)):
        bad = replace(inputs(at, 9000), measurements=(InverterMeasurement("inv", 4000, report),))
        result, decision = step(state, bad, at)
        assert result.latched and decision.write and decision.allowance_kw == 2


def test_restore_dip_resets_timer_and_only_full_restore_clears_latch() -> None:
    state = GuardState(restore_day=NOW.date(), cut_kw=2, latched=True, restores_used=47)
    for seconds in range(0, 601, 30):
        at = NOW + timedelta(seconds=seconds)
        watts = 10000 if seconds == 270 else 5000
        state, decision = step(state, inputs(at, watts), at)
        if seconds < 600:
            assert state.latched
    assert decision.write and decision.allowance_kw == 4
    assert not state.latched and state.restores_left == 0


@pytest.mark.parametrize("requested", [0, 1, 4])
def test_floor_never_above_request_or_start(requested: float) -> None:
    state = GuardState(restore_day=NOW.date())
    for seconds in range(2):
        at = NOW + timedelta(seconds=seconds)
        state, decision = step(state, replace(inputs(at, 20000), requested_kw=requested), at)
    assert decision.write == (requested > 0)
    if requested > 0:
        assert decision.allowance_kw == 0
    assert state.restores_used == 0


def test_idle_readings_spend_no_restores() -> None:
    state = GuardState(restore_day=NOW.date(), cut_kw=2)
    for seconds in range(0, 601, 30):
        at = NOW + timedelta(seconds=seconds)
        state, decision = step(state, replace(inputs(at, 5000), requested_kw=0), at)
        assert not decision.write
    assert state.restores_used == 0 and state.cut_kw == 2


@pytest.mark.parametrize("zone", ["Pacific/Auckland", "America/New_York"])
def test_midnight_uses_household_day(zone: str) -> None:
    at = datetime(2026, 10, 29, 23, 59, 59, tzinfo=ZoneInfo(zone))
    state = GuardState(restore_day=at.date(), restores_used=48)
    state, _ = step(state, replace(inputs(at), requested_kw=0, household_day=at.date()), at)
    assert state.restores_left == 0
    at += timedelta(seconds=1)
    state, _ = step(state, replace(inputs(at), requested_kw=0, household_day=at.date()), at)
    assert state.restores_left == 48


@pytest.mark.parametrize("missing", ["supply", "grid", "inverter", "setpoint"])
def test_dormancy(missing: str) -> None:
    data = replace(inputs(NOW), limit_kw=0 if missing == "supply" else 10,
                   grid_configured=missing != "grid", controllable=missing != "setpoint",
                   measurements=() if missing == "inverter" else inputs(NOW).measurements)
    state, decision = step(GuardState(restore_day=NOW.date()), data, NOW)
    assert state.dormant_reason is not None and not decision.write


def test_restore_accepts_exact_200_w_headroom() -> None:
    state = GuardState(restore_day=NOW.date(), cut_kw=1)
    for seconds in range(0, 301, 30):
        at = NOW + timedelta(seconds=seconds)
        data = replace(inputs(at, 17800), limit_kw=18, requested_kw=2,
                       measurements=(InverterMeasurement("inv", 2000, at),))
        state, decision = step(state, data, at)
    assert decision.write and decision.allowance_kw == 2
    assert state.cut_kw is None


def test_cached_report_is_not_a_second_overload_reading() -> None:
    state, _ = step(GuardState(restore_day=NOW.date()), inputs(NOW), NOW)
    state, decision = step(state, inputs(NOW), NOW + timedelta(seconds=5))
    assert state.over_count == 1 and not decision.write


def test_new_cut_after_restart_writes_in_current_epoch() -> None:
    loaded = GuardState(restore_day=NOW.date(), cut_kw=3.5, epoch=7, restored=True)
    unknown = replace(MIRROR, epoch=None)
    state, decision = step(loaded, replace(inputs(NOW), mirror=unknown), NOW)
    assert not decision.write and decision.warn_reason == "authority_unavailable"
    at = NOW + timedelta(seconds=1)
    state, decision = step(state, replace(inputs(at), mirror=unknown), at)
    assert decision.write and decision.allowance_kw == 3 and decision.warn_reason is None
    assert not state.restored
