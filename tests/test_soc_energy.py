"""Energy-evidence integrity and observed commissioning glitches (#244)."""

from datetime import UTC, datetime, timedelta

import pytest

from ha_spark.energy.soc_integrity import SocStatus, check_soc
from ha_spark.energy.soc_monitor import SocMonitor, SocOperatingState
from ha_spark.ha.models import EntityState

NOW = datetime(2026, 10, 5, tzinfo=UTC)
CAPACITY = 26.88


def state(value: float, at: datetime) -> EntityState:
    return EntityState(entity_id="sensor.soc", state=str(value), last_reported=at)


@pytest.mark.parametrize("baseline,power,seconds", [(57, 920, 8), (50, 3170, 12), (73, 0, 60)])
def test_commissioning_glitches_rejected(baseline: float, power: float, seconds: int) -> None:
    result = check_soc(
        state(100, NOW),
        observed_at=NOW,
        max_age=timedelta(minutes=10),
        baseline_soc=baseline,
        energy_kwh=power * seconds / 3_600_000,
        battery_capacity_kwh=CAPACITY,
    )
    assert result.status is SocStatus.IMPLAUSIBLE
    assert f"{baseline:g}→100%" in result.reason
    assert result.soc_now == 0


@pytest.mark.parametrize(
    "value,energy,expected", [(55, 1, True), (50, 0.54, False), (100, None, True), (52, 0, True)]
)
def test_energy_boundaries(value: float, energy: float | None, expected: bool) -> None:
    result = check_soc(
        state(value, NOW),
        observed_at=NOW,
        max_age=timedelta(minutes=10),
        baseline_soc=50,
        energy_kwh=energy,
        battery_capacity_kwh=CAPACITY,
    )
    assert result.ok is expected


def observe(monitor: SocMonitor, value: float, seconds: int, power: float | None = 1000):
    at = NOW + timedelta(seconds=seconds)
    return monitor.check_observation(
        state(value, at),
        observed_at=at,
        max_age=timedelta(minutes=10),
        power_w=power,
        battery_capacity_kwh=CAPACITY,
    )


def test_stuck_energy_accumulates_and_cannot_rebaseline() -> None:
    monitor = SocMonitor()
    assert observe(monitor, 50, 0).ok
    assert observe(monitor, 50, 1800).ok  # 0.5 kWh < 0.5376 kWh
    for seconds in (1980, 2040, 2100):
        result = observe(monitor, 50, seconds)
        assert result.status is SocStatus.IMPLAUSIBLE
        assert "stuck" in result.reason
    assert monitor.record(result, failure_threshold=1).state is SocOperatingState.FALLBACK_THRESHOLD
    assert observe(monitor, 52, 2160).ok
    assert observe(monitor, 52, 2220).ok  # changed baseline resets energy


def test_two_read_rebaseline_warns_and_new_monitor_has_no_history(caplog) -> None:
    monitor = SocMonitor()
    assert observe(monitor, 57, 0).ok
    assert not observe(monitor, 100, 60).ok
    assert observe(monitor, 100, 120).ok
    assert "possible BMS recalibration to 100" in caplog.text
    assert observe(SocMonitor(), 100, 0).ok


def test_missing_power_discards_interval_and_breaks_rejected_sequence() -> None:
    monitor = SocMonitor()
    observe(monitor, 50, 0)
    assert observe(monitor, 50, 3600, None).ok
    assert observe(monitor, 50, 7200).ok
    assert not observe(monitor, 100, 7260).ok
    observe(monitor, 50, 7320)
    assert not observe(monitor, 100, 7380).ok


def test_glitch_does_not_end_staleness_block_and_failure_resets_recovery() -> None:
    monitor = SocMonitor()
    observe(monitor, 73, 0, 0)
    stale = check_soc(
        state(73, NOW), observed_at=NOW + timedelta(hours=1), max_age=timedelta(minutes=10)
    )
    monitor.record(stale, failure_threshold=1)
    monitor.request_fallback()
    monitor.complete_fallback(action_line="[FALLBACK] verified")
    monitor.record(observe(monitor, 73, 3660, 0), failure_threshold=1)
    assert monitor._recovery_since is not None
    glitch = observe(monitor, 100, 3720, 0)
    snapshot = monitor.record(glitch, failure_threshold=1)
    assert snapshot.measurement.status is SocStatus.IMPLAUSIBLE
    assert snapshot.consecutive_failures == 2
    assert snapshot.recovery_since is None
    assert not snapshot.recovery_ready
    assert monitor.fallback_active


def test_opposite_power_signs_both_count_as_flow() -> None:
    monitor = SocMonitor()
    observe(monitor, 50, 0, -1000)
    result = observe(monitor, 50, 2000, 1000)
    assert result.status is SocStatus.IMPLAUSIBLE
    assert "stuck" in result.reason


def test_rebaseline_does_not_override_stale_report() -> None:
    monitor = SocMonitor()
    observe(monitor, 57, 0, 0)
    assert not observe(monitor, 100, 60, 0).ok
    result = monitor.check_observation(
        state(100, NOW), observed_at=NOW + timedelta(hours=1),
        max_age=timedelta(minutes=10), power_w=0, battery_capacity_kwh=CAPACITY,
    )
    assert result.status is SocStatus.STALE
