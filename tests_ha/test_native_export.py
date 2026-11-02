"""Supervised export commands, bound diagnostics and store/snapshot records."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from pydantic import JsonValue

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
        snapshot=lambda: []))
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
    assert [intent.outcome for intent in result.intents] == [
        "unchanged", "applied", "applied"]
    assert result.intents[2].written_a == 62.5
    writes = [(t.address, t.payload) for t in native_transport.traffic if t.op == "write"]
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
