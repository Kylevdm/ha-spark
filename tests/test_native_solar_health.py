"""Native source diagnostics are allowlisted and do not require legacy sensors."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ha_spark import health
from ha_spark.api.contract import IntegrationLink, link_view
from ha_spark.config import Settings
from ha_spark.daemon_channel import Reply
from ha_spark.energy.solar_source import (
    NativeForecast,
    ParsedSolarForecast,
    save_solar_source_kind,
)

NOW = datetime(2026, 10, 25, tzinfo=UTC)


def test_native_health_entity_config(tmp_path: Path) -> None:
    settings = Settings(db_path=str(tmp_path / "spark.db"), **{
        field: "sensor.test" for field in health._REQUIRED_ENTITY_FIELDS
        if field != "solar_tomorrow_entity"
    }, battery_power_entity="sensor.power")
    assert health.check_entity_config(settings).status == health.Status.WARN
    save_solar_source_kind(settings, "native")
    assert health.check_entity_config(settings).status == health.Status.OK


async def test_native_health_link(monkeypatch: pytest.MonkeyPatch) -> None:
    forecast = NativeForecast("stale", NOW - timedelta(hours=1), 2, 10, ())
    view = link_view(IntegrationLink(
        None, None, False, ParsedSolarForecast("native", forecast),
    ), NOW)
    assert view["solar_source"] == "native"
    assert view["solar_status"] == "stale"
    assert view["solar_fetch_age_s"] == 3600
    assert "slots" not in view
    monkeypatch.setattr(
        health, "submit_socket", AsyncMock(return_value=Reply(integration_link=view)),
    )
    result = await health.check_integration_link(Settings())
    assert "solar: native" in result.detail
    assert "stale" in result.detail
    assert "fetch age 3600s" in result.detail
