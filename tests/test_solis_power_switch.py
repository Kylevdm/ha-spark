"""#140: ha-spark owns the Solis power-switch lifecycle via a declarative reconcile.

Desired state is a pure function of the clock and ``intent.holds``:
``Off`` while a hold is active, ``On`` otherwise. No remembered edge, so a
restart mid-hold converges on the next tick.

The reconcile is its own ``Device`` seam (``reconcile_holds``), not part of
``apply`` (#143): ``apply`` is a plan diff on the half-hour, while the desired
state is a function of the clock and must converge within a minute. Every
device-driving caller makes one pass before it applies.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.base import ControlAuthority
from ha_spark.devices.inverters.alphaess import AlphaESSDevice
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.energy.export_store import ExportEventStore
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
    # Relative to now, not a fixed time of day: since #144 an event is only
    # armed once its clock face next comes round at the event itself, so a
    # pinned "tomorrow at 10:00" would be refused whenever the suite runs
    # before 10:00.
    start = (datetime.now(_LONDON) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    end = start + timedelta(hours=2)
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
            if address == 43143:
                fields = (
                    "timed_charge_start_hours", "timed_charge_start_minutes",
                    "timed_charge_end_hours", "timed_charge_end_minutes",
                    "timed_discharge_start_hours", "timed_discharge_start_minutes",
                    "timed_discharge_end_hours", "timed_discharge_end_minutes",
                )
                for field, value in zip(fields, list(payload["value"]), strict=True):  # type: ignore[call-overload]
                    self.states[f"sensor.solis_control_{field}"] = str(value)
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


def _now() -> datetime:
    return datetime.now(_LONDON)


async def _reconcile(device: SolisDevice, intent: ChargeIntent) -> list[str]:
    """One reconcile pass on the household clock, as every caller makes."""
    return await device.reconcile_holds(intent, _now())


def _options(rest: FakeRest) -> list[str]:
    return [
        str(call[2]["option"])
        for call in rest.calls
        if call[0:2] == ("select", "select_option")
    ]


@pytest.mark.asyncio
async def test_future_hold_does_not_switch_the_inverter_off_yet(tmp_path) -> None:
    rest = FakeRest(switch="On")
    await _reconcile(_device(rest, tmp_path), _intent(holds=(_window(180),)))

    assert _options(rest) == []


@pytest.mark.asyncio
async def test_active_hold_switches_off_and_verifies_read_back(tmp_path) -> None:
    rest = FakeRest(switch="On")
    lines = await _reconcile(_device(rest, tmp_path), _intent(holds=(_window(-10),)))

    assert _options(rest) == ["Off"]
    assert any(line.startswith("[APPLIED]") and "Off" in line for line in lines)


@pytest.mark.asyncio
async def test_no_active_hold_switches_the_inverter_back_on(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    lines = await _reconcile(_device(rest, tmp_path), _intent(holds=(_window(-180),)))

    assert _options(rest) == ["On"]
    assert any(line.startswith("[APPLIED]") and "On" in line for line in lines)


@pytest.mark.asyncio
async def test_switch_already_in_the_desired_state_is_never_written(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    lines = await _reconcile(_device(rest, tmp_path), _intent(holds=(_window(-10),)))

    assert _options(rest) == []
    assert any(line.startswith("[SKIP]") and "Off" in line for line in lines)


@pytest.mark.asyncio
async def test_apply_never_touches_the_power_switch(tmp_path) -> None:
    """``apply`` is a plan diff; the switch belongs to the reconcile seam.

    Left in ``apply``, the switch would only converge when some plan field
    changed on a half-hour boundary — the defect #143 hole 2 records.
    """
    rest = FakeRest(switch="On")
    lines = await _device(rest, tmp_path).apply(_intent(holds=(_window(-10),)))

    assert _options(rest) == []
    assert not any("power switch" in line for line in lines)


@pytest.mark.asyncio
async def test_a_device_without_the_hold_capability_reconciles_to_nothing() -> None:
    """The seam is narrow: every other inverter answers with a no-op."""
    settings = Settings(inverter="alphaess")
    device = AlphaESSDevice(settings.devices[0], settings, object())  # type: ignore[arg-type]

    assert await device.reconcile_holds(_intent(holds=(_window(-10),)), _now()) == []


def _export_discharge_writes(rest: FakeRest) -> list[dict[str, object]]:
    return [
        call[2]
        for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and int(call[2]["address"]) == 43142
    ]


@pytest.mark.asyncio
async def test_hold_overlapping_the_export_window_refuses_the_whole_event(tmp_path) -> None:
    """Hold beats export on overlap — judged against the window, not the clock.

    The hold is tomorrow, inside the event: nothing is held right now, so the
    refusal cannot come from the power-switch precondition.
    """
    rest = FakeRest(switch="On")
    export = _export()
    hold = (
        export.window_start + timedelta(minutes=15),
        export.window_start + timedelta(minutes=45),
    )
    lines = await _device(rest, tmp_path).apply(_intent(holds=(hold,), export=export))

    assert _options(rest) == []
    assert any("export refused" in line and "hold overlaps" in line for line in lines)
    assert _export_discharge_writes(rest) == []


@pytest.mark.asyncio
async def test_a_hold_the_export_window_never_reaches_does_not_refuse_it(tmp_path) -> None:
    """A dispatch earlier today must not cost tomorrow's paid event."""
    rest = FakeRest(switch="On")
    lines = await _device(rest, tmp_path).apply(
        _intent(holds=(_window(-180),), export=_export())
    )

    assert not any("export refused" in line for line in lines)
    assert _export_discharge_writes(rest) != []


@pytest.mark.asyncio
async def test_export_is_programmed_on_the_tick_the_reconcile_turns_the_switch_on(
    tmp_path,
) -> None:
    """The reconcile settles before anything reads the switch.

    A switch left `Off` by a hold that has just ended must not make
    `_require_power_switch_on` refuse the event against a state this very tick
    is correcting — the next chance to program it could be slots away, because
    an otherwise-unchanged plan is skipped as an unchanged setpoint.
    """
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path)
    intent = _intent(holds=(_window(-40),), export=_export())
    lines = await _reconcile(device, intent) + await device.apply(intent)

    assert _options(rest) == ["On"]
    assert not any("export refused" in line for line in lines)
    assert _export_discharge_writes(rest) != []


@pytest.mark.asyncio
async def test_untrusted_soc_still_reconciles_but_blocks_charge_programming(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path)
    bad_soc = replace(_soc(), status=SocStatus.UNAVAILABLE, value=None, raw_state="unavailable")
    intent = _intent(soc=bad_soc)
    lines = await _reconcile(device, intent) + await device.apply(intent)

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

    lines = await _reconcile(device, _intent(holds=(_window(-10),)))

    assert rest.calls == []
    assert any("Off" in line for line in lines)


class BlindRest(FakeRest):
    """HA reachable for writes, but every state read fails.

    The shape of an HA outage or a flaky proxy at the moment the reconcile runs.
    """

    async def get_state(self, entity_id: str) -> EntityState:
        raise TimeoutError("state machine unreachable")


@pytest.mark.asyncio
async def test_an_unreadable_switch_is_still_driven_off_for_an_active_hold(tmp_path) -> None:
    """Unreadable evidence may close a hold: a flaky GET must not drop it.

    The battery discharging into the car at 7 kW on cheap grid is the failure
    #140 exists to prevent, so this direction writes blind and lets the
    read-back report what it can.
    """
    rest = BlindRest(switch="On")
    lines = await _reconcile(_device(rest, tmp_path), _intent(holds=(_window(-10),)))

    assert _options(rest) == ["Off"]
    assert any("Off" in line for line in lines)


@pytest.mark.asyncio
async def test_an_unreadable_switch_is_never_driven_on(tmp_path) -> None:
    """Unreadable evidence may never *open* a hold.

    At the per-minute cadence (#143) a fall-through here would be up to 60
    blind, unverifiable register writes an hour through an HA outage — and each
    one is the release of a hold ha-spark can no longer see.
    """
    rest = BlindRest(switch="Off")
    lines = await _reconcile(_device(rest, tmp_path), _intent(holds=(_window(-180),)))

    assert _options(rest) == []
    # A refused release is reported as a warning, not a skip: while it holds,
    # the inverter is disabled and the house is entirely on grid import.
    assert any(line.startswith("[WARNING]") and "unreadable" in line for line in lines)


# --- #143 §3: export is gated by the same hold trust ---


def _program_slot_one_export(rest: FakeRest, export: ExportIntent) -> None:
    """Leave Slot 1's discharge half resident at ``export``'s window, as a prior tick would."""
    for field, value in (
        ("timed_discharge_start_hours", export.window_start.hour),
        ("timed_discharge_start_minutes", export.window_start.minute),
        ("timed_discharge_end_hours", export.window_end.hour),
        ("timed_discharge_end_minutes", export.window_end.minute),
    ):
        rest.states[f"sensor.solis_control_{field}"] = str(value)


def _slot_one_writes(rest: FakeRest) -> list[object]:
    return [
        call[2]["value"]
        for call in rest.calls
        if call[0:2] == ("modbus", "write_register") and int(call[2]["address"]) == 43143
    ]


@pytest.mark.asyncio
async def test_untrusted_hold_data_refuses_a_new_export_window(tmp_path) -> None:
    """A failed dispatch read must not program a paid export across a hold it can't see.

    The degraded empty holds pass ``hold_overlaps``; the reconcile would then
    drive the switch ``Off`` mid-event and kill the export half-delivered.
    """
    rest = FakeRest(switch="On")
    intent = replace(_intent(export=_export()), hold_trusted=False)
    lines = await _device(rest, tmp_path).apply(intent)

    assert any("export refused" in line and "untrusted" in line for line in lines)
    assert _export_discharge_writes(rest) == []
    assert all(value[4:] == [0, 0, 0, 0] for value in _slot_one_writes(rest))  # type: ignore[index]


@pytest.mark.asyncio
async def test_untrusted_hold_data_leaves_an_already_programmed_export_alone(tmp_path) -> None:
    rest = FakeRest(switch="On")
    rest.states["sensor.solis_control_timed_discharge_current"] = "62.5"
    export = _export()
    _program_slot_one_export(rest, export)
    intent = replace(_intent(export=export), hold_trusted=False)
    lines = await _device(rest, tmp_path).apply(intent)

    assert not any("export refused" in line for line in lines)
    discharge = [
        int(rest.states[f"sensor.solis_control_{field}"])
        for field in (
            "timed_discharge_start_hours", "timed_discharge_start_minutes",
            "timed_discharge_end_hours", "timed_discharge_end_minutes",
        )
    ]
    assert discharge == [
        export.window_start.hour, export.window_start.minute,
        export.window_end.hour, export.window_end.minute,
    ]


def _discharge_half(rest: FakeRest) -> list[int]:
    return [
        int(rest.states[f"sensor.solis_control_{field}"])
        for field in (
            "timed_discharge_start_hours", "timed_discharge_start_minutes",
            "timed_discharge_end_hours", "timed_discharge_end_minutes",
        )
    ]


def _shifted(export: ExportIntent) -> ExportIntent:
    """The same event, as a trusted plan trimmed it around a dispatch hold."""
    start = export.window_start + timedelta(minutes=30)
    return replace(export, window_start=start, selected_slots=(start,))


@pytest.mark.asyncio
async def test_untrusted_hold_data_never_clears_a_live_verified_export(tmp_path) -> None:
    """Losing the dispatch read must not cost the paid window already accepted.

    A trusted plan trimmed the event around a dispatch; with the holds now
    unreadable the planner offers the whole event instead. That wider window is
    new and refused, but refusing it must not zero the verified one resident in
    Slot 1 on every tick the read stays down.
    """
    rest = FakeRest(switch="On")
    rest.states["sensor.solis_control_timed_discharge_current"] = "62.5"
    export = _export()
    accepted = _shifted(export)
    _program_slot_one_export(rest, accepted)
    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        await store.save("export|accepted", accepted.window_end)

    lines = await _device(rest, tmp_path).apply(
        replace(_intent(export=export), hold_trusted=False)
    )

    assert any("export refused" in line and "untrusted" in line for line in lines)
    assert _discharge_half(rest) == [
        accepted.window_start.hour, accepted.window_start.minute,
        accepted.window_end.hour, accepted.window_end.minute,
    ]
    async with ExportEventStore(str(tmp_path / "events.db")) as store:
        assert await store.load() is not None


@pytest.mark.asyncio
async def test_untrusted_hold_data_still_clears_an_unverified_resident_window(tmp_path) -> None:
    """Only a window ha-spark verified is preserved; anything else stays guarded."""
    rest = FakeRest(switch="On")
    export = _export()
    _program_slot_one_export(rest, _shifted(export))

    await _device(rest, tmp_path).apply(replace(_intent(export=export), hold_trusted=False))

    assert _discharge_half(rest) == [0, 0, 0, 0]
