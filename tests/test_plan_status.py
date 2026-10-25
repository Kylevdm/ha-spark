"""Pure display resolution on the household clock; never device execution."""

from dataclasses import replace
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ha_spark.api.plan_status import resolve
from ha_spark.authority import HoldDevice, HoldRecord
from ha_spark.energy.models import ExportIntent, Reservation
from tests.test_api import _plan

TZ = ZoneInfo("Europe/London")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 24, hour, minute, tzinfo=UTC)


def record(cause: str, end: datetime | None = None) -> HoldRecord:
    return HoldRecord(cause=cause, started_at=at(0), trusted_end=end,
                      device=HoldDevice(driver="solis", power_switch="switch.CANARY",
                                        hub="CANARY", slave=1))


def scenario():
    plan = _plan()
    intent = replace(plan.charge_intent, window_start=time(10), window_end=time(17),
                     holds=((at(11), at(12)), (at(12), at(13))),
                     export=ExportIntent(window_start=at(10), window_end=at(14),
                                         event_identity=("CANARY", at(10), at(14)),
                                         planned_export_kw=1, dno_export_limit_kw=2,
                                         selected_slots=(), slot_export_kw=()))
    return replace(plan, charge_intent=intent)


@pytest.mark.parametrize("hour,action,next_at,next_action", [
    (9, "charge", at(10), "export"), (10, "export", at(11), "hold"),
    (11, "hold", at(13), "export"), (12, "hold", at(13), "export"),
    (13, "export", at(14), "charge"), (14, "charge", at(16), "idle"),
    # 10:00 London the next day, after the clocks go back: 10:00 UTC, not 09:00.
    (16, "idle", datetime(2026, 10, 25, 10, tzinfo=UTC), "charge"),
])
def test_precedence_half_open_and_skipped_boundary(hour, action, next_at, next_action):
    status = resolve(scenario(), (), at(hour), TZ)
    assert status.action == action
    assert status.next_at == next_at
    assert status.next_action == next_action
    assert "CANARY" not in status.reason + status.next_reason


@pytest.mark.parametrize("cause,end,next_at", [
    ("ev", None, None), ("dispatch", at(15), at(15)),
    ("dispatch", at(10), None),
])
def test_store_hold_without_plan_window(cause, end, next_at):
    status = resolve(_plan(), (record(cause, end),), at(12), TZ)
    assert status.action == "hold"
    assert status.next_at == next_at
    if next_at:
        assert status.next_action == "idle"


def test_zero_charge_need_and_midnight_wrap():
    assert resolve(_plan(required_kwh=0), (), at(23), TZ).action == "idle"
    assert resolve(_plan(), (), at(23), TZ).action == "charge"
    assert resolve(_plan(), (), at(0), TZ).action == "charge"


@pytest.mark.parametrize("day,start,end,instant,action", [
    ("2026-03-29", time(0, 30), time(2, 30), "00:30", "charge"),
    ("2026-03-29", time(0, 30), time(2, 30), "01:30", "idle"),
    ("2026-03-29", time(1, 30), time(2), "01:00", "idle"),
    ("2026-10-25", time(0, 30), time(2, 30), "01:30", "charge"),
    ("2026-10-25", time(0, 30), time(2, 30), "02:30", "idle"),
])
def test_dst_window(day, start, end, instant, action):
    plan = _plan()
    plan = replace(plan, charge_intent=replace(plan.charge_intent,
                                              window_start=start, window_end=end))
    assert resolve(plan, (), datetime.fromisoformat(f"{day}T{instant}+00:00"), TZ).action == action


def test_funded_reason_is_fixed_template():
    plan = _plan(charge_funded_reservations=(Reservation(
        name="CANARY", target_slot=1, energy_kwh=3.25,
        obligation_kind="axle-export-event", reason="CANARY"),))
    status = resolve(plan, (), at(23), TZ)
    assert status.reason == "Charging 3.2 kWh to cover the flexibility export."


def test_back_to_back_charge_windows_have_no_next_change():
    plan = _plan(required_kwh=9.6)
    plan = replace(plan, charge_intent=replace(plan.charge_intent, window_start=time(0),
                                               window_end=time(0), holds=(), export=None))
    status = resolve(plan, (), at(12), TZ)
    assert status.action == "charge"
    assert status.next_at is None


@pytest.mark.parametrize("field,value", [
    ("required_kwh", 10**400), ("required_kwh", float("inf")), ("required_kwh", 1e300),
    ("target_soc", float("nan")), ("target_soc", 1e300),
])
def test_reason_numbers_are_bounded_and_finite(field, value):
    plan = replace(_plan(), **{field: value})
    plan = replace(plan, charge_intent=replace(plan.charge_intent, window_start=time(0),
                                               window_end=time(23), holds=(), export=None))
    status = resolve(plan, (), at(12), TZ)
    for reason in (status.reason, status.next_reason or ""):
        assert len(reason) <= 120
        assert "inf" not in reason and "nan" not in reason
