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


def test_a_day_early_window_is_deferred_until_its_clock_face_has_passed() -> None:
    """Tue 18:30 is refused every hour of Monday up to Monday's own 18:30.

    From 19:00 Monday it is armed, and correctly so: Monday's 18:30 is spent, so
    the next 18:30 the register can fire on is the event's own.
    """
    start, end = _at(2026, 9, 15, 18, 30), _at(2026, 9, 15, 19, 30)
    for hour in range(19):
        assert _export_not_yet_armed((start, end), _at(2026, 9, 14, hour, 0)) is not None, hour
    for hour in range(19, 24):
        assert _export_not_yet_armed((start, end), _at(2026, 9, 14, hour, 0)) is None, hour


def test_the_window_arms_the_moment_its_clock_face_is_the_next_occurrence() -> None:
    start, end = _at(2026, 9, 15, 18, 30), _at(2026, 9, 15, 19, 30)

    assert _export_not_yet_armed((start, end), _at(2026, 9, 14, 18, 29)) is not None
    # One minute past Monday's 18:30 the next 18:30 is the event's own.
    assert _export_not_yet_armed((start, end), _at(2026, 9, 14, 18, 31)) is None
    assert _export_not_yet_armed((start, end), _at(2026, 9, 15, 12, 0)) is None


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
