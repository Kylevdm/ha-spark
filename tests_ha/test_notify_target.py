"""Notify options and best-effort delivery never grant or delay control."""
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import ServiceCall
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_fallback import (  # noqa: F401
    clear_liveness,
    fallback_site,
    optimise,
    plan,
)
from tests_ha.test_native_holds import (  # noqa: F401
    hass_config_dir,
    hass_storage,
    held_site,
    tick,
)


@pytest.mark.parametrize("name,error", [
    ("notify.phone", "invalid_notify_target"), ("Phone", "invalid_notify_target"),
    ("my phone", "invalid_notify_target"), ("send_message", "invalid_notify_target"),
    ("missing", "unknown_notify_target"), (" phone ", None), ("", None), (None, None),
])
async def test_notify_options_validate_and_clear(
    held_site: SparkCoordinator, name: str | None, error: str | None,  # noqa: F811
) -> None:
    c = held_site
    async def handler(call: ServiceCall) -> None:
        pass
    c.hass.services.async_register("notify", "phone", handler)
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "notify_target": "phone"})
    old = dict(c.entry.options)
    result = await c.hass.config_entries.options.async_init(c.entry.entry_id)
    assert "notify" in result["menu_options"]
    flow_id = result["flow_id"]
    result = await c.hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "notify"})
    assert result["step_id"] == "notify"
    result = await c.hass.config_entries.options.async_configure(
        flow_id, {"notify_target": name} if name is not None else {})
    if error:
        assert result["errors"] == {"notify_target": error}
        assert dict(c.entry.options) == old
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        expected = {k: v for k, v in old.items() if k != "notify_target"}
        if name is not None and name.strip():
            expected["notify_target"] = name.strip()
        assert result["data"] == expected
    await c.hass.async_block_till_done()


def target(c: SparkCoordinator, handler: Any, name: str = "phone") -> None:
    c.hass.services.async_register("notify", name, handler)
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "notify_target": name,
        "evidence": {"ev_status": {"entity_id": "sensor.ev", "max_age_s": 86400}}})


async def test_hold_delivery_cadence_survives_reload(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = held_site
    calls: list[ServiceCall] = []
    async def handler(call: ServiceCall) -> None:
        calls.append(call)
    target(c, handler)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007, times=10)
    c.hass.states.async_set("sensor.ev", "charging")
    now = datetime.now(UTC)
    for minutes in (0, 1, 31, 62):
        await c.holds.async_reconcile(now + timedelta(minutes=minutes))
        await c.hass.async_block_till_done(wait_background_tasks=True)
        await c.authority.async_load()
        c.holds.notices.clear()
    assert [call.data["message"] for call in calls] == [
        "A protection hold could not be applied."] * 2
    notice.assert_not_called()


async def test_fallback_entered_and_cleared_delivered_once(
    fallback_site: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    freezer: Any,
) -> None:
    c = fallback_site
    calls: list[ServiceCall] = []
    async def handler(call: ServiceCall) -> None:
        calls.append(call)
    target(c, handler)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    await c.fallback.async_evaluate(now)
    await c.hass.async_block_till_done(wait_background_tasks=True)
    assert c.fallback.in_force()
    assert len(calls) == 1
    assert calls[0].data["title"] == "ha-spark fallback"
    notice.assert_not_called()
    await clear_liveness(c, now)
    freezer.tick(timedelta(minutes=5))
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert (await c._async_dispatch_command(plan(c))).outcome == "applied"
    await c.fallback.async_evaluate(datetime.now(UTC))
    await c.fallback.async_evaluate(datetime.now(UTC))
    await c.hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 2
    assert calls[1].data["message"] == "Fallback charging is no longer active."
    assert c.authority.state is not None and not c.authority.state.fallback.active
    notice.assert_not_called()


@pytest.mark.parametrize("hang", [False, True])
async def test_failed_delivery_is_nonblocking_and_redacted(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, hang: bool,
    freezer: Any,
) -> None:
    from custom_components.ha_spark import notify_target

    c = held_site
    started = asyncio.Event()
    async def handler(call: ServiceCall) -> None:
        started.set()
        if hang:
            await asyncio.Event().wait()
        raise RuntimeError("SECRET-CANARY")
    target(c, handler, "canary_target_xyz")
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    caplog.set_level("DEBUG")
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007)
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    await started.wait()
    # The failed apply produced a notice; the next pass still verifies Off while
    # delivery is running, without waiting for the phone service.
    await tick(c)
    assert native_transport.holding[43007] == 222
    assert c.authority.state is not None and c.authority.state.holds[0].confirmed
    if hang:
        freezer.tick(timedelta(seconds=notify_target.DELIVERY_TIMEOUT + 1))
    await c.hass.async_block_till_done(wait_background_tasks=True)
    for canary in ("SECRET-CANARY", "canary_target_xyz"):
        assert canary not in caplog.text
        assert canary not in str(notice.call_args_list)
    if hang:
        # The hold verified before the deadline, so its notice was withdrawn:
        # the late failure must not leave a stale alarm behind.
        notice.assert_not_called()
        return
    assert notice.call_count == 1
    assert notice.call_args.kwargs["notification_id"].startswith("ha_spark_hold_")
    assert notice.call_args.args[1] == "A protection hold could not be applied."


@pytest.mark.parametrize("kind", ["degraded", "release"])
async def test_retained_hold_notices_reach_target_once(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,  # noqa: F811
    kind: str,
) -> None:
    c = held_site
    calls: list[ServiceCall] = []
    async def handler(call: ServiceCall) -> None:
        calls.append(call)
    target(c, handler)
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    now = datetime.now(UTC)
    if kind == "degraded":
        c.hass.states.async_set("sensor.ev", "unavailable")
        await tick(c)
        for minutes in (61, 62):
            await c.holds.async_reconcile(now + timedelta(minutes=minutes))
        message = "Retaining a protection hold without fresh evidence; check its source."
    else:
        native_transport.holding.pop(43141)
        assert not await c.holds.async_release(now)
        for minutes in (61, 62):
            assert not await c.holds.async_release(now + timedelta(minutes=minutes))
        message = "A protection hold could not be released; restoration is not verified."
    await c.hass.async_block_till_done(wait_background_tasks=True)
    assert [call.data["message"] for call in calls] == [message]
    assert native_transport.holding[43007] == 222
    assert c.authority.state is not None and c.authority.state.holds[0].confirmed


@pytest.mark.parametrize("value", [None, "notify.phone", "send_message", "missing", 42])
async def test_delivery_revalidates_current_option_and_falls_back_immediately(
    held_site: SparkCoordinator, value: object,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark.notify_target import async_notice

    c = held_site
    calls: list[ServiceCall] = []
    async def handler(call: ServiceCall) -> None:
        calls.append(call)
    target(c, handler)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "notify_target": value})
    async_notice(c.hass, c.entry, notification_id="notice-id", title="Title", message="Message")
    notice.assert_called_once_with(
        c.hass, "Message", title="Title", notification_id="notice-id")
    await c.hass.async_block_till_done(wait_background_tasks=True)
    assert not calls


async def test_fallback_unverified_notice_repeats_only_once(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,  # noqa: F811
) -> None:
    c = fallback_site
    calls: list[ServiceCall] = []
    async def handler(call: ServiceCall) -> None:
        calls.append(call)
    target(c, handler)
    await optimise(c)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141, times=10)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    for minutes in (0, 1, 31, 62):
        await c.fallback.async_evaluate(now + timedelta(minutes=minutes))
        await c.hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 3  # entered, unverified, one repeat
    assert str(calls[0].data["message"]).startswith("Fallback charging is active:")
    assert calls[1].data == calls[2].data
    assert str(calls[1].data["message"]).startswith("Fallback charging is not verified:")
    assert not c.fallback.in_force()


async def test_hanging_fallback_notice_does_not_delay_verified_schedule(
    fallback_site: SparkCoordinator, freezer: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark.notify_target import DELIVERY_TIMEOUT

    c = fallback_site
    started = asyncio.Event()
    async def handler(call: ServiceCall) -> None:
        started.set()
        await asyncio.Event().wait()
    target(c, handler)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    await started.wait()
    assert c.fallback.in_force()
    assert not c.control.lock.locked()
    assert not c.fallback.lock.locked()
    notice.assert_not_called()
    freezer.tick(timedelta(seconds=DELIVERY_TIMEOUT + 1))
    await c.hass.async_block_till_done(wait_background_tasks=True)
    assert notice.call_count == 1
    assert notice.call_args.kwargs["notification_id"] == "ha_spark_fallback_entered"
    assert c.fallback.in_force()


async def test_unload_during_delivery_falls_back_to_persistent_notification(
    held_site: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    from custom_components.ha_spark.notify_target import async_notice

    c = held_site
    started = asyncio.Event()
    async def handler(call: ServiceCall) -> None:
        started.set()
        await asyncio.Event().wait()
    target(c, handler)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    async_notice(c.hass, c.entry, notification_id="notice-id", title="Title", message="Message")
    await started.wait()
    notice.assert_not_called()
    await c.hass.config_entries.async_unload(c.entry.entry_id)
    await c.hass.async_block_till_done()
    notice.assert_called_once_with(
        c.hass, "Message", title="Title", notification_id="notice-id")


async def test_service_never_runs_inside_the_producers_locks(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,  # noqa: F811
) -> None:
    from homeassistant.core import callback

    c = held_site
    locked: list[bool] = []
    @callback
    def handler(call: ServiceCall) -> None:
        locked.append(c.control.lock.locked() or c.holds.lock.locked()
                      or c.native.lock.locked())
    target(c, handler)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007, times=10)
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    await c.hass.async_block_till_done(wait_background_tasks=True)
    assert locked == [False]


@pytest.mark.parametrize("ignore_cancel", [False, True])
async def test_dismissed_notice_is_not_revived_by_a_late_failure(
    held_site: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    freezer: Any, ignore_cancel: bool,
) -> None:
    from custom_components.ha_spark.notify_target import (
        DELIVERY_TIMEOUT,
        async_dismiss,
        async_notice,
    )

    c = held_site
    started, release = asyncio.Event(), asyncio.Event()
    async def handler(call: ServiceCall) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not ignore_cancel:
                raise
            await release.wait()  # swallows the cancel and keeps hanging
    target(c, handler)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    async_notice(c.hass, c.entry, notification_id="kept", title="Title", message="Kept")
    async_notice(c.hass, c.entry, notification_id="gone", title="Title", message="Gone")
    await started.wait()
    async_dismiss(c.hass, "gone")
    freezer.tick(timedelta(seconds=DELIVERY_TIMEOUT + 1))
    for _ in range(20):
        await asyncio.sleep(0)
    # A service that ignores cancellation still cannot hide the wanted notice.
    notice.assert_called_once_with(c.hass, "Kept", title="Title", notification_id="kept")
    release.set()
    await c.hass.async_block_till_done()
