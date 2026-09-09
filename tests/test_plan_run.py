"""Tests for the single plan-pipeline entry point (`current_plan`, #91)."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.plan_run import PlanRun, current_plan
from ha_spark.energy.tariff import fixed_schedule
from ha_spark.ha.rest import HomeAssistantRest


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db"), **kw
    )


@respx.mock
async def test_current_plan_bundles_plan_inputs_and_schedule(tmp_path: Path) -> None:
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings(tmp_path)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(settings, rest)
    assert isinstance(run, PlanRun)
    # The bundled schedule is the one the planner actually costed against.
    assert run.plan is not None
    assert run.load_source
    assert run.schedule is not None


@respx.mock
async def test_current_plan_costs_against_the_configured_provider(tmp_path: Path) -> None:
    """The fixed provider reproduces a hand-built fixed schedule byte-for-byte."""
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = _settings(tmp_path, tariff_provider="fixed")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(settings, rest)
    assert run.schedule == fixed_schedule(run.inputs, run.cfg)
