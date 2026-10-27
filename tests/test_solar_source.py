"""Tests for the solar source context and its persisted kind store (#519 ticket 02)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.solar_source import (
    SolarSource,
    load_solar_source_kind,
    save_solar_source_kind,
    solar_source_from_store,
)
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from tests.solar_source_helpers import entities_source
from tests.test_energy_sources import _frozen_clock

BASE = "http://ha.test/api"
LONDON = ZoneInfo("Europe/London")


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db")
    )


@pytest.mark.parametrize("kind", ["entities", "native"])
def test_kind_store_round_trips(tmp_path: Path, kind: str) -> None:
    settings = _settings(tmp_path)
    save_solar_source_kind(settings, kind)  # type: ignore[arg-type]
    assert load_solar_source_kind(settings) == kind


def test_missing_kind_store_reads_entities(tmp_path: Path) -> None:
    assert load_solar_source_kind(_settings(tmp_path)) == "entities"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"native"',
        '{"kind": 3}',
        '{"kind": null}',
        '{"kind": "bogus"}',
        "{}",
    ],
)
def test_corrupt_kind_store_reads_entities(tmp_path: Path, raw: str) -> None:
    settings = _settings(tmp_path)
    path = Path(settings.db_path).parent / "ha_spark_solar_source.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    assert load_solar_source_kind(settings) == "entities"


def test_kind_store_holds_only_the_kind(tmp_path: Path) -> None:
    """No site ids or any other value rides along in the store file."""
    settings = _settings(tmp_path)
    save_solar_source_kind(settings, "entities")
    path = Path(settings.db_path).parent / "ha_spark_solar_source.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"kind": "entities"}


@respx.mock
async def test_gather_inputs_requires_a_source_context(tmp_path: Path) -> None:
    """Planning never defaults to the entities: the context is mandatory."""
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings(tmp_path)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        with pytest.raises(TypeError):
            await gather_inputs(settings, rest)


@respx.mock
async def test_entities_path_keeps_its_own_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entities path is byte-for-byte legacy: it reads its own clock.

    The context's ``now`` is the native path's plan instant; it must not move
    the entities plan's ``now``.
    """

    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    frozen = datetime(2026, 6, 8, 12, 0, tzinfo=LONDON)
    monkeypatch.setattr(sources, "datetime", _frozen_clock(frozen))
    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings(tmp_path)
    other_now = datetime(2026, 6, 8, 13, 30, tzinfo=LONDON)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _, _ = await gather_inputs(
            settings, rest, source=SolarSource("entities", None, None, other_now)
        )
    assert inputs.now == frozen


def test_entities_source_reads_the_frozen_test_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = datetime(2026, 6, 8, 12, 0, tzinfo=LONDON)
    monkeypatch.setattr(sources, "datetime", _frozen_clock(frozen))
    source = entities_source(_settings(tmp_path))
    assert source.kind == "entities"
    assert source.forecast is None
    assert source.received_at is None
    assert source.now == frozen


def test_solar_source_from_store_reads_the_persisted_kind(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_solar_source_kind(settings, "entities")
    source = solar_source_from_store(settings)
    assert source.kind == "entities"
    assert source.forecast is None
    assert source.received_at is None
    assert source.now.tzinfo is not None


def _write_kind(tmp_path: Path, data: Any) -> None:
    path = Path(_settings(tmp_path).db_path).parent / "ha_spark_solar_source.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_solar_source_from_store_treats_a_corrupt_store_as_entities(
    tmp_path: Path,
) -> None:
    _write_kind(tmp_path, {"kind": "bogus"})
    assert solar_source_from_store(_settings(tmp_path)).kind == "entities"
