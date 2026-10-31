"""Harness fixtures for the ha-spark integration suite (`tests_ha`).

This suite runs inside Home Assistant's own test harness
(`pytest-homeassistant-custom-component`, pinned); it sits outside the main
suite's `testpaths` and is gated by `tests_ha/mypy.ini`.
"""

import pytest

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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load `custom_components/ha_spark` in every test of this suite."""
    return None
