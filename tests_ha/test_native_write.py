"""Current-limit commands reach only verified, clamped fake registers."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import native_write
from custom_components.ha_spark.commands import Command
from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_device import native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.fixture
async def writer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> SparkCoordinator:
    async def sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    entry = await native_entry(
        hass, aioclient_mock, two_units=bool(getattr(request, "param", False)),
    )
    coordinator: SparkCoordinator = entry.runtime_data
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise",
    }))
    monkeypatch.setattr(coordinator, "soc", SimpleNamespace(
        increases_allowed=lambda bank: True), raising=False)
    assert coordinator.guard is not None
    monkeypatch.setattr(coordinator.guard, "allowance_for", lambda bank: None)
    native_transport.set_input(33132, [32])
    native_transport.set_holding(43141, [100])
    native_transport.traffic.clear()
    return coordinator


def command(writer: SparkCoordinator, current: float = 20.9, **extra: Any) -> Command:
    assert writer.guard is not None
    state = writer.authority.state
    return Command(session_id="test", sequence=1, epoch=state.epoch if state else 1, kind="apply",
                   payload={"intents": [{"kind": "charge_current_limit",
                                          "inverter_id": writer.guard.topology.inverters[0].id,
                                          "current_a": current, **extra}]})


@pytest.mark.parametrize(("fault", "expected"), [
    (None, "applied"), ("silent_noop", "unverified"),
    ("exception", "failed"), ("timeout", "failed"),
    ("delayed_apply", "applied"),
])
async def test_current_result(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    fault: str | None, expected: str,
) -> None:
    if fault:
        native_transport.inject_write_fault(WriteFault(fault, delay_calls=2), address=43141)
    result = await writer._async_dispatch_command(command(writer))
    assert result.outcome == expected
    assert result.intents[0].outcome == expected
    assert result.intents[0].written_a == (20 if expected in ("applied", "unverified") else None)
    assert all(t.address == 43141 for t in native_transport.traffic if t.op == "write")
    if expected == "applied":
        result = await writer._async_dispatch_command(command(writer))
        assert result.outcome == "unchanged"


@pytest.mark.parametrize("fence", [
    "observe", "protect", "unknown", "save_failed", "pending", "read_only", "epoch",
    "manual", "fallback", "transition", "soc", "permission", "exemption", "no_guard",
    "guard_save_failed",
])
async def test_refusals_never_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fence: str,
) -> None:
    request = command(writer)
    state = writer.authority.state
    assert state is not None
    reason = "mode"
    if fence in ("observe", "protect"):
        await writer.authority.async_save(state.model_copy(update={"committed_mode": fence}))
    elif fence in ("unknown", "save_failed"):
        monkeypatch.setattr(writer.authority, "status", fence)
        reason = "authority_unknown"
    elif fence == "pending":
        monkeypatch.setattr(writer.control, "owned", lambda: False)
        reason = "gateway_not_owned"
    elif fence == "read_only":
        native_transport.set_identity(model=0x27)
        await writer.async_reprobe(next(iter(writer.native.inverters)))
        reason = "read_only"
    elif fence == "epoch":
        request = request.model_copy(update={"epoch": state.epoch + 1})
        reason = "stale_epoch"
    elif fence == "manual":
        from custom_components.ha_spark.authority_store import ManualSession

        await writer.authority.async_save(state.model_copy(update={"manual_session": ManualSession(
            deadline=None, return_to="optimise", keep_protection=True)}))
    elif fence == "fallback":
        await writer.authority.async_save(state.model_copy(update={
            "fallback": state.fallback.model_copy(update={"active": True})}))
    elif fence == "transition":
        from datetime import UTC, datetime

        from custom_components.ha_spark.authority_store import Transition
        await writer.authority.async_save(state.model_copy(update={"transition": Transition(
            target="observe", started_at=datetime(2026, 10, 31, tzinfo=UTC), steps=())}))
    elif fence == "soc":
        monkeypatch.delattr(writer, "soc")
        reason = "untrusted_soc"
    elif fence == "permission":
        native_transport.set_input(33132, [0])
        reason = "grid_charge_not_permitted"
    elif fence == "exemption":
        request = command(writer, soc_exempt=True)
        reason = "invalid_intent"
    elif fence == "no_guard":
        writer.guard = None
        reason = "limits"
    elif fence == "guard_save_failed":
        assert writer.guard is not None
        monkeypatch.setattr(writer.guard, "_save_failed", True)
        reason = "limits"
    result = await writer._async_dispatch_command(request)
    assert result.outcome == "refused"
    assert result.reason == reason
    assert not [t for t in native_transport.traffic if t.op == "write"]


@pytest.mark.parametrize("current", [0.0, 5.9, 10.0])
async def test_soc_not_required_for_reductions_or_unchanged(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, current: float,
) -> None:
    monkeypatch.delattr(writer, "soc")
    result = await writer._async_dispatch_command(command(writer, current))
    assert result.outcome == ("unchanged" if current == 10 else "applied")
    assert result.intents[0].written_a == int(current)
    reads = [t for t in native_transport.traffic if t.op == "read" and t.address == 43141]
    assert len(reads) == (7 if current == 0 else 2)


async def test_resident_zero_still_needs_six_matching_readbacks(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43141, [0])
    result = await writer._async_dispatch_command(command(writer, 0))
    assert result.outcome == "unchanged"
    assert result.intents[0].written_a == 0
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert sum(t.address == 43141 for t in native_transport.traffic) == 7


@pytest.mark.parametrize("spurious", [False, True])
async def test_zero_confirmation_and_one_probe_on_mismatch(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, spurious: bool,
) -> None:
    native_transport.drift[43141] = 10
    sleeps = 0

    async def sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if spurious and sleeps == 1:
            native_transport.spurious_zeros.add(("holding", 43141, 1))

    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    result = await writer._async_dispatch_command(command(writer, 0))
    assert result.outcome == "unverified"
    assert not writer.writes.written_a
    reads = [t for t in native_transport.traffic if t.op == "read" and t.address == 43141]
    assert len(reads) == (3 if spurious else 4)
    assert sum(t.address == 35000 for t in native_transport.traffic) == 7


async def test_caching_after_mismatch_fences_next_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.caching = True
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    assert (await writer._async_dispatch_command(command(writer))).outcome == "unverified"
    writes = sum(t.op == "write" for t in native_transport.traffic)
    result = await writer._async_dispatch_command(command(writer))
    assert result.reason == "read_only"
    assert sum(t.op == "write" for t in native_transport.traffic) == writes


async def test_clamp_and_intent_isolation(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    request = command(writer, 62.5)
    request.payload = {"intents": [{"kind": "charge_window"},
                                   {"kind": "charge_current_limit", "current_a": -1},
                                   request.payload["intents"][0]]}  # type: ignore[index]
    result = await writer._async_dispatch_command(request)
    assert result.outcome == "refused"
    assert [i.outcome for i in result.intents] == ["refused", "refused", "applied"]
    assert result.intents[-1].written_a == 62  # 62.5 A driver ceiling, whole amps
    assert (await writer._async_dispatch_command(request.model_copy(
        update={"payload": {"intents": []}}))).outcome == "refused"


async def test_barrier_waits_for_verification(
    writer: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def sleep(seconds: float) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    task = asyncio.create_task(writer._async_dispatch_command(command(writer)))
    await started.wait()
    barrier = asyncio.create_task(writer.writes.async_barrier())
    await asyncio.sleep(0)
    assert not barrier.done()
    release.set()
    assert (await task).outcome == "applied"
    await barrier


async def test_authority_rechecked_after_device_reads(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = native_transport.read_input

    async def revoke(*args: Any, **kwargs: Any) -> Any:
        result = await read(*args, **kwargs)
        monkeypatch.setattr(writer.authority, "status", "save_failed")
        return result

    monkeypatch.setattr(native_transport, "read_input", AsyncMock(side_effect=revoke))
    result = await writer._async_dispatch_command(command(writer))
    assert result.reason == "authority_unknown"
    assert not [t for t in native_transport.traffic if t.op == "write"]


@pytest.mark.parametrize("writer", [True], indirect=True)
@pytest.mark.parametrize("fail_reduction", [False, True])
async def test_peers_reclamped_reductions_before_increases(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fail_reduction: bool,
) -> None:
    assert writer.guard is not None
    second = FakeSolisDevice()
    second.set_identity()
    second.set_input(33132, [32])
    second.set_holding(43141, [0])
    second.traffic = native_transport.traffic
    monkeypatch.setattr(writer.native, "transport", lambda inv: (
        native_transport if inv.gateway_unit_id == 1 else second))
    await writer.async_reprobe("inv2")
    # At 50 V and eta .949, this admits 40 whole DC amps in total.
    monkeypatch.setattr(writer.guard, "allowance_for", lambda bank: 2.01 / 0.949)
    first = await writer._async_dispatch_command(command(writer, 40))
    assert first.outcome == "applied"
    assert first.intents[0].written_a == 40
    native_transport.traffic.clear()
    if fail_reduction:
        native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    request = command(writer, 40)
    request.payload = {"intents": [{"kind": "charge_current_limit",
                                   "inverter_id": "inv2", "current_a": 40.0}]}
    result = await writer._async_dispatch_command(request)
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert [(t.device_id, t.payload) for t in writes] == (
        [(1, (200,))] if fail_reduction else [(1, (200,)), (2, (200,))])
    assert result.outcome == ("unverified" if fail_reduction else "applied")
    if fail_reduction:
        # inv2 was never attempted: the peer's 20 A is not attributed to it.
        assert result.intents[0].written_a is None
        assert writer.guard.last_requested_a.get("inv2") is None
    assert not any(t.address != 43141 for t in writes)
    if not fail_reduction:
        native_transport.traffic.clear()
        result = await writer._async_dispatch_command(command(writer, 10))
        assert result.intents[0].written_a == 8  # requested device, not its increasing peer
        writes = [t for t in native_transport.traffic if t.op == "write"]
        assert [(t.device_id, t.payload) for t in writes] == [(1, (80,)), (2, (320,))]


@pytest.mark.parametrize("confirmed", [True, False])
async def test_effective_limit_narrows_and_unconfirmed_is_zero(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, confirmed: bool,
) -> None:
    from custom_components.ha_spark.topology import effective_limits

    assert writer.guard is not None
    site = writer.guard.topology
    inv = site.inverters[0].model_copy(update={"charge_limit_a": 15.5, "confirmed": confirmed})
    site = site.model_copy(update={"inverters": (inv,)})
    writer.hass.config_entries.async_update_entry(writer.entry, options={
        **writer.entry.options, "topology": site.model_dump(mode="json"),
    })
    # Keep this transaction local; a production options save reloads the entry.
    writer.guard.topology = site
    writer.guard.limits = effective_limits(site)
    writer.native.inverters[inv.id] = inv
    result = await writer._async_dispatch_command(command(writer, 62.5))
    assert result.outcome == "applied"
    assert result.intents[0].written_a == (15 if confirmed else 0)
    await writer.hass.async_block_till_done()


async def test_unload_start_fences_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    writer.async_stop()
    result = await writer._async_dispatch_command(command(writer))
    assert (result.outcome, result.reason) == ("refused", "read_only")
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_stopped_guard_fences_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert writer.guard is not None
    monkeypatch.setattr(writer.guard, "_active", False)
    result = await writer._async_dispatch_command(command(writer))
    assert (result.outcome, result.reason) == ("refused", "limits")
    assert not [t for t in native_transport.traffic if t.op == "write"]


@pytest.mark.parametrize("allowed", [False, True])
async def test_real_or_false_soc_refuses_increase(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, allowed: bool,
) -> None:
    banks: list[str] = []

    def increases_allowed(bank: str) -> bool:
        banks.append(bank)
        return allowed

    monkeypatch.setattr(writer, "soc", SimpleNamespace(increases_allowed=increases_allowed))
    result = await writer._async_dispatch_command(command(writer, 20))
    assert result.outcome == ("applied" if allowed else "refused")
    assert set(banks) == {writer.native.inverters["inv1"].bank_id}
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert len(writes) == (1 if allowed else 0)
    if not allowed:
        assert result.reason == "untrusted_soc"
        assert writer.guard is not None
        assert "inv1" not in writer.guard.last_requested_a  # never a latent peer request


async def test_exception_after_mismatch_still_probes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_transport.drift[43141] = 10
    calls = 0

    async def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("bus broke")

    probes: list[str] = []

    async def mismatch(inverter_id: str) -> None:
        probes.append(inverter_id)

    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    monkeypatch.setattr(writer.native, "async_readback_mismatch", mismatch)
    result = await writer._async_dispatch_command(command(writer))
    assert result.outcome == "failed"
    assert probes == ["inv1"]
    assert not writer.writes.written_a


async def test_increase_judged_against_resident_read_last(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_transport.set_holding(43141, [300])
    read = native_transport.read_input

    async def drop(*args: Any, **kwargs: Any) -> Any:
        result = await read(*args, **kwargs)
        native_transport.set_holding(43141, [50])  # changed by another controller
        return result

    monkeypatch.setattr(native_transport, "read_input", AsyncMock(side_effect=drop))
    monkeypatch.setattr(writer, "soc", SimpleNamespace(increases_allowed=lambda bank: False))
    result = await writer._async_dispatch_command(command(writer, 20))
    assert (result.outcome, result.reason) == ("refused", "untrusted_soc")
    assert not [t for t in native_transport.traffic if t.op == "write"]
