"""#537: the legacy Solis driver commands within the effective limits.

With legacy settings the effective per-inverter limit is min(``max_charge_current_a``,
the 62.5 A driver ceiling), and every commanded current is rounded down to whole
amps. The fixed 62.5 A export command is refused when it exceeds an effective
discharge ceiling, and the fallback and the daemon supply guard read the effective
connection import.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from ha_spark.devices.inverters.solis import (
    FIXED_EXPORT_REFUSAL,
    FallbackSchedule,
    SolisDevice,
    validate_fallback_schedule,
)
from ha_spark.energy.models import ChargeIntent
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.supply_guard import SupplyGuard
from tests.authority_helpers import legacy_settings as Settings
from tests.test_solis_export import FakeRest, _export
from tests.test_solis_export import _intent as _export_intent


@pytest.fixture(autouse=True)
def _skip_read_back_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)


def _device(rest: FakeRest, tmp_path, **overrides: object) -> SolisDevice:
    settings = Settings(
        authority_mode="optimise",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
        timezone="Europe/London",
        **overrides,  # type: ignore[arg-type]
    )
    return SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]


def _saturating_intent() -> ChargeIntent:
    """One minute of window left and 10% to add: sized far above any ceiling."""
    at = datetime(2026, 10, 4, 4, 29, tzinfo=UTC)  # 05:29 BST
    soc = SocMeasurement(
        status=SocStatus.OK,
        observed_at=at,
        value=60.0,
        raw_state="60.0",
        reported_at=at,
        age_s=0.0,
        max_age_s=600.0,
    )
    return ChargeIntent(70.0, soc, time(23, 30), time(5, 30))


def _current_writes(rest: FakeRest) -> list[object]:
    return [
        call[2]["value"]
        for call in rest.calls
        if call[:2] == ("modbus", "write_register") and call[2]["address"] == 43141
    ]


# --- normal and restore currents ---


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner_max_a", "written_raw"), [(100.0, 620), (42.6, 420)])
async def test_normal_current_is_capped_at_the_effective_limit_and_rounded_down(
    tmp_path, owner_max_a: float, written_raw: int
) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, max_charge_current_a=owner_max_a)

    lines = await device.apply(_saturating_intent())

    assert _current_writes(rest) == [written_raw]
    assert any(f"charge current to {written_raw // 10} A" in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner_max_a", "written_raw"), [(100.0, 620), (42.6, 420)])
async def test_restore_current_is_capped_at_the_effective_limit_and_rounded_down(
    tmp_path, owner_max_a: float, written_raw: int
) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, max_charge_current_a=owner_max_a, battery_voltage_v=51.0)

    await device.set_charge_rate(100.0 * 51.0)

    assert _current_writes(rest) == [written_raw]


@pytest.mark.asyncio
async def test_settings_outside_the_topology_envelope_command_no_charge(tmp_path) -> None:
    """An unusable legacy limit narrows to zero, never to a default current."""
    rest = FakeRest()
    rest.states["sensor.solis_control_timed_charge_current"] = "30"
    device = _device(rest, tmp_path)
    device._settings = device._settings.model_copy(update={"supply_max_current_a": 500.0})

    await device.set_charge_rate(30.0 * 51.0)

    assert _current_writes(rest) == [0]


# --- fallback ---


def test_fallback_within_the_effective_limit_keeps_its_current() -> None:
    result = validate_fallback_schedule(
        Settings(solis_fallback_current_a=40.0, max_charge_current_a=100.0)
    )
    assert result == FallbackSchedule(40, time(23, 30), time(5, 30))


def test_fallback_above_the_effective_limit_is_refused() -> None:
    settings = Settings(max_charge_current_a=100.0).model_copy(
        update={"solis_fallback_current_a": 63.0}
    )
    assert validate_fallback_schedule(settings) == (
        "solis_fallback_current_a (63 A) is above the inverter register limit (62.5 A)"
    )


def test_fallback_above_the_effective_site_supply_is_refused() -> None:
    # 40 A x 51 V = 2040 W against 8 A x 240 V = 1920 W of connection import.
    result = validate_fallback_schedule(
        Settings(solis_fallback_current_a=40.0, supply_max_current_a=8.0)
    )
    assert result == (
        "fallback charge power (2040 W) is above the site supply limit (1920 W)"
    )


def test_fallback_outside_the_topology_envelope_is_refused() -> None:
    settings = Settings(solis_fallback_current_a=40.0).model_copy(
        update={"supply_max_current_a": 500.0}
    )
    assert validate_fallback_schedule(settings) == (
        "the legacy limits are outside the supported topology"
    )


# --- export command ---


@pytest.mark.asyncio
async def test_export_command_above_the_effective_discharge_ceiling_is_refused(
    tmp_path,
) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, max_charge_current_a=50.0)

    lines = await device.apply(_export_intent(_export()))

    assert f"[BLOCKED] export refused: {FIXED_EXPORT_REFUSAL}" in lines
    assert not any(
        call[2].get("address") == 43142 for call in rest.calls if call[0] == "modbus"
    )
    assert rest.states["sensor.solis_control_timed_discharge_start_hours"] == "0"


@pytest.mark.asyncio
async def test_export_command_with_default_limits_is_programmed(tmp_path) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path)
    export = _export()

    lines = await device.apply(_export_intent(export))

    assert not any("export refused" in line for line in lines)
    assert rest.states["sensor.solis_control_timed_discharge_current"] == "62.5"
    assert rest.states["sensor.solis_control_timed_discharge_start_hours"] == str(
        export.window_start.hour
    )


def test_fixed_export_refusal_reason_is_shared_with_the_planner() -> None:
    assert FIXED_EXPORT_REFUSAL == "fixed export command exceeds the effective discharge ceiling"


# --- daemon supply guard ---


class _GuardRest(FakeRest):
    def __init__(self, supply_w: float, setpoint_a: float) -> None:
        super().__init__()
        self.states["sensor.grid_power"] = str(supply_w)
        self.states["sensor.solis_control_timed_charge_current"] = str(setpoint_a)


@pytest.mark.asyncio
async def test_daemon_guard_cuts_to_the_effective_connection_import(tmp_path) -> None:
    # 20 A x 240 V = 4800 W of import; 4000 W other load + 1020 W charging.
    # The battery gets 800 W: 800 / 51 = 15.7 A, rounded down to 15 A.
    settings = Settings(
        authority_mode="optimise",
        db_path=str(tmp_path / "events.db"),
        grid_power_entity="sensor.grid_power",
        supply_max_current_a=20.0,
        battery_voltage_v=51.0,
    )
    rest = _GuardRest(supply_w=5020.0, setpoint_a=20.0)

    line = await SupplyGuard(settings, rest).tick(2000.0)  # type: ignore[arg-type]

    assert line is not None
    assert _current_writes(rest) == [150]


@pytest.mark.asyncio
async def test_daemon_guard_outside_the_topology_envelope_never_grants_charge(
    tmp_path,
) -> None:
    settings = Settings(
        authority_mode="optimise",
        db_path=str(tmp_path / "events.db"),
        grid_power_entity="sensor.grid_power",
        battery_voltage_v=51.0,
    ).model_copy(update={"supply_max_current_a": 500.0})
    rest = _GuardRest(supply_w=1020.0, setpoint_a=20.0)

    await SupplyGuard(settings, rest).tick(2040.0)  # type: ignore[arg-type]

    assert _current_writes(rest) == [0]

