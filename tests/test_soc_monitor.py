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
    assert data == {"consecutive_failures": 2}
    # A fresh monitor (e.g. after restart) restores the count.
    assert SocMonitor.load(s).record(_measurement(ok=False), failure_threshold=3) \
        .consecutive_failures == 3


def test_passing_reset_is_persisted(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    m = SocMonitor.load(s)
    m.record(_measurement(ok=False), failure_threshold=3)
    m.record(_measurement(ok=True), failure_threshold=3)
    assert json.loads((tmp_path / MONITOR_FILE).read_text(encoding="utf-8")) == {
        "consecutive_failures": 0
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
