"""Clock commands, automatic sync and repairs use only the in-memory bus."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import native_clock
from custom_components.ha_spark.commands import Command
from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_device import native_entry


@pytest.fixture(autouse=True)
def frozen_clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-30T12:00:00Z")


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.fixture
async def clock(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> SparkCoordinator:
    original = native_transport.write

    async def write(address: int, values: list[int], *, device_id: int = 1) -> Any:
        result = await original(address, values, device_id=device_id)
        if result.ok and address == 43000:
            native_transport.set_input(33022, values)
        return result

    async def sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(native_transport, "write", write)
    monkeypatch.setattr(native_clock, "verify_sleep", sleep)
    entry = await native_entry(hass, aioclient_mock)
    native_transport.traffic.clear()
    coordinator: SparkCoordinator = entry.runtime_data
    return coordinator


def command(clock: SparkCoordinator) -> Command:
    state = clock.authority.state
    return Command(session_id="test", sequence=1, epoch=state.epoch if state else 1,
                   kind="sync_clock", payload={"requester_kind": "cli"})


async def test_clock_command_writes_and_reads_back_in_observe(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    result = await clock._async_dispatch_command(command(clock))
    assert result.outcome == "applied" and result.intents == []
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert len(writes) == 1 and writes[0].address == 43000
    assert native_transport.traffic[-1].address == 33022


@pytest.mark.parametrize("fence", ["unknown", "save_failed", "pending", "read_only",
                                   "transition", "closing", "zone"])
async def test_clock_fences(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fence: str,
) -> None:
    state = clock.authority.state
    assert state is not None
    if fence in ("unknown", "save_failed"):
        monkeypatch.setattr(clock.authority, "status", fence)
    elif fence == "pending":
        monkeypatch.setattr(clock.control, "owned", lambda: False)
    elif fence == "read_only":
        monkeypatch.setattr(clock.native, "reason", lambda inv: "caching_on")
    elif fence == "transition":
        from custom_components.ha_spark.authority_store import Transition
        await clock.authority.async_save(state.model_copy(update={"transition": Transition(
            target="optimise", started_at=datetime.now(UTC), steps=("apply_plan",),
        )}))
    elif fence == "closing":
        clock.native.polling = False
    else:
        monkeypatch.setattr(clock.control, "household_zone", lambda: None)
    result = await clock._async_dispatch_command(command(clock))
    assert result.outcome == "refused"
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_clock_rechecks_after_read(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = native_transport.read_input

    async def read(address: int, count: int, *, device_id: int = 1) -> Any:
        result = await original(address, count, device_id=device_id)
        monkeypatch.setattr(clock.authority, "status", "unknown")
        return result

    monkeypatch.setattr(native_transport, "read_input", read)
    assert (await clock._async_dispatch_command(command(clock))).reason == "authority_unknown"
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_clock_disagreement_is_unverified_and_probed_once(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bypass the test clock echo: a silent no-op must never count as synced.
    monkeypatch.setattr(native_transport, "write", FakeSolisDevice.write.__get__(native_transport))
    native_transport.set_input(33022, [26, 1, 1, 0, 0, 0])
    result = await clock._async_dispatch_command(command(clock))
    assert result.outcome == "unverified"
    assert sum(t.address == 33022 for t in native_transport.traffic) == 4
    assert sum(t.address == 33000 for t in native_transport.traffic) == 1


async def test_clock_write_error_is_failed(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.inject_write_fault(WriteFault("exception"), address=43000)
    assert (await clock._async_dispatch_command(command(clock))).outcome == "failed"


async def test_automatic_sync_retries_after_mode_change_and_only_once(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = datetime(2026, 10, 25, 2, tzinfo=UTC)
    freezer.move_to(now)
    await clock.clock.async_tick(now)
    assert not any(t.op == "write" for t in native_transport.traffic)
    state = clock.authority.state
    assert state is not None
    await clock.authority.async_save(state.model_copy(update={"committed_mode": "protect"}))
    await clock.clock.async_tick(now)
    await clock.clock.async_tick(now + timedelta(minutes=1))
    assert sum(t.op == "write" for t in native_transport.traffic) == 1


async def test_drift_repair_freshness_and_threshold(
    clock: SparkCoordinator, hass: HomeAssistant,
) -> None:
    from custom_components.ha_spark.solis_frames import Reading
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    inverter = next(iter(clock.native.inverters.values()))
    key = ("ha_spark", clock.clock.issue_id(inverter))
    clock.native.readings[inverter.id]["input_33022"] = Reading(now, (26, 10, 30, 12, 6, 0), 0)
    clock.clock.check_drift(now)
    assert ir.async_get(hass).issues[key].severity == ir.IssueSeverity.WARNING
    clock.native.readings[inverter.id]["input_33022"] = Reading(now, (26, 10, 30, 12, 30, 0), 0)
    clock.clock.check_drift(now)
    assert ir.async_get(hass).issues[key].severity == ir.IssueSeverity.ERROR
    clock.native.readings[inverter.id]["input_33022"] = Reading(now, (26, 10, 30, 12, 0, 0), 0)
    clock.clock.check_drift(now + timedelta(seconds=61))
    assert key in ir.async_get(hass).issues
    clock.clock.check_drift(now)
    assert key not in ir.async_get(hass).issues


@pytest.mark.parametrize("mode,manual,fallback", [
    ("observe", False, False), ("protect", False, False),
    ("optimise", False, True), ("optimise", True, False),
])
async def test_explicit_and_automatic_mode_table(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    mode: str, manual: bool, fallback: bool, freezer: FrozenDateTimeFactory,
) -> None:
    from custom_components.ha_spark.authority_store import FallbackState, ManualSession
    now = datetime(2026, 10, 25, 2, tzinfo=UTC)
    freezer.move_to(now)
    state = clock.authority.state
    assert state is not None
    session = ManualSession(return_to="optimise", keep_protection=True) if manual else None
    assert await clock.authority.async_save(state.model_copy(update={
        "committed_mode": mode, "manual_session": session,
        "fallback": FallbackState(active=fallback, causes=("soc_integrity",) if fallback else ()),
    }))
    await clock.clock.async_tick(now)
    automatic = mode != "observe" and not manual
    assert sum(t.op == "write" for t in native_transport.traffic) == int(automatic)
    native_transport.traffic.clear()
    assert (await clock._async_dispatch_command(command(clock))).outcome == "applied"
    assert sum(t.op == "write" for t in native_transport.traffic) == 1


async def test_automatic_failure_retries_until_success_or_lookback_ends(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = datetime(2026, 10, 25, 2, tzinfo=UTC)
    freezer.move_to(now)
    state = clock.authority.state
    assert state is not None
    assert await clock.authority.async_save(state.model_copy(update={"committed_mode": "protect"}))
    native_transport.inject_write_fault(WriteFault("exception"), address=43000, times=2)
    await clock.clock.async_tick(now)
    await clock.clock.async_tick(now + timedelta(minutes=1))
    await clock.clock.async_tick(now + timedelta(hours=6))
    assert sum(t.op == "write" for t in native_transport.traffic) == 2
    await clock.clock.async_tick(now + timedelta(minutes=2))
    await clock.clock.async_tick(now + timedelta(minutes=3))
    assert sum(t.op == "write" for t in native_transport.traffic) == 3


async def test_read_error_retries_and_confirmation_is_final(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark.solis_transport import ReadResult, TransportError
    original = native_transport.read_input
    calls = 0

    async def read(address: int, count: int, *, device_id: int = 1) -> ReadResult:
        nonlocal calls
        if address == 33022:
            calls += 1
            if calls != 3:  # unreadable before write, then first verification fails
                return ReadResult(0.02, error=TransportError.TIMEOUT)
        return await original(address, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", read)
    assert (await clock._async_dispatch_command(command(clock))).outcome == "applied"
    assert calls == 3  # no later failing read can undo the confirmed sync


async def test_command_isolates_inverters_and_returns_worst(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sleep(seconds: float) -> None:
        pass
    monkeypatch.setattr(native_clock, "verify_sleep", sleep)
    entry = await native_entry(hass, aioclient_mock, two_units=True)
    coordinator: SparkCoordinator = entry.runtime_data
    native_transport.traffic.clear()
    native_transport.inject_write_fault(WriteFault("exception"), address=43000)
    result = await coordinator._async_dispatch_command(command(coordinator))
    assert result.outcome == "failed" and result.intents == []
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert len(writes) == 2
    assert [t.device_id for t in writes] == [1, 2]


async def test_drift_fix_flow_syncs_only_its_inverter(
    clock: SparkCoordinator, hass: HomeAssistant, native_transport: FakeSolisDevice,
) -> None:
    from custom_components.ha_spark.repairs import ClockRepairFlow, async_create_fix_flow
    inverter = next(iter(clock.native.inverters.values()))
    flow = await async_create_fix_flow(hass, clock.clock.issue_id(inverter), {
        "entry_id": clock.entry.entry_id, "inverter_id": inverter.id,
    })
    assert isinstance(flow, ClockRepairFlow)
    flow.hass = hass
    assert (await flow.async_step_init())["type"] == "form"
    assert not any(t.op == "write" for t in native_transport.traffic)
    assert (await flow.async_step_confirm({}))["type"] == "create_entry"
    assert sum(t.op == "write" for t in native_transport.traffic) == 1


async def test_drift_fix_flow_refuses_unknown_authority(
    clock: SparkCoordinator, hass: HomeAssistant, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark.repairs import async_create_fix_flow
    inverter = next(iter(clock.native.inverters.values()))
    flow = await async_create_fix_flow(hass, clock.clock.issue_id(inverter), {
        "entry_id": clock.entry.entry_id, "inverter_id": inverter.id,
    })
    flow.hass = hass
    monkeypatch.setattr(clock.authority, "status", "unknown")
    result = await flow.async_step_confirm({})
    assert result["type"] == "form" and result["errors"] == {"base": "authority_unknown"}
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("words,age,raised", [
    ((26, 10, 30, 12, 5, 0), 0, False),
    ((26, 10, 30, 12, 5, 1), 0, True),
    ((26, 10, 30, 12, 6, 0), 60, True),
    ((26, 10, 30, 12, 6, 0), 61, False),
    ((26, 10, 30, 12, 6, 0), -1, False),
    ((2026, 10, 30, 12, 6, 0), 0, False),
    (None, 0, False),
])
async def test_drift_requires_readable_fresh_evidence(
    clock: SparkCoordinator, hass: HomeAssistant, words: tuple[int, ...] | None,
    age: int, raised: bool,
) -> None:
    from custom_components.ha_spark.solis_frames import Reading
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    inverter = next(iter(clock.native.inverters.values()))
    clock.native.readings[inverter.id]["input_33022"] = Reading(now, words, 0)
    clock.clock.check_drift(now + timedelta(seconds=age))
    assert (("ha_spark", clock.clock.issue_id(inverter)) in ir.async_get(hass).issues) is raised


async def test_clock_bus_lock_and_pending_automatic_pass_are_serialised(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    import asyncio
    now = datetime(2026, 10, 25, 2, tzinfo=UTC)
    freezer.move_to(now)
    state = clock.authority.state
    assert state is not None
    assert await clock.authority.async_save(state.model_copy(update={"committed_mode": "protect"}))
    async with clock.native.lock:
        pending = asyncio.create_task(clock.clock.async_tick(now))
        await asyncio.sleep(0)
        await clock.clock.async_tick(now)
        assert not any(t.op == "write" for t in native_transport.traffic)
    await pending
    assert sum(t.op == "write" for t in native_transport.traffic) == 1


async def test_automatic_sync_waits_for_a_transition(
    clock: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    from custom_components.ha_spark.authority_store import Transition
    now = datetime(2026, 10, 25, 2, tzinfo=UTC)
    freezer.move_to(now)
    state = clock.authority.state
    assert state is not None
    moving = state.model_copy(update={"committed_mode": "protect", "transition": Transition(
        target="optimise", started_at=now, steps=("apply_plan",),
    )})
    assert await clock.authority.async_save(moving)
    await clock.clock.async_tick(now)
    assert not any(t.op == "write" for t in native_transport.traffic)
    state = clock.authority.state
    assert state is not None
    assert await clock.authority.async_save(state.model_copy(update={"transition": None}))
    await clock.clock.async_tick(now + timedelta(minutes=1))
    assert sum(t.op == "write" for t in native_transport.traffic) == 1
