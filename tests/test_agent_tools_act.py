"""Tests for the agent tool core act functions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx

from ha_spark.agent import tools
from ha_spark.config import Settings


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db"), **kw
    )


async def test_add_context_persists(tmp_path: Path) -> None:
    result = await tools.add_context(
        _settings(tmp_path), "away", date(2026, 7, 1), date(2026, 7, 5), note="hol"
    )
    assert any(f["kind"] == "away" for f in result.facts)


@respx.mock
async def test_run_plan_simulate_makes_no_service_call(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    service = respx.post(url__regex=r"http://ha\.test/api/services/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    await tools.run_plan(_settings(tmp_path, proactive_mode="simulate"))
    assert not service.called
