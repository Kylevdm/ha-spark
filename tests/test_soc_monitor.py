"""Tests for per-minute SoC integrity monitoring and pending failure (#114)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus, check_soc
from ha_spark.energy.soc_monitor import (
    MONITOR_FILE,
    SocMonitor,
    SocOperatingState,
    observe_soc,
)
from ha_spark.ha.rest import HomeAssistantRest


def _measurement(ok: bool, *, value: float = 30.0) -> SocMeasurement:
    """One checked measurement: passing at ``value``, or stale (a failure)."""
    now = datetime.now(UTC)
    if ok:
        return check_soc(
            _entity(str(value), reported_at=now), observed_at=now, max_age=timedelta(minutes=10)
        )
    return check_soc(
        _entity(str(value), reported_at=now - timedelta(hours=1)),
        observed_at=now,
        max_age=timedelta(minutes=10),
    )


def _entity(state: str, *, reported_at: datetime) -> object:
    from ha_spark.ha.models import EntityState

    return EntityState(
        entity_id="sensor.soc",
        state=state,
        attributes={},
        last_reported=reported_at,
    )


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(  # type: ignore[arg-type]
        ha_url="http://ha.test",
        ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        **kw,
    )


# --- state transitions ---


def test_first_failure_enters_pending_failure(tmp_path: Path) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    snap = m.record(_measurement(ok=False), failure_threshold=3)
    assert snap.state is SocOperatingState.PENDING_FAILURE
    assert snap.consecutive_failures == 1
    assert snap.failure_threshold == 3
    assert not snap.measurement.ok


def test_passing_observation_is_normal(tmp_path: Path) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    snap = m.record(_measurement(ok=True), failure_threshold=3)
    assert snap.state is SocOperatingState.NORMAL
    assert snap.consecutive_failures == 0


def test_default_third_consecutive_failure_reaches_threshold(tmp_path: Path) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    states = [
        m.record(_measurement(ok=False), failure_threshold=3).state for _ in range(3)
    ]
    assert states == [
        SocOperatingState.PENDING_FAILURE,
        SocOperatingState.PENDING_FAILURE,
        SocOperatingState.FALLBACK_THRESHOLD,
    ]
    assert m.record(_measurement(ok=False), failure_threshold=3).consecutive_failures == 4


def test_custom_threshold_behaves_equivalently(tmp_path: Path) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    first = m.record(_measurement(ok=False), failure_threshold=1)
    assert first.state is SocOperatingState.FALLBACK_THRESHOLD
    assert first.consecutive_failures == 1


def test_fallback_confirmation_persists_and_pass_does_not_clear_active_fallback(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monitor = SocMonitor.load(settings)
    for _ in range(3):
        monitor.record(_measurement(ok=False), failure_threshold=3)

    requested = monitor.request_fallback()
    assert requested is not None
    assert requested.state is SocOperatingState.FALLBACK_REQUESTED
    monitor.complete_fallback(action_line="[FALLBACK] verified")

    passed = monitor.record(_measurement(ok=True), failure_threshold=3)
    assert passed.state is SocOperatingState.FALLBACK_VERIFIED
    assert passed.consecutive_failures == 3
    assert passed.fallback_confirmed
    assert passed.fallback_action is None

    restored = SocMonitor.load(settings)
    restored_snapshot = restored.record(_measurement(ok=False), failure_threshold=3)
    assert restored_snapshot.state is SocOperatingState.FALLBACK_VERIFIED
    assert not restored_snapshot.fallback_confirmed
    retry = restored.request_fallback()
    assert retry is not None
    assert retry.state is SocOperatingState.FALLBACK_REQUESTED


def test_unconfirmed_fallback_is_retried_and_not_reported_as_verified(
    tmp_path: Path,
) -> None:
    monitor = SocMonitor.load(_settings(tmp_path))
    for _ in range(3):
        monitor.record(_measurement(ok=False), failure_threshold=3)
    assert monitor.request_fallback() is not None
    monitor.complete_fallback(action_line="[FAILED] read-back mismatch")
    assert monitor.request_fallback() is None  # one attempt for this observation

    failed = monitor.record(_measurement(ok=False), failure_threshold=3)
    assert failed.state is SocOperatingState.FALLBACK_FAILED
    assert not failed.fallback_confirmed
    retry = monitor.request_fallback()
    assert retry is not None
    assert retry.state is SocOperatingState.FALLBACK_REQUESTED


def test_fallback_status_requires_a_fallback_confirmation_action(tmp_path: Path) -> None:
    monitor = SocMonitor.load(_settings(tmp_path))
    monitor.record(_measurement(ok=False), failure_threshold=1)
    assert monitor.request_fallback() is not None

    snapshot = monitor.complete_fallback(action_line="[FAILED] current read-back mismatch")

    assert snapshot.state is SocOperatingState.FALLBACK_FAILED
    assert not snapshot.fallback_confirmed
    assert snapshot.fallback_action.startswith("[FAILED]")


@pytest.mark.parametrize(
    "action_line",
    [
        "[SIMULATE] Solis fallback at 45 A in 23:30-05:30 would be programmed (not written)",
        "[SKIP] Solis fallback not written (mode off)",
    ],
)
def test_non_actuation_fallback_line_stays_requested_and_unconfirmed(
    tmp_path: Path, action_line: str
) -> None:
    monitor = SocMonitor.load(_settings(tmp_path))
    monitor.record(_measurement(ok=False), failure_threshold=1)
    assert monitor.request_fallback() is not None

    snapshot = monitor.complete_fallback(action_line=action_line)

    assert snapshot.state is SocOperatingState.FALLBACK_REQUESTED
    assert not snapshot.fallback_confirmed
    assert snapshot.fallback_action == action_line


def test_changed_fallback_configuration_invalidates_prior_confirmation(tmp_path: Path) -> None:
    monitor = SocMonitor.load(_settings(tmp_path))
    monitor.record(_measurement(ok=False), failure_threshold=1)
    assert monitor.request_fallback() is not None
    monitor.complete_fallback(action_line="[FALLBACK] verified")

    monitor.invalidate_fallback_confirmation()
    snapshot = monitor.record(_measurement(ok=True), failure_threshold=1)

    assert snapshot.state is SocOperatingState.FALLBACK_VERIFIED
    assert not snapshot.fallback_confirmed
    assert snapshot.consecutive_failures == 1


def test_pass_resets_consecutive_failures(tmp_path: Path) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    m.record(_measurement(ok=False), failure_threshold=3)
    m.record(_measurement(ok=False), failure_threshold=3)
    snap = m.record(_measurement(ok=True), failure_threshold=3)
    assert snap.state is SocOperatingState.NORMAL
    assert snap.consecutive_failures == 0
    # And the next failure starts counting from one again.
    assert m.record(_measurement(ok=False), failure_threshold=3).consecutive_failures == 1


def test_one_observation_counts_once_even_when_recorded_twice(tmp_path: Path) -> None:
    """Reusing one measurement across consumers must not double-count: the
    same observation object recorded again returns the same snapshot."""
    m = SocMonitor.load(_settings(tmp_path))
    failed = _measurement(ok=False)
    first = m.record(failed, failure_threshold=3)
    again = m.record(failed, failure_threshold=3)
    assert again is first
    assert m.record(_measurement(ok=False), failure_threshold=3).consecutive_failures == 2


# --- persistence ---


def test_failure_count_is_persisted(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    m = SocMonitor.load(s)
    for _ in range(2):
        m.record(_measurement(ok=False), failure_threshold=3)
    data = json.loads((tmp_path / MONITOR_FILE).read_text(encoding="utf-8"))
    assert data == {"consecutive_failures": 2, "fallback_status": "pending_failure"}
    # A fresh monitor (e.g. after restart) restores the count.
    assert SocMonitor.load(s).record(_measurement(ok=False), failure_threshold=3) \
        .consecutive_failures == 3


def test_passing_reset_is_persisted(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    m = SocMonitor.load(s)
    m.record(_measurement(ok=False), failure_threshold=3)
    m.record(_measurement(ok=True), failure_threshold=3)
    assert json.loads((tmp_path / MONITOR_FILE).read_text(encoding="utf-8")) == {
        "consecutive_failures": 0,
        "fallback_status": "normal",
    }


def test_load_tolerates_missing_corrupt_and_bad_files(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    # No file yet: starts at zero, and a steady normal run writes nothing.
    m = SocMonitor.load(s)
    assert m.record(_measurement(ok=True), failure_threshold=3).consecutive_failures == 0
    assert not (tmp_path / MONITOR_FILE).exists()
    # Corrupt JSON degrades to zero rather than crashing the daemon.
    (tmp_path / MONITOR_FILE).write_text("{not json", encoding="utf-8")
    assert SocMonitor.load(s).record(_measurement(ok=False), failure_threshold=3) \
        .consecutive_failures == 1
    # A nonsensical negative count is refused, not propagated.
    (tmp_path / MONITOR_FILE).write_text('{"consecutive_failures": -7}', encoding="utf-8")
    assert SocMonitor.load(s).record(_measurement(ok=False), failure_threshold=3) \
        .consecutive_failures == 1


# --- observation ---


@respx.mock
async def test_observe_soc_checks_one_ha_observation() -> None:
    s = _settings(Path("/tmp"), soc_entity="sensor.soc")
    reported = datetime.now(UTC) - timedelta(minutes=2)
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.soc",
                "state": "42",
                "attributes": {},
                "last_reported": reported.isoformat(),
            },
        )
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        m = await observe_soc(s, rest)
    assert m.ok
    assert m.value == 42.0
    assert m.status is SocStatus.OK


@respx.mock
async def test_observe_soc_turns_read_failure_into_failed_measurement() -> None:
    s = _settings(Path("/tmp"), soc_entity="sensor.soc")
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=httpx.Response(500)
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        m = await observe_soc(s, rest)
    assert not m.ok
    assert m.status is SocStatus.READ_FAILED


@respx.mock
async def test_observe_soc_judges_freshness_against_configured_max_age() -> None:
    s = _settings(
        Path("/tmp"), soc_entity="sensor.soc", soc_max_report_age_minutes=5.0
    )
    reported = datetime.now(UTC) - timedelta(minutes=8)
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.soc",
                "state": "42",
                "attributes": {},
                "last_reported": reported.isoformat(),
            },
        )
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        m = await observe_soc(s, rest)
    assert m.status is SocStatus.STALE
    assert m.max_age_s == 300.0


def _state_json(entity_id: str, state: str, reported: datetime) -> dict[str, object]:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {},
        "last_reported": reported.isoformat(),
    }


@respx.mock
async def test_observe_soc_accepts_unchanged_soc_while_voltage_reports() -> None:
    """#169: the battery-voltage entity is the SoC source's liveness signal."""
    s = _settings(
        Path("/tmp"), soc_entity="sensor.soc", battery_voltage_entity="sensor.volts"
    )
    now = datetime.now(UTC)
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=httpx.Response(
            200, json=_state_json("sensor.soc", "76", now - timedelta(minutes=38))
        )
    )
    respx.get("http://ha.test/api/states/sensor.volts").mock(
        return_value=httpx.Response(
            200, json=_state_json("sensor.volts", "52.1", now - timedelta(seconds=3))
        )
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        m = await observe_soc(s, rest)
    assert m.ok
    assert m.value == 76.0


@respx.mock
async def test_observe_soc_failed_voltage_read_falls_back_to_own_report() -> None:
    s = _settings(
        Path("/tmp"), soc_entity="sensor.soc", battery_voltage_entity="sensor.volts"
    )
    now = datetime.now(UTC)
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=httpx.Response(
            200, json=_state_json("sensor.soc", "76", now - timedelta(minutes=38))
        )
    )
    respx.get("http://ha.test/api/states/sensor.volts").mock(
        return_value=httpx.Response(500)
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        m = await observe_soc(s, rest)
    assert m.status is SocStatus.STALE


# --- operator-visible logs ---


@pytest.mark.parametrize("threshold,count", [(3, 1), (3, 3), (2, 2)])
def test_pending_and_threshold_states_are_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    threshold: int,
    count: int,
) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    with caplog.at_level("WARNING"):
        for _ in range(count):
            m.record(_measurement(ok=False), failure_threshold=threshold)
    text = caplog.text
    assert "SoC integrity" in text
    assert f"failure {count}/{threshold}" in text
    if count >= threshold:
        assert "fallback-entry threshold reached" in text


def test_recovery_after_pending_failure_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    m.record(_measurement(ok=False), failure_threshold=3)
    with caplog.at_level("INFO"):
        m.record(_measurement(ok=True), failure_threshold=3)
    assert "SoC integrity: recovered after 1 consecutive failure" in caplog.text


def test_steady_normal_observations_are_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    m = SocMonitor.load(_settings(tmp_path))
    with caplog.at_level("INFO"):
        m.record(_measurement(ok=True), failure_threshold=3)
        m.record(_measurement(ok=True), failure_threshold=3)
    assert "SoC integrity" not in caplog.text


# --- recovery from an active fallback (#117) ---

_T0 = datetime(2026, 6, 10, 1, 0, tzinfo=UTC)
_RECOVERY = timedelta(minutes=10)


def _pass_at(
    minute: int, *, reported_minute: int | None = None, value: float = 30.0
) -> SocMeasurement:
    """A passing measurement observed ``minute`` minutes after ``_T0``."""
    observed = _T0 + timedelta(minutes=minute)
    reported = _T0 + timedelta(minutes=minute if reported_minute is None else reported_minute)
    return check_soc(
        _entity(str(value), reported_at=reported),
        observed_at=observed,
        max_age=timedelta(minutes=10),
    )


def _in_verified_fallback(tmp_path: Path) -> SocMonitor:
    monitor = SocMonitor.load(_settings(tmp_path))
    for _ in range(3):
        monitor.record(_measurement(ok=False), failure_threshold=3)
    assert monitor.request_fallback() is not None
    monitor.complete_fallback(action_line="[FALLBACK] verified")
    return monitor


def _record(monitor: SocMonitor, m: SocMeasurement) -> object:
    return monitor.record(m, failure_threshold=3, recovery_duration=_RECOVERY)


def test_recovery_needs_the_configured_continuous_duration(tmp_path: Path) -> None:
    monitor = _in_verified_fallback(tmp_path)
    for minute in range(10):
        _record(monitor, _pass_at(minute))
        assert not monitor.recovery_ready
    snap = monitor.record(_pass_at(10), failure_threshold=3, recovery_duration=_RECOVERY)
    assert monitor.recovery_ready and snap.recovery_ready
    assert snap.recovery_since == _T0
    # Ready is not recovered: the fallback stays the effective state.
    assert snap.state is SocOperatingState.FALLBACK_VERIFIED
    assert snap.fallback_confirmed


def test_failure_during_recovery_resets_progress(tmp_path: Path) -> None:
    monitor = _in_verified_fallback(tmp_path)
    for minute in range(9):
        _record(monitor, _pass_at(minute))
    snap = monitor.record(_measurement(ok=False), failure_threshold=3)
    assert snap.recovery_since is None and not snap.recovery_ready
    for minute in range(10, 20):
        _record(monitor, _pass_at(minute))
        assert not monitor.recovery_ready
    _record(monitor, _pass_at(20))
    assert monitor.recovery_ready
    assert monitor._last_snapshot is not None
    assert monitor._last_snapshot.recovery_since == _T0 + timedelta(minutes=10)


def test_recovery_allows_soc_movement(tmp_path: Path) -> None:
    monitor = _in_verified_fallback(tmp_path)
    for minute in range(11):
        _record(monitor, _pass_at(minute, value=30.0 + minute))
    assert monitor.recovery_ready


def test_recovery_needs_a_report_newer_than_its_baseline(tmp_path: Path) -> None:
    """One cached state re-read for the whole duration never recovers."""
    monitor = _in_verified_fallback(tmp_path)
    short = timedelta(minutes=5)
    for minute in range(10):  # cached but still within the 10-minute report age
        monitor.record(
            _pass_at(minute, reported_minute=0), failure_threshold=3, recovery_duration=short
        )
    assert not monitor.recovery_ready
    monitor.record(_pass_at(10), failure_threshold=3, recovery_duration=short)
    assert monitor.recovery_ready


def test_backwards_report_time_restarts_recovery(tmp_path: Path) -> None:
    monitor = _in_verified_fallback(tmp_path)
    for minute in range(10):
        _record(monitor, _pass_at(minute))
    _record(monitor, _pass_at(10, reported_minute=5))  # would have been ready
    assert not monitor.recovery_ready
    for minute in range(11, 20):
        _record(monitor, _pass_at(minute))
    assert not monitor.recovery_ready
    _record(monitor, _pass_at(20))  # ten minutes after the restart at minute 10
    assert monitor.recovery_ready


def test_complete_recovery_returns_to_normal_and_persists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    monitor = _in_verified_fallback(tmp_path)
    for minute in range(11):
        _record(monitor, _pass_at(minute))
    snap = monitor.complete_recovery(action_line="[RECOVERED] normal programming resumed")
    assert snap.state is SocOperatingState.NORMAL
    assert snap.consecutive_failures == 0
    assert not snap.fallback_confirmed and not snap.recovery_ready
    assert snap.recovery_action == "[RECOVERED] normal programming resumed"
    assert not monitor.fallback_active and not monitor.recovery_ready
    assert json.loads((tmp_path / MONITOR_FILE).read_text()) == {
        "consecutive_failures": 0,
        "fallback_status": "normal",
    }
    assert SocMonitor.load(settings).fallback_active is False


def test_recovery_ready_suppresses_fallback_reprogramming(tmp_path: Path) -> None:
    monitor = _in_verified_fallback(tmp_path)
    for minute in range(11):
        _record(monitor, _pass_at(minute))
    monitor.invalidate_fallback_confirmation()
    assert monitor.request_fallback() is None


def test_recovery_progress_is_not_persisted(tmp_path: Path) -> None:
    """Downtime is not healthy evidence: a restart starts recovery over."""
    settings = _settings(tmp_path)
    monitor = _in_verified_fallback(tmp_path)
    for minute in range(11):
        _record(monitor, _pass_at(minute))
    assert monitor.recovery_ready
    restored = SocMonitor.load(settings)
    snap = restored.record(_pass_at(12), failure_threshold=3, recovery_duration=_RECOVERY)
    assert not restored.recovery_ready
    assert snap.recovery_since == _T0 + timedelta(minutes=12)


def test_pending_failure_still_resets_on_one_pass(tmp_path: Path) -> None:
    """The recovery duration applies to an active fallback only."""
    monitor = SocMonitor.load(_settings(tmp_path))
    monitor.record(_measurement(ok=False), failure_threshold=3)
    snap = monitor.record(_pass_at(0), failure_threshold=3, recovery_duration=_RECOVERY)
    assert snap.state is SocOperatingState.NORMAL


@respx.mock
async def test_observe_soc_uses_power_evidence_and_tolerates_failed_power_read() -> None:
    settings = _settings(Path('/tmp'), soc_entity='sensor.soc', battery_power_entity='sensor.power')
    monitor = SocMonitor()
    reported = datetime.now(UTC).isoformat()
    soc_route = respx.get('http://ha.test/api/states/sensor.soc')
    power_route = respx.get('http://ha.test/api/states/sensor.power')
    soc_route.mock(return_value=httpx.Response(200, json={
        'entity_id': 'sensor.soc', 'state': '57', 'last_reported': reported,
    }))
    power_route.mock(return_value=httpx.Response(200, json={
        'entity_id': 'sensor.power', 'state': '-920',
    }))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert (await observe_soc(settings, rest, monitor=monitor)).ok
        soc_route.mock(return_value=httpx.Response(200, json={
            'entity_id': 'sensor.soc', 'state': '100', 'last_reported': reported,
        }))
        assert (await observe_soc(settings, rest, monitor=monitor)).status is SocStatus.IMPLAUSIBLE
        power_route.mock(return_value=httpx.Response(500))
        assert (await observe_soc(settings, rest, monitor=monitor)).ok
    assert power_route.call_count == 3
