"""Manual Octopus dispatch refresh through the native tick and HA service registry."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import EventStateChangedData, async_track_state_change_event
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.test_native_holds import hass_config_dir, hass_storage, held_site  # noqa: F401

DISPATCH = "binary_sensor.dispatch"
DOMAIN = "octopus_energy"
SERVICE = "refresh_intelligent_dispatches"


@pytest.fixture
def refresh_site(held_site: SparkCoordinator) -> SparkCoordinator:  # noqa: F811
    c = held_site
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
        **c.entry.options["evidence"],
        "ev_plug": {"entity_id": "sensor.plug", "max_age_s": 300},
        "live_dispatch": {"entity_id": DISPATCH, "max_age_s": 300},
    }})
    c.hass.states.async_set(DISPATCH, "off", {"manually_refresh_dispatches": True,
                                              "planned_dispatches": []})
    return c


@pytest.fixture
def refresh_calls(hass: HomeAssistant) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    @callback
    def record(call: ServiceCall) -> None:
        calls.append(dict(call.data))

    hass.services.async_register(DOMAIN, SERVICE, record)
    return calls


async def tick(c: SparkCoordinator, freezer: Any, seconds: int = 30) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(c.hass, datetime.now(UTC))
    await c.hass.async_block_till_done(wait_background_tasks=True)


async def test_manual_dispatch_refresh_every_five_minutes(
    refresh_site: SparkCoordinator, refresh_calls: list[dict[str, Any]], freezer: Any,
) -> None:
    c = refresh_site
    c.control.startup_complete = False
    c.hass.states.async_set("sensor.plug", "EV connected")
    await tick(c, freezer)
    assert refresh_calls == [{"entity_id": DISPATCH}]

    for _ in range(9):
        c.hass.states.async_set("sensor.plug", "EV connected")
        await tick(c, freezer)
    assert len(refresh_calls) == 1
    await tick(c, freezer)
    assert refresh_calls == [{"entity_id": DISPATCH}] * 2


@pytest.mark.parametrize(("entity", "before", "after"), [
    ("sensor.ev", "Charging", "Boosting"),
    ("sensor.plug", "Waiting for EV", "Charging"),
])
async def test_ev_value_change_refreshes_once_at_sixty_second_floor(
    refresh_site: SparkCoordinator, refresh_calls: list[dict[str, Any]], freezer: Any,
    entity: str, before: str, after: str,
) -> None:
    c = refresh_site
    c.hass.states.async_set(entity, before)
    await tick(c, freezer)
    assert len(refresh_calls) == 1

    c.hass.states.async_set(entity, after)
    await tick(c, freezer)
    assert len(refresh_calls) == 1
    await tick(c, freezer)
    assert len(refresh_calls) == 2
    await tick(c, freezer, seconds=60)
    assert len(refresh_calls) == 2


@pytest.mark.parametrize("condition", [
    "flag absent", "flag false", "flag string", "dispatch missing", "dispatch unavailable",
    "dispatch unknown", "service absent", "ev missing", "ev stale", "ev unavailable",
    "ev idle", "addon owned", "ownership unverified",
])
async def test_dispatch_refresh_skips_ineligible_ticks(
    refresh_site: SparkCoordinator, refresh_calls: list[dict[str, Any]], freezer: Any,
    condition: str, caplog: pytest.LogCaptureFixture,
) -> None:
    c = refresh_site
    hass = c.hass
    caplog.set_level(logging.DEBUG, logger="custom_components.ha_spark.dispatch_refresh")
    if condition != "ev missing":
        hass.states.async_set("sensor.ev", "stopped" if condition == "ev idle" else
                              "unavailable" if condition == "ev unavailable" else "delivering")
        hass.states.async_set("sensor.plug", "ev disconnected" if condition == "ev idle" else
                              "unavailable" if condition == "ev unavailable" else "ev connected")
    if condition == "flag absent":
        hass.states.async_set(DISPATCH, "off", {"planned_dispatches": []})
    elif condition in ("flag false", "flag string"):
        hass.states.async_set(DISPATCH, "off", {"manually_refresh_dispatches":
                                               False if condition == "flag false" else "true"})
    elif condition == "dispatch missing":
        hass.states.async_remove(DISPATCH)
    elif condition in ("dispatch unavailable", "dispatch unknown"):
        hass.states.async_set(DISPATCH, condition.split()[1], {"manually_refresh_dispatches": True})
    elif condition == "service absent":
        hass.services.async_remove(DOMAIN, SERVICE)
    elif condition == "addon owned":
        c.ownership.owner = "addon"
    elif condition == "ownership unverified":
        c.ownership.verified = False
    await tick(c, freezer, seconds=301 if condition == "ev stale" else 30)
    assert not refresh_calls
    assert "Octopus dispatch refresh failed" not in caplog.text


async def test_dispatch_refresh_accepts_sibling_ev_liveness(
    refresh_site: SparkCoordinator, refresh_calls: list[dict[str, Any]], freezer: Any,
) -> None:
    c = refresh_site
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
        **c.entry.options["evidence"], "ev_liveness_entity": "sensor.ev_alive",
    }})
    c.hass.states.async_set("sensor.ev", "delivering")
    freezer.tick(timedelta(minutes=10))
    c.hass.states.async_set("sensor.ev_alive", "ok")
    await tick(c, freezer)
    assert refresh_calls == [{"entity_id": DISPATCH}]


async def test_failed_dispatch_refresh_is_redacted_and_does_not_retry_early(
    refresh_site: SparkCoordinator, freezer: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    c = refresh_site
    attempts = []

    async def fail(call: ServiceCall) -> None:
        attempts.append(call.data["entity_id"])
        raise RuntimeError("private service payload")

    c.hass.services.async_register(DOMAIN, SERVICE, fail)
    c.hass.states.async_set("sensor.plug", "ev connected")
    caplog.set_level(logging.DEBUG, logger="custom_components.ha_spark.dispatch_refresh")
    await tick(c, freezer)
    await tick(c, freezer)
    assert attempts == [DISPATCH]
    assert "Octopus dispatch refresh failed" in caplog.text
    assert "private service payload" not in caplog.text


@pytest.mark.timeout(10)
async def test_slow_dispatch_refresh_times_out_without_delaying_hold(
    refresh_site: SparkCoordinator, freezer: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    c = refresh_site
    hass = c.hass
    hold_on = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @callback
    def observe_hold(event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is not None and state.state == "on":
            hold_on.set()

    async def slow(call: ServiceCall) -> None:
        started.set()
        try:
            await asyncio.Future[None]()
        finally:
            cancelled.set()

    hass.services.async_register(DOMAIN, SERVICE, slow)
    hass.states.async_set("sensor.ev", "charging")
    # A provider request is not itself trusted dispatch evidence.
    hass.states.async_set(DISPATCH, "on", {"manually_refresh_dispatches": True})
    caplog.set_level(logging.DEBUG, logger="custom_components.ha_spark.dispatch_refresh")
    unsub = async_track_state_change_event(hass, "binary_sensor.ha_spark_ev_hold", observe_hold)
    try:
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass, datetime.now(UTC))
        await started.wait()
        await hold_on.wait()
        assert not cancelled.is_set()
        assert not c.holds.evidence.read(datetime.now(UTC)).dispatch_trusted
        await tick(c, freezer)
        assert cancelled.is_set()
        assert "Octopus dispatch refresh timed out" in caplog.text
    finally:
        unsub()
        # Let the timeout run at teardown even if an assertion failed.
        freezer.tick(timedelta(seconds=30))
