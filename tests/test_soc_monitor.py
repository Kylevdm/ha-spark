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
    EffectiveProgram,
    RecoveryOutcome,
    RecoveryState,
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


def _recovery_measurement(
    observed_at: datetime, reported_at: datetime | None, *, value: float = 30.0, ok: bool = True
) -> SocMeasurement:
    return SocMeasurement(
        status=SocStatus.OK if ok else SocStatus.STALE,
        observed_at=observed_at,
        value=value,
        raw_state=str(value),
        reported_at=reported_at,
        age_s=(observed_at - reported_at).total_seconds() if reported_at else None,
        max_age_s=600.0,
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


def _fallback_monitor(stable_minutes: int = 2) -> SocMonitor:
    monitor = SocMonitor(recovery_stable_minutes=stable_minutes)
    failed = _recovery_measurement(datetime(2026, 10, 6, 20, tzinfo=UTC), None, ok=False)
    monitor.record(failed, failure_threshold=1)
    assert monitor.request_fallback() is not None
    monitor.complete_fallback(action_line="[FALLBACK] verified")
    return monitor


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


def test_recovery_waits_for_stable_time_and_new_report_but_allows_soc_movement() -> None:
    monitor = _fallback_monitor(stable_minutes=2)
    started = datetime(2026, 10, 6, 20, tzinfo=UTC)

    first = monitor.record(
        _recovery_measurement(started, started, value=40), failure_threshold=1
    )
    cached = monitor.record(
        _recovery_measurement(started + timedelta(minutes=1), started, value=63),
        failure_threshold=1,
    )

    assert first.recovery_state == "stabilizing"
    assert cached.recovery_state == "stabilizing"
    assert not monitor.recovery_qualified
    assert cached.recovery_elapsed_seconds == 60

    qualified = monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=2), started + timedelta(minutes=1), value=28
        ),
        failure_threshold=1,
    )
    assert qualified.recovery_state == "qualified"
    assert qualified.recovery_elapsed_seconds == 120
    assert monitor.recovery_qualified


def test_recovery_does_not_qualify_from_a_cached_report_alone() -> None:
    monitor = _fallback_monitor(stable_minutes=2)
    started = datetime(2026, 10, 6, 20, tzinfo=UTC)

    for minute in range(13):
        snapshot = monitor.record(
            _recovery_measurement(
                started + timedelta(minutes=minute), started, value=25 + minute
            ),
            failure_threshold=1,
        )

    assert snapshot.recovery_elapsed_seconds == 720
    assert snapshot.recovery_state == "stabilizing"
    assert not monitor.recovery_qualified


def test_recovery_failure_resets_the_continuous_passing_interval() -> None:
    monitor = _fallback_monitor(stable_minutes=2)
    started = datetime(2026, 10, 6, 20, tzinfo=UTC)
    monitor.record(_recovery_measurement(started, started), failure_threshold=1)
    monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=1), started + timedelta(minutes=1)
        ),
        failure_threshold=1,
    )

    failed = monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=2), started + timedelta(minutes=2), ok=False
        ),
        failure_threshold=1,
    )
    assert failed.recovery_state == "waiting"
    assert failed.recovery_elapsed_seconds == 0
    assert not monitor.recovery_qualified

    monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=3), started + timedelta(minutes=3)
        ),
        failure_threshold=1,
    )
    qualified = monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=5), started + timedelta(minutes=4)
        ),
        failure_threshold=1,
    )
    assert qualified.recovery_state == "qualified"
    assert monitor.recovery_qualified


def test_recovery_report_regression_restarts_the_interval() -> None:
    monitor = _fallback_monitor(stable_minutes=2)
    started = datetime(2026, 10, 6, 20, tzinfo=UTC)
    monitor.record(_recovery_measurement(started, started), failure_threshold=1)
    monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=1), started + timedelta(minutes=1)
        ),
        failure_threshold=1,
    )

    regressed = monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=2), started - timedelta(minutes=1)
        ),
        failure_threshold=1,
    )
    assert regressed.recovery_state == "stabilizing"
    assert regressed.recovery_elapsed_seconds == 0
    assert not monitor.recovery_qualified

    monitor.record(
        _recovery_measurement(started + timedelta(minutes=3), started),
        failure_threshold=1,
    )
    qualified = monitor.record(
        _recovery_measurement(
            started + timedelta(minutes=4), started + timedelta(minutes=1)
        ),
        failure_threshold=1,
    )
    assert qualified.recovery_state == "qualified"


def test_recovery_duration_comes_from_settings_and_progress_is_not_persisted(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, soc_recovery_stable_minutes=3)
    monitor = SocMonitor.load(settings)
    failed = _recovery_measurement(datetime(2026, 10, 6, 19, tzinfo=UTC), None, ok=False)
    monitor.record(failed, failure_threshold=1)
    assert monitor.request_fallback() is not None
    monitor.complete_fallback(action_line="[FALLBACK] verified")
    started = datetime(2026, 10, 6, 20, tzinfo=UTC)
    for minute in range(3):
        snapshot = monitor.record(
            _recovery_measurement(started + timedelta(minutes=minute), started),
            failure_threshold=1,
        )
    assert snapshot.recovery_stable_minutes == 3
    assert snapshot.recovery_state == "stabilizing"

    restored = SocMonitor.load(settings)
    assert not restored.recovery_qualified
    first_after_load = restored.record(
        _recovery_measurement(started + timedelta(minutes=4), started + timedelta(minutes=4)),
        failure_threshold=1,
    )
    assert first_after_load.recovery_elapsed_seconds == 0
    assert first_after_load.recovery_state == "stabilizing"


def test_prewrite_recovery_failure_conservatively_downgrades_verified_fallback() -> None:
    monitor = _fallback_monitor()
    started = datetime(2026, 10, 6, 20, tzinfo=UTC)
    monitor.record(_recovery_measurement(started, started), failure_threshold=1)
    monitor.record(
        _recovery_measurement(started + timedelta(minutes=2), started + timedelta(minutes=1)),
        failure_threshold=1,
    )
    assert monitor.fallback_confirmed

    failed = monitor.complete_recovery(RecoveryOutcome.FAILED)

    # A BLOCKED gate can fail before any write. The apply verdict has no
    # write-progress proof, so recovery must conservatively treat hardware as unknown.
    assert failed.state is SocOperatingState.FALLBACK_FAILED
    assert failed.fallback_confirmed is False
    assert failed.effective_program is EffectiveProgram.UNKNOWN
    assert failed.recovery_action is not None
    assert failed.recovery_action.startswith("[FAILED]")
    assert failed.recovery_state is RecoveryState.QUALIFIED


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
