"""Durable protection rules use report times, not repeated cached observations."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from custom_components.ha_spark.authority_store import HoldDevice, HoldRecord
from custom_components.ha_spark.native_hold import (
    SOURCES,
    DispatchWindow,
    EvidenceOptions,
    EvidenceSource,
    advance_ev,
    dispatch_kept,
    fresh,
    hold_device,
    hold_identity_valid,
)
from custom_components.ha_spark.topology import Inverter

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
DEVICE = HoldDevice(driver="solis_native", power_switch="inv", hub="gateway.invalid:502", slave=1)


def record(**changes: object) -> HoldRecord:
    return HoldRecord.model_validate({"cause": "ev", "started_at": NOW,
                                      "device": DEVICE, "confirmed": True, **changes})


@pytest.mark.parametrize("offset", [-301, -300, 0, 1])
def test_evidence_uses_nonfuture_report_time(offset: int) -> None:
    assert fresh(NOW + timedelta(seconds=offset), NOW, 300) is (-300 <= offset <= 0)


def test_gateway_identity_is_never_truncated() -> None:
    inverter = Inverter(id="inv", bank_id="bank", driver_kind="solis", transport="native",
                        gateway_host="a" * 124)
    assert hold_identity_valid(inverter)
    assert hold_device(inverter).hub == "a" * 124 + ":502"
    assert not hold_identity_valid(inverter.model_copy(update={"gateway_host": "a" * 125}))


def test_native_evidence_golden() -> None:
    raw = json.loads((Path(__file__).parents[1] /
                      "tests/fixtures/contract/evidence_native.json").read_text())
    assert EvidenceOptions.model_validate(raw).snapshot() == raw


def test_evidence_required_sources_and_native_default() -> None:
    assert not EvidenceOptions().configured
    options = EvidenceOptions.model_validate({"ev_status": {
        "entity_id": "sensor.ev_status", "max_age_s": 300}})
    assert options.configured
    assert options.battery_direction.source == "native"
    for source in ({"source": "entity"}, {"entity_id": "not an entity"},
                   {"entity_id": "sensor.ev", "max_age_s": float("nan")}):
        with pytest.raises(ValidationError):
            EvidenceOptions.model_validate({"ev_status": source})


def test_entity_evidence_requires_explicit_max_age_but_native_keeps_default() -> None:
    with pytest.raises(ValidationError):
        EvidenceSource(entity_id="sensor.ev")
    assert EvidenceSource(source="native").max_age_s == 300


@pytest.mark.parametrize("key", SOURCES)
def test_any_entity_source_without_max_age_invalidates_evidence_options(key: str) -> None:
    with pytest.raises(ValidationError):
        EvidenceOptions.model_validate({key: {"entity_id": "sensor.evidence"}})


def test_ev_needs_five_advancing_reports_and_stale_rearms_pending_release() -> None:
    held = record()
    for n in range(4):
        held = advance_ev(held, False, NOW + timedelta(seconds=n))
        assert not held.pending_release and held.clear_count == n + 1
        assert advance_ev(held, False, NOW + timedelta(seconds=n)) == held
    held = advance_ev(held, False, NOW + timedelta(seconds=4))
    assert held.pending_release
    held = advance_ev(held, None, None)
    assert not held.pending_release and held.clear_count == 0


def test_dispatch_ladder_requires_independent_power_for_contradiction() -> None:
    window = DispatchWindow(start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=10))
    assert dispatch_kept(window, NOW)
    assert not dispatch_kept(window, NOW, plug="ev disconnected")
    assert dispatch_kept(window, NOW, live=True, plug="ev disconnected")
    assert dispatch_kept(window, NOW, adjusted=True, plug="ev disconnected")
    assert dispatch_kept(window, NOW, status="charging", plug="ev disconnected")
    assert dispatch_kept(window, NOW, car_kw=1.4, plug="ev disconnected")
    assert dispatch_kept(window, NOW, grid_kw=4, house_kw=1, plug="ev disconnected")
    assert dispatch_kept(window, NOW, car_kw=0, grid_kw=0)  # no invented house forecast
    assert not dispatch_kept(window, NOW, car_kw=0, grid_kw=0, house_kw=1)
    assert dispatch_kept(window, NOW - timedelta(seconds=1), car_kw=0, grid_kw=0, house_kw=1)
    assert dispatch_kept(window, NOW, car_kw=0, grid_kw=0, house_kw=1, plug="ev connected")


def test_ev_backwards_or_charging_resets_clearance() -> None:
    held = advance_ev(record(), False, NOW)
    assert advance_ev(held, False, NOW - timedelta(seconds=1)).clear_count == 0
    assert advance_ev(held, True, NOW).clear_count == 0
