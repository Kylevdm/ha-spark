"""Native integrity policy at its observation/result seam; no HA or I/O."""
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ha_spark.soc_trust import BankTrust, Policy, SocMonitor, check_soc

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def observe(monitor: SocMonitor, minute: int, value: object = 50, **kwargs: bool) -> BankTrust:
    at = NOW + timedelta(minutes=minute)
    return monitor.observe(value, read_at=at, now=at, power_w=0, capacity_kwh=26.88, **kwargs)


def test_first_failure_threshold_and_deduplication() -> None:
    monitor = SocMonitor()
    assert observe(monitor, 0).trust == "trusted"
    assert observe(monitor, 1, None).trust == "untrusted"
    assert not monitor.increases_allowed
    observe(monitor, 1, None)
    observe(monitor, 2, None)
    assert not monitor.fallback_requested
    observe(monitor, 3, None)
    assert monitor.fallback_requested


def test_pass_resets_pending_failure() -> None:
    monitor = SocMonitor()
    observe(monitor, 0, None)
    assert observe(monitor, 1).trust == "trusted"
    observe(monitor, 2, None)
    observe(monitor, 3, None)
    assert not monitor.fallback_requested


def test_continuous_recovery_and_restart() -> None:
    monitor = SocMonitor(Policy(soc_failure_threshold=1, soc_recovery_minutes=2))
    observe(monitor, 0, None)
    assert observe(monitor, 1).trust == "recovering"
    observe(monitor, 3)  # missed minute restarts the window
    observe(monitor, 4)
    assert not monitor.recovery_ready
    observe(monitor, 5, fallback_active=True)
    assert monitor.recovery_ready
    assert not monitor.increases_allowed
    assert observe(monitor, 6).trust == "trusted"
    restarted = SocMonitor(monitor.policy)
    assert observe(restarted, 7, fallback_active=True).trust == "recovering"
    assert not restarted.recovery_ready


@pytest.mark.parametrize("value,reason", [(None, "unavailable"), ("bad", "malformed"),
    (float("inf"), "not_finite"), (101, "out_of_range"), (-1, "out_of_range")])
def test_invalid_values_never_substituted(value: object, reason: str) -> None:
    result = observe(SocMonitor(), 0, value)
    assert result.trust == "untrusted"
    assert result.reason == reason
    assert result.value is None


def test_energy_jump_two_read_recalibration() -> None:
    monitor = SocMonitor()
    observe(monitor, 0, 57)
    assert observe(monitor, 1, 100).reason == "implausible"
    assert observe(monitor, 2, 100).trust == "trusted"


def test_same_read_cannot_recalibrate_or_advance_recovery() -> None:
    monitor = SocMonitor()
    observe(monitor, 0, 57)
    observe(monitor, 1, 100)
    at = NOW + timedelta(minutes=1)
    result = monitor.observe(100, read_at=at, now=at + timedelta(minutes=1),
                             power_w=0, capacity_kwh=26.88)
    assert result.reason == "implausible"


def test_stuck_energy_and_missing_power() -> None:
    monitor = SocMonitor()
    for minute in range(35):
        at = NOW + timedelta(minutes=minute)
        result = monitor.observe(50, read_at=at, now=at, power_w=1000, capacity_kwh=26.88)
    assert result.reason == "implausible"
    monitor = SocMonitor()
    for minute, power in [(0, 1000), (60, None), (120, 1000)]:
        at = NOW + timedelta(minutes=minute)
        result = monitor.observe(50, read_at=at, now=at, power_w=power, capacity_kwh=26.88)
    assert result.trust == "trusted"


@pytest.mark.parametrize("seconds,reason", [(-1, "report_time_future"), (0, "ok"),
                                           (600, "ok"), (601, "stale")])
def test_freshness_boundaries(seconds: int, reason: str) -> None:
    result = check_soc(73, NOW - timedelta(seconds=seconds), now=NOW,
                       max_age=timedelta(minutes=10))
    assert result.reason == reason
    assert (result.trust == "trusted") == (reason == "ok")


@pytest.mark.parametrize("stamp", [None, NOW.replace(tzinfo=None)])
def test_no_substitute_report_time(stamp: datetime | None) -> None:
    result = check_soc(73, stamp, now=NOW, max_age=timedelta(minutes=10))
    assert result.reason == "report_time_unusable"
    assert result.trust == "untrusted"


@pytest.mark.parametrize("value,energy,expected", [(55, 1, True), (50, 0.54, False),
    (100, None, True), (52, 0, True)])
def test_energy_boundaries(value: float, energy: float | None, expected: bool) -> None:
    result = check_soc(value, NOW, now=NOW, max_age=timedelta(minutes=10),
                       baseline_soc=50, energy_kwh=energy, capacity_kwh=26.88)
    assert (result.trust == "trusted") is expected


def test_absolute_power_both_directions() -> None:
    monitor = SocMonitor()
    for minute, power in [(0, -1000), (34, 1000)]:
        at = NOW + timedelta(minutes=minute)
        result = monitor.observe(50, read_at=at, now=at, power_w=power, capacity_kwh=26.88)
    assert result.reason == "implausible"


def test_recovery_failure_and_backwards_read_restart_window() -> None:
    monitor = SocMonitor(Policy(soc_failure_threshold=1, soc_recovery_minutes=2))
    observe(monitor, 0, None)
    observe(monitor, 1)
    observe(monitor, 2, None)
    observe(monitor, 3)
    monitor.observe(50, read_at=NOW + timedelta(minutes=2),
                    now=NOW + timedelta(minutes=4), power_w=0, capacity_kwh=26.88)
    observe(monitor, 5)
    observe(monitor, 6)
    assert not monitor.recovery_ready
    assert observe(monitor, 7).trust == "trusted"


def test_failure_count_only_for_current_owner() -> None:
    monitor = SocMonitor(Policy(soc_failure_threshold=1))
    assert observe(monitor, 0, None, count=False).trust == "untrusted"
    assert not monitor.fallback_requested
    assert observe(monitor, 1, None).trust == "untrusted"
    assert monitor.fallback_requested


def test_cached_read_expires_without_advancing_or_recalibrating() -> None:
    monitor = SocMonitor()
    observe(monitor, 0)
    for minute in range(1, 11):
        result = monitor.observe(100, read_at=NOW, now=NOW + timedelta(minutes=minute),
                                 power_w=0, capacity_kwh=26.88)
        assert result.value == 50
        assert result.trust == "trusted"
    result = monitor.observe(100, read_at=NOW, now=NOW + timedelta(minutes=11),
                             power_w=0, capacity_kwh=26.88)
    assert result.reason == "stale"


def test_recalibration_never_overrides_age_check() -> None:
    monitor = SocMonitor()
    observe(monitor, 0, 57)
    observe(monitor, 1, 100)
    result = monitor.observe(100, read_at=NOW + timedelta(minutes=2),
                             now=NOW + timedelta(hours=1), power_w=0, capacity_kwh=26.88)
    assert result.reason == "stale"


def test_same_minute_lost_validation_counts_once_and_restarts_recovery() -> None:
    monitor = SocMonitor(Policy(soc_failure_threshold=1, soc_recovery_minutes=2))
    observe(monitor, 0, None)
    observe(monitor, 1)
    before = monitor.failures
    at = NOW + timedelta(minutes=1, seconds=30)
    for _ in range(2):
        monitor.observe(None, read_at=None, now=at, power_w=None, capacity_kwh=26.88, valid=False)
    assert monitor.failures == before + 1
    assert observe(monitor, 2).trust == "recovering"
    assert observe(monitor, 3).trust == "recovering"
    assert observe(monitor, 4).trust == "trusted"


def test_same_minute_lost_validation_requests_fallback_at_threshold() -> None:
    monitor = SocMonitor(Policy(soc_failure_threshold=1))
    observe(monitor, 0)
    at = NOW + timedelta(seconds=30)
    monitor.observe(None, read_at=None, now=at, power_w=None, capacity_kwh=26.88, valid=False)
    assert monitor.fallback_requested


def test_failed_minute_discards_recalibration_candidate() -> None:
    monitor = SocMonitor()
    observe(monitor, 0, 57)
    assert observe(monitor, 1, 100).reason == "implausible"
    at = NOW + timedelta(minutes=2)
    monitor.observe(None, read_at=None, now=at, power_w=None, capacity_kwh=26.88, valid=False)
    assert observe(monitor, 3, 100).reason == "implausible"
    assert observe(monitor, 4, 100).reason == "ok"  # third failure blocked: recovering


@pytest.mark.parametrize("key,value", [("soc_max_report_age_minutes", 1e308),
    ("soc_recovery_minutes", 10**12), ("soc_failure_threshold", 0)])
def test_policy_rejects_unrepresentable_bounds(key: str, value: float) -> None:
    with pytest.raises(ValueError):
        Policy.model_validate({key: value})


@pytest.mark.parametrize("baseline,power,seconds", [(50, 3170, 12), (73, 0, 60)])
def test_commissioning_glitches_rejected(baseline: float, power: float, seconds: int) -> None:
    result = check_soc(100, NOW, now=NOW, max_age=timedelta(minutes=10), baseline_soc=baseline,
                       energy_kwh=power * seconds / 3_600_000, capacity_kwh=26.88)
    assert result.reason == "implausible"
    assert result.trust == "untrusted"


def test_missing_power_discards_interval_and_breaks_rejected_sequence() -> None:
    monitor = SocMonitor()

    def at(value: float, minute: int, power: float | None = 1000) -> BankTrust:
        stamp = NOW + timedelta(minutes=minute)
        return monitor.observe(value, read_at=stamp, now=stamp, power_w=power,
                               capacity_kwh=26.88)

    at(50, 0)
    assert at(50, 60, None).trust == "trusted"
    assert at(50, 120).trust == "trusted"
    assert at(100, 121).trust == "untrusted"
    at(50, 122)
    assert at(100, 123).trust == "untrusted"
