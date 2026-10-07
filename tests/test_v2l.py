"""Tests for the V2L observe + tally + notify surface."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan, FlexibilityEvent, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.tariff import fixed_schedule
from ha_spark.energy.v2l import (
    Notice,
    TopUp,
    TopUpRecord,
    V2LSession,
    apply_sample,
    delivered_fraction,
    event_id,
    integrate,
    load_session,
    load_topup,
    notification_service,
    notifications,
    payload,
    run_v2l_tick,
    run_v2l_topup,
    save_session,
    save_topup,
    savings,
    topup_notice,
    topup_request,
    warn_deprecated_notify_target,
)
from ha_spark.ha.rest import HomeAssistantRest, notify

BASE = "http://ha.test/api"


def test_notification_target_prefers_shared_service() -> None:
    settings = Settings(notify_service="mobile_app_phone", v2l_notify_service="mobile_app_car")

    assert notification_service(settings) == "mobile_app_phone"


def test_notification_target_falls_back_to_deprecated_service() -> None:
    assert notification_service(Settings(v2l_notify_service="mobile_app_car")) == "mobile_app_car"


def test_notification_target_is_empty_when_both_options_are_blank() -> None:
    assert notification_service(Settings()) == ""


def test_deprecated_notify_target_warns(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(v2l_notify_service="mobile_app_car")

    with caplog.at_level("WARNING", logger="ha_spark.energy.v2l"):
        warn_deprecated_notify_target(settings)

    assert "v2l_notify_service is deprecated; use notify_service instead" in caplog.text


def test_deprecated_notify_target_is_silent_when_shared_target_is_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(notify_service="mobile_app_phone", v2l_notify_service="mobile_app_car")

    with caplog.at_level("WARNING", logger="ha_spark.energy.v2l"):
        warn_deprecated_notify_target(settings)

    assert caplog.text == ""


def test_deprecated_notify_target_is_silent_when_both_options_are_blank(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="ha_spark.energy.v2l"):
        warn_deprecated_notify_target(Settings())

    assert caplog.text == ""


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
async def test_run_v2l_tick_uses_shared_notify_service(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
        notify_service="mobile_app_phone",
        v2l_cutoff_time="01:00",
    )
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.car_v2l_power", "state": "2000", "attributes": {}}
        )
    )
    notify_route = respx.post(f"{BASE}/services/notify/mobile_app_phone").mock(
        return_value=httpx.Response(200, json=[])
    )
    save_session(
        s,
        V2LSession(
            day="2026-06-28", active=True, last_sample_ts="2026-06-28T01:01:00"
        ),
    )

    await run_v2l_tick(s, datetime(2026, 6, 28, 1, 5, 0))

    assert notify_route.called


@respx.mock
async def test_run_v2l_tick_shared_service_wins_over_deprecated(
    tmp_path: Path,
) -> None:
    s = Settings(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
        notify_service="mobile_app_phone",
        v2l_notify_service="mobile_app_car",
        v2l_cutoff_time="01:00",
    )
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.car_v2l_power", "state": "2000", "attributes": {}}
        )
    )
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    save_session(
        s,
        V2LSession(day="2026-06-28", active=True, last_sample_ts="2026-06-28T01:01:00"),
    )

    await run_v2l_tick(s, datetime(2026, 6, 28, 1, 5, 0))

    paths = [call.request.url.path for call in posts.calls]
    assert any(path.endswith("/services/notify/mobile_app_phone") for path in paths)
    assert not any(path.endswith("/services/notify/mobile_app_car") for path in paths)


@respx.mock
async def test_run_v2l_tick_makes_no_notify_call_when_both_targets_are_blank(
    tmp_path: Path,
) -> None:
    s = Settings(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
        v2l_cutoff_time="01:00",
    )
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.car_v2l_power", "state": "2000", "attributes": {}}
        )
    )
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    save_session(
        s,
        V2LSession(day="2026-06-28", active=True, last_sample_ts="2026-06-28T01:01:00"),
    )

    await run_v2l_tick(s, datetime(2026, 6, 28, 1, 5, 0))

    assert not any("/services/notify/" in call.request.url.path for call in posts.calls)


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

    s = Settings(db_path=str(tmp_path / "ha_spark.db"), v2l_power_entity="")
    assert await _cmd_v2l(s) == 2


# --- V2L top-up request for an underfunded Axle event (#208) ---


def _topup_settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        timezone="UTC",
        notify_service="mobile_app_phone",
        v2l_power_entity="sensor.car_v2l_power",
        charge_efficiency=1.0,
        v2l_round_trip_efficiency=0.85,
        v2l_offpeak_rate_gbp=0.07,
    )
    base.update(kw)
    return Settings(**base)


_EVENT = FlexibilityEvent(
    start=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
    end=datetime(2026, 6, 9, 18, 0, tzinfo=UTC),
    direction="export",
    updated_at=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
    rate_gbp_kwh=0.30,
)


def _ok_soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK, observed_at=now, value=value, raw_state=str(value),
        reported_at=now, age_s=0.0, max_age_s=600.0,
    )


def _overnight_plan() -> ChargePlan:
    """The evening-before plan: 40 kWh from 20%, the 62.5 A window adds 19.125 kWh.

    The 17:30 slot is funded and the 17:00 slot skipped for funding. The plan
    charges to 67.75%, so the path is at 39% at 17:00 against 41.75% needed:
    1.1 kWh short.
    """
    config = PlannerConfig(
        capacity_kwh=40.0, voltage_v=51.0, min_soc=20.0, target_cap=90.0,
        max_current_a=62.5, solar_haircut_k=1.0,
        window_start=time(23, 30), window_end=time(5, 30),
        buffer_pct=0.0, charge_efficiency=1.0,
        battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36,
    )
    inputs = PlannerInputs(
        soc=_ok_soc(20.0), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        load_slots=(0.5,) * 48, solar_slots=(0.0,) * 48,
        horizon_start=datetime(2026, 6, 8, 23, 30, tzinfo=UTC), flexibility_event=_EVENT,
    )
    return compute_plan(inputs, config, fixed_schedule(inputs, config))


def _event_day(plan: ChargePlan, soc: float) -> ChargePlan:
    """The event-day plan: same funding verdict, live SoC, a path for tomorrow."""
    return replace(plan, soc=_ok_soc(soc), soc_path=())


def _record(plan: ChargePlan, capped: bool = False) -> TopUpRecord:
    return TopUpRecord(
        event_id=event_id(_EVENT),
        path=[(ts.isoformat(), pct) for ts, pct in plan.soc_path],
        capped=capped,
    )


def test_delivered_fraction_counts_rectifier_and_discharge_leg() -> None:
    s = Settings(v2l_rectifier_efficiency=0.94, charge_efficiency=0.81)

    assert delivered_fraction(s) == pytest.approx(0.94 * 0.9)


def test_savings_counts_only_what_reaches_the_house() -> None:
    avoided, refill, _ = savings(10.0, 0.30, 0.07, 0.85, delivered=0.8)

    assert avoided == pytest.approx(10.0 * 0.8 * 0.30)
    assert refill == pytest.approx((10.0 / 0.85) * 0.07)


def test_topup_when_the_overnight_charge_was_maxed(tmp_path: Path) -> None:
    plan = _overnight_plan()
    assert plan.overnight_charge_capped

    topup = topup_request(
        plan, _EVENT, TopUpRecord(event_id(_EVENT)), datetime(2026, 6, 8, 21, 0, tzinfo=UTC),
        _topup_settings(tmp_path),
    )

    assert topup is not None
    assert topup.shortfall_kwh == pytest.approx(1.1)
    assert topup.car_kwh == pytest.approx(1.1 / 0.94)
    assert topup.needed_at == _EVENT.start
    # 1.1 kWh into the battery at 2.15 kW DC.
    assert topup.start_by == _EVENT.start - timedelta(hours=1.1 / 2.15)
    assert topup.reason == "overnight charge maxed"
    assert not topup.budget_capped


def test_no_topup_when_the_charge_window_was_not_the_limit(tmp_path: Path) -> None:
    plan = replace(_overnight_plan(), overnight_charge_capped=False)

    assert topup_request(
        plan, _EVENT, TopUpRecord(event_id(_EVENT)), datetime(2026, 6, 8, 21, 0, tzinfo=UTC),
        _topup_settings(tmp_path),
    ) is None


def test_no_topup_when_v2l_costs_more_than_the_event_pays(tmp_path: Path) -> None:
    # Break-even: 0.07 / (0.85 x 0.94) = GBP 0.0876.
    cheap_event = replace(_EVENT, rate_gbp_kwh=0.08)

    assert topup_request(
        _overnight_plan(), cheap_event, TopUpRecord(event_id(_EVENT)),
        datetime(2026, 6, 8, 21, 0, tzinfo=UTC), _topup_settings(tmp_path),
    ) is None


def test_no_topup_on_an_untrusted_soc(tmp_path: Path) -> None:
    plan = replace(
        _overnight_plan(),
        soc=SocMeasurement(status=SocStatus.UNAVAILABLE, observed_at=datetime.now(UTC)),
    )

    assert topup_request(
        plan, _EVENT, TopUpRecord(event_id(_EVENT)), datetime(2026, 6, 8, 21, 0, tzinfo=UTC),
        _topup_settings(tmp_path),
    ) is None


def test_topup_when_event_day_soc_is_below_the_overnight_path(tmp_path: Path) -> None:
    overnight = _overnight_plan()
    # Expected at 12:00: 67.75 - 13 slots x 1.25 = 51.5%. Live 45% is 6.5 below.
    topup = topup_request(
        _event_day(overnight, 45.0), _EVENT, _record(overnight),
        datetime(2026, 6, 9, 12, 0, tzinfo=UTC), _topup_settings(tmp_path),
    )

    assert topup is not None
    # 41.75% needed against 39% - 6.5 = 32.5% expected at 17:00.
    assert topup.shortfall_kwh == pytest.approx(9.25 / 100 * 40.0)
    assert "below plan" in topup.reason


def test_no_topup_when_event_day_soc_is_within_tolerance(tmp_path: Path) -> None:
    overnight = _overnight_plan()

    assert topup_request(
        _event_day(overnight, 48.0), _EVENT, _record(overnight),
        datetime(2026, 6, 9, 12, 0, tzinfo=UTC), _topup_settings(tmp_path),
    ) is None


def test_event_day_topup_when_the_overnight_record_was_capped(tmp_path: Path) -> None:
    overnight = _overnight_plan()

    topup = topup_request(
        _event_day(overnight, 51.5), _EVENT, _record(overnight, capped=True),
        datetime(2026, 6, 9, 12, 0, tzinfo=UTC), _topup_settings(tmp_path),
    )

    assert topup is not None
    assert topup.shortfall_kwh == pytest.approx(1.1)
    assert topup.reason == "overnight charge maxed"


def test_no_event_day_topup_without_an_overnight_record(tmp_path: Path) -> None:
    assert topup_request(
        _event_day(_overnight_plan(), 30.0), _EVENT, TopUpRecord(event_id(_EVENT)),
        datetime(2026, 6, 9, 12, 0, tzinfo=UTC), _topup_settings(tmp_path),
    ) is None


def test_topup_is_held_to_the_v2l_budget(tmp_path: Path) -> None:
    topup = topup_request(
        _overnight_plan(), _EVENT, TopUpRecord(event_id(_EVENT)),
        datetime(2026, 6, 8, 21, 0, tzinfo=UTC), _topup_settings(tmp_path, v2l_budget_kwh=0.5),
    )

    assert topup is not None
    assert topup.car_kwh == 0.5
    assert topup.budget_capped
    notice = topup_notice(topup, _EVENT, datetime(2026, 6, 8, 21, 0, tzinfo=UTC))
    assert "whole V2L budget" in notice.message


def test_topup_notice_names_shortfall_and_start_time() -> None:
    topup = TopUp(
        shortfall_kwh=1.1, car_kwh=1.17, needed_at=_EVENT.start,
        start_by=datetime(2026, 6, 9, 16, 29, tzinfo=UTC), budget_capped=False,
        reason="overnight charge maxed",
    )

    notice = topup_notice(topup, _EVENT, datetime(2026, 6, 8, 21, 0, tzinfo=UTC))

    assert notice.message.startswith(
        "Axle export 17:00-18:00 is short by 1.1 kWh (overnight charge maxed). "
        "Start V2L by 16:29 to draw about 1.2 kWh from the car before 17:00."
    )
    late = topup_notice(topup, _EVENT, datetime(2026, 6, 9, 16, 45, tzinfo=UTC))
    assert "Start V2L now" in late.message


def test_topup_record_survives_a_restart_and_resets_for_another_event(tmp_path: Path) -> None:
    s = _topup_settings(tmp_path)
    record = _record(_overnight_plan(), capped=True)
    record.last_sent_kwh = 1.1
    save_topup(s, record)

    assert load_topup(s, event_id(_EVENT)) == record
    assert load_topup(s, "export|other") == TopUpRecord("export|other")


def test_corrupt_topup_record_starts_fresh(tmp_path: Path) -> None:
    s = _topup_settings(tmp_path)
    (tmp_path / "ha_spark_v2l_topup.json").write_text('{"event_id": 3}', encoding="utf-8")

    assert load_topup(s, event_id(_EVENT)) == TopUpRecord(event_id(_EVENT))


@respx.mock
async def test_run_v2l_topup_records_the_overnight_path_and_sends_once(tmp_path: Path) -> None:
    s = _topup_settings(tmp_path)
    route = respx.post(f"{BASE}/services/notify/mobile_app_phone").mock(
        return_value=httpx.Response(200, json=[])
    )
    plan = _overnight_plan()
    in_window = datetime(2026, 6, 9, 2, 0, tzinfo=UTC)

    async with HomeAssistantRest(BASE, "token") as rest:
        await run_v2l_topup(s, rest, plan, _EVENT, in_window)
        await run_v2l_topup(s, rest, plan, _EVENT, in_window + timedelta(minutes=30))

    assert route.call_count == 1
    record = load_topup(s, event_id(_EVENT))
    assert record.capped
    assert record.path[0] == (datetime(2026, 6, 9, 5, 30, tzinfo=UTC).isoformat(), 67.75)
    assert record.last_sent_kwh == pytest.approx(1.1)


@respx.mock
async def test_run_v2l_topup_sends_again_once_the_shortfall_grows(tmp_path: Path) -> None:
    s = _topup_settings(tmp_path)
    route = respx.post(f"{BASE}/services/notify/mobile_app_phone").mock(
        return_value=httpx.Response(200, json=[])
    )
    overnight = _overnight_plan()
    save_topup(s, replace(_record(overnight), last_sent_kwh=3.4))
    noon = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    async with HomeAssistantRest(BASE, "token") as rest:
        # 3.7 kWh short: under 0.5 kWh more than last time.
        await run_v2l_topup(s, rest, _event_day(overnight, 45.0), _EVENT, noon)
        assert route.call_count == 0
        # 1.25 points lower: 4.2 kWh short.
        await run_v2l_topup(s, rest, _event_day(overnight, 43.75), _EVENT, noon)

    assert route.call_count == 1
    assert load_topup(s, event_id(_EVENT)).last_sent_kwh == pytest.approx(4.2)


@respx.mock
async def test_failed_topup_send_is_retried(tmp_path: Path) -> None:
    s = _topup_settings(tmp_path)
    route = respx.post(f"{BASE}/services/notify/mobile_app_phone").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=[])]
    )
    plan = _overnight_plan()
    evening = datetime(2026, 6, 8, 21, 0, tzinfo=UTC)

    async with HomeAssistantRest(BASE, "token") as rest:
        await run_v2l_topup(s, rest, plan, _EVENT, evening)
        assert load_topup(s, event_id(_EVENT)).last_sent_kwh is None
        await run_v2l_topup(s, rest, plan, _EVENT, evening)

    assert route.call_count == 2
    assert load_topup(s, event_id(_EVENT)).last_sent_kwh == pytest.approx(1.1)
