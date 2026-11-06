"""Native fallback controller: acceptance-time freshness and helper seams (#599)."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory

from custom_components.ha_spark.authority_store import Transition
from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_fallback import (
    clear_liveness,
    connect_soc,
    optimise,
    plan,
)
from tests_ha.test_native_fallback import (
    fallback_site as fallback_fixture,
)

fallback_site = fallback_fixture


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


# --- Gap 5: acceptance-time freshness with an in-flight concurrent apply ------

async def test_in_flight_apply_accepted_before_exit_keeps_fallback_active(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan accepted before the exit transition began must not clear fallback."""
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    state = c.authority.state
    assert state is not None and state.fallback.active and c.fallback.in_force()

    await clear_liveness(c, now)
    # Accepted ten seconds before the liveness cause can clear.
    freezer.tick(timedelta(minutes=4, seconds=50))
    # The thirty-second native tick's export retry takes control.lock then
    # guard.lock; no export is involved here, so keep it from contending.
    monkeypatch.setattr(c, "async_retry_export_records", AsyncMock())

    assert c.guard is not None
    async with c.guard.lock:
        # The guard lock models another write transaction still in flight: the
        # plan is accepted now and waits for the lock.
        task = asyncio.create_task(c.writes.async_apply(plan(c)))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()
        freezer.tick(timedelta(seconds=40))
        # The exit begins after the plan was accepted (clears the cause, sets
        # exit_since); the schedule is verified, so this needs no write lock.
        exit_began = datetime.now(UTC)
        # Background polls may have broken the streak while the test yielded;
        # restate five continuous minutes of liveness without yielding.
        c.connected = True
        c.fallback.liveness_since = exit_began - timedelta(minutes=5)
        await c.fallback.async_evaluate(datetime.now(UTC))
        state = c.authority.state
        assert state is not None and state.fallback.active and not state.fallback.causes
        assert c.fallback.exit_since == exit_began
    result = await task
    assert result.outcome == "applied"

    # The plan completed after the exit began but was accepted before it.
    freezer.tick(timedelta(seconds=30))
    await c.fallback.async_evaluate(datetime.now(UTC))
    state = c.authority.state
    assert state is not None and state.fallback.active

    # A fresh plan accepted after exit_since finally clears fallback.
    freezer.tick(timedelta(minutes=1))
    assert (await c.writes.async_apply(plan(c, current=15))).outcome == "applied"
    await c.fallback.async_evaluate(datetime.now(UTC))
    state = c.authority.state
    assert state is not None and not state.fallback.active


# --- Per-module coverage -----------------------------------------------------

async def test_valid_false_without_a_schedule(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """valid() reports False when the saved schedule is missing or invalid."""
    c = fallback_site
    await optimise(c)
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "fallback": {"window_start": "23:30"}})
    now = datetime.now(UTC)
    await connect_soc(c, now)
    assert not c.fallback.valid(now)


async def test_retrying_after_a_failed_exit_plan(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    """retrying() is true only inside the five-minute backoff window."""
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c.fallback.retry_at = now + timedelta(minutes=5)
    assert c.fallback.retrying(now)
    assert not c.fallback.retrying(now + timedelta(minutes=6))


async def test_verify_fresh_plan_step_needs_a_plan_accepted_after_transition(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    """async_step('verify_fresh_plan') honours acceptance time and epoch."""
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    started = now - timedelta(minutes=1)
    state = c.authority.state
    assert state is not None

    # No last_apply -> False.
    c.writes.last_apply = None
    transition = state.model_copy(update={
        "transition": Transition(target="optimise", started_at=started, steps=())})
    assert not await c.fallback.async_step("verify_fresh_plan", transition, now=now)

    # Accepted before the transition started -> False.
    c.writes.last_apply = (state.epoch, started - timedelta(seconds=30), True)
    assert not await c.fallback.async_step("verify_fresh_plan", transition, now=now)

    # Accepted after the transition started, same epoch -> True.
    c.writes.last_apply = (state.epoch, started + timedelta(seconds=30), True)
    assert await c.fallback.async_step("verify_fresh_plan", transition, now=now)

    # Stale epoch -> False.
    c.writes.last_apply = (state.epoch + 1, started + timedelta(seconds=30), True)
    assert not await c.fallback.async_step("verify_fresh_plan", transition, now=now)


async def test_unknown_step_returns_false(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """async_step fails closed for steps it does not implement."""
    c = fallback_site
    await optimise(c)
    state = c.authority.state
    assert state is not None
    assert not await c.fallback.async_step("no_such_step", state, now=datetime.now(UTC))


async def test_in_force_needs_active_fallback_and_verified_schedule(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """in_force() reflects verified per-inverter fallback applies."""
    c = fallback_site
    await optimise(c)
    now = datetime.now(UTC)
    c._clear_deadline()
    c._expire(now)
    await c.fallback.async_evaluate(now)
    assert c.fallback.in_force()

    # A different epoch clears verification.
    state = c.authority.state
    assert state is not None
    await c.authority.async_save(state.model_copy(update={"epoch": state.epoch + 1}))
    assert not c.fallback.in_force()
