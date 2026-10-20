"""#422: fallback schedule validation — one named reason per invalid case.

Validation is one pure function over the settings in force (plus the candidate
schedule's own export flag and controls, which the fallback's construction
never varies): either a valid fallback schedule or the named reason it is
invalid. An above-limit current is blocked with the case named, never clamped,
and the validation is re-run immediately before every apply, so a limit
lowered below the fallback current blocks the next apply.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time

import pytest

from ha_spark.devices.base import Capability
from ha_spark.devices.inverters.solis import (
    FallbackSchedule,
    SolisDevice,
    validate_fallback_schedule,
    validated_fallback_current_a,
)
from ha_spark.devices.outcomes import ActionOutcome
from ha_spark.energy import scheduler
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from tests.authority_helpers import legacy_settings as Settings
from tests.test_solis_power_switch import FakeRest, _device, _export, _register_writes, _soc

# One invalid case per entry: (id, settings overrides, expected reason substring).
# Overrides land via model_copy, so cases pydantic itself would refuse (nan,
# above the register limit) still reach the pure validator.
_CASES = [
    ("not-configured", {"solis_fallback_current_a": None},
     "solis_fallback_current_a is not configured"),
    ("non-finite-current", {"solis_fallback_current_a": float("nan")},
     "solis_fallback_current_a is not a finite current"),
    ("below-1a", {"solis_fallback_current_a": 0.5},
     "solis_fallback_current_a must be a current of at least 1 A"),
    ("above-configured-max", {"max_charge_current_a": 35.0},
     "is above max_charge_current_a (35 A)"),
    ("above-register-limit", {"solis_fallback_current_a": 70.0, "max_charge_current_a": 80.0},
     "is above the inverter register limit (62.5 A)"),
    ("above-site-supply", {"supply_max_current_a": 8.0},
     "is above the site supply limit (1920 W)"),
    ("max-not-a-positive-finite-limit", {"max_charge_current_a": 0.0},
     "max_charge_current_a is not a positive finite limit"),
    ("battery-voltage-not-positive", {"battery_voltage_v": 0.0},
     "battery_voltage_v is not a positive finite limit"),
    ("supply-max-not-positive", {"supply_max_current_a": 0.0},
     "supply_max_current_a is not a positive finite limit"),
    ("supply-voltage-not-positive", {"supply_voltage_v": 0.0},
     "supply_voltage_v is not a positive finite limit"),
    ("start-unparseable", {"charge_window_start": "banana"},
     "charge_window_start 'banana' is not a valid HH:MM time"),
    ("end-unparseable", {"charge_window_end": "24:00"},
     "charge_window_end '24:00' is not a valid HH:MM time"),
    ("start-not-on-clock", {"charge_window_start": "23:30:15"},
     "charge_window_start '23:30:15' is not a time on the household clock"),
    ("end-not-on-clock", {"charge_window_end": "05:30+01:00"},
     "charge_window_end '05:30+01:00' is not a time on the household clock"),
    ("zero-length", {"charge_window_start": "05:30"},
     "the fallback charge window is zero-length"),
]


def _reason(**overrides: object) -> str:
    """The named reason one invalid case produces (the defaults are a valid base)."""
    settings = Settings(solis_fallback_current_a=42.0).model_copy(update=overrides)
    result = validate_fallback_schedule(settings)
    assert isinstance(result, str), f"expected a reason, got {result!r}"
    return result


@pytest.mark.parametrize(
    ("case", "overrides", "expected"),
    [pytest.param(case, overrides, expected, id=case) for case, overrides, expected in _CASES],
)
def test_invalid_fallback_case_is_named(case: str, overrides: dict, expected: str) -> None:
    assert expected in _reason(**overrides)


def test_fallback_schedule_with_export_is_invalid() -> None:
    """The fallback must clear/never program discharge: export is refused."""
    result = validate_fallback_schedule(
        Settings(solis_fallback_current_a=42.0), export=_export()
    )
    assert result == "the fallback schedule must not program export or discharge"


def test_fallback_control_without_read_back_support_is_invalid() -> None:
    result = validate_fallback_schedule(
        Settings(solis_fallback_current_a=42.0),
        controls=frozenset({Capability.CHARGE_WINDOW, Capability.STOP_DISCHARGE}),
    )
    assert result == "fallback control stop_discharge has no read-back support"


def test_every_invalid_case_reason_is_distinct() -> None:
    """Each invalid case names its own reason; no two cases share a string."""
    settings = Settings(solis_fallback_current_a=42.0)
    reasons = [_reason(**overrides) for _case, overrides, _expected in _CASES]
    reasons += [
        validate_fallback_schedule(settings, export=_export()),
        validate_fallback_schedule(
            settings, controls=frozenset({Capability.STOP_DISCHARGE})
        ),
    ]
    assert all(isinstance(reason, str) for reason in reasons)
    assert len(reasons) == len(set(reasons))


def test_non_finite_limit_shares_its_limits_named_reason() -> None:
    """Non-finite and non-positive are the same named case per limit."""
    for field in (
        "max_charge_current_a", "battery_voltage_v",
        "supply_max_current_a", "supply_voltage_v",
    ):
        reason = _reason(**{field: float("nan")})
        assert reason == f"{field} is not a positive finite limit"


def test_valid_fallback_schedule_carries_current_and_window() -> None:
    settings = Settings(
        solis_fallback_current_a=42.0,
        charge_window_start="01:15",
        charge_window_end="04:30",
    )
    assert validate_fallback_schedule(settings) == FallbackSchedule(
        42, time(1, 15), time(4, 30)
    )
    # The register encodes whole amps; a fractional current floors, never clamps.
    assert validate_fallback_schedule(
        Settings(solis_fallback_current_a=42.9)
    ) == FallbackSchedule(42, time(23, 30), time(5, 30))


def test_validated_fallback_current_a_is_none_while_invalid() -> None:
    assert validated_fallback_current_a(Settings(solis_fallback_current_a=42.0)) == 42
    assert validated_fallback_current_a(
        Settings(solis_fallback_current_a=42.0, max_charge_current_a=35.0)
    ) is None
    assert validated_fallback_current_a(Settings()) is None


# --- the device apply re-runs validation immediately before every apply ---


def _failed_soc() -> SocMeasurement:
    return replace(_soc(), status=SocStatus.STALE)


@pytest.mark.asyncio
async def test_limit_lowered_below_the_fallback_current_blocks_the_next_apply(tmp_path) -> None:
    """(#422) The apply re-validates against the settings in force then: a limit
    lowered below the fallback current blocks the next apply with the case
    named, instead of silently clamping to the new limit."""
    rest = FakeRest(switch="On")
    device = _device(rest, tmp_path, solis_fallback_current_a="45")

    first = await device.apply_fallback(_failed_soc())

    assert any(line.startswith("[FALLBACK]") for line in first)
    written = _register_writes(rest)
    assert written[0] == (43141, 450)

    lowered = _device(rest, tmp_path, max_charge_current_a="35", solis_fallback_current_a="45")
    second = await lowered.apply_fallback(_failed_soc())

    assert second[0].outcome is ActionOutcome.BLOCKED
    assert "is above max_charge_current_a (35 A)" in second[0]
    assert not any(line.startswith("[FALLBACK]") for line in second)
    # No further hardware write: the blocked apply left the resident program alone.
    assert _register_writes(rest) == written


@pytest.mark.asyncio
async def test_blocked_fallback_is_retryable_not_confirmed(tmp_path) -> None:
    """A validation block reaches the monitor as an unconfirmed attempt (the
    `[FAILED] ... unconfirmed` retry path), never as a confirmed fallback."""
    from ha_spark.energy.soc_monitor import SocMonitor

    rest = FakeRest(switch="On")
    device = _device(rest, tmp_path, max_charge_current_a="35", solis_fallback_current_a="45")
    monitor = SocMonitor()
    monitor.record(_failed_soc(), failure_threshold=1)
    assert monitor.request_fallback() is not None

    lines = await device.apply_fallback(_failed_soc())
    snapshot = monitor.complete_fallback(action_line=lines[0])

    assert monitor.fallback_active
    assert snapshot.fallback_confirmed is False
    assert snapshot.fallback_action is not None
    assert snapshot.fallback_action.startswith("[FAILED] Solis fallback unconfirmed: [BLOCKED]")
    assert _register_writes(rest) == []


@pytest.mark.asyncio
async def test_fallback_entry_save_adopts_the_bumped_epoch_for_the_same_tick(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#422) The store is saved (active + cause + bumped epoch) before the
    fallback write is requested, and the apply on the same tick plans under the
    new epoch; work planned under the old epoch is still refused."""
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    from ha_spark.devices.execution import execution_context

    settings = Settings(authority_mode="optimise", solis_fallback_current_a="45")
    context = execution_context()
    old_epoch = context.planned_epoch.get()
    assert old_epoch is not None

    assert scheduler._save_fallback_authority(True) is True
    assert context.planned_epoch.get() == old_epoch + 1

    rest = FakeRest(switch="On")
    device = SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]
    lines = await device.apply_fallback(_failed_soc())
    assert any(line.startswith("[FALLBACK]") for line in lines)
    written = _register_writes(rest)

    # Anything planned under the old epoch must still be refused.
    context.planned_epoch.set(old_epoch)
    refused = await device.apply_fallback(_failed_soc())
    assert refused[0].outcome is ActionOutcome.REFUSED
    assert "stale authority" in refused[0]
    assert _register_writes(rest) == written
