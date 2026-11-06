"""Supervised export commands, bound diagnostics and store/snapshot records."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from pydantic import JsonValue
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_spark.commands import Command
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.transitions import Preconditions, TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as current_writer

writer = current_writer
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
EVENT = {"direction": "export", "start": (NOW + timedelta(hours=2)).isoformat(),
         "end": (NOW + timedelta(hours=3)).isoformat()}


@pytest.fixture(autouse=True)
def export_clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)


def command(c: SparkCoordinator, ratings: dict[str, float] | None = None,
            *, user: str | None = None, sequence: int = 1,
            event: dict[str, Any] | None = None,
            kind: Literal["supervise_export", "end_supervision"] = "supervise_export",
            epoch: int | None = None, supervision_id: str = "missing") -> Command:
    state = c.authority.state
    assert state is not None
    payload: dict[str, Any] = {
        "requester_kind": "cli" if user is None else "ingress", "user_id": user,
    }
    if kind == "supervise_export":
        payload.update(event=event or EVENT, ratings_w=ratings or {"inv1": 3000.0})
    else:
        payload.update(supervision_id=supervision_id)
    return Command(session_id="test", sequence=sequence,
                   epoch=state.epoch if epoch is None else epoch, kind=kind, payload=payload)


async def optimise(c: SparkCoordinator) -> None:
    state = c.authority.state
    assert state is not None
    assert await c.authority.async_save(state.model_copy(update={"committed_mode": "optimise"}))


async def test_supervise_export_stores_bound_identity_claim_and_renders_diagnostics(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    await optimise(writer)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(command(writer))
    assert (result.outcome, result.reason) == ("applied", None)
    state = writer.authority.state
    assert state is not None
    [row] = state.export_supervisions
    assert row.rating_w == 3000.0
    assert row.identity == (0x0026, 0x2031, 0x0000)
    assert row.claim.host == "gateway.invalid" and row.claim.unit_id == 1
    assert row.confirming_user_id == "cli" and row.epoch == state.epoch
    assert row.event.end == NOW + timedelta(hours=3)
    snapshot = writer._export_snapshot(state, writer.guard.topology)  # type: ignore[union-attr]
    [entry] = snapshot
    assert entry["bound"] == {"watts": 3000.0, "validated": True, "reason": None}
    supervision = entry["supervision"]
    assert isinstance(supervision, dict)
    assert supervision["id"] == str(row.id)
    assert entry["record"] is None and entry["stop_latch"] is None
    writer.async_update_listeners()
    await writer.hass.async_block_till_done()
    bound = writer.hass.states.get("sensor.solis_inverter_export_bound")
    assert bound is not None and float(bound.state) == 3000.0
    assert not [t for t in native_transport.traffic
                if t.op == "write" and t.address in (43073, 43074)]


@pytest.mark.parametrize(("admin", "system"), [(False, False), (True, True)])
async def test_ingress_supervision_requires_a_real_admin(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, admin: bool, system: bool,
) -> None:
    await optimise(writer)
    monkeypatch.setattr(writer.hass.auth, "async_get_user", AsyncMock(return_value=SimpleNamespace(
        is_admin=admin, system_generated=system, id="user-id")))
    before = writer.authority.state
    result = await writer._async_dispatch_command(command(writer, user="user-id"))
    assert (result.outcome, result.reason) == ("refused", "not_admin")
    assert writer.authority.state == before
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_admin_ingress_supervision_records_the_user(
    writer: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await optimise(writer)
    monkeypatch.setattr(writer.hass.auth, "async_get_user", AsyncMock(return_value=SimpleNamespace(
        is_admin=True, system_generated=False, id="user-id")))
    result = await writer._async_dispatch_command(command(writer, user="user-id"))
    assert result.outcome == "applied"
    state = writer.authority.state
    assert state is not None and state.export_supervisions[0].confirming_user_id == "user-id"


@pytest.mark.parametrize("fence", ["observe", "epoch", "manual", "fallback", "transition"])
async def test_supervise_export_mode_fences_store_nothing(
    writer: SparkCoordinator, monkeypatch: pytest.MonkeyPatch, fence: str,
) -> None:
    from datetime import datetime as dt

    from custom_components.ha_spark.authority_store import FallbackState, ManualSession, Transition
    state = writer.authority.state
    assert state is not None
    await optimise(writer)
    state = writer.authority.state
    assert state is not None
    if fence == "observe":
        await writer.authority.async_save(state.model_copy(update={"committed_mode": "observe"}))
    elif fence == "manual":
        await writer.authority.async_save(state.model_copy(update={"manual_session": ManualSession(
            deadline=None, return_to="optimise", keep_protection=True)}))
    elif fence == "fallback":
        await writer.authority.async_save(state.model_copy(update={"fallback": FallbackState(
            active=True, causes=("addon_liveness",))}))
    elif fence == "transition":
        await writer.authority.async_save(state.model_copy(update={"transition": Transition(
            target="observe", started_at=dt(2026, 11, 2, tzinfo=UTC), steps=())}))
    request = command(writer, epoch=state.epoch + (1 if fence == "epoch" else 0))
    result = await writer._async_dispatch_command(request)
    assert result.outcome == "refused"
    assert result.reason == ("stale_authority" if fence == "epoch" else "mode")
    current = writer.authority.state
    assert current is not None and not current.export_supervisions


@pytest.mark.parametrize(("ratings", "reason"), [
    ({"inv1": 3700.0}, "export_bounds_exceed_site_limit"),
    ({"inv1": 0.0}, "invalid_intent"),
    ({"other": 1000.0}, "invalid_intent"),
])
async def test_supervise_export_ratings_are_refused(
    writer: SparkCoordinator, ratings: dict[str, float], reason: str,
) -> None:
    await optimise(writer)
    result = await writer._async_dispatch_command(command(writer, ratings))
    assert (result.outcome, result.reason) == ("refused", reason)
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


async def test_end_supervision_and_expiry_drop_rows(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43143, [0] * 8)
    await optimise(writer)
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"
    state = writer.authority.state
    assert state is not None
    supervision_id = str(state.export_supervisions[0].id)
    missing = await writer._async_dispatch_command(
        command(writer, kind="end_supervision", sequence=2))
    assert (missing.outcome, missing.reason) == ("refused", "invalid_intent")
    ended = await writer._async_dispatch_command(command(
        writer, kind="end_supervision", sequence=3, supervision_id=supervision_id))
    assert ended.outcome == "applied"
    state = writer.authority.state
    assert state is not None and not state.export_supervisions
    assert (await writer._async_dispatch_command(command(writer, sequence=4))).outcome == "applied"
    await writer.async_expire_supervisions(NOW + timedelta(hours=2, minutes=59))
    state = writer.authority.state
    assert state is not None and len(state.export_supervisions) == 1
    await writer.async_expire_supervisions(NOW + timedelta(hours=3))
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


async def test_leaving_optimise_drops_supervision_in_the_same_save(
    writer: SparkCoordinator,
) -> None:
    await optimise(writer)
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"
    writer.control.preconditions = lambda: Preconditions(True, True, True)
    decision = await writer.control.async_request(
        TransitionRequest(target="observe"), now=NOW, zone=ZoneInfo("UTC"),
    )
    assert decision.refusal is None
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


# --- the supervised export apply path (#144/#148/#143/#161/#178/#218/#536) ----

from custom_components.ha_spark.authority_store import (  # noqa: E402
    EventIdentity,
    ExportRecord,
    ExportWindow,
    HoldDevice,
    HoldRecord,
)
from custom_components.ha_spark.solis_frames import Reading  # noqa: E402


def apply_command(c: SparkCoordinator, intents: list[dict[str, Any]], *, sequence: int = 1,
                  epoch: int | None = None) -> Command:
    state = c.authority.state
    assert state is not None
    return Command(session_id="test", sequence=sequence,
                   epoch=state.epoch if epoch is None else epoch, kind="apply",
                   payload={"intents": [cast(JsonValue, item) for item in intents]})


def window_intent(*, event: dict[str, Any] | None = None, start: datetime | None = None,
                  end: datetime | None = None, **extra: Any) -> dict[str, Any]:
    event = event or EVENT
    start = start or datetime.fromisoformat(event["start"])
    end = end or datetime.fromisoformat(event["end"])
    return {"kind": "export", "inverter_id": "inv1", "request": "window",
            "event": event,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "planned_export_kw": 3.0, "dno_export_limit_kw": 3.6,
            "soc": {"ok": True, "soc_pct": 60.0, "observed_at": NOW.isoformat()},
            **extra}


def charge_intents(current: float = 20.0) -> list[dict[str, Any]]:
    return [
        {"kind": "charge_window", "inverter_id": "inv1", "current_a": current,
         "start": "23:30", "end": "05:30"},
        {"kind": "charge_current_limit", "inverter_id": "inv1", "current_a": current},
    ]


def trusted_evidence(c: SparkCoordinator) -> None:
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "evidence": {
            "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300},
            "live_dispatch": {"entity_id": "binary_sensor.dispatch", "max_age_s": 300},
        }})
    c.hass.states.async_set("sensor.ev", "stopped")
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": []})


def allow_export(c: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
                 transport: FakeSolisDevice, *, clock_offset: timedelta = timedelta(0),
                 power: int = 190) -> None:
    monkeypatch.setattr(c, "soc", SimpleNamespace(
        trusted=lambda bank: True, increases_allowed=lambda bank: True,
        bank=lambda bank: SimpleNamespace(trust="trusted", reason=None),
        snapshot=lambda: [], observe=lambda now: None,
        fallback_requested=False, recovery_ready=False))
    trusted_evidence(c)
    transport.set_holding(43007, [power])
    transport.set_holding(43142, [0])
    transport.set_holding(43143, [0] * 8)
    transport.set_holding(43153, [0] * 8)
    transport.set_holding(43163, [0] * 8)
    face = NOW + clock_offset
    registers = (face.year % 100, face.month, face.day, face.hour, face.minute, face.second)
    transport.set_input(33022, list(registers))
    c.native.readings.setdefault("inv1", {})["input_33022"] = Reading(
        NOW, registers, 0)
    transport.traffic.clear()


def assert_no_export_limit_writes(transport: FakeSolisDevice) -> None:
    assert not [t for t in transport.traffic
                if t.op == "write" and t.address in (43073, 43074)]


def block_writes(transport: FakeSolisDevice) -> list[tuple[int, ...]]:
    return [cast(tuple[int, ...], t.payload) for t in transport.traffic
            if t.op == "write" and t.address == 43143]


async def arm(c: SparkCoordinator, *, event: dict[str, Any] | None = None,
              ratings: dict[str, float] | None = None) -> None:
    await optimise(c)
    result = await c._async_dispatch_command(
        command(c, ratings or {"inv1": 3000.0}, event=event))
    assert result.outcome == "applied"


async def test_a_supervised_window_writes_the_fixed_current_and_one_atomic_block(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.traffic.clear()
    intents = [*charge_intents(), window_intent()]
    result = await writer._async_dispatch_command(apply_command(writer, intents))
    assert result.outcome in ("applied", "unchanged")
    # The grouped window registers and writes its own current before the block
    # (review fix): the later current intent then has nothing left to raise.
    assert [intent.outcome for intent in result.intents] == [
        "unchanged", "unchanged", "applied"]
    assert result.intents[2].written_a == 62.5
    writes = [(t.address, t.payload) for t in native_transport.traffic if t.op == "write"]
    assert writes.index((43141, (200,))) < writes.index((43143, (23, 30, 5, 30, 14, 0, 15, 0)))
    assert (43142, (625,)) in writes
    assert (43143, (23, 30, 5, 30, 14, 0, 15, 0)) in writes
    assert not [t for t in native_transport.traffic if t.op == "write" and t.address != 43141
                and t.address not in (43142, 43143, 43153, 43163)]
    assert_no_export_limit_writes(native_transport)
    state = writer.authority.state
    assert state is not None
    [record] = state.export_records
    assert (record.state, record.cleanup_verified) == ("armed", False)
    assert record.event == EventIdentity(direction="export",
                                         start=NOW + timedelta(hours=2),
                                         end=NOW + timedelta(hours=3))


async def test_a_second_identical_apply_writes_nothing_new(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    intents = [*charge_intents(), window_intent()]
    await writer._async_dispatch_command(apply_command(writer, intents, sequence=2))
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, intents, sequence=3))
    assert result.outcome == "unchanged"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert_no_export_limit_writes(native_transport)


async def test_an_unsupervised_window_is_refused_and_the_discharge_half_cleared(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    result = await writer._async_dispatch_command(
        apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", "not_supervised")
    assert block_writes(native_transport)[-1] == (23, 30, 5, 30, 0, 0, 0, 0)
    assert not [t for t in native_transport.traffic if t.op == "write" and t.address == 43142]
    assert_no_export_limit_writes(native_transport)


@pytest.mark.parametrize(("fence", "reason"), [
    ("clock", "clock_not_trusted"),
    ("power", "power_switch_not_on"),
    ("identity", "export_bound_unverified"),
    ("limits", "limits"),
])
async def test_each_export_refusal_clears_and_zero_confirms_the_discharge_half(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fence: str, reason: str,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    if fence == "clock":
        native_transport.set_input(33022, [26, 11, 2, 11, 0, 0])
        writer.native.readings["inv1"]["input_33022"] = Reading(
            NOW, (26, 11, 2, 11, 0, 0), 0)
    elif fence == "power":
        native_transport.set_holding(43007, [222])
    elif fence == "identity":
        native_transport.set_identity(model=0x27)
    else:
        assert writer.guard is not None
        inverter = writer.guard.topology.inverters[0].model_copy(
            update={"discharge_limit_a": 30})
        writer.guard.topology = writer.guard.topology.model_copy(
            update={"inverters": (inverter,)})
        writer.hass.config_entries.async_update_entry(writer.entry, options={
            **writer.entry.options,
            "topology": writer.guard.topology.model_dump(mode="json")})
        writer.native.inverters[inverter.id] = inverter
        from custom_components.ha_spark.topology import effective_limits
        writer.guard.limits = effective_limits(writer.guard.topology)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", reason)
    assert block_writes(native_transport)[-1] == (23, 30, 5, 30, 0, 0, 0, 0)
    assert not [t for t in native_transport.traffic if t.op == "write" and t.address == 43142]
    assert_no_export_limit_writes(native_transport)


async def test_a_live_record_skips_both_slot_reads_and_writes_for_untrusted(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    intent = await writer._async_dispatch_command(apply_command(
        writer, [*charge_intents(), window_intent()], sequence=2))
    assert intent.outcome in ("applied", "unchanged")
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "charge_current_limit", "inverter_id": "inv1", "current_a": 5.0},
        {"kind": "export", "inverter_id": "inv1", "request": "untrusted"},
    ], sequence=3))
    assert [row.outcome for row in result.intents] == ["unchanged", "unchanged"]
    assert native_transport.traffic == []
    assert_no_export_limit_writes(native_transport)


async def test_an_untrusted_read_without_a_record_is_treated_as_none(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    result = await writer._async_dispatch_command(apply_command(
        writer, [{"kind": "export", "inverter_id": "inv1", "request": "untrusted"}]))
    assert result.outcome == "applied"
    assert block_writes(native_transport)[-1] == (23, 30, 5, 30, 0, 0, 0, 0)


async def test_the_clock_gates_arming_only_not_a_live_record(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    intents = [window_intent()]
    assert (await writer._async_dispatch_command(
        apply_command(writer, intents, sequence=2))).outcome == "applied"
    native_transport.set_input(33022, [26, 11, 2, 11, 0, 0])
    writer.native.readings["inv1"]["input_33022"] = Reading(NOW, (26, 11, 2, 11, 0, 0), 0)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(
        apply_command(writer, intents, sequence=3))
    assert result.outcome == "unchanged"
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_grid_charge_permission_gates_charging_only(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_input(33132, [3])  # bit 5 clear
    native_transport.set_holding(43143, [22, 0, 6, 0, 0, 0, 0, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(
        apply_command(writer, [*charge_intents(), window_intent()]))
    assert (result.intents[0].outcome, result.intents[0].reason) == (
        "refused", "grid_charge_not_permitted")
    assert result.intents[2].outcome == "applied"
    assert block_writes(native_transport)[-1] == (22, 0, 6, 0, 14, 0, 15, 0)


async def test_a_day_early_window_is_deferred_and_writes_no_discharge(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    far = {"direction": "export", "start": (NOW + timedelta(hours=26)).isoformat(),
           "end": (NOW + timedelta(hours=27)).isoformat()}
    await arm(writer, event=far)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=far)]))
    assert result.outcome == "deferred"
    assert block_writes(native_transport) == []
    assert not [t for t in native_transport.traffic if t.op == "write" and t.address == 43142]
    assert_no_export_limit_writes(native_transport)


async def test_a_deferred_window_matching_a_live_record_is_preserved(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    far = {"direction": "export", "start": (NOW + timedelta(hours=26)).isoformat(),
           "end": (NOW + timedelta(hours=27)).isoformat()}
    await arm(writer, event=far)
    state = writer.authority.state
    assert state is not None
    supervision = state.export_supervisions[0]
    event = EventIdentity(direction="export", start=datetime.fromisoformat(far["start"]),
                          end=datetime.fromisoformat(far["end"]))
    record = ExportRecord(
        inverter_id="inv1", event=event,
        window=ExportWindow(start=event.start, end=event.end),
        store_id=state.store_id, epoch=state.epoch, supervision_id=supervision.id,
        state="armed", last_reconcile_at=NOW)
    assert await writer.authority.async_save(state.model_copy(
        update={"export_records": (record,)}))
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=far)]))
    assert result.outcome == "deferred"
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_a_hold_overlapping_the_export_window_refuses_and_clears(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    state = writer.authority.state
    assert state is not None and writer.guard is not None
    inverter = writer.guard.topology.inverters[0]
    record = HoldRecord(cause="ev", started_at=NOW, device=HoldDevice(
        driver="solis_native", power_switch=inverter.id,
        hub=f"{inverter.gateway_host}:{inverter.gateway_port}",
        slave=inverter.gateway_unit_id), confirmed=True)
    assert await writer.authority.async_save(state.model_copy(
        update={"holds": (record,)}))
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", "hold_overlaps_export")
    assert block_writes(native_transport)[-1] == (23, 30, 5, 30, 0, 0, 0, 0)


async def test_untrusted_hold_data_admits_an_exact_resident_window(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    # Remove the dispatch source: configured evidence, but no trusted read.
    writer.hass.config_entries.async_update_entry(writer.entry, options={
        **writer.entry.options, "evidence": {
            "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300}}})
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    # The resident half is this very window: the oracle's exact-match branch proceeds.
    assert result.outcome in ("applied", "unchanged")
    assert block_writes(native_transport) == []


async def test_untrusted_hold_data_keeps_a_live_verified_record(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    intents = [window_intent()]
    assert (await writer._async_dispatch_command(
        apply_command(writer, intents, sequence=2))).outcome == "applied"
    second = {"direction": "export", "start": (NOW + timedelta(hours=5)).isoformat(),
              "end": (NOW + timedelta(hours=6)).isoformat()}
    assert (await writer._async_dispatch_command(
        command(writer, event=second, sequence=3))).outcome == "applied"
    writer.hass.config_entries.async_update_entry(writer.entry, options={
        **writer.entry.options, "evidence": {
            "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300}}})
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=second)], sequence=4))
    assert (result.outcome, result.reason) == ("refused", "hold_data_untrusted")
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_a_failed_zero_clear_keeps_the_record_and_reports_unverified(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))).outcome in ("applied", "unchanged")
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.inject_write_fault(__import__(
        "tests_ha.fake_solis", fromlist=["WriteFault"]).WriteFault("silent_noop"),
        address=43143)
    result = await writer._async_dispatch_command(apply_command(
        writer, [{"kind": "export", "inverter_id": "inv1", "request": "none"}]))
    assert result.outcome == "unverified"
    state = writer.authority.state
    assert state is not None
    assert state.export_records[0].state != "cleared"


async def test_end_supervision_clears_and_marks_the_record(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))).outcome in ("applied", "unchanged")
    state = writer.authority.state
    assert state is not None
    supervision_id = str(state.export_supervisions[0].id)
    native_transport.traffic.clear()
    ended = await writer._async_dispatch_command(command(
        writer, kind="end_supervision", sequence=3, supervision_id=supervision_id))
    assert ended.outcome == "applied"
    state = writer.authority.state
    assert state is not None and not state.export_supervisions
    assert state.export_records[0].state == "cleared"
    assert state.export_records[0].cleanup_verified is True
    assert block_writes(native_transport)[-1] == (0, 0, 0, 0, 0, 0, 0, 0)


async def test_the_native_tick_retries_an_uncleared_non_live_record(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))).outcome in ("applied", "unchanged")
    state = writer.authority.state
    assert state is not None
    # The supervision expires; the record is no longer live.
    assert await writer.authority.async_save(state.model_copy(
        update={"export_supervisions": ()}))
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.traffic.clear()
    await writer.async_retry_export_records(NOW + timedelta(hours=1))
    state = writer.authority.state
    assert state is not None
    assert state.export_records[0].state == "cleared"
    assert block_writes(native_transport)[-1] == (23, 30, 5, 30, 0, 0, 0, 0)


async def test_a_charge_current_change_keeps_the_live_export_half(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#218: a mid-event replan must not zero Slot 1's discharge half."""
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_holding(43142, [625])
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.set_holding(43141, [10])  # resident 1.0 A
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(
        apply_command(writer, [*charge_intents(20.0), window_intent()]))
    assert result.outcome in ("applied", "unchanged")
    writes = block_writes(native_transport)
    assert writes[0] == (0, 0, 0, 0, 14, 0, 15, 0)
    assert all(block[4:] == (14, 0, 15, 0) for block in writes)
    assert not [t for t in native_transport.traffic
                if t.op == "write" and t.address == 43142]


async def test_none_clears_the_discharge_half_under_the_plan_policy(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [{"kind": "export", "inverter_id": "inv1", "request": "none"}]))
    assert result.outcome == "applied"
    assert block_writes(native_transport)[-1] == (0, 0, 0, 0, 0, 0, 0, 0)
    reads = [t for t in native_transport.traffic
             if t.op == "read" and t.address == 43143]
    assert len(reads) >= 7  # first match plus five further matching zero reads


# --- review fixes (#584 astra/Opus review): each fails on the delegate's code ----
from custom_components.ha_spark.authority_store import StopLatch  # noqa: E402


def export_writes(transport: FakeSolisDevice) -> list[tuple[int, tuple[int, ...]]]:
    return [(t.address, cast(tuple[int, ...], t.payload)) for t in transport.traffic
            if t.op == "write" and (t.address == 43142 or (
                t.address == 43143 and any(cast(tuple[int, ...], t.payload)[4:])))]


async def test_supervision_lost_mid_arm_writes_no_export(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    original = writer.writes._bound_reason

    async def revoked(*args: Any, **kwargs: Any) -> Any:
        reason = await original(*args, **kwargs)
        state = writer.authority.state
        assert state is not None
        assert await writer.authority.async_save(state.model_copy(update={
            "export_supervisions": ()}))
        return reason

    monkeypatch.setattr(writer.writes, "_bound_reason", revoked)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert result.intents[0].outcome != "applied"
    assert export_writes(native_transport) == []


async def test_a_stop_latch_refuses_arming(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    state = writer.authority.state
    assert state is not None
    event = EventIdentity(direction="export", start=NOW + timedelta(hours=2),
                          end=NOW + timedelta(hours=3))
    assert await writer.authority.async_save(state.model_copy(update={"stop_latches": (
        StopLatch(inverter_id="inv1", event=event, window_end=event.end, epoch=state.epoch),)}))
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "tripped")
    assert export_writes(native_transport) == []


async def test_arming_is_judged_on_the_household_clock(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    # Fall-back night: 00:50Z is 01:50 BST, inside the local 01:30-02:00 face of a
    # 01:30Z-02:00Z (GMT) window, so programming it now would export at once.
    now = datetime(2026, 10, 25, 0, 50, tzinfo=UTC)
    freezer.move_to(now)
    zone = ZoneInfo("Europe/London")
    monkeypatch.setattr(writer.control, "household_zone", lambda: zone)
    event = {"direction": "export", "start": datetime(2026, 10, 25, 1, 30, tzinfo=UTC).isoformat(),
             "end": datetime(2026, 10, 25, 2, 0, tzinfo=UTC).isoformat()}
    allow_export(writer, monkeypatch, native_transport)
    local = now.astimezone(zone)
    registers = (local.year % 100, local.month, local.day, local.hour, local.minute, 0)
    native_transport.set_input(33022, list(registers))
    writer.native.readings["inv1"]["input_33022"] = Reading(now, registers, 0)
    await arm(writer, event=event)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=event)]))
    assert result.intents[0].outcome != "applied"
    assert export_writes(native_transport) == []


async def test_a_clear_beside_a_charge_half_is_zero_confirmed_on_the_half(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.traffic.clear()
    result = await writer.writes.async_clear_export("inv1", epoch=writer.authority.state.epoch)  # type: ignore[union-attr]
    assert result.outcome == "applied"
    write_at = next(i for i, t in enumerate(native_transport.traffic)
                    if t.op == "write" and t.address == 43143)
    reads = [t for t in native_transport.traffic[write_at:]
             if t.op == "read" and t.address == 43143]
    assert len(reads) >= 6  # first match plus five further reads of the zero half


async def test_a_malformed_export_still_clears_the_discharge_half(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(planned_export_kw=-1.0)]))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "invalid_intent")
    assert block_writes(native_transport)[-1] == (0,) * 8
    state = writer.authority.state
    assert state is not None and state.export_supervisions  # malformed is not a cancel


async def test_untrusted_soc_writes_nothing(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    monkeypatch.setattr(writer.soc, "trusted", lambda bank: False)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [*charge_intents(), window_intent()]))
    assert {(r.outcome, r.reason) for r in result.intents} == {("refused", "untrusted_soc")}
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_a_window_outside_its_event_is_refused(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent(
        start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=1, minutes=30))]))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "invalid_intent")
    assert export_writes(native_transport) == []


async def test_a_trusted_none_ends_the_supervision(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    await writer._async_dispatch_command(apply_command(
        writer, [{"kind": "export", "inverter_id": "inv1", "request": "none"}]))
    state = writer.authority.state
    assert state is not None and not state.export_supervisions
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "not_supervised")
    assert export_writes(native_transport) == []


async def test_a_reprobe_with_a_changed_identity_drops_the_bound(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_input(35000, [0x2032])
    await writer.async_reprobe("inv1")
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


async def test_supervision_waits_for_the_authority_lock(writer: SparkCoordinator) -> None:
    import asyncio

    await optimise(writer)
    async with writer.control.lock:
        task = asyncio.ensure_future(writer._async_dispatch_command(command(writer)))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done()
        state = writer.authority.state
        assert state is not None and not state.export_supervisions
    assert (await task).outcome == "applied"


# --- #585 durable export transition log and live notices ----------------------

def transition_kinds(c: SparkCoordinator) -> list[tuple[str, str | None]]:
    state = c.authority.state
    assert state is not None
    return [(row.kind, row.reason) for row in state.export_transitions]


async def test_accepted_then_armed_then_started_are_logged_once(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(
        apply_command(writer, [window_intent()]))).outcome in ("applied", "unchanged")
    state = writer.authority.state
    assert state is not None
    [accepted, armed] = state.export_transitions
    assert (accepted.kind, accepted.event) == ("accepted", EventIdentity(
        direction="export", start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3)))
    assert (armed.kind, armed.event) == ("armed", accepted.event)
    # A second verified pass before the window adds nothing.
    await writer._async_dispatch_command(apply_command(
        writer, [*charge_intents(), window_intent()], sequence=2))
    assert [row.kind for row in writer.authority.state.export_transitions] == [  # type: ignore[union-attr]
        "accepted", "armed"]
    # The first verified pass after the window begins logs the start.
    later = NOW + timedelta(hours=2, minutes=1)
    freezer.move_to(later)
    registers = (later.year % 100, later.month, later.day, later.hour, later.minute,
                 later.second)
    native_transport.set_input(33022, list(registers))
    writer.native.readings["inv1"]["input_33022"] = Reading(later, registers, 0)
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=3))
    assert result.outcome in ("applied", "unchanged")
    assert [row.kind for row in writer.authority.state.export_transitions] == [  # type: ignore[union-attr]
        "accepted", "armed", "started"]


async def test_a_refusal_logs_a_terminal_abort_with_its_reason(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", "not_supervised")
    state = writer.authority.state
    assert state is not None
    [aborted] = state.export_transitions
    assert (aborted.kind, aborted.reason, aborted.cleared) == (
        "aborted", "not_supervised", True)


async def test_the_clock_gate_is_a_retryable_clock_hold_not_an_abort(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await optimise(writer)
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"
    native_transport.set_input(33022, [26, 11, 2, 11, 0, 0])
    writer.native.readings["inv1"]["input_33022"] = Reading(NOW, (26, 11, 2, 11, 0, 0), 0)
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", "clock_not_trusted")
    state = writer.authority.state
    assert state is not None
    assert [(row.kind, row.reason) for row in state.export_transitions] == [
        ("accepted", None), ("clock_hold", "clock_not_trusted")]


async def test_untrusted_hold_data_and_deferral_never_abort(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await optimise(writer)
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"
    writer.hass.config_entries.async_update_entry(writer.entry, options={
        **writer.entry.options, "evidence": {
            "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300}}})
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", "hold_data_untrusted")
    far = {"direction": "export", "start": (NOW + timedelta(hours=26)).isoformat(),
           "end": (NOW + timedelta(hours=27)).isoformat()}
    assert (await writer._async_dispatch_command(
        command(writer, event=far, sequence=2))).outcome == "applied"
    deferred = await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=far)], sequence=3))
    assert deferred.outcome == "deferred"
    state = writer.authority.state
    assert state is not None
    assert "aborted" not in [row.kind for row in state.export_transitions]


async def test_cleanup_verified_is_logged_only_after_a_verified_zero(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests_ha.fake_solis import WriteFault

    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))).outcome in ("applied", "unchanged")
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    failed = await writer._async_dispatch_command(apply_command(
        writer, [{"kind": "export", "inverter_id": "inv1", "request": "none"}]))
    assert failed.outcome == "unverified"
    kinds = [row.kind for row in writer.authority.state.export_transitions]  # type: ignore[union-attr]
    assert "cleanup_verified" not in kinds
    assert ("aborted", "unverified") in transition_kinds(writer)
    native_transport.advance(1)
    state = writer.authority.state
    assert state is not None
    cleared = await writer.writes.async_clear_export("inv1", epoch=state.epoch)
    assert cleared.outcome == "applied"
    assert "cleanup_verified" in [row.kind for row in writer.authority.state.export_transitions]  # type: ignore[union-attr]


async def test_untrusted_soc_aborts_without_a_read_or_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await optimise(writer)
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"
    monkeypatch.setattr(writer.soc, "trusted", lambda bank: False)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", "untrusted_soc")
    assert native_transport.traffic == []
    state = writer.authority.state
    assert state is not None
    [aborted] = [row for row in state.export_transitions if row.kind == "aborted"]
    assert (aborted.kind, aborted.reason, aborted.cleared) == (
        "aborted", "untrusted_soc", False)


async def test_acknowledged_rows_leave_the_snapshot_and_prune_after_the_event(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(
        apply_command(writer, [window_intent()], sequence=2))).outcome in (
            "applied", "unchanged")
    state = writer.authority.state
    assert state is not None
    ids = tuple(str(row.id) for row in state.export_transitions)
    snapshot = writer._export_snapshot(state, writer.guard.topology)  # type: ignore[union-attr]
    [entry] = snapshot
    transitions = cast(list[dict[str, object]], entry["transitions"])
    assert [row["kind"] for row in transitions] == ["accepted", "armed"]
    await writer._async_ack_export_transitions(ids, NOW)
    state = writer.authority.state
    assert state is not None
    assert all(row.acked for row in state.export_transitions)
    snapshot = writer._export_snapshot(state, writer.guard.topology)  # type: ignore[union-attr]
    assert cast(list[dict[str, object]], snapshot[0]["transitions"]) == []
    await writer.async_expire_supervisions(NOW + timedelta(hours=3))
    state = writer.authority.state
    assert state is not None and state.export_transitions == ()


async def test_only_emitted_transition_ids_are_acknowledged(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    state = writer.authority.state
    assert state is not None
    ids = tuple(str(row.id) for row in state.export_transitions)
    writer.emitted_transitions = frozenset()
    await writer._async_ack_export_transitions(ids, NOW)
    state = writer.authority.state
    assert state is not None
    assert not any(row.acked for row in state.export_transitions)
    writer._export_snapshot(state, writer.guard.topology)  # type: ignore[union-attr]
    await writer._async_ack_export_transitions(ids, NOW)
    assert all(row.acked for row in writer.authority.state.export_transitions)  # type: ignore[union-attr]


async def test_a_cancel_whose_zero_confirm_fails_logs_an_abort(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))).outcome in ("applied", "unchanged")
    from custom_components.ha_spark import native_slots
    from custom_components.ha_spark.native_verify import confirm as real_confirm

    async def zero_never_confirms(target: list[int], *args: object, **kwargs: object) -> bool:
        if len(target) == 8 and not any(target[4:]):
            return False  # the clear verified, the 5-read zero confirm did not
        return await real_confirm(target, *args, **kwargs)  # type: ignore[arg-type]

    # W9-3: the sustained zero confirm lives in the Slot 1 block write.
    monkeypatch.setattr(native_slots, "confirm", zero_never_confirms)
    state = writer.authority.state
    assert state is not None
    result = await writer.writes.async_clear_export("inv1", epoch=state.epoch)
    assert result.outcome == "unverified"
    assert ("aborted", "unverified") in transition_kinds(writer)
    assert "cleanup_verified" not in [kind for kind, _ in transition_kinds(writer)]


async def test_a_clock_hold_whose_clear_fails_stays_retryable(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark import native_slots

    allow_export(writer, monkeypatch, native_transport)
    await optimise(writer)
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"
    native_transport.set_input(33022, [26, 11, 2, 11, 0, 0])
    writer.native.readings["inv1"]["input_33022"] = Reading(NOW, (26, 11, 2, 11, 0, 0), 0)

    async def never(*args: object, **kwargs: object) -> bool:
        return False

    # W9-3: the sustained zero confirm lives in the Slot 1 block write.
    monkeypatch.setattr(native_slots, "confirm", never)
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert result.outcome == "unverified"
    assert [kind for kind, _ in transition_kinds(writer)] == ["accepted", "clock_hold"]


async def test_held_window_retries_verifies_and_matches_notice_fixture(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    from tests_ha.fake_solis import WriteFault

    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143, times=2)
    for sequence in (2, 3):
        result = await writer._async_dispatch_command(apply_command(
            writer, [window_intent()], sequence=sequence))
        assert result.outcome == "unverified"
        assert transition_kinds(writer) == [("accepted", None), ("unverified", "unverified")]
        state = writer.authority.state
        assert state is not None
        assert state.export_transitions[-1].cleared is False
        assert state.export_records[-1].state != "armed"
        native_transport.advance(1)
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=4))
    assert result.outcome in ("applied", "unchanged")
    assert transition_kinds(writer)[-1] == ("armed", None)
    later = NOW + timedelta(hours=2, minutes=1)
    freezer.move_to(later)
    await writer.async_abort_held_exports(later)
    assert "aborted" not in [kind for kind, _ in transition_kinds(writer)]
    trusted_evidence(writer)
    registers = (26, 11, 2, 14, 1, 0)
    native_transport.set_input(33022, list(registers))
    writer.native.readings["inv1"]["input_33022"] = Reading(later, registers, 0)
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=5))
    assert result.outcome in ("applied", "unchanged")
    state = writer.authority.state
    assert state is not None and writer.guard is not None
    snapshot = writer._export_snapshot(state, writer.guard.topology)
    rows = cast(list[dict[str, object]], snapshot[0]["transitions"])
    fixture = json.loads((Path(__file__).parents[1] /
                         "tests/fixtures/contract/export_transitions_held.json").read_text())
    assert [{k: v for k, v in row.items() if k not in ("id", "at")} for row in rows] == [
        {k: v for k, v in row.items() if k not in ("id", "at")} for row in fixture]
    # Flush the Store writes the clock jump left pending (#666 lingering timers).
    async_fire_time_changed(writer.hass, later + timedelta(minutes=1))
    await writer.hass.async_block_till_done()


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_unverified_current_is_held_only_when_cleanup_verifies(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool,
) -> None:
    from tests_ha.fake_solis import WriteFault

    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43142)
    if cleanup_fails:
        native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
        native_transport.inject_write_fault(WriteFault("exception"), address=43143)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()], sequence=2))
    state = writer.authority.state
    assert state is not None
    row = state.export_transitions[-1]
    assert (row.kind, row.reason, row.cleared) == (
        "aborted" if cleanup_fails else "unverified", "unverified", not cleanup_fails)


async def test_held_event_start_aborts_once_without_writing(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests_ha.fake_solis import WriteFault

    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()], sequence=2))
    native_transport.traffic.clear()
    await writer.async_abort_held_exports(NOW + timedelta(hours=2))
    await writer.async_abort_held_exports(NOW + timedelta(hours=2, seconds=30))
    assert transition_kinds(writer) == [
        ("accepted", None), ("unverified", "unverified"), ("aborted", "unverified")]
    assert native_transport.traffic == []


@pytest.mark.parametrize("reason", [
    "invalid_intent", "export_bound_unverified", "hold_overlaps_export", "tripped",
])
async def test_terminal_refusals_abort_immediately_not_held(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    state = writer.authority.state
    assert state is not None and writer.guard is not None
    intent = window_intent()
    if reason == "invalid_intent":
        intent["planned_export_kw"] = -1
    elif reason == "export_bound_unverified":
        native_transport.set_identity(model=0x27)
    elif reason == "hold_overlaps_export":
        inverter = writer.guard.topology.inverters[0]
        hold = HoldRecord(cause="ev", started_at=NOW, device=HoldDevice(
            driver="solis_native", power_switch=inverter.id,
            hub=f"{inverter.gateway_host}:{inverter.gateway_port}",
            slave=inverter.gateway_unit_id), confirmed=True)
        assert await writer.authority.async_save(state.model_copy(update={"holds": (hold,)}))
    else:
        event = EventIdentity.model_validate(EVENT)
        latch = StopLatch(inverter_id="inv1", event=event,
                          window_end=event.end, epoch=state.epoch)
        assert await writer.authority.async_save(state.model_copy(
            update={"stop_latches": (latch,)}))
    await writer._async_dispatch_command(apply_command(writer, [intent], sequence=2))
    assert transition_kinds(writer) == [("accepted", None), ("aborted", reason)]


@pytest.mark.parametrize("after_start", [False, True])
async def test_arm_failure_aborts_at_start_or_on_transport_failure(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, after_start: bool,
) -> None:
    from tests_ha.fake_solis import WriteFault

    # An event already under way, rather than moving the clock (#666 timers).
    event = ({"direction": "export", "start": (NOW - timedelta(minutes=1)).isoformat(),
              "end": (NOW + timedelta(hours=1)).isoformat()} if after_start else EVENT)
    allow_export(writer, monkeypatch, native_transport)
    if after_start:
        trusted_evidence(writer)
    await arm(writer, event=event)
    native_transport.inject_write_fault(
        WriteFault("silent_noop" if after_start else "exception"), address=43143)
    await writer._async_dispatch_command(apply_command(
        writer, [window_intent(event=event)], sequence=2))
    assert transition_kinds(writer) == [
        ("accepted", None), ("aborted", "unverified" if after_start else "failed")]


async def test_unverified_current_with_a_refused_cleanup_aborts(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark import native_slots
    from custom_components.ha_spark.commands import IntentResult
    from tests_ha.fake_solis import WriteFault

    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43142)

    async def refused(self: object) -> IntentResult:
        return IntentResult(outcome="refused", reason="read_only")

    monkeypatch.setattr(native_slots.NativeSlots, "clear_discharge", refused)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()], sequence=2))
    state = writer.authority.state
    assert state is not None
    row = state.export_transitions[-1]
    # Only a verified cleanup keeps the pass retryable; a refused one stays an abort.
    assert (row.kind, row.reason, row.cleared) == ("aborted", "unverified", False)


@pytest.mark.parametrize("rearmed", [True, False])
async def test_a_hold_after_a_verified_arm_is_settled_only_by_a_live_record(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, rearmed: bool,
) -> None:
    from tests_ha.fake_solis import WriteFault

    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    assert (await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=2))).outcome in ("applied", "unchanged")
    native_transport.set_holding(43143, [0] * 8)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    assert (await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], sequence=3))).outcome == "unverified"
    assert [kind for kind, _ in transition_kinds(writer)] == [
        "accepted", "armed", "unverified"]
    native_transport.advance(1)
    if rearmed:
        # The re-arm verifies; `armed` is logged once per event, so the record decides.
        assert (await writer._async_dispatch_command(apply_command(
            writer, [window_intent()], sequence=4))).outcome in ("applied", "unchanged")
    await writer.async_abort_held_exports(NOW + timedelta(hours=2))
    kinds = [kind for kind, _ in transition_kinds(writer)]
    assert ("aborted" in kinds) is not rearmed


async def test_an_acked_hold_past_its_event_still_aborts(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests_ha.fake_solis import WriteFault

    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()], sequence=2))
    state = writer.authority.state
    assert state is not None
    ids = tuple(str(row.id) for row in state.export_transitions)
    writer._export_snapshot(state, writer.guard.topology)  # type: ignore[union-attr]
    await writer._async_ack_export_transitions(ids, NOW)
    # HA was down across the whole event: setup's sweep runs before any prune.
    await writer.async_abort_held_exports(NOW + timedelta(hours=4))
    assert ("aborted", "unverified") in transition_kinds(writer)
