"""Tests for the morning plan digest."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.digest import format_digest, run_digest_tick
from ha_spark.energy.models import ChargeIntent, ChargePlan, Reservation
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.rest import HomeAssistantRest


def _plan() -> ChargePlan:
    now = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
    soc = SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=32.0,
        raw_state="32",
        reported_at=now,
        age_s=0,
        max_age_s=600,
    )
    intent = ChargeIntent(
        target_soc_pct=78,
        soc=soc,
        window_start=time(23, 30),
        window_end=time(5, 30),
    )
    return ChargePlan(
        soc=soc,
        capacity_kwh=26.88,
        solar_kwh=5.0,
        effective_solar_kwh=5.0,
        load_kwh=24.0,
        cheap_covered_kwh=0,
        usable_now_kwh=3.0,
        deficit_kwh=12.0,
        buffer_pct=0,
        required_kwh=12.0,
        target_soc=78,
        window_hours=6,
        ev_charging=False,
        ha_template_needed=None,
        charge_intent=intent,
        planned_cost=1.42,
        baseline_cost=5.80,
        reservations=(
            Reservation(
                name="morning load",
                target_slot=18,
                energy_kwh=6.0,
                obligation_kind="load",
                reason="keeps enough charge for the morning peak",
            ),
        ),
    )


def test_format_digest_covers_charge_reservation_and_cost() -> None:
    title, message = format_digest(
        _plan(), Settings(proactive_mode="on"), datetime(2026, 10, 4, 7, 0, tzinfo=UTC)
    )

    assert title == "ha-spark morning plan — 4 October"
    assert "Tonight ha-spark charges from 23:30 to 05:30, targeting 78% SoC." in message
    assert "keeps enough charge for the morning peak" in message
    assert "£1.42" in message


def test_simulate_digest_says_it_describes_would_do_actions() -> None:
    _, message = format_digest(
        _plan(), Settings(proactive_mode="simulate"), datetime(2026, 10, 4, 7, 0, tzinfo=UTC)
    )

    assert "would do: charge tonight" in message
    assert "would do" in message


def test_untrusted_soc_digest_does_not_claim_a_charge_or_target() -> None:
    plan = replace(
        _plan(),
        soc=replace(_plan().soc, status=SocStatus.STALE, raw_state="unknown"),
    )

    _, message = format_digest(
        plan, Settings(proactive_mode="on"), datetime(2026, 10, 4, 7, 0, tzinfo=UTC)
    )

    assert plan.soc.reason in message
    assert "will not program a charge tonight" in message
    assert "targeting" not in message
    assert "ha-spark charges" not in message


def test_no_charge_needed_digest_does_not_claim_a_charge() -> None:
    plan = replace(_plan(), required_kwh=0)

    _, message = format_digest(
        plan, Settings(proactive_mode="on"), datetime(2026, 10, 4, 7, 0, tzinfo=UTC)
    )

    assert "No grid charge is needed tonight" in message
    assert "targeting" not in message
    assert "ha-spark charges" not in message


def test_digest_does_not_include_secret_settings() -> None:
    sentinels = ("ha-token-sentinel", "supervisor-token-sentinel", "axle-key-sentinel")
    settings = Settings(
        ha_token=sentinels[0],
        supervisor_token=sentinels[1],
        axle_api_key=sentinels[2],
        octopus_api_key="octopus-key-sentinel",
    )

    title, message = format_digest(_plan(), settings, datetime.now(UTC))

    assert all(secret not in title + message for secret in (*sentinels, "octopus-key-sentinel"))


def test_missing_daily_cost_is_explained() -> None:
    _, message = format_digest(
        replace(_plan(), planned_cost=None, baseline_cost=None),
        Settings(proactive_mode="off"),
        datetime(2026, 10, 4, 7, 0, tzinfo=UTC),
    )

    assert "Expected cost is unavailable" in message


@pytest.mark.asyncio
async def test_due_digest_sends_once_across_restart_and_uses_notify_service(tmp_path: Path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "ha_spark.db"),
        timezone="Europe/London",
        digest_time="07:00",
        notify_service="mobile_app_phone",
        ha_url="http://ha.test",
        ha_token="test-token",
    )
    before = datetime(2026, 10, 4, 5, 59, tzinfo=UTC)
    due = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
    with respx.mock(base_url="http://ha.test/api") as mock:
        call = mock.post("/services/notify/mobile_app_phone").respond(200, json=[])
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            await run_digest_tick(settings, _plan(), before, rest)
            await run_digest_tick(settings, _plan(), due, rest)
        # A new settings instance models a process restart.
        restarted = Settings.model_validate(settings.model_dump())
        async with HomeAssistantRest(restarted.ha_rest_url, restarted.auth_token) as rest:
            await run_digest_tick(restarted, _plan(), due, rest)

    assert call.call_count == 1
    assert json.loads(call.calls[0].request.content) == {
        "title": "ha-spark morning plan — 4 October",
        "message": format_digest(_plan(), settings, due)[1],
    }


@pytest.mark.asyncio
async def test_start_after_digest_time_still_sends_that_local_day(tmp_path: Path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "ha_spark.db"),
        timezone="Europe/London",
        digest_time="07:00",
        notify_service="mobile_app_phone",
        ha_url="http://ha.test",
        ha_token="test-token",
    )
    started_late = datetime(2026, 10, 4, 8, 30, tzinfo=UTC)
    with respx.mock(base_url="http://ha.test/api") as mock:
        call = mock.post("/services/notify/mobile_app_phone").respond(200, json=[])
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            await run_digest_tick(settings, _plan(), started_late, rest)

    assert call.call_count == 1


@pytest.mark.asyncio
async def test_failed_send_retries_next_tick_and_blank_service_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        db_path=str(tmp_path / "ha_spark.db"),
        timezone="UTC",
        digest_time="07:00",
        notify_service="mobile_app_phone",
        ha_url="http://ha.test",
        ha_token="test-token",
    )
    due = datetime(2026, 10, 4, 7, 1, tzinfo=UTC)
    with respx.mock(base_url="http://ha.test/api") as mock:
        route = mock.post("/services/notify/mobile_app_phone").mock(
            side_effect=[httpx_error(), httpx_response()]
        )
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            await run_digest_tick(settings, _plan(), due, rest)
            await run_digest_tick(settings, _plan(), due, rest)
        assert route.call_count == 2
    assert "Morning digest notify failed; will retry next tick" in caplog.text

    blank = Settings(
        db_path=str(tmp_path / "other.db"), notify_service="", ha_url="http://ha.test", ha_token="t"
    )
    # The blank target returns before attempting any network call.
    async with HomeAssistantRest(blank.ha_rest_url, blank.auth_token) as rest:
        await run_digest_tick(blank, _plan(), due, rest)


@pytest.mark.asyncio
async def test_corrupt_persistence_is_treated_as_unsent(tmp_path: Path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "ha_spark.db"),
        timezone="UTC",
        notify_service="mobile_app_phone",
        ha_url="http://ha.test",
        ha_token="test-token",
    )
    (tmp_path / "ha_spark_digest.json").write_text("not json", encoding="utf-8")
    due = datetime(2026, 10, 4, 7, 1, tzinfo=UTC)
    with respx.mock(base_url="http://ha.test/api") as mock:
        call = mock.post("/services/notify/mobile_app_phone").respond(200, json=[])
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            await run_digest_tick(settings, _plan(), due, rest)

    assert call.call_count == 1


def httpx_error() -> object:
    import httpx

    return httpx.ConnectError("failed")


def httpx_response() -> object:
    import httpx

    return httpx.Response(200, json=[])
