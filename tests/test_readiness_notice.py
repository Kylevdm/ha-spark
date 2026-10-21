"""Daily readiness notices are durable, isolated recommendations only."""
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response

from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.devices.execution import ExecutionContext
from ha_spark.energy.readiness import evaluate_readiness, notify_readiness, readiness_tick
from ha_spark.energy.readiness_measurements import ReadinessDays
from ha_spark.ha.rest import HomeAssistantRest
from tests.authority_helpers import seed_authority
from tests.test_readiness import fixture


@respx.mock
async def test_notice_once_rearms_only_after_fail_and_persists_first(tmp_path):
    settings = Settings(ha_url="http://ha.test", ha_token="test",
                        db_path=str(tmp_path / "events.db"), readiness_window_days=1)
    store = seed_authority(settings, "protect", context=ExecutionContext())
    before = store.state.model_dump(exclude={"readiness_notice"})
    day, forecast, guards = fixture()
    result = evaluate_readiness([day], guards, [forecast], settings)
    route = respx.post("http://ha.test/api/services/persistent_notification/create")

    def delivered(request):
        assert AuthorityStore(settings).state.readiness_notice.notified
        assert store.state.model_dump(exclude={"readiness_notice"}) == before
        assert b"Cost delta" in request.content
        assert b"MAE" in request.content
        return Response(200, json=[])

    route.mock(side_effect=delivered)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        await notify_readiness(result, settings, rest, store)
        await notify_readiness(result, settings, rest, store)
        await notify_readiness(result.model_copy(update={"verdict": "insufficient_evidence"}),
                               settings, rest, store)
        await notify_readiness(result, settings, rest, store)
        assert route.call_count == 1
        await notify_readiness(result.model_copy(update={"verdict": "fail"}), settings, rest, store)
        assert not store.state.readiness_notice.notified
        await notify_readiness(result, settings, rest, store)
    assert route.call_count == 2
    assert store.state.model_dump(exclude={"readiness_notice"}) == before


@pytest.mark.parametrize("failure", ["notify", "save"])
async def test_notice_failures_are_isolated(tmp_path, monkeypatch, failure):
    settings = Settings(ha_url="http://ha.test", ha_token="test",
                        db_path=str(tmp_path / "events.db"), readiness_window_days=1)
    store = seed_authority(settings, "observe", context=ExecutionContext())
    day, forecast, guards = fixture()
    result = evaluate_readiness([day], guards, [forecast], settings)
    sender = AsyncMock(side_effect=RuntimeError("failed"))
    monkeypatch.setattr("ha_spark.energy.readiness.send_notification", sender)
    if failure == "save":
        def fail_save(state):
            raise OSError("failed")
        monkeypatch.setattr(store, "save", fail_save)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        await notify_readiness(result, settings, rest, store)
    assert sender.call_count == (1 if failure == "notify" else 0)


async def test_daily_evaluation_waits_for_snapshot_then_runs_once(tmp_path, monkeypatch):
    settings = Settings(ha_url="http://ha.test", ha_token="test",
                        db_path=str(tmp_path / "events.db"))
    store = seed_authority(settings, "observe", context=ExecutionContext())
    day, forecast, guards = fixture()
    result = evaluate_readiness([day], guards, [forecast], settings)
    loader = AsyncMock(return_value=result)
    monkeypatch.setattr("ha_spark.energy.readiness.load_readiness", loader)
    now = day.slots[0].slot_start.replace(day=day.local_date.day + 1, hour=12)
    assert await readiness_tick(settings, now, None, store) is None
    await ReadinessDays(settings.db_path).record(day)
    evaluated = await readiness_tick(settings, now, None, store)
    assert evaluated == day.local_date
    assert await readiness_tick(settings, now, evaluated, store) == evaluated
    assert loader.call_count == 1


async def test_daily_load_failure_is_isolated(tmp_path, monkeypatch):
    settings = Settings(ha_url="http://ha.test", ha_token="test",
                        db_path=str(tmp_path / "events.db"))
    store = seed_authority(settings, "observe", context=ExecutionContext())
    before = store.state
    day, _, _ = fixture()
    await ReadinessDays(settings.db_path).record(day)
    monkeypatch.setattr("ha_spark.energy.readiness.load_readiness",
                        AsyncMock(side_effect=OSError("failed")))
    now = day.slots[0].slot_start.replace(day=day.local_date.day + 1, hour=12)
    assert await readiness_tick(settings, now, None, store) is None
    assert store.state == before
