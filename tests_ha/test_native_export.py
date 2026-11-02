"""Supervised export commands, bound diagnostics and store/snapshot records."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory

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
    writer: SparkCoordinator,
) -> None:
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
