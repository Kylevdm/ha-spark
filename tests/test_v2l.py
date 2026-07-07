"""Tests for the V2L observe + tally + notify surface."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.v2l import (
    Notice,
    V2LSession,
    apply_sample,
    integrate,
    load_session,
    notifications,
    notify,
    payload,
    run_v2l_tick,
    save_session,
    savings,
)
from ha_spark.ha.rest import HomeAssistantRest

BASE = "http://ha.test/api"


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
    assert by_id["sensor.ha_spark_v2l_power_w"][1]["state_class"] == "measurement"
    assert by_id["sensor.ha_spark_v2l_energy_kwh"][0] == "2.00"
    assert by_id["sensor.ha_spark_v2l_energy_kwh"][1]["state_class"] == "total_increasing"
    assert by_id["sensor.ha_spark_v2l_net_saving_gbp"][1]["device_class"] == "monetary"
    assert by_id["sensor.ha_spark_v2l_net_saving_gbp"][1]["state_class"] == "measurement"
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


def test_apply_sample_aware_timestamps_integrate_normally() -> None:
    t0 = datetime(2026, 6, 28, 19, 0, 0, tzinfo=UTC)
    s = apply_sample(V2LSession(day=""), 2000.0, t0)
    t1 = datetime(2026, 6, 28, 19, 3, 0, tzinfo=UTC)
    s = apply_sample(s, 2000.0, t1)
    assert abs(s.kwh_delivered - 0.1) < 1e-9


def test_apply_sample_mixed_tz_skips_interval_without_raising() -> None:
    # naive stored timestamp, tz-aware now -> degrade to no integration this tick
    s = V2LSession(day="2026-06-28", last_sample_ts="2026-06-28T19:00:00")
    now = datetime(2026, 6, 28, 19, 3, 0, tzinfo=UTC)
    s = apply_sample(s, 2000.0, now)
    assert s.kwh_delivered == 0.0
    assert s.last_sample_ts == now.isoformat()


def test_apply_sample_garbage_timestamp_skips_interval_without_raising() -> None:
    s = V2LSession(day="2026-06-28", last_sample_ts="not-a-date")
    now = datetime(2026, 6, 28, 19, 3, 0)
    s = apply_sample(s, 2000.0, now)
    assert s.kwh_delivered == 0.0
    assert s.last_sample_ts == now.isoformat()


def test_apply_sample_non_string_timestamp_skips_interval_without_raising() -> None:
    # a hand-edited/corrupt session file could carry a non-string JSON value;
    # fromisoformat raises TypeError (not ValueError) for that shape
    s = V2LSession(day="2026-06-28", last_sample_ts=12345)  # type: ignore[arg-type]
    now = datetime(2026, 6, 28, 19, 3, 0)
    s = apply_sample(s, 2000.0, now)
    assert s.kwh_delivered == 0.0
    assert s.last_sample_ts == now.isoformat()


def test_apply_sample_self_heals_after_mixed_tz_tick() -> None:
    s = V2LSession(day="2026-06-28", last_sample_ts="2026-06-28T19:00:00")
    now = datetime(2026, 6, 28, 19, 3, 0, tzinfo=UTC)
    s = apply_sample(s, 2000.0, now)  # mixed-tz tick: skipped
    now2 = datetime(2026, 6, 28, 19, 6, 0, tzinfo=UTC)
    s = apply_sample(s, 2000.0, now2)  # both aware now: integrates
    assert abs(s.kwh_delivered - 0.1) < 1e-9


def _nsettings(**kw: object) -> Settings:
    base: dict[str, object] = dict(
        v2l_notify_service="mobile_app_x",
        v2l_cutoff_time="01:00",
        v2l_budget_kwh=0.0,
        v2l_peak_rate_gbp=0.30,
        v2l_offpeak_rate_gbp=0.07,
        v2l_round_trip_efficiency=0.85,
    )
    base.update(kw)
    return Settings(**base)


def test_no_notifications_without_service() -> None:
    s = _nsettings(v2l_notify_service="")
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True)
    assert notifications(sess, datetime(2026, 6, 28, 1, 5), s) == []


def test_n1_unplug_fires_within_cutoff_window_when_active() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 1, 5), s)}
    assert "notified_unplug" in flags


def test_n1_does_not_fire_in_afternoon() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 14, 0), s)}
    assert "notified_unplug" not in flags


def test_n1_fire_once() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True, notified_unplug=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 1, 5), s)}
    assert "notified_unplug" not in flags


def test_n2_plug_in_fires_when_idle_after_delivering() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=3.0, active=False)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 22, 0), s)}
    assert "notified_plug_in" in flags


def test_n2_no_fire_while_active_or_zero() -> None:
    s = _nsettings()
    active = V2LSession(day="2026-06-28", kwh_delivered=3.0, active=True)
    empty = V2LSession(day="2026-06-28", kwh_delivered=0.0, active=False)
    assert "notified_plug_in" not in {
        n.flag for n in notifications(active, datetime(2026, 6, 28, 22, 0), s)
    }
    assert "notified_plug_in" not in {
        n.flag for n in notifications(empty, datetime(2026, 6, 28, 22, 0), s)
    }


def test_n3_predictive_fires_near_budget() -> None:
    s = _nsettings(v2l_budget_kwh=5.0)
    # 4.9 kWh delivered, 2000 W -> 0.1 kWh to go = 0.05 h = 3 min <= lead(20)
    sess = V2LSession(day="2026-06-28", kwh_delivered=4.9, last_power_w=2000.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 22, 0), s)}
    assert "notified_budget" in flags


def test_n3_disabled_without_budget() -> None:
    s = _nsettings(v2l_budget_kwh=0.0)
    sess = V2LSession(day="2026-06-28", kwh_delivered=4.9, last_power_w=2000.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 22, 0), s)}
    assert "notified_budget" not in flags


def test_notice_carries_flag_title_message() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True)
    notices = notifications(sess, datetime(2026, 6, 28, 1, 5), s)
    assert all(isinstance(n, Notice) and n.flag and n.title and n.message for n in notices)


def test_session_round_trip(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    assert load_session(s).day == ""  # no file yet
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.5, notified_unplug=True)
    save_session(s, sess)
    back = load_session(s)
    assert back.day == "2026-06-28"
    assert back.kwh_delivered == 2.5
    assert back.notified_unplug is True


def test_save_session_leaves_no_stray_tmp_file(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    save_session(s, V2LSession(day="2026-06-28", kwh_delivered=1.0))
    files = {p.name for p in tmp_path.iterdir()}
    assert "ha_spark_v2l_session.json" in files
    assert "ha_spark_v2l_session.json.tmp" not in files


def test_save_session_failed_replace_preserves_prior_file(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    good = V2LSession(day="2026-06-28", kwh_delivered=2.5, notified_unplug=True)
    save_session(s, good)

    with patch("ha_spark.energy.v2l.os.replace", side_effect=OSError("disk full")):
        save_session(s, V2LSession(day="2026-06-29", kwh_delivered=9.0))  # does not raise

    back = load_session(s)
    assert back.day == "2026-06-28"
    assert back.kwh_delivered == 2.5


def test_load_session_tolerates_garbage(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    (tmp_path / "ha_spark_v2l_session.json").write_text("{not json", encoding="utf-8")
    assert load_session(s).day == ""


@respx.mock
async def test_notify_calls_notify_service() -> None:
    route = respx.post(f"{BASE}/services/notify/mobile_app_x").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with HomeAssistantRest(BASE, "token") as rest:
        await notify(rest, "mobile_app_x", "Title", "Body")
    assert route.called
    sent = route.calls.last.request
    assert b"Body" in sent.content


@respx.mock
async def test_run_v2l_tick_integrates_publishes_and_notifies(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
        v2l_notify_service="mobile_app_x",
        v2l_cutoff_time="01:00",
    )
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.car_v2l_power", "state": "2000", "attributes": {}}
        )
    )
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))

    # Seed a prior sample 4 min earlier so this tick integrates ~0.13 kWh, and an
    # active session past cutoff so N1 fires.
    prior = V2LSession(
        day="2026-06-28",
        kwh_delivered=0.0,
        active=True,
        last_sample_ts="2026-06-28T01:01:00",
    )
    save_session(s, prior)

    await run_v2l_tick(s, datetime(2026, 6, 28, 1, 5, 0))

    back = load_session(s)
    assert back.kwh_delivered > 0.0  # integrated the interval
    assert back.notified_unplug is True  # N1 fired and was flagged
    paths = [c.request.url.path for c in posts.calls]
    assert any(p.endswith("/services/notify/mobile_app_x") for p in paths)
    assert any("sensor.ha_spark_v2l_energy_kwh" in p for p in paths)


@respx.mock
async def test_run_v2l_tick_skips_on_unreadable_sensor(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
    )
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(return_value=httpx.Response(500))
    # must not raise
    await run_v2l_tick(s, datetime(2026, 6, 28, 19, 0, 0))


@respx.mock
async def test_cmd_v2l_prints_tally(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from ha_spark.cli import _cmd_v2l

    s = Settings(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
        v2l_peak_rate_gbp=0.30,
        v2l_offpeak_rate_gbp=0.07,
        v2l_round_trip_efficiency=0.85,
    )
    save_session(s, V2LSession(day="2026-06-28", kwh_delivered=2.0, peak_power_w=1500.0))
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.car_v2l_power", "state": "1400", "attributes": {}}
        )
    )
    rc = await _cmd_v2l(s)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1400 W" in out
    assert "2.00 kWh" in out


async def test_cmd_v2l_unconfigured_returns_2(tmp_path: Path) -> None:
    from ha_spark.cli import _cmd_v2l

    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    assert await _cmd_v2l(s) == 2
