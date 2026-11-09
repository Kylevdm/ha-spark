"""Harness fixtures for the ha-spark integration suite (`tests_ha`).

This suite runs inside Home Assistant's own test harness
(`pytest-homeassistant-custom-component`, pinned); it sits outside the main
suite's `testpaths` and is gated by `tests_ha/mypy.ini`.
"""

from collections.abc import AsyncGenerator

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

# Bind the top-level `custom_components` name to this repository at collection
# time, before the harness mounts its own `testing_config` directory: that
# directory also holds a `custom_components` package, and an import that lands
# there first would hide the integration from Home Assistant's loader.
import custom_components  # noqa: F401
from tests_ha.fake_solis import FakeSolisDevice


@pytest.fixture(autouse=True)
def native_transport(monkeypatch: pytest.MonkeyPatch) -> FakeSolisDevice:
    """No native setup in this suite may open a socket."""
    from custom_components.ha_spark import native

    device = FakeSolisDevice()
    device.set_identity()
    def factory(host: str, port: int) -> FakeSolisDevice:
        device.connected = True
        return device

    monkeypatch.setattr(native, "transport_factory", factory)
    return device


@pytest.fixture
async def hass(hass: HomeAssistant) -> AsyncGenerator[HomeAssistant]:
    """The harness's `hass`, settled while it still runs before the harness stops it.

    Interval ticks run as HA background tasks, so a test that moves the frozen
    clock or fires time can end with one still in flight; neither entry unload
    nor `async_block_till_done()` waits for it. The harness then stops HA, which
    cancels it without waiting. Its cancelled authority save raises a repair
    issue, and if that lands after the final-write stage the issue registry's
    delayed save is a timer nothing will flush (#666): a lingering-timer ERROR.
    Unload here and let those ticks finish first, so every registry save is
    scheduled while HA runs and flushed by its final write.
    """
    yield hass
    for entry in hass.config_entries.async_entries():
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load `custom_components/ha_spark` in every test of this suite."""
    return None
