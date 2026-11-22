"""Axle history is independent of mode and of the notice queue's lifetime."""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import respx

from ha_spark.api.snapshot_v4 import Authority
from ha_spark.authority import AuthorityStore
from ha_spark.energy import scheduler
from ha_spark.energy.axle_events import AxleEventStore
from tests.authority_helpers import run_daemon
from tests.native_helpers import native_link
from tests.test_authority_gate import daemon_ticks
from tests.test_axle_events import ACCEPTED, END, START, plan_revision, snapshot
from tests.test_energy_ledger import settings_for


@respx.mock
@pytest.mark.parametrize("mode", ["observe", "protect", "optimise"])
async def test_daemon_keeps_acceptance_when_notice_ack_removes_it(tmp_path, monkeypatch, mode):
    settings = settings_for(tmp_path).model_copy(update={"axle_event_rate_gbp_kwh": 1.75})
    authority = AuthorityStore(settings)
    authority.initialise()
    assert authority.handover("pending_integration") == "retired"
    v4 = replace(snapshot("accepted"), authority=Authority.model_validate({
        "mode": mode, "epoch": 7, "store_identity": "store", "transition": None,
        "manual_session": None, "store_state": "ok",
        "fallback": {"active": False, "causes": []},
    }))
    app_states = []

    def app(state):
        app_states.append(state)
        state.integration_link = replace(native_link(settings), v4=v4)
        return object()

    notify = scheduler.notify_export_transitions

    async def ack_and_replace(*args, **kwargs):
        await notify(*args, **kwargs)
        [state] = app_states
        assert "row-0" in state.acknowledged_exports
        state.integration_link = replace(state.integration_link, v4=replace(v4, export=()))

    monkeypatch.setattr(scheduler, "build_app", app)
    monkeypatch.setattr(scheduler, "notify_export_transitions", ack_and_replace)
    monkeypatch.setattr(scheduler, "snapshot_soc", lambda *_: None)
    monkeypatch.setattr(scheduler, "run_once", AsyncMock(return_value=SimpleNamespace(
        plan=plan_revision().run.plan, applied_plan=None, plan_run=None, revision=None,
    )))
    monkeypatch.setattr(scheduler, "_planned_rate_w", AsyncMock())
    for name in ("sample_signals", "record_slot", "record_closed_days", "readiness_tick",
                 "backfill_energy_ledger", "fill_closed_days", "pull_meter_history"):
        monkeypatch.setattr(scheduler, name, AsyncMock(return_value=True))
    stop = daemon_ticks(monkeypatch, [ACCEPTED])
    original_sleep = asyncio.sleep
    store = AxleEventStore(settings.db_path)

    async def sleep(seconds):
        if seconds == 123:
            async with asyncio.timeout(0.5):
                while not await store.events(START, END):
                    await original_sleep(0.001)
        await original_sleep(seconds)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return ACCEPTED.astimezone(tz)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(scheduler, "utc_datetime", Clock)
    with pytest.raises(stop):
        await run_daemon(settings, poll_seconds=123)
    [event] = await store.events(START, END)
    assert event.accepted_at == ACCEPTED
    assert event.rate_gbp_kwh == 1.75
    assert event.committed_kwh is None
    assert not respx.calls
