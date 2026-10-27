"""Whole-batch Solcast wire rounding and quality-scale completeness regressions."""

from collections import Counter
from pathlib import Path

import homeassistant
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from tests_ha.test_solcast_forecast import NOW, URL, coordinator, payload


@pytest.mark.parametrize("power,expected", [(1.0004, 1.001), (1.0, 1.0), (1.001, 1.001)])
async def test_snapshot_p90_rounds_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, power: float, expected: float,
) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("UTC")
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, json=payload(
        pv_estimate10=1, pv_estimate=1, pv_estimate90=power,
    ))
    await c.async_refresh()
    assert c.snapshot_forecast(NOW)["slots"] == [[NOW.isoformat(), 1.0, 1.0, expected]]
    await c.async_shutdown()


def test_quality_scale_lists_all_bronze_to_gold_rules() -> None:
    components = Path(homeassistant.__file__).parent / "components"
    counts: Counter[str] = Counter()
    for path in components.glob("*/quality_scale.yaml"):
        data = load_yaml_dict(path)
        counts.update(data.get("rules", {}).keys())
    expected = {rule for rule, count in counts.items() if count >= 300} - {
        "async-dependency", "inject-websession", "strict-typing",
    }
    assert expected  # Guard against a changed HA installation layout.
    actual = load_yaml_dict(Path("custom_components/ha_spark/quality_scale.yaml"))
    assert set(actual["rules"]) == expected
