"""Live export tripwire: counting rules, the durable stop latch and its cleanup.

Every integration case asserts register effects, store state or fixed codes. The
tripwire never writes a register itself: cleanup is the boundary's clear.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.native_tripwire import (
    TripwireState,
    fresh,
    step,
)
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_export import (  # noqa: F401 - hass fixtures
    allow_export,
    apply_command,
    arm,
    block_writes,
    export_writes,
    transition_kinds,
    window_intent,
)
from tests_ha.test_native_export_interplay import full_soc, restart
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as current_writer

writer = current_writer
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
# An event already under way, so the window is inside without moving the clock.
LIVE = {"direction": "export", "start": (NOW - timedelta(minutes=10)).isoformat(),
        "end": (NOW + timedelta(minutes=50)).isoformat()}
LIVE_END = NOW + timedelta(minutes=50)
LIMIT = 3600.0


@pytest.fixture(autouse=True)
def export_clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)


# --- pure counting rules ---------------------------------------------------------
def over(state: TripwireState, at: datetime, export_w: float = LIMIT + 201
         ) -> tuple[TripwireState, bool]:
    state, decision = step(state, import_w=-export_w, reported_at=at, limit_w=LIMIT, now=at)
    return state, decision.trip


def test_two_fresh_advancing_over_reports_trip_on_the_second() -> None:
    state, trip = over(TripwireState(), NOW)
    assert not trip
    state, trip = over(state, NOW + timedelta(seconds=1))
    assert trip


def test_the_deadband_is_exclusive() -> None:
    state, _ = over(TripwireState(), NOW, LIMIT + 200)
    state, trip = over(state, NOW + timedelta(seconds=1), LIMIT + 200)
    assert not trip and state.over_count == 0
    state, _ = over(state, NOW + timedelta(seconds=2), LIMIT + 201)
    assert state.over_count == 1


def test_a_repeated_report_counts_once() -> None:
    state, _ = over(TripwireState(), NOW)
    state, trip = over(state, NOW)
    assert not trip and state.over_count == 1


def test_a_non_over_report_resets_the_count() -> None:
    state, _ = over(TripwireState(), NOW)
    state, _ = over(state, NOW + timedelta(seconds=1), 0.0)
    assert state.over_count == 0
    state, trip = over(state, NOW + timedelta(seconds=2))
    assert not trip


@pytest.mark.parametrize("import_w,age_s", [
    (-4000.0, 61), (-4000.0, -6), (float("nan"), 0), (float("inf"), 0), (None, 0),
])
def test_unusable_readings_are_missing(import_w: float | None, age_s: int) -> None:
    assert not fresh(import_w, NOW - timedelta(seconds=age_s), NOW)


def test_naive_or_absent_report_times_are_missing() -> None:
    assert not fresh(-4000.0, datetime(2026, 11, 2, 12), NOW)
    assert not fresh(-4000.0, None, NOW)
    assert fresh(-4000.0, NOW - timedelta(seconds=60), NOW)
    assert fresh(-4000.0, NOW + timedelta(seconds=5), NOW)


def test_a_gap_warns_once_repairs_at_two_minutes_and_keeps_the_count() -> None:
    state, _ = over(TripwireState(), NOW)
    warns = 0
    decision = None
    for seconds in range(10, 130, 10):
        now = NOW + timedelta(seconds=seconds)
        state, decision = step(state, import_w=None, reported_at=None, limit_w=LIMIT, now=now)
        assert not decision.trip
        warns += decision.warn
        assert decision.repair == (seconds - 10 >= 120)
    assert warns == 1 and state.over_count == 1
    assert decision is not None and decision.missing_for is not None
    later = NOW + timedelta(seconds=140)
    state, decision = step(state, import_w=-5000.0, reported_at=later, limit_w=LIMIT, now=later)
    assert not (decision.warn or decision.repair or decision.missing_for)
    assert state.missing_since is None and state.over_count == 2 and decision.trip


def test_the_previous_count_stands_across_a_gap() -> None:
    state, _ = over(TripwireState(), NOW)
    gap = NOW + timedelta(seconds=90)
    state, _ = step(state, import_w=None, reported_at=None, limit_w=LIMIT, now=gap)
    state, trip = over(state, NOW + timedelta(seconds=95))
    assert trip


# --- integration -----------------------------------------------------------------
async def armed_inside(
    c: SparkCoordinator, transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    allow_export(c, monkeypatch, transport)
    full_soc(c, monkeypatch)
    await arm(c, event=LIVE)
    result = await c._async_dispatch_command(apply_command(
        c, [window_intent(event=LIVE)], sequence=2))
    assert result.outcome in ("applied", "unchanged")
    transport.traffic.clear()


def report(c: SparkCoordinator, freezer: FrozenDateTimeFactory, export_w: float) -> bool:
    freezer.tick(timedelta(seconds=1))
    now = datetime.now(UTC)
    return c.tripwire.observe("grid", -export_w, now, now)


async def trip(c: SparkCoordinator, freezer: FrozenDateTimeFactory) -> None:
    assert not report(c, freezer, LIMIT + 500)
    assert report(c, freezer, LIMIT + 500)
    await c.tripwire.async_trip("grid", datetime.now(UTC))


async def test_a_trip_latches_before_cleanup_and_clears(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    saved: list[tuple[int, str]] = []
    original = writer.authority.async_save

    async def spy(candidate: Any) -> bool:
        record = candidate.export_records[-1]
        saved.append((len(candidate.stop_latches), record.state))
        return await original(candidate)

    monkeypatch.setattr(writer.authority, "async_save", spy)
    await trip(writer, freezer)
    # The first save carries the latch and stop_pending, before any register write.
    assert saved[0] == (1, "stop_pending")
    state = writer.authority.state
    assert state is not None
    [latch] = state.stop_latches
    assert (latch.inverter_id, latch.window_end, latch.epoch) == (
        "inv1", LIVE_END, state.epoch)
    assert latch.event.end == LIVE_END
    record = state.export_records[-1]
    assert (record.state, record.stop_reason, record.cleanup_verified) == (
        "cleared", "tripped", True)
    assert not state.export_supervisions
    assert ("tripped", "tripped") in transition_kinds(writer)
    assert block_writes(native_transport)[-1][4:] == (0, 0, 0, 0)
    assert [native_transport.holding.get(r, 0) for r in range(43147, 43151)] == [0, 0, 0, 0]
    snapshot = writer._export_snapshot(state, writer.guard.topology)  # type: ignore[union-attr]
    [entry] = snapshot
    assert entry["stop_latch"] is not None and entry["bound"] is None


async def test_a_failed_save_writes_nothing(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)

    async def refuse(candidate: Any) -> bool:
        return False

    monkeypatch.setattr(writer.authority, "async_save", refuse)
    await trip(writer, freezer)
    state = writer.authority.state
    assert state is not None and not state.stop_latches and state.export_supervisions
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_a_failed_clear_keeps_the_latch_and_the_tick_retries(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    await trip(writer, freezer)
    state = writer.authority.state
    assert state is not None and len(state.stop_latches) == 1
    assert state.export_records[-1].state == "stop_pending"
    await writer.async_retry_export_records(datetime.now(UTC))
    state = writer.authority.state
    assert state is not None and state.export_records[-1].state == "cleared"
    assert len(state.stop_latches) == 1


async def test_a_latched_event_is_refused_tripped_even_after_a_new_supervision(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    await trip(writer, freezer)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=LIVE)], sequence=3))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "tripped")
    await arm(writer)
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=LIVE)], sequence=4))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "tripped")
    assert export_writes(native_transport) == []


async def test_the_latch_survives_restart_and_is_never_preserved(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    # Slot 1 holds the exact window face when the latch lands but cleanup cannot.
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    await trip(writer, freezer)
    restored = await restart(writer)
    await restored.async_retry_export_records(datetime.now(UTC))
    state = restored.authority.state
    assert state is not None and len(state.stop_latches) == 1
    assert state.export_records[-1].state == "cleared"


async def test_the_latch_survives_a_successful_reprobe(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    await trip(writer, freezer)
    await writer.async_reprobe("inv1")
    state = writer.authority.state
    assert state is not None and len(state.stop_latches) == 1
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=LIVE)], sequence=3))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "tripped")


async def test_a_preserved_record_trips(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    restored = await restart(writer)
    await restored.async_retry_export_records(datetime.now(UTC))
    state = restored.authority.state
    assert state is not None and state.export_records[-1].state == "preserved"
    await trip(restored, freezer)
    state = restored.authority.state
    assert state is not None and len(state.stop_latches) == 1
    assert state.export_records[-1].state == "cleared"


async def test_pv_only_export_is_never_curtailed(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    full_soc(writer, monkeypatch)
    # Nothing armed.
    assert not report(writer, freezer, LIMIT + 500)
    assert not report(writer, freezer, LIMIT + 500)
    await writer.tripwire.async_trip("grid", datetime.now(UTC))
    # Armed, but the window has not started yet (the default event is two hours out).
    await arm(writer)
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))
    assert result.outcome in ("applied", "unchanged")
    native_transport.traffic.clear()
    assert not report(writer, freezer, LIMIT + 500)
    assert not report(writer, freezer, LIMIT + 500)
    await writer.tripwire.async_trip("grid", datetime.now(UTC))
    state = writer.authority.state
    assert state is not None and not state.stop_latches
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert "tripped" not in [kind for kind, _ in transition_kinds(writer)]


async def test_missing_readings_warn_then_repair_with_no_cut(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    assert not report(writer, freezer, LIMIT + 500)
    stale = datetime.now(UTC) - timedelta(minutes=5)
    registry = ir.async_get(writer.hass)
    issue = writer.tripwire.issue_id("grid")
    caplog.set_level(logging.WARNING)
    for _ in range(7):
        freezer.tick(timedelta(seconds=10))
        assert not writer.tripwire.observe("grid", -5000.0, stale, datetime.now(UTC))
    assert len([r for r in caplog.records if "grid reading missing" in r.getMessage()]) == 1
    assert registry.async_get_issue("ha_spark", issue) is None
    for _ in range(6):
        freezer.tick(timedelta(seconds=10))
        assert not writer.tripwire.observe("grid", -5000.0, stale, datetime.now(UTC))
    assert registry.async_get_issue("ha_spark", issue) is not None
    state = writer.authority.state
    assert state is not None and not state.stop_latches
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert not report(writer, freezer, 0.0)
    assert registry.async_get_issue("ha_spark", issue) is None


async def test_the_latch_expires_at_the_window_end(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    await trip(writer, freezer)
    await writer.async_expire_supervisions(LIVE_END - timedelta(minutes=1))
    state = writer.authority.state
    assert state is not None and len(state.stop_latches) == 1
    await writer.async_expire_supervisions(LIVE_END)
    state = writer.authority.state
    assert state is not None and state.stop_latches == ()


async def test_the_guard_loop_feeds_the_tripwire(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    guard = writer.guard
    assert guard is not None
    for _ in range(2):
        freezer.tick(timedelta(seconds=1))
        reading = (-(LIMIT + 500), datetime.now(UTC))
        monkeypatch.setattr(guard, "_power", lambda entity, r=reading: r)
        await guard.async_evaluate()
    await writer.hass.async_block_till_done(wait_background_tasks=True)
    state = writer.authority.state
    assert state is not None and len(state.stop_latches) == 1


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_two_inverters_on_one_connection_both_latch(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    full_soc(writer, monkeypatch)
    writer.native.readings["inv2"] = dict(writer.native.readings["inv1"])
    await arm(writer, event=LIVE, ratings={"inv1": 1500.0, "inv2": 1500.0})
    for sequence, inverter in ((2, "inv1"), (3, "inv2")):
        result = await writer._async_dispatch_command(apply_command(
            writer, [window_intent(event=LIVE, inverter_id=inverter)], sequence=sequence))
        assert result.outcome in ("applied", "unchanged")
    await trip(writer, freezer)
    state = writer.authority.state
    assert state is not None
    assert {latch.inverter_id for latch in state.stop_latches} == {"inv1", "inv2"}
    assert not state.export_supervisions
    assert {r.inverter_id: r.state for r in state.export_records} == {
        "inv1": "cleared", "inv2": "cleared"}


async def test_a_trip_decided_before_a_reload_latches_before_the_store_closes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    """Unload drains a pending trip while its session still persists (#672)."""
    await armed_inside(writer, native_transport, monkeypatch, freezer)
    guard = writer.guard
    assert guard is not None
    hass, entry = writer.hass, writer.entry
    # The trip queues behind authority work in flight, as on a busy tick.
    await writer.control.lock.acquire()
    try:
        for _ in range(2):
            freezer.tick(timedelta(seconds=1))
            reading = (-(LIMIT + 500), datetime.now(UTC))
            monkeypatch.setattr(guard, "_power", lambda entity, r=reading: r)
            await guard.async_evaluate()
        reload = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        for _ in range(200):
            if reload.done():
                break
            await asyncio.sleep(0)
        # Unload waits for the decided trip rather than closing the store under it.
        assert not reload.done() and not writer.authority.closed
    finally:
        writer.control.lock.release()
    assert await reload
    await hass.async_block_till_done()
    restored: SparkCoordinator = entry.runtime_data
    assert restored is not writer and writer.authority.closed
    state = restored.authority.state
    assert state is not None and len(state.stop_latches) == 1
    assert state.export_records[-1].state == "stop_pending"
    # The new session finishes the cleanup the closing session could not write.
    await restored.async_retry_export_records(datetime.now(UTC))
    state = restored.authority.state
    assert state is not None and state.export_records[-1].state == "cleared"
    assert len(state.stop_latches) == 1
