"""The daemon's ordered handover drives the real Solis read-back contract."""
from datetime import UTC, datetime

import pytest
import respx

from ha_spark.authority import AuthorityStore, FallbackState
from ha_spark.devices.execution import execution_context
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.energy.export_store import ExportEventStore
from tests.test_daemon_executor import running_daemon, settings_at
from tests.test_handover_release import _resident_export
from tests.test_mode_lifecycle import _hold
from tests.test_optimise_lifecycle import wait_for
from tests.test_solis_power_switch import _SWITCH, FakeRest, _options


@pytest.mark.parametrize("target", ["protect", "observe"])
@respx.mock
async def test_daemon_handover_verifies_export_then_current_then_window_then_release(
    tmp_path, monkeypatch, target,
):
    settings = settings_at(tmp_path, inverter_power_switch_entity=_SWITCH,
                           solis_fallback_current_a=20)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={
        "holds": (_hold(settings),),
        "fallback": FallbackState(active=True, causes=("soc_integrity",)),
    }))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, _, _):
            rest = FakeRest(switch="Off")
            _resident_export(rest)
            rest.states["sensor.solis_control_timed_charge_current"] = "40"
            device = SolisDevice(settings.devices[0], settings, rest)
            monkeypatch.setattr("ha_spark.energy.scheduler.inverter_device", lambda *args: device)
            export = ExportEventStore(settings.db_path)
            async with export:
                await export.save("export|2026-10-21T17:00|2026-10-21T18:00", datetime.now(UTC))
            original = rest.call_service

            async def stuck_current(domain, service, data=None):
                if domain == "modbus" and data.get("address") == 43141:
                    rest.calls.append((domain, service, data))
                    return []
                return await original(domain, service, data)

            rest.call_service = stuck_current
            epoch = state.executor.authority.state.epoch
            reply = await state.executor.submit_mode(target, epoch)
            assert reply.error is None
            await wait_for(lambda: state.executor.authority.state.transition
                           and state.executor.authority.state.transition.failing_actions)
            current = state.executor.authority.state
            assert current.committed_mode == "optimise"
            assert current.transition.steps[0] == "write_fallback"
            assert not export.exists
            assert _options(rest) == []
            first_current = next(i for i, (_, _, data) in enumerate(rest.calls)
                                 if data.get("address") == 43141)
            # Export cancellation precedes any fallback current attempt.
            assert rest.calls[0][2]["address"] == 43143
            assert rest.calls[0][2]["value"][4:] == [0, 0, 0, 0]
            assert first_current > 0
            # A non-handover charging action is refused while switching.
            bus = execution_context().coordinator_for(settings.devices[0], settings)
            async with bus.transition():
                before = len(rest.calls)
                refused = await device.set_charge_rate(1000)
                assert refused.startswith("[REFUSED]")
                assert len(rest.calls) == before
            rest.call_service = original
            await wait_for(lambda: state.executor.authority.state.transition is None)
            current = state.executor.authority.state
            assert current.committed_mode == target
            assert not current.fallback.active
            assert rest.states["sensor.solis_control_timed_charge_current"] == "20.0"
            writes = [data for domain, _, data in rest.calls
                      if domain in ("modbus", "select")]
            verified_current = max(i for i, data in enumerate(writes)
                                   if data.get("address") == 43141)
            final_window = max(i for i, data in enumerate(writes)
                               if data.get("address") == 43143)
            assert verified_current < final_window
            assert writes[final_window]["value"] == [23, 30, 5, 30, 0, 0, 0, 0]
            if target == "observe":
                assert _options(rest) == ["On"]
                assert writes[-1]["option"] == "On"
                assert current.holds == ()
            else:
                assert _options(rest) == []
                assert current.holds
    finally:
        execution_context().authority = prior
