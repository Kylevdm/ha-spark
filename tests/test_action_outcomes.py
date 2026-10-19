"""Typed verdicts, not presentation text, establish resident hardware state."""

import pytest

from ha_spark.devices.outcomes import ActionLine, ActionOutcome, is_verified
from ha_spark.energy.scheduler import _apply_failed, recovery_verified
from ha_spark.energy.soc_monitor import SocMonitor
from tests.test_soc_monitor import _measurement


@pytest.mark.parametrize(
    "outcome",
    [
        ActionOutcome.SIMULATED,
        ActionOutcome.REFUSED,
        ActionOutcome.BLOCKED,
        ActionOutcome.FAILED,
        ActionOutcome.UNVERIFIED,
    ],
)
def test_non_verified_outcomes_cannot_establish_a_baseline(outcome: ActionOutcome) -> None:
    line = ActionLine("[APPLIED] misleading presentation", outcome)
    assert not is_verified(line)
    assert _apply_failed([line])
    assert not recovery_verified((line,))


def test_plain_apply_lines_are_not_verification() -> None:
    assert not is_verified("[APPLIED] plain string")
    assert _apply_failed(["[APPLIED] plain string"])
    assert not recovery_verified(("[APPLIED] plain string",))


@pytest.mark.parametrize("outcome", [ActionOutcome.VERIFIED, ActionOutcome.UNCHANGED_VERIFIED])
def test_verified_outcome_is_independent_of_text(outcome: ActionOutcome) -> None:
    line = ActionLine("presentation without a prefix", outcome)
    assert is_verified(line)
    assert not _apply_failed([line])
    assert recovery_verified((line,))


@pytest.mark.parametrize(
    "outcome",
    [
        ActionOutcome.SIMULATED,
        ActionOutcome.REFUSED,
        ActionOutcome.BLOCKED,
        ActionOutcome.FAILED,
        ActionOutcome.UNVERIFIED,
    ],
)
def test_fallback_confirmation_does_not_trust_typed_line_prefix(
    tmp_path,
    outcome: ActionOutcome,
) -> None:
    monitor = SocMonitor(path=tmp_path / "soc.json")
    monitor.record(_measurement(ok=False), failure_threshold=1)
    monitor.request_fallback()
    snapshot = monitor.complete_fallback(action_line=ActionLine("[FALLBACK] claimed", outcome))
    assert not snapshot.fallback_confirmed


@pytest.mark.parametrize("outcome", list(ActionOutcome))
async def test_real_write_baseline_uses_outcomes(monkeypatch, outcome: ActionOutcome) -> None:
    from ha_spark.energy.scheduler import run_once
    from tests.authority_helpers import legacy_settings as Settings
    from tests.test_scheduler import _AmpsDevice, _patch_run_once_io, _plan_at

    settings = Settings(authority_mode="optimise")
    plan = _plan_at(31.0)
    device = _AmpsDevice({31.0: 38.0}, settings.battery_voltage_v)
    device.results = [[ActionLine("[APPLIED] presentation only", outcome)]]
    _patch_run_once_io(monkeypatch, plan, device)
    result = await run_once(settings, soc=plan.soc)
    verified = outcome in (ActionOutcome.VERIFIED, ActionOutcome.UNCHANGED_VERIFIED)
    assert result.applied_plan is (plan if verified else None)


@pytest.mark.parametrize(
    "line",
    [
        ActionLine("[APPLIED] simulated", ActionOutcome.SIMULATED),
        ActionLine("[APPLIED] refused", ActionOutcome.REFUSED),
        ActionLine("[APPLIED] blocked", ActionOutcome.BLOCKED),
        ActionLine("[APPLIED] failed", ActionOutcome.FAILED),
        ActionLine("[WARNING] read-back mismatch", ActionOutcome.UNVERIFIED),
        "[APPLIED] untyped apply result",
    ],
)
async def test_real_write_recovery_remains_active(tmp_path, line: str) -> None:
    from ha_spark.energy.scheduler import finish_recovery
    from tests.authority_helpers import legacy_settings as Settings

    monitor = SocMonitor(path=tmp_path / "soc.json")
    monitor.record(_measurement(ok=False), failure_threshold=1)
    monitor.request_fallback()
    monitor.complete_fallback(action_line=ActionLine("fallback confirmed", ActionOutcome.VERIFIED))
    await finish_recovery(Settings(authority_mode="optimise"), monitor, (line,))
    assert monitor.fallback_active


@pytest.mark.parametrize("mode", ["on", "simulate", "off"])
async def test_production_solis_paths_emit_typed_lines(tmp_path, monkeypatch, mode: str) -> None:
    from ha_spark.devices.inverters import solis
    from tests.test_solis_power_switch import FakeRest, _device, _intent, _now

    monkeypatch.setattr(solis, "_READ_BACK_DELAY_SECONDS", 0)
    rest = FakeRest()
    device = _device(rest, tmp_path, mode=mode, solis_fallback_current_a="20")
    intent = _intent()
    applied = await device.apply(intent)
    reconciled = await device.reconcile_holds(intent, _now())
    fallback = await device.apply_fallback(intent.soc)
    safe = await device.write_safe_state()
    rate = await device.set_charge_rate(1000)
    _, clock = await device.sync_clock()
    for lines in (applied, reconciled, fallback, safe, [rate], clock):
        assert lines
        assert all(isinstance(line, ActionLine) for line in lines)
    if mode == "on":
        assert recovery_verified(tuple(applied))
        assert fallback[-1].outcome is ActionOutcome.VERIFIED
    else:
        assert not recovery_verified(tuple(applied))
        assert fallback[-1].outcome is ActionOutcome.SIMULATED


async def test_real_write_baseline_rejects_plain_apply_results(monkeypatch) -> None:
    from ha_spark.energy.scheduler import run_once
    from tests.authority_helpers import legacy_settings as Settings
    from tests.test_scheduler import _AmpsDevice, _patch_run_once_io, _plan_at

    settings = Settings(authority_mode="optimise")
    plan = _plan_at(31.0)
    device = _AmpsDevice({31.0: 38.0}, settings.battery_voltage_v)
    _patch_run_once_io(monkeypatch, plan, device)
    result = await run_once(settings, soc=plan.soc)
    assert result.applied_plan is None


async def test_production_refused_switch_has_a_non_verified_outcome(tmp_path) -> None:
    from tests.test_solis_power_switch import _device, _intent, _now, _UnreadableSwitchRest

    device = _device(_UnreadableSwitchRest(), tmp_path)
    lines = await device.reconcile_holds(_intent(), _now())
    assert len(lines) == 1
    assert lines[0].startswith("[WARNING]")
    assert lines[0].outcome is ActionOutcome.REFUSED
    assert not recovery_verified(tuple(lines))


@pytest.mark.parametrize("mode", ["on", "simulate", "off"])
async def test_production_alphaess_emits_typed_lines(mode: str) -> None:
    from unittest.mock import AsyncMock

    from ha_spark.devices.inverters.alphaess import AlphaESSDevice
    from tests.authority_helpers import legacy_settings as Settings
    from tests.test_solis_power_switch import _intent

    settings = Settings(
        inverter="alphaess", authority_mode="optimise" if mode == "on" else "observe"
    )
    device = AlphaESSDevice(settings.devices[0], settings, AsyncMock())
    lines = await device.apply(_intent())
    assert lines and all(isinstance(line, ActionLine) for line in lines)
    rate = await device.set_charge_rate(1000)
    assert isinstance(rate, ActionLine)
    assert not is_verified(rate)


async def test_typed_safe_state_keeps_per_action_failure_isolation(tmp_path, monkeypatch) -> None:
    from ha_spark.devices.inverters import solis
    from tests.test_solis_power_switch import _device, _FailingSwitchWriteRest

    monkeypatch.setattr(solis, "_READ_BACK_DELAY_SECONDS", 0)
    device = _device(_FailingSwitchWriteRest(switch="Off"), tmp_path)
    lines = await device.write_safe_state()
    assert len(lines) == 2
    assert lines[0].outcome is ActionOutcome.FAILED
    assert is_verified(lines[1])
    assert _apply_failed(list(lines))

