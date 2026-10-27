"""Direct plan previews explain why a persisted native source uses zero solar."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from ha_spark import cli
from ha_spark.agent.tools import get_plan, get_state
from ha_spark.config import Settings
from ha_spark.copilot import build_grounding
from ha_spark.energy import sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.solar_source import NATIVE_PREVIEW_NOTE, save_solar_source_kind
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.intent_parser import parse_offline
from tests.test_solar_forecasts import _frozen_clock


@respx.mock
async def test_native_previews_explain_zero_solar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "spark.db"),
        latitude=51, longitude=0, solar_tomorrow_entity="sensor.forbidden",
    )
    save_solar_source_kind(settings, "native")
    monkeypatch.setattr(sources, "datetime", _frozen_clock(datetime(2026, 6, 8, 12, tzinfo=UTC)))
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(return_value=LoadForecast(
        24, None, "test",
    )))
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(return_value=httpx.Response(404))
    assert await cli._cmd_plan(settings, apply=False) == 0
    assert NATIVE_PREVIEW_NOTE in capsys.readouterr().out
    result = await get_plan(settings)
    assert NATIVE_PREVIEW_NOTE in result.text
    assert all("forbidden" not in str(call.request.url) for call in respx.calls)


@pytest.mark.parametrize("kind", ["native", "entities"])
@respx.mock
async def test_offline_surfaces_explain_native_zero_solar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind,
) -> None:
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "spark.db"),
        latitude=51, longitude=0,
    )
    save_solar_source_kind(settings, kind)
    monkeypatch.setattr(sources, "datetime", _frozen_clock(datetime(2026, 6, 8, 12, tzinfo=UTC)))
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(return_value=LoadForecast(
        24, None, "test",
    )))
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(return_value=httpx.Response(404))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        for query in ("plan tonight", "solar forecast"):
            answer = await parse_offline(query, settings, rest)
            assert answer.matched
            assert (NATIVE_PREVIEW_NOTE in answer.text) == (kind == "native")
        grounding = await build_grounding(settings, rest)
    assert grounding is not None
    assert (NATIVE_PREVIEW_NOTE in grounding) == (kind == "native")
    state = await get_state(settings)
    if kind == "native":
        assert state.inputs["solar_note"] == NATIVE_PREVIEW_NOTE
    else:
        assert "solar_note" not in state.inputs
