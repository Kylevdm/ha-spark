"""Tests for the V2L observe + tally + notify surface."""

from __future__ import annotations

from datetime import datetime

from ha_spark.config import Settings
from ha_spark.energy.v2l import (
    V2LSession,
    apply_sample,
    integrate,
    payload,
    savings,
)


def test_integrate_rectangle() -> None:
    # 2000 W for 1800 s (30 min) = 1.0 kWh
    assert integrate(0.0, 2000.0, 1800.0) == 1.0
    # accumulates onto the prior total
    assert integrate(1.0, 2000.0, 1800.0) == 2.0


def test_savings_discounts_refill_by_efficiency() -> None:
    # 10 kWh, peak 0.30, offpeak 0.07, eff 0.85
    avoided, refill, net = savings(10.0, 0.30, 0.07, 0.85)
    assert avoided == 3.0
    assert abs(refill - (10.0 / 0.85) * 0.07) < 1e-9
    assert abs(net - (3.0 - (10.0 / 0.85) * 0.07)) < 1e-9


def test_savings_net_can_go_negative() -> None:
    # peak below offpeak/eff -> using V2L costs more than it saves
    _, _, net = savings(10.0, 0.05, 0.10, 0.85)
    assert net < 0


def test_savings_zero_efficiency_is_safe() -> None:
    avoided, refill, net = savings(10.0, 0.30, 0.07, 0.0)
    assert refill == 0.0
    assert net == avoided


def test_payload_maps_three_sensors() -> None:
    s = Settings(v2l_peak_rate_gbp=0.30, v2l_offpeak_rate_gbp=0.07, v2l_round_trip_efficiency=0.85)
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, last_power_w=1400.0, peak_power_w=1500.0)
    by_id = {eid: (state, attrs) for eid, state, attrs in payload(sess, s)}
    assert by_id["sensor.ha_spark_v2l_power_w"][0] == "1400"
    assert by_id["sensor.ha_spark_v2l_power_w"][1]["device_class"] == "power"
    assert by_id["sensor.ha_spark_v2l_energy_kwh"][0] == "2.00"
    assert by_id["sensor.ha_spark_v2l_net_saving_gbp"][1]["device_class"] == "monetary"
    assert "avoided_gbp" in by_id["sensor.ha_spark_v2l_net_saving_gbp"][1]


def test_apply_sample_first_sample_sets_day_no_integration() -> None:
    now = datetime(2026, 6, 28, 19, 0, 0)
    s = apply_sample(V2LSession(day=""), 1400.0, now)
    assert s.day == "2026-06-28"
    assert s.kwh_delivered == 0.0  # no prior timestamp -> no interval
    assert s.last_power_w == 1400.0
    assert s.active is True
    assert s.last_sample_ts == now.isoformat()


def test_apply_sample_integrates_between_samples() -> None:
    t0 = datetime(2026, 6, 28, 19, 0, 0)
    s = apply_sample(V2LSession(day=""), 2000.0, t0)
    t1 = datetime(2026, 6, 28, 19, 3, 0)  # +180 s (within the dt clamp)
    s = apply_sample(s, 2000.0, t1)
    # 2000 W * 180 s = 0.1 kWh
    assert abs(s.kwh_delivered - 0.1) < 1e-9
    assert s.peak_power_w == 2000.0


def test_apply_sample_clamps_long_gap() -> None:
    # a long gap (1 h) between samples is clamped to _DT_CLAMP_S (300 s)
    t0 = datetime(2026, 6, 28, 19, 0, 0)
    s = apply_sample(V2LSession(day=""), 1000.0, t0)
    s = apply_sample(s, 1000.0, datetime(2026, 6, 28, 20, 0, 0))  # +3600 s
    assert abs(s.kwh_delivered - (1000.0 / 1000.0) * (300.0 / 3600.0)) < 1e-9


def test_apply_sample_marks_idle() -> None:
    t0 = datetime(2026, 6, 28, 19, 0, 0)
    s = apply_sample(V2LSession(day=""), 2000.0, t0)
    s = apply_sample(s, 0.0, datetime(2026, 6, 28, 19, 1, 0))
    assert s.active is False
    assert s.peak_power_w == 2000.0  # peak retained


def test_apply_sample_resets_on_new_day_when_idle() -> None:
    s = V2LSession(day="2026-06-27", kwh_delivered=5.0, notified_unplug=True)
    s = apply_sample(s, 0.0, datetime(2026, 6, 28, 14, 0, 0))
    assert s.day == "2026-06-28"
    assert s.kwh_delivered == 0.0
    assert s.notified_unplug is False


def test_apply_sample_does_not_reset_mid_session_across_midnight() -> None:
    # active past midnight: keep accumulating under the original day
    s = V2LSession(day="2026-06-27", kwh_delivered=5.0, last_sample_ts="2026-06-28T00:59:00")
    s = apply_sample(s, 2000.0, datetime(2026, 6, 28, 1, 0, 0))
    assert s.day == "2026-06-27"
    assert s.kwh_delivered > 5.0
