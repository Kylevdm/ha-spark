"""Corroborated Kraken windows through counted heartbeats and native writes."""

from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.native_evidence import EvidenceRead
from custom_components.ha_spark.native_hold import DispatchWindow
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_export import NOW
from tests_ha.test_native_hold_handover import (
    DISPATCH_END,
    export_clock,
    hand_over,
    held,
    report_dispatch,
    switch_writes,
)
from tests_ha.test_native_holds import hass_config_dir, hass_storage, held_site, tick
from tests_ha.test_native_write import writer
from tests_ha.test_ownership import peer_body
from tests_ha.test_polling import URL, fixture

__all__ = ["export_clock", "hass_config_dir", "hass_storage", "held_site", "writer"]


def report_car(c: SparkCoordinator, plug: str) -> None:
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
        **c.entry.options["evidence"],
        "ev_plug": {"entity_id": "sensor.plug", "max_age_s": 300},
        "dispatch_grid_power": {"entity_id": "sensor.grid", "max_age_s": 300},
        "live_dispatch": {"entity_id": "binary_sensor.dispatch", "max_age_s": 300},
    }})
    c.hass.states.async_set("sensor.ev", "stopped")
    c.hass.states.async_set("sensor.plug", plug)
    c.hass.states.async_set("sensor.grid", "8000", {"unit_of_measurement": "W"})
    report_dispatch(c, None)


async def heartbeat(c: SparkCoordinator, mock: AiohttpClientMocker, block: object, *,
                    version: int = 7, **updates: Any) -> None:
    mock.clear_requests()
    body = peer_body(active="retired", version=version) | {
        "built_at": datetime.now(UTC).isoformat(), "legacy_overlay": {"inv1": False},
        "addon_dispatches": block,
    } | updates
    mock.post(URL, json=body)
    await c.async_refresh()


@pytest.mark.parametrize("case", ["corroborated", "no_car", "stale_car", "stale_fetch",
                                  "future_fetch", "stale_tick", "wrong_session", "stale_heartbeat"])
async def test_only_current_corroborated_kraken_evidence_starts_a_hold(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, case: str,
) -> None:
    c = held_site
    block = fixture("addon_dispatches")
    now = datetime.now(UTC)
    updates: dict[str, Any] = {}
    if case == "stale_fetch":
        block["fetched_at"] = (now - timedelta(seconds=2701)).isoformat()
    elif case == "future_fetch":
        block["fetched_at"] = (now + timedelta(seconds=61)).isoformat()
    elif case == "stale_tick":
        updates["loop_tick"] = (now - timedelta(seconds=121)).isoformat()
    elif case == "wrong_session":
        updates["session_id"] = "not-this-session"
    await heartbeat(c, aioclient_mock, block, **updates)
    if case == "stale_heartbeat":
        freezer.tick(timedelta(seconds=121))
    report_car(c, "EV Disconnected" if case == "no_car" else "EV Connected")
    if case == "stale_car":
        freezer.tick(timedelta(seconds=301))
        await heartbeat(c, aioclient_mock, block)
        report_dispatch(c, None)
    await tick(c)
    assert c.authority.state is not None
    if case == "corroborated":
        (hold,) = c.authority.state.holds
        assert hold.cause == "dispatch" and hold.confirmed
        assert hold.trusted_end == datetime.fromisoformat(block["windows"][0]["end"])
        assert native_transport.holding[43007] == 222
    else:
        assert not c.authority.state.holds
        assert not switch_writes(native_transport)


@pytest.mark.parametrize("loss", ["quiet", "null", "stale", "empty"])
async def test_loss_of_kraken_evidence_does_not_release_before_the_trusted_end(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, loss: str,
) -> None:
    c = held_site
    block = fixture("addon_dispatches")
    report_car(c, "EV Connected")
    await heartbeat(c, aioclient_mock, block)
    await tick(c)
    assert c.authority.state is not None and c.authority.state.holds[0].confirmed
    freezer.tick(timedelta(minutes=46))
    report_car(c, "EV Connected")
    if loss == "quiet":
        aioclient_mock.clear_requests()
        aioclient_mock.post(URL, exc=aiohttp.ClientError())
        await c.async_refresh()
    elif loss == "null":
        await heartbeat(c, aioclient_mock, None)
    elif loss == "stale":
        await heartbeat(c, aioclient_mock, block)
    else:
        await heartbeat(c, aioclient_mock, {"fetched_at": datetime.now(UTC).isoformat(),
                                          "windows": []})
    assert not await c.holds.async_release(datetime.now(UTC), removal=True)
    await tick(c)
    assert c.authority.state is not None and c.authority.state.holds[0].confirmed
    assert native_transport.holding[43007] == 222
    freezer.move_to(block["windows"][0]["end"])
    await tick(c)
    assert native_transport.holding[43007] == 190
    assert c.authority.state is not None and not c.authority.state.holds


async def test_a_v6_peer_still_releases_on_trusted_empty_entity_evidence(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    c = held_site
    report_car(c, "EV Connected")
    block = fixture("addon_dispatches")
    await heartbeat(c, aioclient_mock, block, version=6)
    (window,) = block["windows"]
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": [window]})
    await tick(c)
    assert native_transport.holding[43007] == 222
    report_dispatch(c, None)
    await tick(c)
    assert native_transport.holding[43007] == 190
    assert c.authority.state is not None and not c.authority.state.holds


@pytest.mark.parametrize("block", [None, {"windows": []}])
async def test_a_v7_entity_hold_still_releases_when_the_addon_has_no_window(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    aioclient_mock: AiohttpClientMocker, block: dict[str, Any] | None,
) -> None:
    # A hold the entity formed is not add-on-backed: a v7 add-on's unknown or
    # empty list must not stop the entity's own clearance (non-Kraken sites).
    c = held_site
    report_car(c, "EV Connected")
    if block is not None:
        block = block | {"fetched_at": datetime.now(UTC).isoformat()}
    await heartbeat(c, aioclient_mock, block)
    (window,) = fixture("addon_dispatches")["windows"]
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": [window]})
    await tick(c)
    assert native_transport.holding[43007] == 222
    report_dispatch(c, None)
    await tick(c)
    assert native_transport.holding[43007] == 190
    assert c.authority.state is not None and not c.authority.state.holds


async def test_kraken_evidence_can_extend_but_not_shorten_a_dispatch_hold(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    c = held_site
    report_car(c, "EV Connected")
    now = datetime.now(UTC)
    end = now + timedelta(hours=1)
    window = {"start": now.isoformat(), "end": end.isoformat()}
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": [window]})
    await tick(c)
    assert c.authority.state is not None and c.authority.state.holds[0].trusted_end == end
    report_dispatch(c, None)
    native_transport.traffic.clear()
    block = fixture("addon_dispatches")
    await heartbeat(c, aioclient_mock, block)
    await tick(c)
    extended_end = datetime.fromisoformat(block["windows"][0]["end"])
    assert c.authority.state is not None and c.authority.state.holds[0].trusted_end == extended_end
    await heartbeat(c, aioclient_mock, block | {"windows": [window]})
    await tick(c)
    assert c.authority.state is not None and c.authority.state.holds[0].trusted_end == extended_end
    assert not switch_writes(native_transport)


@pytest.mark.parametrize("entity_available", [True, False])
async def test_handover_stays_live_on_a_corroborated_kraken_dispatch(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, aioclient_mock: AiohttpClientMocker,
    entity_available: bool,
) -> None:
    c = writer
    await held(c, monkeypatch, native_transport)
    if entity_available:
        report_dispatch(c, None)
    else:
        c.hass.states.async_set("binary_sensor.dispatch", "unavailable")
    await heartbeat(c, aioclient_mock, {
        "fetched_at": NOW.isoformat(), "windows": [
            {"start": NOW.isoformat(), "end": DISPATCH_END.isoformat()},
        ],
    })
    assert await hand_over(c, native_transport) == "applied"
    native_transport.traffic.clear()
    await tick(c)
    assert native_transport.holding[43007] == 190
    assert not switch_writes(native_transport)


async def test_an_uncorroborated_addon_window_has_no_vote_on_release(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    # An entity-formed hold, the car unplugged: an active Kraken window that
    # fails corroboration must not keep the hold once the entity clears.
    c = held_site
    report_car(c, "EV Connected")
    report_dispatch(c, datetime.fromisoformat(fixture("addon_dispatches")["windows"][0]["end"]))
    await tick(c)
    assert native_transport.holding[43007] == 222
    report_car(c, "EV Disconnected")
    await heartbeat(c, aioclient_mock, fixture("addon_dispatches"))
    await tick(c)
    assert native_transport.holding[43007] == 190
    assert c.authority.state is not None and not c.authority.state.holds
    assert not c.holds.addon_backed


async def test_backing_extends_only_to_the_addon_window_end(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    # The entity corrects its end from 17:00 to 13:00 while a corroborated
    # Kraken window runs to 14:30: the hold lasts to 14:30, not 17:00, and the
    # entity's later empty list cannot release it before then.
    c = held_site
    block = fixture("addon_dispatches")
    addon_end = datetime.fromisoformat(block["windows"][0]["end"])
    report_car(c, "EV Connected")
    report_dispatch(c, NOW.replace(hour=17))
    await tick(c)
    await heartbeat(c, aioclient_mock, block)
    report_dispatch(c, NOW.replace(hour=13))
    await tick(c)
    assert c.authority.state is not None
    (hold,) = c.authority.state.holds
    assert hold.trusted_end == addon_end
    freezer.move_to(NOW.replace(hour=13, minute=30))
    report_car(c, "EV Connected")
    report_dispatch(c, None)
    await heartbeat(c, aioclient_mock, None)
    await tick(c)
    assert c.authority.state is not None and c.authority.state.holds
    assert native_transport.holding[43007] == 222
    freezer.move_to(addon_end)
    report_car(c, "EV Connected")
    report_dispatch(c, None)
    await tick(c)
    assert native_transport.holding[43007] == 190


async def test_backing_needs_a_dispatch_hold(held_site: SparkCoordinator) -> None:
    c = held_site
    window = DispatchWindow(start=NOW, end=NOW + timedelta(hours=1))
    evidence = EvidenceRead(None, None, False, (window,), False, corroborated=(window,))
    inverter = next(iter(c.native.inverters.values()))
    assert c.holds._backed_end(inverter, NOW, evidence, ()) is None
    assert not c.holds.addon_backed
