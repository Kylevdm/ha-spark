"""The add-on never overwrites integration-owned states over HA REST."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from ha_spark.energy import scheduler
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.v2l import event_id, load_topup
from tests.authority_helpers import run_daemon
from tests.test_scheduler import _AmpsDevice, _patch_recovery_loop
from tests.test_v2l import _EVENT, _overnight_plan, _topup_settings


@respx.mock
async def test_plan_run_never_pushes_states_and_still_requests_v2l_topup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _topup_settings(tmp_path)
    plan = _overnight_plan()
    now = datetime(2026, 6, 8, 21, 0, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    async def current_plan(*_args, **_kwargs):
        return SimpleNamespace(
            plan=plan, inputs=SimpleNamespace(ev_hold_charging=None, flexibility_event=_EVENT),
            load_source="test", tz=None,
            forecast=LoadForecast(total_kwh=24.0, slots=None, source="test"),
        )

    monkeypatch.setattr(scheduler, "datetime", FixedDatetime)
    monkeypatch.setattr(scheduler, "current_plan", current_plan)
    class PlanDevice(_AmpsDevice):
        capabilities = frozenset()

    device = PlanDevice({20.0: 62.5}, settings.battery_voltage_v)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *_args: device)
    pushes = respx.post(url__regex=r"http://ha\.test/api/states/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    notice = respx.post(url__regex=r"http://ha\.test/api/services/notify/.*").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = await scheduler.run_once(settings, soc=plan.soc)

    assert result.plan == plan
    assert device.applied
    assert notice.call_count == 1
    assert "V2L" in notice.calls[0].request.content.decode()
    assert load_topup(settings, event_id(_EVENT)).last_sent_kwh == pytest.approx(1.1)
    assert not pushes.called
    assert not (tmp_path / "ha_spark_published.json").exists()


@respx.mock
async def test_mode_soc_fallback_and_recovery_ticks_never_push_or_replay_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop, runs, settings = _patch_recovery_loop(
        tmp_path, monkeypatch, [0, 1, 2, 3],
        [["[APPLIED] set timed charge current to 20 A over 3.5 h of the window"]],
    )
    # An old cache is left untouched, and never replayed at startup.
    cache = tmp_path / "ha_spark_published.json"
    cached = '[["sensor.ha_spark_plan_status", "legacy", {}]]'
    cache.write_text(cached)
    respx.post(url__regex=r"http://ha\.test/api/services/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    pushes = respx.post(url__regex=r"http://ha\.test/api/states/.*").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(stop):
        await run_daemon(settings, poll_seconds=0)

    from ha_spark.authority import load_authority

    assert runs
    authority = load_authority(settings)
    assert authority.state is not None and not authority.state.fallback.active
    assert cache.read_text() == cached
    assert not pushes.called
