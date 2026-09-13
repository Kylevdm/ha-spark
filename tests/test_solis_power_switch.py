"""#140: ha-spark owns the Solis power-switch lifecycle via a declarative reconcile.

Desired state is a pure function of the clock and ``intent.holds``:
``Off`` while a hold is active, ``On`` otherwise. No remembered edge, so a
restart mid-hold converges on the next tick.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.base import ControlAuthority
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.energy.models import ChargeIntent, ExportIntent
from ha_spark.energy.scheduler import setpoint_changed
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.models import EntityState

_LONDON = ZoneInfo("Europe/London")
_SWITCH = "select.solisac_power_switch"


def _soc(value: float = 60.0) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )


def _window(offset_minutes: int, *, minutes: int = 30) -> tuple[datetime, datetime]:
    start = datetime.now(_LONDON).replace(second=0, microsecond=0) + timedelta(
        minutes=offset_minutes
    )
    return start, start + timedelta(minutes=minutes)


def _intent(
    *,
    holds: tuple[tuple[datetime, datetime], ...] = (),
    soc: SocMeasurement | None = None,
    export: ExportIntent | None = None,
) -> ChargeIntent:
    return ChargeIntent(
        77.0, soc or _soc(), time(23, 30), time(5, 30), holds=holds, export=export
    )


def _export() -> ExportIntent:
    start = (datetime.now(_LONDON) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    end = start.replace(hour=12)
    return ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )


class FakeRest:
    """Minimal HA stub: option writes land in ``states`` so read-back verifies."""

    def __init__(self, *, switch: str = "On") -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.states: dict[str, str] = {
            _SWITCH: switch,
            "sensor.solis_control_work_mode_bitfield": "35",
            "sensor.solis_control_timed_charge_current": "0",
            "sensor.solis_control_timed_discharge_current": "0",
        }
        fields = (
            "timed_charge_start_hours", "timed_charge_start_minutes",
            "timed_charge_end_hours", "timed_charge_end_minutes",
            "timed_discharge_start_hours", "timed_discharge_start_minutes",
            "timed_discharge_end_hours", "timed_discharge_end_minutes",
        )
        for suffix in ("", "_2", "_3"):
            for field in fields:
                self.states[f"sensor.solis_control_{field}{suffix}"] = "0"

    async def get_state(self, entity_id: str) -> EntityState:
        return EntityState(entity_id=entity_id, state=self.states[entity_id], attributes={})

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        payload = data or {}
        self.calls.append((domain, service, payload))
        if (domain, service) == ("select", "select_option"):
            self.states[str(payload["entity_id"])] = str(payload["option"])
        if domain == "modbus" and service == "write_register":
            address = int(payload["address"])
            if address == 43142:
                self.states["sensor.solis_control_timed_discharge_current"] = "62.5"
            if address == 43141:
                self.states["sensor.solis_control_timed_charge_current"] = str(
                    int(payload["value"]) / 10
                )
        return []


def _device(
    rest: FakeRest, tmp_path, *, mode: str = "on", control: ControlAuthority | None = None
) -> SolisDevice:
    settings = Settings(
        proactive_mode=mode,
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity=_SWITCH,
        timezone="Europe/London",
    )
    config = settings.devices[0]
    if control is not None:
        config = config.model_copy(update={"control": control})
    return SolisDevice(config, settings, rest)  # type: ignore[arg-type]


def _options(rest: FakeRest) -> list[str]:
    return [
        str(call[2]["option"])
        for call in rest.calls
        if call[0:2] == ("select", "select_option")
    ]


@pytest.mark.asyncio
async def test_future_hold_does_not_switch_the_inverter_off_yet(tmp_path) -> None:
    rest = FakeRest(switch="On")
    await _device(rest, tmp_path).apply(_intent(holds=(_window(180),)))

    assert _options(rest) == []


@pytest.mark.asyncio
async def test_active_hold_switches_off_and_verifies_read_back(tmp_path) -> None:
    rest = FakeRest(switch="On")
    lines = await _device(rest, tmp_path).apply(_intent(holds=(_window(-10),)))

    assert _options(rest) == ["Off"]
    assert any(line.startswith("[APPLIED]") and "Off" in line for line in lines)


@pytest.mark.asyncio
async def test_no_active_hold_switches_the_inverter_back_on(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    lines = await _device(rest, tmp_path).apply(_intent(holds=(_window(-180),)))

    assert _options(rest) == ["On"]
    assert any(line.startswith("[APPLIED]") and "On" in line for line in lines)


@pytest.mark.asyncio
async def test_switch_already_in_the_desired_state_is_never_written(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    lines = await _device(rest, tmp_path).apply(_intent(holds=(_window(-10),)))

    assert _options(rest) == []
    assert any(line.startswith("[SKIP]") and "Off" in line for line in lines)


@pytest.mark.asyncio
async def test_active_hold_beats_export_and_the_refusal_is_explicit(tmp_path) -> None:
    rest = FakeRest(switch="On")
    lines = await _device(rest, tmp_path).apply(
        _intent(holds=(_window(-10),), export=_export())
    )

    assert _options(rest) == ["Off"]
    assert any("export refused" in line and "hold" in line for line in lines)
    assert not any(
        call[0:2] == ("modbus", "write_register") and int(call[2]["address"]) == 43142
        for call in rest.calls
    )


@pytest.mark.asyncio
async def test_untrusted_soc_still_reconciles_but_blocks_charge_programming(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    bad_soc = replace(_soc(), status=SocStatus.UNAVAILABLE, value=None, raw_state="unavailable")
    lines = await _device(rest, tmp_path).apply(_intent(soc=bad_soc))

    assert _options(rest) == ["On"]
    assert not any(call[0] == "modbus" for call in rest.calls)
    assert any(line.startswith("[BLOCKED]") for line in lines)


def test_setpoint_changed_across_a_hold_boundary_with_identical_intents() -> None:
    start, end = _window(-30)
    intent = _intent(holds=((start, end),))

    assert setpoint_changed(intent, intent, since=start, now=end) is True
    assert setpoint_changed(intent, intent, since=start, now=end - timedelta(minutes=1)) is False


@pytest.mark.parametrize(
    ("mode", "control"),
    [
        ("simulate", None),
        ("off", None),
        # "observe": real proactive mode, but authority is not ha-spark.
        ("on", ControlAuthority.OBSERVE),
    ],
)
@pytest.mark.asyncio
async def test_unauthorized_modes_compute_the_reconcile_without_writing(
    tmp_path, mode, control
) -> None:
    rest = FakeRest(switch="On")
    device = _device(rest, tmp_path, mode=mode, control=control)

    lines = await device.apply(_intent(holds=(_window(-10),)))

    assert rest.calls == []
    assert any("Off" in line for line in lines)
