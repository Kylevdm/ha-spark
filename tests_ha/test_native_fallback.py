"""Native fallback: options, causes, verified apply, recovery and restart of Optimise."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import native_write
from custom_components.ha_spark.channel import Outcome
from custom_components.ha_spark.commands import Command
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.soc_trust import BankTrust
from custom_components.ha_spark.solis_frames import Reading
from custom_components.ha_spark.transitions import TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_device import native_entry
from tests_ha.test_topology_flow import (
    STORED_TOPOLOGY,
    choose,
    entry_for,
    open_topology,
)


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


def frame(permission: int = 32, soc: int = 50) -> tuple[int, ...]:
    words = [0] * 21
    words[0] = permission
    words[7] = soc
    return tuple(words)


async def connect_soc(c: SparkCoordinator, now: datetime) -> None:
    inverter = next(iter(c.native.inverters.values()))
    c.native.readings[inverter.id]["input_33132"] = Reading(now, frame(), 0)
    c.native.readings[inverter.id]["holding_43007"] = Reading(now, (190,), 0)
    c.soc.observe(now)
    assert c.soc.all_trusted


@pytest.fixture
async def fallback_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> SparkCoordinator:
    freezer.move_to("2026-11-02T12:00:00Z")

    async def sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    native_transport.set_read_frames()
    native_transport.set_input(33132, frame())
    native_transport.set_holding(43007, [190])
    native_transport.set_holding(43141, [0])
    entry = await native_entry(hass, aioclient_mock)
    hass.config_entries.async_update_entry(entry, options={
        **entry.options,
        "evidence": {"ev_status": {"entity_id": "sensor.ev", "max_age_s": 300}},
        "fallback": {"window_start": "23:30", "window_end": "05:30",
                     "currents": {"inv1": 20}},
    })
    c: SparkCoordinator = entry.runtime_data
    await c.async_refresh()
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    now = datetime.now(UTC)
    c.control.ready = True
    c.control.startup_complete = True
    c.control.grace_until = now - timedelta(seconds=1)
    await connect_soc(c, now)
    native_transport.traffic.clear()
    assert c.control.permits_non_protection(now)
    return c


async def optimise(c: SparkCoordinator) -> None:
    now = datetime.now(UTC)
    zone = ZoneInfo("UTC")
    assert (await c.control.async_request(
        TransitionRequest(target="protect"), now=now, zone=zone)).refusal is None
    result = await c.control.async_request(
        TransitionRequest(target="optimise"), now=now, zone=zone)
    assert result.refusal is None, result.reasons
    state = c.authority.state
    assert state is not None and state.transition is not None
    command = Command(session_id="addon", sequence=1, epoch=state.epoch, kind="apply", payload={
        "intents": [
            {"kind": "charge_current_limit", "inverter_id": "inv1", "current_a": 10},
            {"kind": "charge_window", "inverter_id": "inv1", "current_a": 10,
             "start": "01:00", "end": "03:00"},
        ]})
    assert (await c._async_dispatch_command(command)).outcome in ("applied", "unchanged")
    await c.control.async_retry()
    state = c.authority.state
    assert state is not None and state.committed_mode == "optimise"
    assert state.transition is None


def plan(c: SparkCoordinator, current: float = 12) -> Command:
    state = c.authority.state
    assert state is not None
    return Command(session_id="addon", sequence=2, epoch=state.epoch, kind="apply", payload={
        "intents": [{"kind": "charge_current_limit", "inverter_id": "inv1",
                     "current_a": current}]})


async def clear_liveness(c: SparkCoordinator, now: datetime) -> None:
    c.connected = True
    c.fallback.observe_outcome(Outcome.COUNTED, now)


async def test_options_flow_saves_only_a_validated_schedule(hass: HomeAssistant) -> None:
    entry = entry_for(hass, topology=native_topology())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert "fallback" in result["menu_options"]
    flow_id = result["flow_id"]
    await choose(hass, flow_id, "fallback")
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(flow_id, {
            "window_start": "23:30", "window_end": "05:30", "current_inv1": 20,
        })
        await hass.async_block_till_done()
    assert result["type"].value == "create_entry"
    assert entry.options["fallback"] == {
        "window_start": "23:30", "window_end": "05:30", "currents": {"inv1": 20.0}}


@pytest.mark.parametrize("case", ["inverter", "bank", "supply"])
async def test_options_flow_rejects_a_schedule_over_a_limit(
    hass: HomeAssistant, case: str,
) -> None:
    site = native_topology()
    payload: dict[str, Any] = {
        "window_start": "23:30", "window_end": "05:30", "current_inv1": 20,
    }
    if case == "inverter":
        site["inverters"][0]["charge_limit_a"] = 10
    elif case == "bank":
        site["banks"][0]["charge_limit_a"] = 10
    else:
        site["connections"][0]["import_limit_a"] = 10
        payload["current_inv1"] = 50
    entry = entry_for(hass, topology=site)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    result = await choose(hass, flow_id, "fallback")
    result = await hass.config_entries.options.async_configure(flow_id, payload)
    assert result["type"].value == "form"
    assert result["errors"] == {"base": "invalid_fallback"}


async def test_options_flow_rejects_an_invalid_window(hass: HomeAssistant) -> None:
    entry = entry_for(hass, topology=native_topology())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    choose_result = await choose(hass, flow_id, "fallback")
    assert choose_result["type"].value == "form"
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(flow_id, {
            "window_start": "24:00", "window_end": "05:30", "current_inv1": 20,
        })
    assert "fallback" not in entry.options


async def test_topology_limit_edit_that_invalidates_fallback_is_refused(
    hass: HomeAssistant,
) -> None:
    entry = entry_for(hass, topology=native_topology(), topology_revision=1, fallback={
        "window_start": "23:30", "window_end": "05:30", "currents": {"inv1": 20}})
    flow_id = await open_topology(hass, entry)
    await choose(hass, flow_id, "banks")
    await choose(hass, flow_id, "banks_edit")
    await hass.config_entries.options.async_configure(flow_id, {"id": "bank"})
    result = await hass.config_entries.options.async_configure(
        flow_id, {**STORED_TOPOLOGY["banks"][0], "charge_limit_a": 10})
    assert result["type"].value == "menu"
    await choose(hass, flow_id, "banks_done")
    result = await choose(hass, flow_id, "save")
    assert result["type"].value == "form"
    assert result["errors"] == {"base": "fallback_invalidated"}
    assert entry.options["topology"] == native_topology()


async def test_observe_protect_optimise_and_liveness_fallback(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    state = c.authority.state
    assert state is not None and state.fallback.active
    assert state.fallback.causes == ("addon_liveness",)
    assert c.fallback.in_force()
    assert native_transport.holding[43141] == 200
    assert native_transport.holding[43143] == 23
    assert [native_transport.holding.get(43143 + i, 0) for i in range(8)] == [
        23, 30, 5, 30, 0, 0, 0, 0]
    assert [native_transport.holding.get(43153 + i, 0) for i in range(8)] == [0] * 8

    # Any session's five continuous minutes clear the liveness cause.
    await clear_liveness(c, now)
    freezer.tick(timedelta(minutes=5))
    await c.fallback.async_evaluate(datetime.now(UTC))
    state = c.authority.state
    assert state is not None and state.fallback.active
    assert state.fallback.causes == ()

    # A fresh plan verifies the exit on charge intents only.
    assert (await c._async_dispatch_command(plan(c))).outcome == "applied"
    await c.fallback.async_evaluate(datetime.now(UTC))
    state = c.authority.state
    assert state is not None and not state.fallback.active
    assert state.epoch > 1


async def test_soc_integrity_fallback_and_recovery_window(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = fallback_site
    await optimise(c)
    bank = c.guard.topology.banks[0].id if c.guard is not None else "bank"
    monitor = c.soc.monitors[bank]
    monitor._blocked = True
    monitor.recovery_ready = False
    now = datetime.now(UTC)
    await c.fallback.async_evaluate(now)
    state = c.authority.state
    assert state is not None and state.fallback.active
    assert state.fallback.causes == ("soc_integrity",)
    assert c.fallback.in_force()
    assert native_transport.holding[43141] == 200

    monitor._blocked = True
    monitor.recovery_ready = True
    monitor.view = BankTrust(50.0, now, "trusted", "ok")
    await c.fallback.async_evaluate(now)
    state = c.authority.state
    assert state is not None and state.fallback.causes == ()
    assert (await c._async_dispatch_command(plan(c))).outcome == "applied"
    await c.fallback.async_evaluate(now)
    state = c.authority.state
    assert state is not None and not state.fallback.active


async def test_partial_failure_reapplies_fallback_and_waits_five_minutes(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    await clear_liveness(c, now)
    freezer.tick(timedelta(minutes=5))
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert c.authority.state is not None and c.authority.state.fallback.causes == ()

    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    failed = await c._async_dispatch_command(plan(c))
    assert failed.outcome == "unverified"
    await c.fallback.async_evaluate(datetime.now(UTC))
    state = c.authority.state
    assert state is not None and state.fallback.active
    assert c.fallback.retry_at is not None
    assert c.fallback.in_force()
    assert native_transport.holding[43141] == 200

    freezer.tick(timedelta(minutes=6))
    assert (await c._async_dispatch_command(plan(c))).outcome == "applied"
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert c.authority.state is not None and not c.authority.state.fallback.active


async def test_restoration_during_fallback_reads_the_verified_schedule(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    state = c.authority.state
    assert state is not None and c.fallback.in_force()
    inverter = next(iter(c.native.inverters.values()))
    native_transport.set_holding(43141, [200, 0, 23, 30, 5, 30] + [0] * 24)
    assert c.guard is not None
    async with c.guard.lock:
        async with c.native.lock:
            assert await c.writes.async_restoration_held(inverter, epoch=state.epoch)
    native_transport.set_holding(43141, [200, 0, 22, 30, 5, 30] + [0] * 24)
    async with c.guard.lock:
        async with c.native.lock:
            assert not await c.writes.async_restoration_held(inverter, epoch=state.epoch)


async def test_fallback_entity_reports_in_force(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    await c.hass.async_block_till_done()
    state = c.hass.states.get("binary_sensor.ha_spark_fallback_active")
    assert state is not None and state.state == "on"
    assert state.attributes["causes"] == ["addon_liveness"]
    assert state.attributes["in_force"] is True


def native_topology() -> dict[str, Any]:
    raw = deepcopy(STORED_TOPOLOGY)
    raw["inverters"][0].update(transport="native", gateway_host="gateway.invalid")
    return raw


async def test_fallback_write_needs_an_active_fallback(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = fallback_site
    await optimise(c)
    state = c.authority.state
    assert state is not None and not state.fallback.active
    native_transport.traffic.clear()
    result = await c.writes.async_fallback(
        "inv1", current_a=20, start="23:30", end="05:30", epoch=state.epoch)
    assert result.outcome == "refused"
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_returning_cause_reapplies_fallback_over_an_exit_plan(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    await clear_liveness(c, now)
    freezer.tick(timedelta(minutes=5))
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert (await c._async_dispatch_command(plan(c))).outcome == "applied"
    assert native_transport.holding[43141] == 120
    # The add-on is lost again before the exit is judged.
    c._expire(datetime.now(UTC))
    await c.fallback.async_evaluate(datetime.now(UTC))
    state = c.authority.state
    assert state is not None and state.fallback.causes == ("addon_liveness",)
    assert native_transport.holding[43141] == 200
    assert c.fallback.in_force()


async def test_exit_retry_refuses_plans_for_five_minutes(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    await clear_liveness(c, now)
    freezer.tick(timedelta(minutes=5))
    await c.fallback.async_evaluate(datetime.now(UTC))
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    assert (await c._async_dispatch_command(plan(c))).outcome == "unverified"
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert native_transport.holding[43141] == 200
    freezer.tick(timedelta(minutes=2))
    assert (await c._async_dispatch_command(plan(c))).outcome == "refused"
    assert native_transport.holding[43141] == 200
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert c.authority.state is not None and c.authority.state.fallback.active
    freezer.tick(timedelta(minutes=4))
    assert (await c._async_dispatch_command(plan(c))).outcome == "applied"
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert c.authority.state is not None and not c.authority.state.fallback.active



async def test_restoration_during_fallback_needs_the_verified_fallback_current(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    state = c.authority.state
    assert state is not None and c.fallback.in_force()
    inverter = next(iter(c.native.inverters.values()))
    # An exit plan moved the current while the fallback window stayed resident.
    c.writes.written_a[inverter.id] = 12
    native_transport.set_holding(43141, [120, 0, 23, 30, 5, 30] + [0] * 24)
    assert c.guard is not None
    async with c.guard.lock:
        async with c.native.lock:
            assert not await c.writes.async_restoration_held(inverter, epoch=state.epoch)


@pytest.mark.parametrize(("target", "done"), [("observe", True), ("protect", False)])
async def test_write_fallback_step_accepts_refusals_only_into_observe(
    fallback_site: SparkCoordinator, monkeypatch: pytest.MonkeyPatch, target: str,
    done: bool,
) -> None:
    from custom_components.ha_spark.authority_store import Transition
    from custom_components.ha_spark.commands import IntentResult
    from custom_components.ha_spark.topology import Inverter

    c = fallback_site
    await optimise(c)
    outcomes = iter(["applied", "refused"])
    native = c.fallback._native()
    monkeypatch.setattr(c.fallback, "_native", lambda: native * 2)

    async def apply_one(inverter: Inverter, schedule: object, epoch: int) -> IntentResult:
        return IntentResult(outcome=next(outcomes))  # type: ignore[arg-type]

    monkeypatch.setattr(c.fallback, "_apply_one", apply_one)
    state = c.authority.state
    assert state is not None
    now = datetime.now(UTC)
    state = state.model_copy(update={"transition": Transition(
        target=target, started_at=now, steps=("write_fallback",))})  # type: ignore[arg-type]
    assert await c.fallback.async_step("write_fallback", state, now=now) is done
