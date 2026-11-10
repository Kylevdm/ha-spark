"""A session's interval ticks and stores end with it (#672).

Each case holds one real ``native_tick`` inside an awaited I/O seam, then
reloads or unloads the entry. The unloaded session must never save authority
built from its own memory: not before the new session loads, not after.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_spark.authority import AuthorityStore
from custom_components.ha_spark.authority_store import AuthorityState, FallbackState
from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.transitions import TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_fallback import fallback_site as fallback_fixture
from tests_ha.test_native_fallback import optimise

fallback_site = fallback_fixture


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.fixture
async def hass(hass: HomeAssistant) -> AsyncGenerator[HomeAssistant]:
    yield hass
    for entry in hass.config_entries.async_entries():
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
async def release(hass: HomeAssistant) -> AsyncGenerator[asyncio.Event]:
    """Frees a parked tick before ``hass`` tears down, even when a test fails."""
    event = asyncio.Event()
    yield event
    event.set()


async def _disk(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    raw = await Store[dict[str, Any]](hass, 1, f"ha_spark.authority.{entry_id}").async_load()
    assert raw is not None
    return raw


async def _hold_old_tick(
    hass: HomeAssistant, old: SparkCoordinator, freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch, release: asyncio.Event,
) -> None:
    """Fire the real 30 s native_tick and park it inside its first awaited I/O."""
    entered = asyncio.Event()
    real = old.native.async_check_connections

    async def slow_io() -> None:  # models a Modbus reprobe waiting on its timeout
        entered.set()
        await release.wait()
        await real()

    monkeypatch.setattr(old.native, "async_check_connections", slow_io)
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    for _ in range(20):
        if entered.is_set():
            break
        await asyncio.sleep(0)
    assert entered.is_set(), "native_tick did not start"


def observing(state: AuthorityState) -> bool:
    """Committed to Observe, or on the way there by the owner's request."""
    return state.committed_mode == "observe" or (
        state.transition is not None and state.transition.target == "observe")


async def test_stale_tick_cannot_overwrite_new_sessions_observe_after_reload(
    hass: HomeAssistant, fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch, release: asyncio.Event,
) -> None:
    old = fallback_site
    entry = old.entry
    await optimise(old)
    before = old.authority.state
    assert before is not None and before.committed_mode == "optimise"
    assert not before.fallback.active

    await _hold_old_tick(hass, old, freezer, monkeypatch, release)

    # Options-flow save, rediscovery and similar all end in this reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    new: SparkCoordinator = entry.runtime_data
    assert new is not old
    assert old._running is False and old.native.closed and old.connected is False

    # The owner moves the new session to Observe (owner request, epoch bump).
    decision = await new.control.async_request(
        TransitionRequest(target="observe"), now=datetime.now(UTC), zone=ZoneInfo("UTC"))
    assert decision.refusal is None, decision
    new_state = new.authority.state
    assert new_state is not None
    assert new_state.epoch == before.epoch + 1
    assert observing(new_state)

    # Release whatever is left of the old session's tick; let everything settle.
    native_transport.traffic.clear()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not native_transport.traffic

    # The durable record is exactly the new session's, in memory and on disk.
    disk = await _disk(hass, entry.entry_id)
    assert disk == new_state.model_dump(mode="json")
    assert new.authority.state == new_state
    assert new.authority.status == "ok"

    # A restart (modelled by another reload) adopts the owner's Observe.
    assert await hass.config_entries.async_reload(entry.entry_id)
    after: SparkCoordinator = entry.runtime_data
    reloaded = after.authority.state
    assert reloaded is not None
    assert observing(reloaded) and not reloaded.fallback.active


async def test_stale_tick_before_new_load_never_enters_fallback(
    hass: HomeAssistant, fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch, release: asyncio.Event,
) -> None:
    """The old session's shutdown sets connected=False; that is not add-on loss."""
    old = fallback_site
    entry = old.entry
    await optimise(old)
    before = old.authority.state
    assert before is not None
    await _hold_old_tick(hass, old, freezer, monkeypatch, release)
    assert await hass.config_entries.async_unload(entry.entry_id)
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    disk = await _disk(hass, entry.entry_id)
    assert disk == before.model_dump(mode="json")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = entry.runtime_data.authority.state
    assert state is not None and not state.fallback.active
    assert state.committed_mode == "optimise" and state.epoch == before.epoch


async def test_a_closed_session_persists_nothing_and_leaves_the_repair_issue(
    hass: HomeAssistant, fallback_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """Even a stale caller that escaped the tick cancellation cannot save."""
    old = fallback_site
    entry = old.entry
    await optimise(old)
    before = old.authority.state
    assert before is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert old.authority.closed and old.ownership.closed
    registry = ir.async_get(hass)
    issue_id = old.authority.issue_id
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    now = datetime.now(UTC)

    # Disk still equals the old memory, so only the closed fence refuses these.
    assert old.authority.state == before
    assert not await old.control.async_fallback(
        FallbackState(active=True, causes=("addon_liveness",), entered_at=now), now=now)
    assert not await old.authority.async_save(before.model_copy(update={"epoch": 9}))
    assert not await old.authority.async_confirm_observe()
    assert not await old.authority.async_initialise()
    assert not await old.authority.async_retire()
    old.authority.publish_save_failed()
    assert await _disk(hass, entry.entry_id) == before.model_dump(mode="json")
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    # A repair issue the next session raised is not the closed session's to clear.
    ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=True,
                          severity=ir.IssueSeverity.ERROR, translation_key="authority_unknown",
                          data={"entry_id": entry.entry_id})
    assert not await old.authority.async_save(before)
    await old.authority.async_load()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    ownership = await Store[dict[str, Any]](hass, 1, old.ownership.key).async_load()
    owner, verified = old.ownership.owner, old.ownership.verified
    assert not await old.ownership._save("pending_addon")
    assert (old.ownership.owner, old.ownership.verified) == (owner, verified)
    assert await Store[dict[str, Any]](hass, 1, old.ownership.key).async_load() == ownership


async def test_a_save_is_refused_unless_disk_still_holds_this_sessions_record(
    hass: HomeAssistant,
) -> None:
    """Compare-and-set: a foreign same-epoch write fences the stale writer as unknown."""
    stale = AuthorityStore(hass, "cas", lambda: None)
    assert await stale.async_initialise()
    live = AuthorityStore(hass, "cas", lambda: None)
    await live.async_load()
    base = live.state
    assert base is not None and base == stale.state

    # The foreign write keeps the epoch the stale store remembers: only the
    # contents differ, as in the observed reload collision.
    ours = base.model_copy(update={"committed_mode": "protect"})
    assert await live.async_save(ours)
    theirs = base.model_copy(update={"epoch": 2, "fallback": FallbackState(
        active=True, causes=("addon_liveness",), entered_at=base.last_changed_at)})
    assert not await stale.async_save(theirs)
    assert stale.status == "unknown" and stale.state is None
    assert await _disk(hass, "cas") == ours.model_dump(mode="json")
    assert ir.async_get(hass).async_get_issue(DOMAIN, stale.issue_id) is not None

    # The live session, whose memory matches disk, still saves.
    assert await live.async_save(ours.model_copy(update={"epoch": 2}))


async def test_an_admitted_write_finishes_before_close_and_blocks_a_racing_save(
    hass: HomeAssistant,
) -> None:
    """The compare and the write are one step across sessions; close drains it."""
    old = AuthorityStore(hass, "race", lambda: None)
    assert await old.async_initialise()
    new = AuthorityStore(hass, "race", lambda: None)
    await new.async_load()
    base = new.state
    assert base is not None
    writing, release = asyncio.Event(), asyncio.Event()
    real_save = old.store.async_save

    async def slow_save(data: dict[str, Any]) -> None:
        writing.set()
        await release.wait()
        await real_save(data)

    old.store.async_save = slow_save  # type: ignore[method-assign]
    stale = base.model_copy(update={"committed_mode": "protect"})
    old_save = asyncio.create_task(old.async_save(stale))
    await writing.wait()
    closing = asyncio.create_task(old.async_close())
    racing = asyncio.create_task(new.async_save(base.model_copy(update={"epoch": 2})))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not closing.done() and not racing.done()
    release.set()
    assert await old_save
    await closing
    # The racing save compares after the admitted write landed: refused, not a clobber.
    assert not await racing
    assert new.status == "unknown"
    assert await _disk(hass, "race") == stale.model_dump(mode="json")
