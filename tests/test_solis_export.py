from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.inverters.solis import (
    SolisDevice,
    _export_not_yet_armed,
    _export_window,
)
from ha_spark.energy.export_store import ExportEventStore
from ha_spark.energy.models import ChargeIntent, ExportIntent
from ha_spark.energy.scheduler import setpoint_changed
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.models import EntityState


@pytest.fixture(autouse=True)
def _skip_read_back_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bounded read-back retry tests fast without changing production timing."""
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)


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
    *, hours_ahead: float = 2.0, duration_h: float = 2.0, tz: ZoneInfo = _LONDON
) -> ExportIntent:
    """An armed event: near enough that its clock face next comes round at it.

    Built relative to ``now`` rather than pinned to a time of day. A fixed
    "tomorrow at 10:00" is only armed when the suite happens to run after
    10:00 — before that the day-early guard refuses it, which is the whole
    point of #144.
    """
    # Local tz by default, as the planner builds its slots from a local horizon.
    start = (datetime.now(tz) + timedelta(hours=hours_ahead)).replace(
        minute=0, second=0, microsecond=0
    )
    end = start + timedelta(hours=duration_h)
    return ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )


def _intent(
    export: ExportIntent | None = None, *, export_trusted: bool = True
) -> ChargeIntent:
    return ChargeIntent(
        77.0,
        _soc(),
        time(23, 30),
        time(5, 30),
        export=export,
        export_trusted=export_trusted,
    )


CLOCK = "sensor.solis_control_inverter_clock"


def _clock_face(at: datetime, offset: timedelta = timedelta(0), zone: ZoneInfo = _LONDON) -> str:
    face = at.astimezone(zone) + offset
    return f"{face.year % 100},{face.month},{face.day},{face.hour},{face.minute},{face.second}"


class FakeRest:
    def __init__(self, *, power: str = "On", work_mode: str = "35") -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        # The inverter clock reads the household clock, freshly, unless a test
        # sets `clock_offset`, `clock_age` or `clock_state`.
        self.clock_offset = timedelta(0)
        self.clock_age = timedelta(seconds=5)
        self.clock_state: str | None = None
        self.clock_zone = _LONDON
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
        if entity_id == CLOCK:
            read_at = datetime.now(UTC) - self.clock_age
            state = self.clock_state or _clock_face(read_at, self.clock_offset, self.clock_zone)
            return EntityState(entity_id=entity_id, state=state, last_updated=read_at)
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


class SequencedCurrentRest(FakeRest):
    def __init__(self, readings: list[str]) -> None:
        super().__init__()
        self.current_readings = readings
        self.current_reads = 0

    async def get_state(self, entity_id: str) -> EntityState:
        if entity_id == "sensor.solis_control_timed_charge_current":
            reading_index = self.current_reads
            self.current_reads += 1
            if reading_index < len(self.current_readings):
                reading = self.current_readings[reading_index]
                return EntityState(entity_id=entity_id, state=reading, attributes={})
            if self.current_readings:
                reading = self.current_readings[-1]
                return EntityState(entity_id=entity_id, state=reading, attributes={})
        return await super().get_state(entity_id)

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        result = await super().call_service(domain, service, data)
        if (
            domain == "modbus"
            and service == "write_register"
            and (data or {}).get("address") == 43141
        ):
            self.current_readings = []
            self.current_reads = 0
        return result


_SLOT_FIELDS = (
    "timed_charge_start_hours",
    "timed_charge_start_minutes",
    "timed_charge_end_hours",
    "timed_charge_end_minutes",
    "timed_discharge_start_hours",
    "timed_discharge_start_minutes",
    "timed_discharge_end_hours",
    "timed_discharge_end_minutes",
)


class SequencedSlotBlockRest(FakeRest):
    def __init__(self, readings: list[list[int]]) -> None:
        super().__init__()
        self.block_readings = readings
        self.block_reads = 0
        self.current_block: list[int] | None = None
        self._set_slot([1, 0, 2, 0, 3, 0, 4, 0])

    def _set_slot(self, block: list[int]) -> None:
        for field, value in zip(_SLOT_FIELDS, block, strict=True):
            self.states[f"sensor.solis_control_{field}"] = str(value)

    async def get_state(self, entity_id: str) -> EntityState:
        prefix = "sensor.solis_control_"
        if entity_id.startswith(prefix) and entity_id[len(prefix) :] in _SLOT_FIELDS:
            if entity_id.endswith("timed_charge_start_hours"):
                if self.block_reads < len(self.block_readings):
                    self.current_block = self.block_readings[self.block_reads]
                else:
                    self.current_block = None
                self.block_reads += 1
            if self.current_block is not None:
                field = entity_id[len(prefix) :]
                value = self.current_block[_SLOT_FIELDS.index(field)]
                return EntityState(entity_id=entity_id, state=str(value), attributes={})
        return await super().get_state(entity_id)


class StaleDischargeReadback(FakeRest):
    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        payload = data or {}
        if domain == "modbus" and service == "write_register" and payload.get("address") == 43142:
            self.calls.append((domain, service, payload))
            return []
        return await super().call_service(domain, service, data)


class StaleChargeCurrentReadback(FakeRest):
    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        payload = data or {}
        if domain == "modbus" and service == "write_register" and payload.get("address") == 43141:
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


def _device(
    rest: FakeRest,
    tmp_path,
    *,
    timezone: str = "Europe/London",
    notify_service: str = "",
) -> SolisDevice:
    settings = Settings(
        proactive_mode="on",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
        timezone=timezone,
        notify_service=notify_service,
    )
    return SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_zero_current_readback_rejects_a_transient_zero(tmp_path) -> None:
    rest = SequencedCurrentRest(["0", "12"])

    mismatch = await _device(rest, tmp_path)._verify_current(0)

    assert mismatch == "read back 12 A (wanted 0 A)"


@pytest.mark.asyncio
async def test_zero_current_readback_resets_after_mismatch_when_budget_is_too_short(
    tmp_path,
) -> None:
    rest = SequencedCurrentRest(["0", *(["12"] * 10), *(["0"] * 5)])

    mismatch = await _device(rest, tmp_path)._verify_current(0)

    assert mismatch is not None
    assert rest.current_reads == 16


@pytest.mark.asyncio
async def test_zero_current_readback_passes_after_reset_and_full_held_streak(tmp_path) -> None:
    rest = SequencedCurrentRest(["0", *(["12"] * 5), *(["0"] * 6)])

    mismatch = await _device(rest, tmp_path)._verify_current(0)

    assert mismatch is None
    assert rest.current_reads == 12


@pytest.mark.asyncio
async def test_zero_current_readback_accepts_a_held_zero(tmp_path) -> None:
    rest = SequencedCurrentRest(["0"])

    mismatch = await _device(rest, tmp_path)._verify_current(0)

    assert mismatch is None
    assert rest.current_reads == 6


@pytest.mark.asyncio
async def test_nonzero_current_readback_keeps_first_match(tmp_path) -> None:
    rest = SequencedCurrentRest(["62.5", "0"])

    mismatch = await _device(rest, tmp_path)._verify_current(62.5)

    assert mismatch is None
    assert rest.current_reads == 1


@pytest.mark.asyncio
async def test_transient_zero_on_current_preread_does_not_skip_write(tmp_path) -> None:
    rest = SequencedCurrentRest(["0", "12"])

    _ok, line = await _device(rest, tmp_path)._set_current_result(0, "zero current")

    assert line == "[APPLIED] zero current"
    assert any(
        call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43141
        for call in rest.calls
    )


@pytest.mark.asyncio
async def test_held_zero_current_is_confirmed_once_before_skipping_write(tmp_path) -> None:
    rest = SequencedCurrentRest(["0"])

    _ok, line = await _device(rest, tmp_path)._set_current_result(0, "zero current")

    assert line == "[APPLIED] zero current"
    assert rest.current_reads == 7
    assert not any(call[0] == "modbus" for call in rest.calls)


@pytest.mark.asyncio
async def test_zero_slot_readback_rejects_a_transient_zero(tmp_path) -> None:
    rest = SequencedSlotBlockRest([[0] * len(_SLOT_FIELDS)])

    mismatch = await _device(rest, tmp_path)._verify_slot_block(1, [0] * len(_SLOT_FIELDS))

    assert mismatch == f"read back slot 1 {[1, 0, 2, 0, 3, 0, 4, 0]} (wanted {[0] * 8})"


@pytest.mark.asyncio
async def test_zero_slot_readback_accepts_a_held_zero(tmp_path) -> None:
    rest = FakeRest()

    mismatch = await _device(rest, tmp_path)._verify_slot_block(1, [0] * len(_SLOT_FIELDS))

    assert mismatch is None


@pytest.mark.asyncio
async def test_transient_zero_slot_preread_does_not_skip_zeroing_write(tmp_path) -> None:
    rest = SequencedSlotBlockRest([[0] * len(_SLOT_FIELDS)])

    ok, line = await _device(rest, tmp_path)._zero_slot(1, "zero slot 1")

    assert ok
    assert line == "[APPLIED] zero slot 1"
    assert any(
        call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
        for call in rest.calls
    )


@pytest.mark.asyncio
async def test_held_zero_slot_preread_skips_only_after_confirmation(tmp_path) -> None:
    rest = SequencedSlotBlockRest([])
    rest._set_slot([0] * len(_SLOT_FIELDS))

    ok, line = await _device(rest, tmp_path)._zero_slot(1, "zero slot 1")

    assert ok
    assert line == "[SKIP] slot 1 already zeroed"
    assert rest.block_reads == 7
    assert not any(call[0] == "modbus" for call in rest.calls)


@pytest.mark.asyncio
async def test_transient_zero_generic_slot_preread_does_not_skip_write(tmp_path) -> None:
    rest = SequencedSlotBlockRest([[0] * len(_SLOT_FIELDS)])

    wrote, _zero_confirmed = await _device(rest, tmp_path)._apply_slot_block(
        1, [0] * len(_SLOT_FIELDS)
    )

    assert wrote
    assert any(
        call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
        for call in rest.calls
    )


@pytest.mark.asyncio
async def test_held_zero_generic_slot_preread_is_confirmed_once(tmp_path) -> None:
    rest = SequencedSlotBlockRest([])
    rest._set_slot([0] * len(_SLOT_FIELDS))

    wrote, zero_confirmed = await _device(rest, tmp_path)._apply_slot_block(
        1, [0] * len(_SLOT_FIELDS)
    )

    assert not wrote
    assert zero_confirmed
    assert rest.block_reads == 7
    assert not any(call[0] == "modbus" for call in rest.calls)


@pytest.mark.asyncio
async def test_transient_zero_slot_one_preread_still_deactivates_before_current_change(
    tmp_path,
) -> None:
    rest = SequencedSlotBlockRest([[0] * len(_SLOT_FIELDS)])

    _ok, _line, _deactivation = await _device(rest, tmp_path)._prepare_slot_one_current(
        _intent(), [0, 0, 0, 0]
    )

    assert any(
        call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
        for call in rest.calls
    )


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
async def test_export_refuses_a_power_switch_that_is_off(tmp_path) -> None:
    """Export is a read-only refusal: it never asks for the switch to be turned on.

    ``apply`` writes no select at all — #51's ban, and the reason the reconcile
    was moved off this seam (#143). A switch left ``Off`` with no hold active is
    driven back to ``On``, but by ``reconcile_holds`` answering the clock, on the
    pass the caller makes before this one; never by export asking for it.
    ``tests/test_solis_power_switch.py`` pins the other half of the separation:
    with a hold active the switch goes ``Off`` and export is refused.
    """
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
    assert [call for call in rest.calls if call[0:2] == ("select", "select_option")] == []


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
async def test_accepted_export_notification_is_sent_once_for_replanning(tmp_path) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, notify_service="mobile_app_phone")
    intent = _intent(_export())

    await device.apply(intent)
    await device.apply(intent)

    notifications = [call for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert len(notifications) == 1
    assert notifications[0][2]["title"] == "Axle export accepted"
    assert "Required preparation" in notifications[0][2]["message"]


@pytest.mark.asyncio
async def test_simulate_mode_notifies_of_an_accepted_event_without_claiming_a_write(
    tmp_path,
) -> None:
    rest = FakeRest()
    settings = Settings(
        proactive_mode="simulate",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
        notify_service="mobile_app_phone",
    )
    device = SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]

    await device.apply(_intent(_export()))

    notifications = [call for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert [call[2]["title"] for call in notifications] == ["Axle export accepted"]
    assert not any(call[0] == "modbus" for call in rest.calls)


@pytest.mark.asyncio
async def test_changed_export_identity_gets_a_new_acceptance_notice(tmp_path) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, notify_service="mobile_app_phone")

    await device.apply(_intent(_export(hours_ahead=2.0)))
    await device.apply(_intent(_export(hours_ahead=3.0)))

    notifications = [call for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert [call[2]["title"] for call in notifications] == [
        "Axle export accepted",
        "Axle export accepted",
    ]


@pytest.mark.asyncio
async def test_cleanup_notification_follows_verified_discharge_clear(tmp_path) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, notify_service="mobile_app_phone")
    export = _export()

    await device.apply(_intent(export))
    rest.calls.clear()
    await device.apply(_intent())
    await device.apply(_intent())

    notifications = [call for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert len(notifications) == 1
    assert notifications[0][2]["title"] == "Axle export cleanup verified"
    assert "cleared and read back" in notifications[0][2]["message"]


@pytest.mark.asyncio
async def test_started_notification_follows_verified_active_export(tmp_path) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, notify_service="mobile_app_phone")
    now = datetime.now(_LONDON).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=30)
    end = now + timedelta(minutes=30)
    export = ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )

    await device.apply(_intent(export))

    notifications = [call for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert [call[2]["title"] for call in notifications] == [
        "Axle export accepted",
        "Axle export started",
    ]
    assert "read back successfully" in notifications[1][2]["message"]


@pytest.mark.asyncio
async def test_abort_notification_reports_refusal_and_safe_result(tmp_path) -> None:
    rest = FakeRest(power="Off")
    device = _device(rest, tmp_path, notify_service="mobile_app_phone")

    await device.apply(_intent(_export()))

    notifications = [call for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert len(notifications) == 1
    assert notifications[0][2]["title"] == "Axle export aborted"
    assert "power_switch is 'Off'" in notifications[0][2]["message"]
    assert "no export window remains programmed" in notifications[0][2]["message"]


@pytest.mark.asyncio
async def test_untrusted_hold_refusal_does_not_emit_terminal_abort(tmp_path) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path, notify_service="mobile_app_phone")

    await device.apply(replace(_intent(_export()), hold_trusted=False))

    notifications = [call for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert notifications == []


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
async def test_untrusted_axle_read_preserves_a_live_verified_export(tmp_path) -> None:
    rest = FakeRest()
    export = _export()
    device = _device(rest, tmp_path)
    await device.apply(_intent(export))
    rest.calls.clear()

    lines = await device.apply(_intent(export_trusted=False))

    assert lines == ["[SKIP] Axle event read untrusted; preserving the verified export window"]
    assert not any(call[0] == "modbus" for call in rest.calls)
    block = [
        rest.states[f"sensor.solis_control_{field}"]
        for field in (
            "timed_discharge_start_hours",
            "timed_discharge_start_minutes",
            "timed_discharge_end_hours",
            "timed_discharge_end_minutes",
        )
    ]
    assert block == [str(export.window_start.hour), "0", str(export.window_end.hour), "0"]

    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        assert await store.load() is not None


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
    assert setpoint_changed(accepted, replace(accepted, export=_export(hours_ahead=3))) is True


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
    kolkata = ZoneInfo("Asia/Kolkata")
    rest.clock_zone = kolkata
    export = _export(tz=ZoneInfo("UTC"))  # a whole UTC hour, so :30 in IST
    device = _device(rest, tmp_path, timezone="Asia/Kolkata")  # UTC+5:30, no DST

    await device.apply(_intent(export))

    block = next(
        call[2]["value"] for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
    )
    start_ist = export.window_start.astimezone(kolkata)
    end_ist = export.window_end.astimezone(kolkata)
    assert block == [23, 30, 5, 30, start_ist.hour, 30, end_ist.hour, 30]


def _discharge_writes(rest: FakeRest) -> list[dict[str, object]]:
    """Every write that could actuate a discharge: the current, or Slot 1's second half."""
    out: list[dict[str, object]] = []
    for call in rest.calls:
        if call[0:2] != ("modbus", "write_register"):
            continue
        payload = call[2]
        if int(payload["address"]) == 43142:
            out.append(payload)
        if int(payload["address"]) == 43143 and any(list(payload["value"])[4:]):
            out.append(payload)
    return out


@pytest.mark.asyncio
async def test_an_event_a_day_out_is_refused_until_its_clock_face_comes_round(tmp_path) -> None:
    """The Slot 1 registers hold no date, so a day-early write exports a day early.

    25 hours ahead puts the event's clock face roughly an hour from now: writing
    it today would discharge the battery tonight, outside the paid window.
    """
    rest = FakeRest()
    export = _export(hours_ahead=25.0)

    lines = await _device(rest, tmp_path).apply(_intent(export))

    assert any("export refused" in line and "not yet armed" in line for line in lines)
    assert _discharge_writes(rest) == []


@pytest.mark.asyncio
async def test_a_deferred_event_arms_once_its_clock_face_is_the_next_occurrence(tmp_path) -> None:
    """The same event, re-offered nearer the time, programs normally."""
    rest = FakeRest()

    lines = await _device(rest, tmp_path).apply(_intent(_export(hours_ahead=2.0)))

    assert not any("export refused" in line for line in lines)
    assert _discharge_writes(rest) != []


@pytest.mark.asyncio
async def test_a_day_early_refusal_leaves_no_event_record_to_clean_up(tmp_path) -> None:
    """Deferral is not a verified event: nothing is persisted, nothing is cleared."""
    rest = FakeRest()

    await _device(rest, tmp_path).apply(_intent(_export(hours_ahead=25.0)))

    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        assert await store.load() is None


@pytest.mark.asyncio
async def test_a_day_early_refusal_preserves_same_verified_resident_export(tmp_path) -> None:
    """A deferred retry must not clear the verified event it finds in Slot 1."""
    rest = FakeRest()
    now = datetime.now(_LONDON).replace(second=0, microsecond=0)
    start = (now + timedelta(hours=1)).replace(second=0, microsecond=0) + timedelta(days=1)
    end = start + timedelta(hours=2)
    export = ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )
    resident = [23, 30, 5, 30, start.hour, start.minute, end.hour, end.minute]
    await rest.call_service("modbus", "write_register", {"address": 43142, "value": 625})
    await rest.call_service("modbus", "write_register", {"address": 43143, "value": resident})
    event_id = f"export|{start.astimezone(UTC).isoformat()}|{end.astimezone(UTC).isoformat()}"
    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        await store.save(event_id, end)
    rest.calls.clear()

    lines = await _device(rest, tmp_path).apply(_intent(export))

    assert any("export refused" in line and "not yet armed" in line for line in lines)
    slot_writes = [
        call for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
    ]
    assert slot_writes == []
    assert _discharge_writes(rest) == []
    assert [
        rest.states[f"sensor.solis_control_{field}"]
        for field in (
            "timed_charge_start_hours",
            "timed_charge_start_minutes",
            "timed_charge_end_hours",
            "timed_charge_end_minutes",
            "timed_discharge_start_hours",
            "timed_discharge_start_minutes",
            "timed_discharge_end_hours",
            "timed_discharge_end_minutes",
        )
    ] == [str(value) for value in resident]
    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        assert await store.load() == (event_id, end.astimezone(UTC))


@pytest.mark.asyncio
async def test_an_event_already_under_way_is_still_armed_for_its_remainder(tmp_path) -> None:
    """A day-of pickup mid-event must deliver the rest, not defer for 24 hours."""
    rest = FakeRest()
    now = datetime.now(_LONDON).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=30)
    end = now + timedelta(minutes=30)
    export = ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )

    lines = await _device(rest, tmp_path).apply(_intent(export))

    assert not any("export refused" in line for line in lines)
    assert _discharge_writes(rest) != []


@pytest.mark.asyncio
async def test_a_midnight_wrapping_window_is_judged_on_its_start(tmp_path) -> None:
    """Only the start's clock face decides arming; the end may be the next day."""
    rest = FakeRest()
    now = datetime.now(_LONDON).replace(second=0, microsecond=0)
    start = (now + timedelta(hours=1)).replace(minute=0)
    end = start + timedelta(hours=2)
    wrapping = ExportIntent(
        event_identity=("export", start, end),
        window_start=start,
        window_end=end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
        selected_slots=(start,),
        slot_export_kw=(3.2,),
    )

    lines = await _device(rest, tmp_path).apply(_intent(wrapping))

    assert not any("export refused" in line for line in lines)
    block = next(
        call[2]["value"] for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and int(call[2]["address"]) == 43143
    )
    # The end's hour may be on the far side of midnight; the registers carry the
    # clock face either way, and the inverter reads the end as following the start.
    assert block[4:] == [start.hour, start.minute, end.hour, end.minute]


# --- arming guard, with a pinned clock ---------------------------------------
# `apply()` reads its own `datetime.now(tz)`, so the device-level tests above
# can only express "relative to now". These drive `_export_not_yet_armed`
# directly, which is where the midnight-wrap and DST cases become expressible.


def _at(year: int, month: int, day: int, hour: int, minute: int = 0, *, fold: int = 0):
    return datetime(year, month, day, hour, minute, tzinfo=_LONDON, fold=fold)


def test_a_day_early_window_is_deferred_until_its_clock_window_has_closed() -> None:
    """Tue 18:30-19:30 is refused every hour of Monday until Monday's window closes.

    From 20:00 Monday it is armed, and correctly so: Monday's 18:30-19:30 is
    spent, so the next time the register's window opens is the event's own.
    """
    start, end = _at(2026, 9, 15, 18, 30), _at(2026, 9, 15, 19, 30)
    for hour in range(20):
        assert _export_not_yet_armed((start, end), _at(2026, 9, 14, hour, 0)) is not None, hour
    for hour in range(20, 24):
        assert _export_not_yet_armed((start, end), _at(2026, 9, 14, hour, 0)) is None, hour


def test_the_window_arms_once_the_day_before_has_left_its_clock_window() -> None:
    """Past Monday's 18:30 is not enough: Monday's 18:30-19:30 is still open.

    The registers carry no date, so programming Tuesday's window at Monday 18:31
    puts the inverter inside it at once and exports unpaid until 19:30. It arms
    only when the clock has left the window, from which point the next time it
    opens is the event's own.
    """
    start, end = _at(2026, 9, 15, 18, 30), _at(2026, 9, 15, 19, 30)

    assert _export_not_yet_armed((start, end), _at(2026, 9, 14, 18, 29)) is not None
    assert _export_not_yet_armed((start, end), _at(2026, 9, 14, 18, 31)) is not None
    assert _export_not_yet_armed((start, end), _at(2026, 9, 14, 19, 29)) is not None
    assert _export_not_yet_armed((start, end), _at(2026, 9, 14, 19, 30)) is None
    assert _export_not_yet_armed((start, end), _at(2026, 9, 15, 12, 0)) is None


def test_a_midnight_wrapping_window_still_open_from_the_night_before_is_not_armed() -> None:
    """At 00:15 the previous night's 23:30-00:30 is open on the clock."""
    start, end = _at(2026, 9, 15, 23, 30), _at(2026, 9, 16, 0, 30)

    assert _export_not_yet_armed((start, end), _at(2026, 9, 15, 0, 15)) is not None
    assert _export_not_yet_armed((start, end), _at(2026, 9, 15, 0, 30)) is None


def test_a_midnight_wrapping_window_is_armed_on_the_evening_it_starts() -> None:
    """23:30-00:30 is judged on its start; the end lands on the next date."""
    start, end = _at(2026, 9, 15, 23, 30), _at(2026, 9, 16, 0, 30)

    assert _export_not_yet_armed((start, end), _at(2026, 9, 15, 20, 0)) is None
    # A day earlier the same clock face comes round first on the 14th.
    assert _export_not_yet_armed((start, end), _at(2026, 9, 14, 20, 0)) is not None


def test_a_window_already_under_way_stays_armed() -> None:
    start, end = _at(2026, 9, 15, 18, 30), _at(2026, 9, 15, 19, 30)

    assert _export_not_yet_armed((start, end), _at(2026, 9, 15, 18, 45)) is None


def test_an_event_in_the_fall_back_repeated_hour_is_refused_not_armed() -> None:
    """01:30 happens twice on 2026-10-25; the register fires on the first.

    Comparing two same-zone aware datetimes ignores `fold`, so the second 01:30
    would look like the first and arm 1.5 h early — an hour of unpaid export.
    Refusing loses one event a year; arming exports outside the paid window.
    """
    start = _at(2026, 10, 25, 1, 30, fold=1)  # the second 01:30, GMT
    end = _at(2026, 10, 25, 2, 30, fold=1)
    now = _at(2026, 10, 25, 1, 0, fold=0)  # the first 01:00, still BST

    assert now.astimezone(UTC) < start.astimezone(UTC)  # the event is genuinely ahead
    assert _export_not_yet_armed((start, end), now) is not None


# --- inverter clock gate (#161) ---


def _export_blocks(rest: FakeRest) -> list[list[int]]:
    return [
        call[2]["value"]  # type: ignore[misc]
        for call in rest.calls
        if call[0:2] == ("modbus", "write_register")
        and call[2]["address"] == 43143
        and call[2]["value"][4:] != [0, 0, 0, 0]  # type: ignore[index]
    ]


@pytest.mark.asyncio
async def test_export_is_armed_on_a_clock_within_tolerance(tmp_path) -> None:
    rest = FakeRest()
    rest.clock_offset = timedelta(minutes=4)
    await _device(rest, tmp_path).apply(_intent(_export()))

    assert len(_export_blocks(rest)) == 1


@pytest.mark.asyncio
async def test_export_is_refused_on_a_clock_outside_tolerance(tmp_path) -> None:
    rest = FakeRest()
    rest.clock_offset = timedelta(hours=-1)
    lines = await _device(rest, tmp_path).apply(_intent(_export()))

    assert _export_blocks(rest) == []
    assert any(
        line.startswith("[BLOCKED] export refused: inverter clock is 60 min behind")
        for line in lines
    )


@pytest.mark.asyncio
async def test_export_is_refused_on_a_stale_clock_reading(tmp_path) -> None:
    rest = FakeRest()
    rest.clock_age = timedelta(seconds=90)
    lines = await _device(rest, tmp_path).apply(_intent(_export()))

    assert _export_blocks(rest) == []
    assert any("inverter clock reading is 90 s old" in line for line in lines)


@pytest.mark.asyncio
async def test_export_is_refused_on_an_unreadable_clock(tmp_path) -> None:
    rest = FakeRest()
    rest.clock_state = "unavailable"
    lines = await _device(rest, tmp_path).apply(_intent(_export()))

    assert _export_blocks(rest) == []
    assert any("export refused: inverter clock unreadable" in line for line in lines)


@pytest.mark.asyncio
async def test_a_bad_clock_leaves_the_charge_path_unchanged(tmp_path) -> None:
    rest = FakeRest()
    rest.clock_offset = timedelta(hours=-1)
    lines = await _device(rest, tmp_path).apply(_intent(_export()))

    assert any(line.startswith("[APPLIED] set charge window 23:30-05:30") for line in lines)


@pytest.mark.asyncio
async def test_the_clock_gates_arming_only_not_a_verified_resident_window(tmp_path) -> None:
    """A stale or unreadable blip mid-event must not clear a paid export (#161)."""
    rest = FakeRest()
    device = _device(rest, tmp_path)
    export = _export()
    await device.apply(_intent(export))
    assert len(_export_blocks(rest)) == 1

    rest.clock_state = "unavailable"
    rest.calls.clear()
    lines = await device.apply(_intent(export))

    assert not any("inverter clock" in line for line in lines)
    assert rest.states["sensor.solis_control_timed_discharge_start_hours"] == str(
        export.window_start.hour
    )


@pytest.mark.asyncio
async def test_a_clock_refusal_notifies_once_and_is_not_an_abort(tmp_path) -> None:
    rest = FakeRest()
    rest.clock_offset = timedelta(hours=-1)
    device = _device(rest, tmp_path, notify_service="mobile_app_phone")

    await device.apply(_intent(_export()))
    await device.apply(_intent(_export()))

    notifications = [call[2] for call in rest.calls if call[0:2] == ("notify", "mobile_app_phone")]
    assert len(notifications) == 1
    assert notifications[0]["title"] == "Axle export held: inverter clock"
    assert "60 min behind" in str(notifications[0]["message"])
    assert "sync-clock" in str(notifications[0]["message"])


@pytest.mark.asyncio
async def test_simulate_takes_the_same_clock_decision(tmp_path) -> None:
    """A read, not a write: the rehearsal must refuse what `on` would refuse."""
    rest = FakeRest()
    rest.clock_state = "unavailable"
    settings = Settings(
        proactive_mode="simulate",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    lines = await SolisDevice(settings.devices[0], settings, rest).apply(  # type: ignore[arg-type]
        _intent(_export())
    )

    assert any("export refused: inverter clock unreadable" in line for line in lines)
    assert not any(call[0] in {"modbus", "select"} for call in rest.calls)


class _SlotReadFailsOnce(FakeRest):
    """The first Slot 1 read after arming fails, as on a REST or modbus blip."""

    fail_next_slot_read = False

    async def get_state(self, entity_id: str) -> EntityState:
        if self.fail_next_slot_read and entity_id.startswith(
            "sensor.solis_control_timed_discharge_start_hours"
        ) and not entity_id.endswith(("_2", "_3")):
            self.fail_next_slot_read = False
            raise RuntimeError("blip")
        return await super().get_state(entity_id)


@pytest.mark.asyncio
async def test_a_slot_read_blip_with_a_bad_clock_never_clears_a_verified_export(
    tmp_path,
) -> None:
    rest = _SlotReadFailsOnce()
    device = _device(rest, tmp_path)
    export = _export()
    await device.apply(_intent(export))

    rest.clock_state = "unavailable"
    rest.fail_next_slot_read = True
    lines = await device.apply(_intent(export))

    assert not any("inverter clock" in line for line in lines)
    assert rest.states["sensor.solis_control_timed_discharge_start_hours"] == str(
        export.window_start.hour
    )


@pytest.mark.asyncio
async def test_an_overlapping_hold_is_reported_before_the_clock(tmp_path) -> None:
    """A terminal refusal must not be masked by the retryable clock notice."""
    rest = FakeRest()
    rest.clock_offset = timedelta(hours=-1)
    export = _export()
    intent = replace(
        _intent(export), holds=((export.window_start, export.window_start + timedelta(hours=1)),)
    )
    lines = await _device(rest, tmp_path).apply(intent)

    assert any("a dispatch hold overlaps the export window" in line for line in lines)
    assert not any("inverter clock" in line for line in lines)


def _simulate_device(rest: FakeRest, tmp_path) -> SolisDevice:
    settings = Settings(
        proactive_mode="simulate",
        db_path=str(tmp_path / "events.db"),
        inverter_power_switch_entity="select.solisac_power_switch",
    )
    return SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]


def _writes(rest: FakeRest) -> list[tuple[str, str, dict[str, object]]]:
    return [call for call in rest.calls if call[0] in {"modbus", "select"}]


@pytest.mark.asyncio
async def test_simulate_blocks_an_untrusted_soc_like_on_mode(tmp_path) -> None:
    """#175: simulate must not preview a max charge sized from a dead SoC sensor."""
    rest = FakeRest()
    bad_soc = replace(_soc(), status=SocStatus.UNAVAILABLE, raw_state="unavailable")
    intent = ChargeIntent(77.0, bad_soc, time(23, 30), time(5, 30))

    lines = await _simulate_device(rest, tmp_path).apply(intent)

    assert lines[0].startswith("[SIMULATE] [BLOCKED]")
    assert not any("would" in line for line in lines)
    assert _writes(rest) == []


@pytest.mark.asyncio
async def test_simulate_blocks_charge_when_grid_charging_is_not_permitted(tmp_path) -> None:
    """#175: bit 5 unset refuses the real write, so simulate previews the refusal."""
    rest = FakeRest(work_mode="3")

    lines = await _simulate_device(rest, tmp_path).apply(_intent())

    assert any(
        line.startswith("[SIMULATE] [BLOCKED]") and "grid charging not permitted" in line
        for line in lines
    )
    assert _writes(rest) == []


@pytest.mark.asyncio
async def test_simulate_refuses_export_when_the_power_switch_is_off(tmp_path) -> None:
    """#175: an Off power switch refuses a real export, so simulate reports that too."""
    rest = FakeRest(power="Off")

    lines = await _simulate_device(rest, tmp_path).apply(_intent(_export()))

    assert "[SIMULATE] [BLOCKED] export refused: power_switch is 'Off'" in lines
    assert _writes(rest) == []


def _set_slot_one(rest: FakeRest, block: list[int]) -> None:
    for field, value in zip(_SLOT_FIELDS, block, strict=True):
        rest.states[f"sensor.solis_control_{field}"] = str(value)


def _block_writes(rest: FakeRest) -> list[list[int]]:
    return [
        list(call[2]["value"])  # type: ignore[call-overload]
        for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and call[2]["address"] == 43143
    ]


@pytest.mark.asyncio
async def test_charge_current_change_keeps_a_live_export_window(tmp_path) -> None:
    """#218: a charge-current change zeroes only the charge half of Slot 1.

    On the 2026-10-05 event every replan zeroed the whole block, discharge half
    included, and the paid export stopped for 10-15 s until the window rewrite.
    """
    rest = FakeRest()
    export = _export(hours_ahead=-0.5, duration_h=1.0)
    discharge = [export.window_start.hour, 0, export.window_end.hour, 0]
    _set_slot_one(rest, [23, 30, 5, 30, *discharge])
    rest.states["sensor.solis_control_timed_charge_current"] = "1.0"
    rest.states["sensor.solis_control_timed_discharge_current"] = "62.5"

    lines = await _device(rest, tmp_path).apply(_intent(export))

    writes = _block_writes(rest)
    assert writes[0] == [0, 0, 0, 0, *discharge]
    assert all(block[4:] == discharge for block in writes)
    assert any(line.startswith("[APPLIED] deactivate timed slot 1 charge") for line in lines)
    assert any(line.startswith("[APPLIED] set timed charge current") for line in lines)


@pytest.mark.asyncio
async def test_failed_current_change_leaves_charge_half_zero_and_export_kept(tmp_path) -> None:
    """The fail-safe still holds: the old charge window is not left active."""
    rest = StaleChargeCurrentReadback()
    export = _export(hours_ahead=-0.5, duration_h=1.0)
    discharge = [export.window_start.hour, 0, export.window_end.hour, 0]
    _set_slot_one(rest, [23, 30, 5, 30, *discharge])
    rest.states["sensor.solis_control_timed_charge_current"] = "1.0"
    rest.states["sensor.solis_control_timed_discharge_current"] = "62.5"

    lines = await _device(rest, tmp_path).apply(_intent(export))

    assert _block_writes(rest) == [[0, 0, 0, 0, *discharge]]
    assert next(line for line in lines if "timed charge current" in line).startswith("[WARNING]")


@pytest.mark.asyncio
async def test_misread_discharge_half_is_not_kept(tmp_path) -> None:
    """A resident discharge half that isn't the planned export is zeroed, not kept.

    One overlay sensor misreading 0 (#178) must not be written back as a
    longer export window.
    """
    rest = FakeRest()
    export = _export(hours_ahead=-0.5, duration_h=1.0)
    _set_slot_one(rest, [23, 30, 5, 30, export.window_start.hour, 0, 0, 0])
    rest.states["sensor.solis_control_timed_charge_current"] = "1.0"
    rest.states["sensor.solis_control_timed_discharge_current"] = "62.5"

    await _device(rest, tmp_path).apply(_intent(export))

    assert _block_writes(rest)[0] == [0] * 8
