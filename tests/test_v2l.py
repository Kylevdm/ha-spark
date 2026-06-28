"""Tests for the V2L observe + tally + notify surface."""

from __future__ import annotations

from ha_spark.config import Settings
from ha_spark.energy.v2l import V2LSession, integrate, payload, savings


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
