"""Native entity writes through the one charge-write boundary (#599)."""

from datetime import UTC, datetime, time

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ha_spark.authority_store import FallbackState, Transition
from custom_components.ha_spark.commands import IntentResult
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.number import NativeCurrent
from custom_components.ha_spark.time import NativeTime
from custom_components.ha_spark.topology import InverterLimits, Topology, validate_topology
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write_entities import (
    _set_current_limit,
    mode,
)
from tests_ha.test_native_write_entities import (
    writer as writer_fixture,
)

writer = writer_fixture


@pytest.fixture(autouse=True)
def frozen_clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-11-02T12:00:00Z")


async def _async_entity(
    c: SparkCoordinator, control: str, value: float | list[int], *, offset: int | None = None,
) -> IntentResult:
    state = c.authority.state
    assert state is not None
    inverter_id = next(iter(c.native.inverters))
    return await c.writes.async_entity(
        inverter_id, control, value, epoch=state.epoch, offset=offset)


# --- Gap 1: topology / limits mismatch --------------------------------------

async def test_topology_mismatch_refuses_charge_and_time_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """Options topology changes but guard topology does not -> limits refusal."""
    await mode(writer, "manual", False)
    c = writer
    assert c.guard is not None
    site = c.guard.topology
    inverter = site.inverters[0].model_copy(update={
        "charge_limit_a": site.inverters[0].charge_limit_a - 1, "confirmed": True})
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options,
        "topology": site.model_copy(update={"inverters": (inverter,)}).model_dump(mode="json"),
    })
    drifted = validate_topology(c.entry.options["topology"])
    assert isinstance(drifted, Topology) and drifted != c.guard.topology
    before = dict(c.guard.last_requested_a)
    native_transport.traffic.clear()

    result = await _async_entity(c, "charge", 20)
    assert result.outcome == "refused"
    assert result.reason == "limits"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert c.guard.last_requested_a == before

    result = await _async_entity(c, "charge", [22, 15], offset=0)
    assert result.outcome == "refused"
    assert result.reason == "limits"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert c.guard.last_requested_a == before


async def test_topology_mismatch_refuses_discharge_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """Discharge current also fails limits when options topology drifts."""
    await mode(writer, "manual", False)
    c = writer
    assert c.guard is not None
    site = c.guard.topology
    inverter = site.inverters[0].model_copy(update={
        "discharge_limit_a": site.inverters[0].discharge_limit_a - 1, "confirmed": True})
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options,
        "topology": site.model_copy(update={"inverters": (inverter,)}).model_dump(mode="json"),
    })
    drifted = validate_topology(c.entry.options["topology"])
    assert isinstance(drifted, Topology) and drifted != c.guard.topology
    before = dict(c.guard.last_requested_a)
    native_transport.traffic.clear()

    result = await _async_entity(c, "discharge", 20)
    assert result.outcome == "refused"
    assert result.reason == "limits"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert c.guard.last_requested_a == before


async def test_guard_limits_mismatch_refuses_all_entity_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """guard.limits replaced by a stale EffectiveLimits -> limits refusal."""
    await mode(writer, "manual", False)
    c = writer
    assert c.guard is not None
    limits = c.guard.limits
    inv = InverterLimits(
        id=limits.inverters[0].id, bank_id=limits.inverters[0].bank_id,
        charge_a=limits.inverters[0].charge_a + 1,
        discharge_a=limits.inverters[0].discharge_a,
    )
    c.guard.limits = limits.model_copy(update={"inverters": (inv,)})
    before = dict(c.guard.last_requested_a)
    native_transport.traffic.clear()

    for control, value, offset in [
        ("charge", 20, None),
        ("discharge", 20, None),
        ("charge", [22, 15], 0),
    ]:
        result = await _async_entity(c, control, value, offset=offset)  # type: ignore[arg-type]
        assert result.outcome == "refused"
        assert result.reason == "limits"
        assert not [t for t in native_transport.traffic if t.op == "write"]
        assert c.guard.last_requested_a == before


# --- Gap 2: fallback and manual gates ---------------------------------------

async def test_fallback_in_optimise_refuses_entity_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """Active fallback with no manual session blocks owner entity edits."""
    await mode(writer, "optimise")
    c = writer
    state = c.authority.state
    assert state is not None
    now = datetime.now(UTC)
    await c.authority.async_save(state.model_copy(update={
        "fallback": FallbackState(active=True, causes=("addon_liveness",), entered_at=now)}))
    native_transport.traffic.clear()

    for control, value, offset in [
        ("charge", 20, None),
        ("discharge", 20, None),
        ("charge", [22, 15], 0),
    ]:
        result = await _async_entity(c, control, value, offset=offset)  # type: ignore[arg-type]
        assert result.outcome == "refused"
        assert result.reason == "mode"
        assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_active_fallback_alone_refuses_charge_edits_outside_optimise(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """The fallback gate stands on its own: Protect with a live fallback refuses."""
    await mode(writer, "protect")
    c = writer
    state = c.authority.state
    assert state is not None
    assert await c.authority.async_save(state.model_copy(update={
        "fallback": FallbackState(active=True, causes=("addon_liveness",),
                                  entered_at=datetime.now(UTC))}))
    native_transport.traffic.clear()

    cases: list[tuple[float | list[int], int | None]] = [(20, None), ([22, 15], 0)]
    for value, offset in cases:
        result = await _async_entity(c, "charge", value, offset=offset)
        assert result.outcome == "refused"
        assert result.reason == "mode"
    assert not [t for t in native_transport.traffic if t.op == "write"]


@pytest.mark.parametrize("keep_protection", [True, False])
async def test_manual_session_allows_writes_during_fallback(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    keep_protection: bool,
) -> None:
    """Manual control bypasses the fallback gate for charge/discharge/time."""
    await mode(writer, "manual", keep_protection)
    c = writer
    state = c.authority.state
    assert state is not None
    now = datetime.now(UTC)
    await c.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise",
        "fallback": FallbackState(active=True, causes=("addon_liveness",), entered_at=now)}))
    native_transport.set_holding(43141, [0])
    native_transport.set_holding(43142, [0])
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.traffic.clear()

    assert (await _async_entity(c, "charge", 15)).outcome == "applied"
    assert native_transport.holding[43141] == 150

    assert (await _async_entity(c, "discharge", 25)).outcome == "applied"
    assert native_transport.holding[43142] == 250

    result = await _async_entity(c, "charge", [22, 15], offset=0)
    assert result.outcome == "applied"
    assert [native_transport.holding[a] for a in range(43143, 43151)] == [
        22, 15, 5, 30, 0, 0, 0, 0]


async def test_fallback_manual_with_pending_transition_refuses(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """A pending transition beats the manual-session fallback allowance."""
    await mode(writer, "manual", False)
    c = writer
    state = c.authority.state
    assert state is not None
    now = datetime.now(UTC)
    await c.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise",
        "fallback": FallbackState(active=True, causes=("addon_liveness",), entered_at=now),
        "transition": Transition(target="observe", started_at=now, steps=())}))
    native_transport.traffic.clear()

    result = await _async_entity(c, "charge", 20)
    assert result.outcome == "refused"
    assert result.reason == "transition_in_progress"
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_modbus_hub_conflict_refuses_entity_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#647: a loaded modbus hub on the gateway is refused as modbus_hub_conflict."""
    await mode(writer, "manual", False)
    c = writer
    monkeypatch.setattr(c.native, "write_reason", lambda inv: "modbus_hub_conflict")
    native_transport.traffic.clear()

    result = await _async_entity(c, "charge", 20)
    assert result.outcome == "refused"
    assert result.reason == "modbus_hub_conflict"
    assert not [t for t in native_transport.traffic if t.op == "write"]


# --- Gap 3: zero discharge limit clamp --------------------------------------

async def test_zero_discharge_limit_clamps_owner_request_to_zero(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """A 0 A discharge limit forces the written value to 0, never the request."""
    await mode(writer, "manual", False)
    c = writer
    native_transport.set_holding(43142, [50])
    native_transport.traffic.clear()
    _set_current_limit(c, "discharge", 0)

    result = await _async_entity(c, "discharge", 20)
    assert result.outcome == "applied"
    writes = [(t.address, t.payload) for t in native_transport.traffic
              if t.op == "write"]
    assert writes == [(43142, (0,))]
    assert native_transport.holding[43142] == 0


async def test_unconfirmed_discharge_limit_clamps_to_zero(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """An unconfirmed limit is treated as 0 by effective_limits."""
    from custom_components.ha_spark.topology import effective_limits

    await mode(writer, "manual", False)
    c = writer
    native_transport.set_holding(43142, [50])
    native_transport.traffic.clear()
    _set_current_limit(c, "discharge", 100)
    # effective_limits makes unconfirmed -> 0; keep options and guard aligned.
    assert c.guard is not None
    site = c.guard.topology
    inverter = site.inverters[0].model_copy(update={"confirmed": False})
    new_site = site.model_copy(update={"inverters": (inverter,)})
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "topology": new_site.model_dump(mode="json"),
    })
    c.guard.topology = new_site
    c.guard.limits = effective_limits(new_site)

    result = await _async_entity(c, "discharge", 20)
    assert result.outcome == "applied"
    writes = [(t.address, t.payload) for t in native_transport.traffic
              if t.op == "write"]
    assert writes == [(43142, (0,))]
    assert native_transport.holding[43142] == 0


# --- Gap 4: 43143 read-back failure -----------------------------------------

async def test_time_edit_silent_noop_is_unverified_not_aborted(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 43143 write that doesn't read back is unverified; publish is skipped."""
    await mode(writer, "observe")
    c = writer
    inverter = next(iter(c.native.inverters.values()))
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.traffic.clear()
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)

    reading = c.native.readings[inverter.id].get("holding_43141")
    assert reading is not None and reading.registers is not None
    slot_words_before = list(reading.registers)[2:10]

    result = await _async_entity(c, "charge", [22, 15], offset=0)
    assert result.outcome == "unverified"
    assert [t.address for t in native_transport.traffic if t.op == "write"] == [43143]
    assert [native_transport.holding[a] for a in range(43143, 43151)] == [
        23, 30, 5, 30, 0, 0, 0, 0]
    reading = c.native.readings[inverter.id].get("holding_43141")
    assert reading is not None and reading.registers is not None
    assert list(reading.registers)[2:10] == slot_words_before


async def test_time_edit_read_failure_is_failed(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 43143 pre-read failure returns failed, not unverified."""
    from custom_components.ha_spark.solis_transport import ReadResult, TransportError

    await mode(writer, "observe")
    c = writer
    inverter = next(iter(c.native.inverters.values()))
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.traffic.clear()

    original = native_transport.read_holding

    async def read(address: int, count: int, *, device_id: int = 1) -> ReadResult:
        if address == 43143:
            return ReadResult(error=TransportError.EXCEPTION)
        return await original(address, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_holding", read)

    result = await _async_entity(c, "charge", [22, 15], offset=0)
    assert result.outcome == "failed"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    entity = NativeTime(c, inverter, 0)
    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_set_value(time(22, 15))
    assert not isinstance(raised.value, ServiceValidationError)


# --- Per-module coverage -----------------------------------------------------

async def test_entity_classes_raise_on_refusal_and_failure(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """NativeCurrent/NativeTime map outcomes to the expected exception types."""
    await mode(writer, "optimise")
    c = writer
    inverter = next(iter(c.native.inverters.values()))
    native_transport.traffic.clear()

    with pytest.raises(ServiceValidationError):
        await NativeCurrent(c, inverter, False).async_set_native_value(20)
    assert not [t for t in native_transport.traffic if t.op == "write"]

    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    await mode(writer, "observe")
    with pytest.raises(HomeAssistantError) as raised:
        await NativeCurrent(c, inverter, False).async_set_native_value(20)
    assert not isinstance(raised.value, ServiceValidationError)
    assert [t.address for t in native_transport.traffic if t.op == "write"] == [43141]
