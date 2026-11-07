"""AuthorityControl transition execution and helper seams (#599)."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import authority
from custom_components.ha_spark.authority_store import (
    AuthorityState,
    EventIdentity,
    ExportSupervision,
    FallbackState,
    GatewayClaim,
)
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.transitions import Preconditions, TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_device import native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


def _state(c: SparkCoordinator) -> AuthorityState:
    state = c.authority.state
    assert state is not None
    return state


class FakeTransitionWriter:
    """Records steps and returns configurable results."""

    def __init__(self, steps: dict[str, bool] | None = None,
                 barrier_raises: bool = False,
                 startup_failures: set[str] | None = None) -> None:
        self.steps: dict[str, bool] = steps or {}
        self.barrier_raises = barrier_raises
        self.startup_failures = startup_failures or set()
        self.called: list[str] = []
        self.startup_called: list[str] = []

    async def async_barrier(self) -> None:
        if self.barrier_raises:
            raise RuntimeError("barrier")

    async def async_step(self, step: str, state: AuthorityState, *,
                         removal: bool = False) -> bool:
        self.called.append(step)
        return self.steps.get(step, False)

    async def async_startup(self, stage: str) -> bool:
        self.startup_called.append(stage)
        return stage not in self.startup_failures


@pytest.fixture
async def control(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                  native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
                  freezer: FrozenDateTimeFactory,
                  ) -> SparkCoordinator:
    freezer.move_to("2026-11-02T12:00:00Z")
    entry = await native_entry(hass, aioclient_mock)
    c: SparkCoordinator = entry.runtime_data
    c.control.ready = True
    c.control.startup_complete = True
    c.control.grace_until = dt_util.utcnow() - timedelta(seconds=1)
    c.control.preconditions = lambda: Preconditions(
        devices_validated=True, evidence_configured=True, limits_confirmed=True,
        fallback_valid=True, live_session=True, soc_trusted=True, soc_recovered=True)
    return c


async def test_request_refuses_not_owner(
    control: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an integration-owned site may request transitions."""
    native_transport.traffic.clear()
    monkeypatch.setattr(authority, "authority_owner", lambda entry: "addon")
    result = await control.control.async_request(
        TransitionRequest(target="protect", expected_epoch=_state(control).epoch),
        now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert result.refusal == "not_owner"
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_barrier_raise_returns_transition_in_progress(
    control: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing write barrier is reported as transition_in_progress."""
    native_transport.traffic.clear()
    before = _state(control)
    control.control.writer = FakeTransitionWriter(barrier_raises=True)
    result = await control.control.async_request(
        TransitionRequest(target="protect", expected_epoch=before.epoch),
        now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert result.refusal == "transition_in_progress"
    assert control.authority.state == before
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_failed_store_save_returns_authority_unknown(
    control: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save that doesn't verify reload leaves authority fenced."""
    native_transport.traffic.clear()
    monkeypatch.setattr(control.authority, "async_save", AsyncMock(return_value=False))
    result = await control.control.async_request(
        TransitionRequest(target="protect", expected_epoch=_state(control).epoch),
        now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert result.refusal == "authority_unknown"
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_failing_step_keeps_step_and_target_uncommitted(
    control: SparkCoordinator,
) -> None:
    """A step returning False records the failing action and never commits."""
    control.control.writer = FakeTransitionWriter(steps={"evaluate_protection": False})
    result = await control.control.async_request(
        TransitionRequest(target="protect", expected_epoch=_state(control).epoch),
        now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert result.refusal is None
    state = control.authority.state
    assert state is not None
    assert state.transition is not None
    assert state.transition.steps == ("evaluate_protection",)
    assert state.transition.failing_actions == ("evaluate_protection",)
    assert state.committed_mode == "protect"


async def test_all_steps_true_commits_target_and_clears_transition(
    control: SparkCoordinator,
) -> None:
    """Every step passing commits the transition target."""
    control.control.writer = FakeTransitionWriter(steps={"evaluate_protection": True})
    result = await control.control.async_request(
        TransitionRequest(target="protect", expected_epoch=_state(control).epoch),
        now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert result.refusal is None
    state = control.authority.state
    assert state is not None
    assert state.committed_mode == "protect"
    assert state.transition is None


async def test_leaving_optimise_during_fallback_clears_fallback_and_bumps_epoch(
    control: SparkCoordinator,
) -> None:
    """Exiting Optimise while fallback is active ends fallback and advances epoch."""
    state = control.authority.state
    assert state is not None
    await control.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise",
        "fallback": FallbackState(active=True, causes=("addon_liveness",),
                                  entered_at=dt_util.utcnow())}))
    control.control.writer = FakeTransitionWriter(steps={"cancel_export": True,
                                                         "write_fallback": True,
                                                         "release_holds": True})
    epoch_before = _state(control).epoch
    result = await control.control.async_request(
        TransitionRequest(target="observe", expected_epoch=_state(control).epoch),
        now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert result.refusal is None
    state = control.authority.state
    assert state is not None
    assert state.committed_mode == "observe"
    assert not state.fallback.active
    assert state.epoch == epoch_before + 2


async def test_non_protection_steps_wait_for_grace(control: SparkCoordinator,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   ) -> None:
    """verify_fresh_plan/write_fallback/cancel_export run only after grace."""
    state = control.authority.state
    assert state is not None
    await control.authority.async_save(state.model_copy(update={"committed_mode": "observe"}))
    control.control.grace_until = dt_util.utcnow() + timedelta(minutes=2)
    writer = FakeTransitionWriter(steps={"verify_fresh_plan": True})
    control.control.writer = writer
    result = await control.control.async_request(
        TransitionRequest(target="optimise", expected_epoch=_state(control).epoch),
        now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert result.refusal is None
    state = control.authority.state
    assert state is not None
    assert state.transition is not None
    assert state.transition.steps == ("verify_fresh_plan",)
    assert writer.called == []


async def test_permits_non_protection_checks_owned_state_ready_and_grace(
    control: SparkCoordinator,
) -> None:
    """permits_non_protection needs owned, state, ready and elapsed grace."""
    c = control.control
    now = dt_util.utcnow()
    assert c.permits_non_protection(now)
    c.ready = False
    assert not c.permits_non_protection(now)
    c.ready = True
    c.grace_until = now + timedelta(minutes=1)
    assert not c.permits_non_protection(now)
    c.grace_until = now - timedelta(seconds=1)
    c.startup_complete = False
    assert c.permits_non_protection(now)


async def test_fallback_held_bumps_epoch_only_on_active_flip_and_clears_export(
    control: SparkCoordinator,
) -> None:
    """Entering fallback bumps epoch and clears export supervisions; updates don't."""
    state = control.authority.state
    assert state is not None
    now = dt_util.utcnow()
    event = EventIdentity(direction="export", start=now + timedelta(minutes=1),
                          end=now + timedelta(minutes=2))
    supervision = ExportSupervision(
        inverter_id=next(iter(control.native.inverters)), id=uuid4(), event=event,
        confirming_user_id="user", epoch=state.epoch, ends_at=event.end, rating_w=1000.0,
        identity=(0x0026, 0x2031, 0),
        claim=GatewayClaim.model_validate(control.entry.options["gateway_claims"][0]))
    assert await control.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise", "export_supervisions": (supervision,)}))
    assert control.authority.state is not None
    assert control.authority.state.export_supervisions
    epoch_before = _state(control).epoch
    assert await control.control.async_fallback_held(
        FallbackState(active=True, causes=("addon_liveness",), entered_at=now), now=now)
    state = control.authority.state
    assert state is not None
    assert state.fallback.active
    assert state.epoch == epoch_before + 1
    assert not state.export_supervisions

    epoch_after_entry = state.epoch
    assert await control.control.async_fallback_held(
        FallbackState(active=True, causes=("addon_liveness", "soc_integrity"),
                      entered_at=now), now=now)
    state = control.authority.state
    assert state is not None
    assert state.epoch == epoch_after_entry


async def test_new_session_bumps_epoch_only_for_new_id(control: SparkCoordinator) -> None:
    """Same add-on session id is idempotent; a new id advances the epoch."""
    now = dt_util.utcnow()
    epoch_before = _state(control).epoch
    assert await control.control.async_new_session("session-1", now=now)
    state = control.authority.state
    assert state is not None
    assert state.addon_session_id == "session-1"
    assert state.epoch == epoch_before + 1

    assert await control.control.async_new_session("session-1", now=now)
    state = control.authority.state
    assert state is not None
    assert state.epoch == epoch_before + 1

    assert await control.control.async_new_session("session-2", now=now)
    state = control.authority.state
    assert state is not None
    assert state.epoch == epoch_before + 2


async def test_invalid_household_zone_returns_none(control: SparkCoordinator) -> None:
    """An unparseable household_time_zone yields None, never a crash."""
    object.__setattr__(control.entry, "options",
                       {**control.entry.options, "household_time_zone": "Mars/Phobos"})
    assert control.control.household_zone() is None


async def test_startup_stage_failure_leaves_ready_false(control: SparkCoordinator) -> None:
    """async_start stops at the first failed startup stage."""
    control.control.ready = False
    control.control.startup_complete = False
    control.control.writer = FakeTransitionWriter(startup_failures={"read_device_state"})
    await control.control.async_start(now=dt_util.utcnow(), zone=ZoneInfo("UTC"))
    assert not control.control.ready
    assert not control.control.startup_complete
    writer = control.control.writer
    assert isinstance(writer, FakeTransitionWriter)
    assert writer.startup_called == ["connect", "read_device_state"]
