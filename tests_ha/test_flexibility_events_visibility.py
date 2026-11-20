"""Display surfaces allowlist the stored opt-in, never its enabling HA user id."""

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import flexibility_events
from custom_components.ha_spark.diagnostics import async_get_config_entry_diagnostics
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_device import native_entry
from tests_ha.test_solcast_flows import entry_for

CANARY = "stored-admin-user-id-canary"
STORED = {
    "export_during_events": True,
    "export_enabled_by": CANARY,
    "export_enabled_at": "2026-10-24T11:00:00+00:00",
    "export_ratings_w": {"inv1": 5000.0},
}
DISPLAY = {
    **{key: value for key, value in STORED.items() if key != "export_enabled_by"},
    "export_enabled_at": "2026-10-24T11:00:00Z",
}


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.mark.parametrize("loaded", [False, True])
@pytest.mark.parametrize("raw,expected", [
    (STORED, DISPLAY),
    (None, {"export_during_events": False, "export_enabled_at": None, "export_ratings_w": {}}),
    ({"export_during_events": "invalid"}, {
        "export_during_events": False, "export_enabled_at": None, "export_ratings_w": {},
    }),
    ({**STORED, "export_enabled_by": None}, {**DISPLAY, "export_during_events": False}),
])
async def test_diagnostics_both_branches(
    hass: HomeAssistant, loaded: bool, raw: object, expected: dict[str, object],
) -> None:
    entry = entry_for(hass)
    if loaded:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    hass.config_entries.async_update_entry(entry, options={flexibility_events.OPTION_KEY: raw})
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["flexibility_events"] == expected
    assert CANARY not in json.dumps(data)
    assert "export_enabled_by" not in json.dumps(data)


@pytest.mark.parametrize("broken", [False, True])
async def test_snapshot_block_is_private_and_fail_soft(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch, broken: bool,
) -> None:
    native_transport.set_read_frames()
    entry = await native_entry(hass, aioclient_mock)
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(entry, options={
        **entry.options, flexibility_events.OPTION_KEY: STORED,
    })
    if broken:
        def fail(raw: object) -> None:
            raise RuntimeError(CANARY)
        monkeypatch.setattr(flexibility_events, "stored", fail)
    snapshot: dict[str, object] = {"api_version": 4}
    before = len(native_transport.traffic)
    coordinator._ownership_snapshot(snapshot)
    assert len(native_transport.traffic) == before
    assert "authority" in snapshot
    assert "banks" in snapshot
    assert "export" in snapshot
    if broken:
        assert "flexibility_events" not in snapshot
    else:
        assert snapshot["flexibility_events"] == DISPLAY
    assert CANARY not in json.dumps(snapshot)
    assert "export_enabled_by" not in json.dumps(snapshot)
