"""Read-only authority mirrors render only while a heartbeat counts."""

from datetime import timedelta
from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from tests_ha.test_polling import URL, setup, state_value, summary

IDS = {
    "operating_mode": "sensor.ha_spark_operating_mode",
    "fallback_active": "binary_sensor.ha_spark_fallback_active",
    "ev_hold": "binary_sensor.ha_spark_ev_hold",
    "dispatch_hold": "binary_sensor.ha_spark_dispatch_hold",
}


async def test_authority_fixture_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    hass.states.async_set(IDS["operating_mode"], "legacy")
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    for key, entity_id in IDS.items():
        assert state_value(hass, entity_id) == ("authority_unknown" if key == "operating_mode"
                                               else "off")
        registered = er.async_get(hass).async_get(entity_id)
        assert registered is not None
        assert registered.unique_id == f"{entry.data['site_id']}_{key}"
        assert registered.device_id is not None
    mode = hass.states.get(IDS["operating_mode"])
    assert mode is not None
    assert mode.attributes["options"] == ["observe", "protect", "optimise", "authority_unknown"]
    assert mode.attributes["transition_target"] is None
    assert mode.attributes["last_changed_at"] is None
    assert mode.attributes["save_failed"] is False
    assert mode.attributes["device_class"] == "enum"
    body = summary()
    body["authority"] = {
        "mode": "protect", "transition_target": "optimise",
        "last_changed_at": "2026-10-24T11:00:00+00:00", "save_failed": True,
        "fallback": {"active": True, "causes": ["soc_integrity"]},
        "holds": {"ev": {"active": True}, "dispatch": {
            "active": True, "trusted_end": "2026-10-24T14:00:00+00:00",
        }},
        "failing_actions": ["HOUSEHOLD-CANARY"], "hub": "HOUSEHOLD-CANARY",
    }
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=body)
    await entry.runtime_data.async_refresh()
    for key, entity_id in IDS.items():
        assert state_value(hass, entity_id) == ("protect" if key == "operating_mode" else "on")
        state = hass.states.get(entity_id)
        assert state is not None
        assert "HOUSEHOLD-CANARY" not in str(state.attributes)
    mode = hass.states.get(IDS["operating_mode"])
    fallback = hass.states.get(IDS["fallback_active"])
    ev = hass.states.get(IDS["ev_hold"])
    dispatch = hass.states.get(IDS["dispatch_hold"])
    assert mode and fallback and ev and dispatch
    assert mode.attributes["transition_target"] == "optimise"
    assert mode.attributes["last_changed_at"] == "2026-10-24T11:00:00+00:00"
    assert mode.attributes["save_failed"] is True
    assert fallback.attributes["device_class"] == "problem"
    assert fallback.attributes["causes"] == ["soc_integrity"]
    assert ev.attributes["cause"] == "ev" and "trusted_end" not in ev.attributes
    assert dispatch.attributes["cause"] == "dispatch"
    assert dispatch.attributes["trusted_end"] == "2026-10-24T14:00:00+00:00"


@pytest.mark.parametrize("failure", ["down", "401", "deadline"])
async def test_authority_unavailable_without_heartbeat(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, failure: str,
) -> None:
    if failure == "down":
        aioclient_mock.post(URL, exc=aiohttp.ClientError())
    else:
        aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    aioclient_mock.clear_requests()
    if failure == "401":
        aioclient_mock.post(URL, status=401)
        await entry.runtime_data.async_refresh()
    elif failure == "deadline":
        aioclient_mock.post(URL, exc=aiohttp.ClientError())
        freezer.tick(timedelta(seconds=121))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    for entity_id in IDS.values():
        assert state_value(hass, entity_id) == "unavailable"


@pytest.mark.parametrize("path,value", [
    (("mode",), "manual"), (("mode",), True), (("transition_target",), False),
    (("last_changed_at",), "2026-10-24T12:00:00"), (("save_failed",), 1),
    (("fallback", "active"), 1), (("fallback", "causes"), [True]),
    (("fallback", "causes"), ["x"] * 9), (("fallback", "causes"), ["sensor.private"]),
    (("holds", "ev", "active"), "true"),
    (("holds", "dispatch", "trusted_end"), "2026-10-24T12:00:00"),
    ((), None),
])
async def test_malformed_authority_does_not_count(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    path: tuple[str, ...], value: Any,
) -> None:
    body = summary()
    target = body["authority"]
    if not path:
        del body["authority"]
    else:
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    aioclient_mock.post(URL, json=body)
    entry = await setup(hass)
    assert entry.runtime_data.last_outcome == "malformed"
    for entity_id in IDS.values():
        assert state_value(hass, entity_id) == "unavailable"
