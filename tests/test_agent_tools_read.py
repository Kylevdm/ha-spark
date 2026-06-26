"""Tests for the agent tool core read functions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx

from ha_spark.agent import tools
from ha_spark.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db")
    )


@respx.mock
async def test_get_plan_returns_entities(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    result = await tools.get_plan(_settings(tmp_path))
    assert result.generated_at is not None
    assert isinstance(result.plan, list)


async def test_get_context_lists_added_facts(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    from ha_spark.energy.context import ContextStore

    async with ContextStore(s.db_path) as store:
        await store.add("away", date(2026, 7, 1), date(2026, 7, 5), note="holiday")
    result = await tools.get_context(s)
    assert result.facts[0]["kind"] == "away"
    assert result.facts[0]["note"] == "holiday"
