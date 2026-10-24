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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load `custom_components/ha_spark` in every test of this suite."""
    return None
