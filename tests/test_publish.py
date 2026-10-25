"""Tests for the legacy plan payload and public SoC monitor results."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from ha_spark.config import Settings
from ha_spark.energy.models import ChargeIntent, ChargePlan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.soc_monitor import SocMonitor, SocOperatingState


def _soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )


def _plan(**overrides: object) -> ChargePlan:
    defaults: dict[str, object] = dict(
        soc=_soc(40.0),
        capacity_kwh=26.88,
        solar_kwh=5.0,
        effective_solar_kwh=5.0,
        load_kwh=10.0,
        cheap_covered_kwh=0.0,
        usable_now_kwh=5.0,
        deficit_kwh=8.0,
        buffer_pct=20.0,
        required_kwh=9.6,
        target_soc=90.0,
        window_hours=6.0,
        ev_charging=False,
        ha_template_needed=None,
        charge_intent=ChargeIntent(
            target_soc_pct=90.0, soc=_soc(40.0), window_start=time(23, 30), window_end=time(5, 30)
        ),
    )
    defaults.update(overrides)
    return ChargePlan(**defaults)  # type: ignore[arg-type]


def test_plan_to_payload_maps_core_sensors() -> None:
    by_id = {eid: (state, attrs) for eid, state, attrs in plan_to_payload(_plan(), Settings())}
    assert by_id["sensor.ha_spark_target_soc"][0] == "90"
    assert by_id["sensor.ha_spark_target_soc"][1]["device_class"] == "battery"
    # optional cost sensors are omitted when the plan didn't cost itself
    assert "sensor.ha_spark_planned_cost" not in by_id


def test_plan_payload_skips_none_cost_fields() -> None:
    ids = {eid for eid, _, _ in plan_to_payload(
        _plan(planned_cost=None, baseline_cost=None), Settings()
    )}
    assert "sensor.ha_spark_planned_cost" not in ids
    assert "sensor.ha_spark_baseline_cost" not in ids


def test_untrusted_soc_payload_is_unavailable() -> None:
    bad = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=datetime.now(UTC),
        value=70.0,
        raw_state="70",
        age_s=746.0,
        max_age_s=600.0,
    )
    payload = plan_to_payload(_plan(soc=bad, planned_cost=8.25, baseline_cost=10.5), Settings())
    by_id = {eid: state for eid, state, _ in payload}
    for entity_id in (
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_planned_cost",
        "sensor.ha_spark_baseline_cost",
    ):
        assert by_id[entity_id] == "unavailable"
    assert by_id["sensor.ha_spark_forecast_load_kwh"] == "10.00"
    assert by_id["sensor.ha_spark_solar_forecast_kwh"] == "5.00"


def test_plan_status_publishes_soc_status_and_reason() -> None:
    bad = SocMeasurement(
        status=SocStatus.READ_FAILED,
        observed_at=datetime.now(UTC),
        max_age_s=600.0,
    )
    entities = plan_to_payload(
        _plan(soc=bad, planned_cost=8.25, baseline_cost=10.5), Settings()
    )
    by_id = {eid: (state, attrs) for eid, state, attrs in entities}
    attrs = by_id["sensor.ha_spark_plan_status"][1]

    assert attrs["soc_status"] == "read_failed"
    assert attrs["soc_reason"] == bad.reason
    assert "soc_valid" not in attrs
    for entity_id in (
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_planned_cost",
        "sensor.ha_spark_baseline_cost",
    ):
        assert by_id[entity_id][0] == "unavailable"
    assert by_id["sensor.ha_spark_forecast_load_kwh"][0] == "10.00"
    assert by_id["sensor.ha_spark_solar_forecast_kwh"][0] == "5.00"


def test_plan_status_publishes_ok_status_for_a_checked_soc() -> None:
    entities = plan_to_payload(_plan(), Settings())
    attrs = {eid: a for eid, _, a in entities}["sensor.ha_spark_plan_status"]

    assert attrs["soc_status"] == "ok"


def test_plan_status_publishes_event_soc_needed_only_when_present() -> None:
    needed_at = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
    attrs = {
        eid: attributes
        for eid, _, attributes in plan_to_payload(
            _plan(export_soc_needed_pct=61.2, export_soc_needed_at=needed_at), Settings()
        )
    }["sensor.ha_spark_plan_status"]
    without_need = {
        eid: attributes for eid, _, attributes in plan_to_payload(_plan(), Settings())
    }["sensor.ha_spark_plan_status"]

    assert attrs["export_soc_needed_pct"] == 61
    assert attrs["export_soc_needed_at"] == needed_at.isoformat()
    assert "export_soc_needed_pct" not in without_need
    assert "export_soc_needed_at" not in without_need


def test_monitor_exposes_pending_state_and_evidence() -> None:
    now = datetime.now(UTC)
    stale = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=now,
        value=30.0,
        raw_state="30",
        reported_at=now - timedelta(hours=1),
        age_s=3600.0,
        max_age_s=600.0,
    )
    snapshot = SocMonitor().record(stale, failure_threshold=3)
    assert snapshot.state is SocOperatingState.PENDING_FAILURE
    assert snapshot.consecutive_failures == 1
    assert snapshot.failure_threshold == 3
    assert snapshot.measurement.status is SocStatus.STALE
    assert "over the 600s maximum" in snapshot.measurement.reason
    assert snapshot.measurement.value == 30.0
    assert snapshot.measurement.age_s == 3600.0
    assert snapshot.fallback_confirmed is False
    assert snapshot.fallback_action is None


def test_monitor_reports_fallback_confirmation_not_request() -> None:
    failed = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=datetime.now(UTC),
        value=30.0,
        raw_state="30",
        age_s=3600.0,
        max_age_s=600.0,
    )
    monitor = SocMonitor()
    monitor.record(failed, failure_threshold=1)
    requested = monitor.request_fallback()
    assert requested is not None
    assert not requested.fallback_confirmed
    snapshot = monitor.complete_fallback(action_line="[FALLBACK] Solis fallback verified")
    assert snapshot.state is SocOperatingState.FALLBACK_VERIFIED
    assert snapshot.fallback_confirmed is True
    assert snapshot.fallback_action == "[FALLBACK] Solis fallback verified"


def test_monitor_exposes_alphaess_recovery_action() -> None:
    now = datetime.now(UTC)
    failed = SocMeasurement(
        status=SocStatus.STALE, observed_at=now, value=30.0,
        raw_state="30", age_s=3600.0, max_age_s=600.0,
    )
    monitor = SocMonitor()
    monitor.record(failed, failure_threshold=1)
    passing = SocMeasurement(
        status=SocStatus.OK, observed_at=now, value=31.0,
        raw_state="31", reported_at=now, age_s=0.0, max_age_s=600.0,
    )
    monitor.record(passing, failure_threshold=1, recover_without_fallback=True)
    snapshot = monitor.complete_recovery(action_line="[RECOVERED] AlphaESS plan applied")
    assert snapshot.recovery_action == "[RECOVERED] AlphaESS plan applied"
    assert snapshot.fallback_confirmed is False
