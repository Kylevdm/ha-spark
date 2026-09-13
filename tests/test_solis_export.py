from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.inverters.solis import SolisDevice, _export_window
from ha_spark.energy.export_store import ExportEventStore
from ha_spark.energy.models import ChargeIntent, ExportIntent
from ha_spark.energy.scheduler import setpoint_changed
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.models import EntityState

_LONDON = ZoneInfo("Europe/London")


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


def _export(
    *, event_start: int = 10, event_end: int = 12, tz: ZoneInfo = _LONDON
) -> ExportIntent:
    # Local tz by default, as the planner builds its slots from a local horizon.
    start = (datetime.now(tz) + timedelta(days=1)).replace(
        hour=event_start, minute=0, second=0, microsecond=0
    )
    end = start.replace(hour=event_end)
    return ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )


def _intent(export: ExportIntent | None = None) -> ChargeIntent:
    return ChargeIntent(77.0, _soc(), time(23, 30), time(5, 30), export=export)


class FakeRest:
    def __init__(self, *, power: str = "On", work_mode: str = "35") -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.states: dict[str, str] = {
            "select.solisac_power_switch": power,
            "sensor.solis_control_work_mode_bitfield": work_mode,
            "sensor.solis_control_timed_charge_current": "0",
            "sensor.solis_control_timed_discharge_current": "0",
        }
        for field in (
            "timed_charge_start_hours", "timed_charge_start_minutes",
            "timed_charge_end_hours", "timed_charge_end_minutes",
            "timed_discharge_start_hours", "timed_discharge_start_minutes",
            "timed_discharge_end_hours", "timed_discharge_end_minutes",
        ):
            self.states[f"sensor.solis_control_{field}"] = "0"
        for suffix in ("_2", "_3"):
            for field in (
                "timed_charge_start_hours", "timed_charge_start_minutes",
                "timed_charge_end_hours", "timed_charge_end_minutes",
                "timed_discharge_start_hours", "timed_discharge_start_minutes",
                "timed_discharge_end_hours", "timed_discharge_end_minutes",
            ):
                self.states[f"sensor.solis_control_{field}{suffix}"] = "0"

    async def get_state(self, entity_id: str) -> EntityState:
        return EntityState(entity_id=entity_id, state=self.states[entity_id], attributes={})

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        payload = data or {}
        self.calls.append((domain, service, payload))
        if domain == "modbus" and service == "write_register":
            address = int(payload["address"])
            value = payload["value"]
            if address == 43142:
                self.states["sensor.solis_control_timed_discharge_current"] = "62.5"
            if address == 43141:
                self.states["sensor.solis_control_timed_charge_current"] = str(int(value) / 10)
            if address in (43143, 43153, 43163):
                slot = {43143: "", 43153: "_2", 43163: "_3"}[address]
                for field, item in zip(
                    (
                        "timed_charge_start_hours", "timed_charge_start_minutes",
                        "timed_charge_end_hours", "timed_charge_end_minutes",
                        "timed_discharge_start_hours", "timed_discharge_start_minutes",
                        "timed_discharge_end_hours", "timed_discharge_end_minutes",
                    ),
                    value,
                    strict=True,
                ):
                    self.states[f"sensor.solis_control_{field}{slot}"] = str(item)
        return []


class StaleDischargeReadback(FakeRest):
    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        payload = data or {}
        if domain == "modbus" and service == "write_register" and payload.get("address") == 43142:
            self.calls.append((domain, service, payload))
            return []
        return await super().call_service(domain, service, data)


class FailedCleanupReadback(FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.fail_cleanup = True

    async def get_state(self, entity_id: str) -> EntityState:
        if self.fail_cleanup and entity_id.startswith("sensor.solis_control_timed_discharge_"):
            raise RuntimeError("discharge read unavailable")
        return await super().get_state(entity_id)


def _device(rest: FakeRest, tmp_path, *, timezone: str = "Europe/London") -> SolisDevice:
    settings = Settings(
        proactive_mode="on",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
        timezone=timezone,
    )
    return SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_export_programs_fixed_current_and_atomic_window(tmp_path) -> None:
    rest = FakeRest()
    export = _export()
    lines = await _device(rest, tmp_path).apply(_intent(export))

    writes = [call[2] for call in rest.calls if call[0:2] == ("modbus", "write_register")]
    assert {int(w["address"]) for w in writes} >= {43142, 43143}
    block = next(w["value"] for w in writes if w["address"] == 43143)
    assert block == [23, 30, 5, 30, export.window_start.hour, 0, export.window_end.hour, 0]
    assert any(line.startswith("[APPLIED] set charge window") for line in lines)

    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        saved = await store.load()
    assert saved is not None
    expected_identity = (
        f"export|{export.event_identity[1].astimezone(UTC).isoformat()}|"
        f"{export.event_identity[2].astimezone(UTC).isoformat()}"
    )
    assert saved[0] == expected_identity


@pytest.mark.asyncio
async def test_export_refuses_power_switch_off_without_turning_it_on(tmp_path) -> None:
    rest = FakeRest(power="Off")
    lines = await _device(rest, tmp_path).apply(_intent(_export()))

    assert any("power_switch is 'Off'" in line for line in lines)
    export_blocks = [
        call for call in rest.calls
        if call[0:2] == ("modbus", "write_register")
        and call[2]["address"] == 43143
        and call[2]["value"][4:] != [0, 0, 0, 0]
    ]
    assert export_blocks == []
    assert not any(call[0:2] == ("select", "select_option") for call in rest.calls)


@pytest.mark.asyncio
async def test_export_is_independent_of_charge_only_work_mode_bit(tmp_path) -> None:
    rest = FakeRest(work_mode="3")
    export = _export()
    lines = await _device(rest, tmp_path).apply(_intent(export))

    writes = [call[2] for call in rest.calls if call[0:2] == ("modbus", "write_register")]
    block = next(w["value"] for w in writes if w["address"] == 43143)
    assert block[4:] == [export.window_start.hour, 0, export.window_end.hour, 0]
    assert any("export" in line and line.startswith("[APPLIED]") for line in lines)


@pytest.mark.asyncio
async def test_discharge_readback_failure_never_leaves_an_export_window(tmp_path) -> None:
    rest = StaleDischargeReadback()
    lines = await _device(rest, tmp_path).apply(_intent(_export()))

    writes = [call[2] for call in rest.calls if call[0:2] == ("modbus", "write_register")]
    export_blocks = [w for w in writes if w["address"] == 43143 and w["value"][4:] != [0, 0, 0, 0]]
    assert export_blocks == []
    assert any("discharge current was not confirmed" in line for line in lines)


@pytest.mark.asyncio
async def test_simulate_export_never_calls_home_assistant_write_services(tmp_path) -> None:
    rest = FakeRest()
    settings = Settings(
        proactive_mode="simulate",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    device = SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]

    lines = await device.apply(_intent(_export()))

    assert any(line.startswith("[SIMULATE]") for line in lines)
    assert not any(call[0] in {"modbus", "select"} for call in rest.calls)


@pytest.mark.asyncio
async def test_untrusted_soc_refuses_export_writes(tmp_path) -> None:
    rest = FakeRest()
    settings = Settings(
        proactive_mode="on",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    bad_soc = replace(_soc(), status=SocStatus.UNAVAILABLE, raw_state="unavailable")
    intent = ChargeIntent(77.0, bad_soc, time(23, 30), time(5, 30), export=_export())
    lines = await SolisDevice(settings.devices[0], settings, rest).apply(intent)  # type: ignore[arg-type]

    assert any(line.startswith("[BLOCKED]") for line in lines)
    assert not any(call[0] in {"modbus", "select"} for call in rest.calls)


@pytest.mark.asyncio
async def test_unchanged_export_program_skips_modbus_writes(tmp_path) -> None:
    rest = FakeRest()
    export = _export()
    intent = _intent(export)
    await _device(rest, tmp_path).apply(intent)
    rest.calls.clear()

    lines = await _device(rest, tmp_path).apply(intent)

    assert not any(call[0] == "modbus" for call in rest.calls)
    assert any(line.startswith("[SKIP]") for line in lines)


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_last_verified_event_record(tmp_path) -> None:
    rest = FakeRest()
    export = _export()
    await _device(rest, tmp_path).apply(_intent(export))

    await _device(FailedCleanupReadback(), tmp_path).apply(_intent())

    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        saved = await store.load()
    assert saved is not None
    assert saved[0] == (
        f"export|{export.event_identity[1].astimezone(UTC).isoformat()}|"
        f"{export.event_identity[2].astimezone(UTC).isoformat()}"
    )


@pytest.mark.asyncio
async def test_export_overlap_zeroes_charge_half(tmp_path) -> None:
    rest = FakeRest(work_mode="3")
    export = _export()
    intent = ChargeIntent(_soc().soc_now, _soc(), time(23, 30), time(5, 30), export=export)
    lines = await _device(rest, tmp_path).apply(intent)

    writes = [call[2] for call in rest.calls if call[0:2] == ("modbus", "write_register")]
    block = next(w["value"] for w in writes if w["address"] == 43143)
    assert block[:4] == [0, 0, 0, 0]
    assert any("export" in line for line in lines)


@pytest.mark.asyncio
async def test_cancellation_clears_discharge_even_when_grid_charge_is_blocked(tmp_path) -> None:
    rest = FakeRest(work_mode="3")
    initial = [23, 30, 5, 30, 10, 0, 12, 0]
    await rest.call_service("modbus", "write_register", {"address": 43143, "value": initial})

    lines = await _device(rest, tmp_path).apply(_intent())
    clear = [
        call[2]["value"] for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
    ]
    assert clear[-1] == [23, 30, 5, 30, 0, 0, 0, 0]
    assert any("clear timed export window" in line for line in lines)


def test_export_value_is_part_of_scheduler_setpoint() -> None:
    previous = _intent()
    current = replace(previous, export=_export())
    assert setpoint_changed(previous, current) is True


def test_export_cancellation_and_replacement_are_setpoint_changes() -> None:
    accepted = _intent(_export())
    assert setpoint_changed(accepted, _intent()) is True
    assert setpoint_changed(accepted, replace(accepted, export=_export(event_start=11))) is True


def test_export_window_is_resolved_to_inverter_local_wall_clock() -> None:
    """Slot 1 discharge registers are local wall-clock, like the charge half."""
    london = _LONDON
    # A BST event: 18:00-19:00 local is 17:00-18:00 UTC. An adapter may hand the
    # window over in UTC; the registers must still read 18:00-19:00.
    start = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)
    end = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
    intent = _intent(
        ExportIntent(
            event_identity=("export", start, end),
            window_start=start,
            window_end=end,
            planned_export_kw=3.2,
            dno_export_limit_kw=7.36,
            selected_slots=(start,),
            slot_export_kw=(3.2,),
        )
    )

    window = _export_window(intent, london)

    assert window is not None
    assert (window[0].hour, window[0].minute) == (18, 0)
    assert (window[1].hour, window[1].minute) == (19, 0)


@pytest.mark.asyncio
async def test_export_registers_follow_the_configured_timezone(tmp_path) -> None:
    """A non-UTC household clock shifts the programmed window, not the identity."""
    rest = FakeRest()
    export = _export(tz=ZoneInfo("UTC"))  # tomorrow 10:00-12:00 UTC
    device = _device(rest, tmp_path, timezone="Asia/Kolkata")  # UTC+5:30, no DST

    await device.apply(_intent(export))

    block = next(
        call[2]["value"] for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
    )
    assert block == [23, 30, 5, 30, 15, 30, 17, 30]
