"""Whole-batch source-store and daemon source-bridge regressions."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ha_spark.api import server
from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings
from ha_spark.energy import scheduler, sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.solar_source import load_solar_source_kind, save_solar_source_kind
from ha_spark.ha.rest import HomeAssistantRest
from tests.test_integration_route import discovery as discovery
from tests.test_integration_route import state as state
from tests.test_solar_forecasts import _frozen_clock

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def test_oversized_kind_store_is_corrupt(tmp_path: Path) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    path = tmp_path / "ha_spark_solar_source.json"
    path.write_bytes(b'{"kind":"native"}' + b" " * (2048 - len(b'{"kind":"native"}')))
    assert load_solar_source_kind(settings) == "entities"


@respx.mock
async def test_daemon_source_bridge_restores_native_without_entity_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=str(tmp_path / "test.db"), ha_url="http://ha.test", ha_token="test",
        solar_today_entity="sensor.forbidden_today", solar_tomorrow_entity="sensor.forbidden",
    )
    save_solar_source_kind(settings, "native")
    app_state = AppState(settings, tmp_path / "options.json")
    assert app_state.integration_link.solar_forecast.kind == "native"
    assert app_state.integration_link.solar_forecast.forecast is None
    source = scheduler.solar_source_for(app_state.integration_link, NOW)
    assert source.kind == "native" and source.forecast is None
    assert source.now == NOW and source.received_at is None
    monkeypatch.setattr(sources, "datetime", _frozen_clock(NOW))
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(return_value=LoadForecast(
        24, (0.5,) * 48, "test",
    )))
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(return_value=httpx.Response(404))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _, _ = await sources.gather_inputs(
            settings, rest, source=source, soc=SocMeasurement(SocStatus.OK, NOW, 50),
        )
    assert inputs.solar_tomorrow_kwh == 0
    assert not any(inputs.solar_slots or ())
    assert all("forbidden" not in str(call.request.url) for call in respx.calls)


@pytest.mark.parametrize("version,configured", [(2, True), (2, False), (1, True)])
def test_daemon_source_bridge_after_heartbeat(state, discovery, monkeypatch, version, configured):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: NOW)
    body = json.loads(Path("tests/fixtures/contract/site_snapshot_forecast.json").read_text())
    body["api_version"] = version
    if not configured:
        body["solar_forecast"] = None
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        assert client.post("/integration/heartbeat", json=body, headers={
            "Authorization": f"Bearer {discovery.auth_token}",
        }).status_code == 200
    source = scheduler.solar_source_for(state.integration_link, NOW)
    assert source.kind == ("native" if version == 2 and configured else "entities")
    assert source.received_at == NOW and source.now == NOW
    assert source.forecast == state.integration_link.solar_forecast.forecast
    if source.kind == "native":
        assert source.forecast is not None
        assert source.forecast.slots[0][1:] == tuple(body["solar_forecast"]["slots"][0][1:])
    else:
        assert source.forecast is None
