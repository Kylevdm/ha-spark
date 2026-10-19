from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark.agent import tools
from ha_spark.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db")
    )


@respx.mock
async def test_get_plan_returns_entities(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await tools.get_plan(_settings(tmp_path))
    assert result.generated_at is not None
    assert isinstance(result.plan, list)


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise"])
@respx.mock
async def test_get_state_reports_operating_mode(tmp_path: Path, mode) -> None:
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db"), inverter="solis"
    )
    from tests.authority_helpers import seed_authority

    seed_authority(s, mode)
    result = await tools.get_state(s)
    assert result.operating_mode == mode
    assert result.devices == [
        {"id": "main_inverter", "type": "inverter", "driver": "solis"}
    ]


async def test_get_context_lists_added_facts(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    from ha_spark.energy.context import ContextStore

    async with ContextStore(s.db_path) as store:
        await store.add("away", date(2026, 7, 1), date(2026, 7, 5), note="holiday")
    result = await tools.get_context(s)
    assert result.facts[0]["kind"] == "away"
    assert result.facts[0]["note"] == "holiday"


async def test_get_forecast_returns_day_order_slots_and_served_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forecast response carries the unrotated 48-value day profile and its
    served date (#397) — never the horizon-order ``inputs.load_slots``."""
    from types import SimpleNamespace

    from ha_spark.energy.models import LoadForecast

    slots = [float(i) for i in range(48)]
    run = SimpleNamespace(
        plan=SimpleNamespace(load_kwh=12.0),
        load_source="slot profile (14d hourly house stats)",
        inputs=SimpleNamespace(load_slots=tuple(99.0 for _ in range(48))),
        forecast=LoadForecast(
            total_kwh=12.0,
            slots=tuple(slots),
            source="slot profile (14d hourly house stats)",
            target_date=date(2026, 1, 12),
        ),
    )

    async def fake_current_plan(_settings: Settings, _rest: object, **_kw: object) -> object:
        return run

    monkeypatch.setattr(tools, "current_plan", fake_current_plan)
    result = await tools.get_forecast(_settings(tmp_path))
    assert result.load_kwh == 12.0
    assert result.slots == slots
    assert result.date == "2026-01-12"
    assert result.source == "slot profile (14d hourly house stats)"
