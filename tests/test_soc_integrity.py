"""Boundary tests for the checked SoC measurement (#113)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from ha_spark.energy.soc_integrity import SocStatus, check_soc
from ha_spark.ha.models import EntityState

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(minutes=10)


def _state(state: str, *, reported: datetime | str | None = NOW) -> EntityState:
    payload: dict[str, object] = {
        "entity_id": "sensor.soc",
        "state": state,
        "attributes": {},
        "last_updated": "2000-01-01T00:00:00+00:00",
    }
    if reported is not None:
        payload["last_reported"] = reported
    return EntityState.model_validate(payload)


def _check(state: EntityState | None) -> object:
    return check_soc(state, observed_at=NOW, max_age=MAX_AGE)


# --- value integrity -------------------------------------------------------


def test_genuine_zero_passes() -> None:
    m = _check(_state("0"))
    assert m.ok is True
    assert m.status is SocStatus.OK
    assert m.value == 0.0
    assert m.soc_now == 0.0


def test_ordinary_value_passes() -> None:
    m = _check(_state("42.5"))
    assert m.ok is True
    assert m.value == 42.5
    assert m.observed_at == NOW
    assert m.reported_at == NOW
    assert m.age_s == 0.0


def test_read_failure_is_a_failed_measurement() -> None:
    m = _check(None)
    assert m.ok is False
    assert m.status is SocStatus.READ_FAILED
    assert m.value is None
    assert m.soc_now == 0.0
    assert "read" in m.reason.lower()


@pytest.mark.parametrize("raw", ["unavailable", "unknown", "none", "", "  "])
def test_unavailable_states_fail(raw: str) -> None:
    m = _check(_state(raw))
    assert m.ok is False
    assert m.status is SocStatus.UNAVAILABLE
    assert m.raw_state == raw


def test_malformed_value_fails_with_evidence() -> None:
    m = _check(_state("forty"))
    assert m.status is SocStatus.MALFORMED
    assert m.raw_state == "forty"
    assert "forty" in m.reason


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_values_fail(raw: str) -> None:
    assert _check(_state(raw)).status is SocStatus.NOT_FINITE


@pytest.mark.parametrize("raw", ["-0.1", "-1", "100.1", "101", "1000"])
def test_out_of_range_values_fail(raw: str) -> None:
    m = _check(_state(raw))
    assert m.status is SocStatus.OUT_OF_RANGE
    assert m.value == float(raw)


def test_upper_bound_100_passes() -> None:
    assert _check(_state("100")).ok is True


# --- report-time integrity -------------------------------------------------


def test_missing_last_reported_fails() -> None:
    m = _check(_state("50", reported=None))
    assert m.status is SocStatus.REPORT_TIME_UNUSABLE
    assert m.reported_at is None


def test_malformed_last_reported_fails() -> None:
    m = _check(_state("50", reported="not-a-timestamp"))
    assert m.status is SocStatus.REPORT_TIME_UNUSABLE


def test_naive_last_reported_fails() -> None:
    m = _check(_state("50", reported="2026-09-09T12:00:00"))
    assert m.status is SocStatus.REPORT_TIME_UNUSABLE


def test_last_updated_is_not_substituted_for_last_reported() -> None:
    """last_updated is recent, last_reported absent -> still unusable."""
    state = EntityState.model_validate(
        {
            "entity_id": "sensor.soc",
            "state": "50",
            "attributes": {"last_reported": NOW.isoformat()},
            "last_updated": NOW.isoformat(),
        }
    )
    assert _check(state).status is SocStatus.REPORT_TIME_UNUSABLE


def test_future_last_reported_fails() -> None:
    m = _check(_state("50", reported=NOW + timedelta(seconds=1)))
    assert m.status is SocStatus.REPORT_TIME_FUTURE
    assert m.age_s == -1.0


def test_stale_last_reported_fails_with_threshold_evidence() -> None:
    m = _check(_state("50", reported=NOW - timedelta(minutes=10, seconds=1)))
    assert m.status is SocStatus.STALE
    assert m.age_s == pytest.approx(601.0)
    assert m.max_age_s == 600.0
    assert "601" in m.reason


def test_exact_max_age_boundary_passes() -> None:
    m = _check(_state("50", reported=NOW - MAX_AGE))
    assert m.ok is True
    assert m.age_s == 600.0


def test_fresh_but_unchanged_value_passes() -> None:
    """last_reported advances even when the state never changes."""
    m = _check(_state("50", reported=NOW - timedelta(minutes=9)))
    assert m.ok is True


# --- immutability ----------------------------------------------------------


def test_measurement_is_immutable() -> None:
    m = _check(_state("50"))
    with pytest.raises(FrozenInstanceError):
        m.value = 99.0  # type: ignore[misc]
