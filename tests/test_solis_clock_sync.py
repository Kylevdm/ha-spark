"""Operator-run inverter-clock sync on the Solis (#161)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.ha.models import EntityState

_LONDON = ZoneInfo("Europe/London")
CLOCK = "sensor.solis_control_inverter_clock"


class ClockRest:
    """An inverter whose clock is `offset` from the household clock until 43000 is written."""

    def __init__(self, *, offset: timedelta = timedelta(hours=-1), takes_write: bool = True):
        self.offset = offset
        self.takes_write = takes_write
        self.unreadable = False
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def get_state(self, entity_id: str) -> EntityState:
        assert entity_id == CLOCK
        if self.unreadable:
            return EntityState(entity_id=entity_id, state="unavailable")
        read_at = datetime.now(UTC).replace(microsecond=0)
        face = read_at.astimezone(_LONDON) + self.offset
        return EntityState(
            entity_id=entity_id,
            state=f"{face.year % 100},{face.month},{face.day},"
            f"{face.hour},{face.minute},{face.second}",
            last_updated=read_at,
        )

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        payload = data or {}
        self.calls.append((domain, service, payload))
        if (domain, service) == ("modbus", "write_register") and self.takes_write:
            self.offset = timedelta(0)
        return []


def _device(rest: ClockRest, *, mode: str = "on") -> SolisDevice:
    settings = Settings(proactive_mode=mode)
    return SolisDevice(settings.devices[0], settings, rest)  # type: ignore[arg-type]


def _writes(rest: ClockRest) -> list[dict[str, object]]:
    return [call[2] for call in rest.calls if call[0:2] == ("modbus", "write_register")]


async def test_sync_writes_the_household_clock_to_43000_and_reads_it_back() -> None:
    rest = ClockRest()

    outcome, lines = await _device(rest).sync_clock()

    assert outcome == "synced"
    (write,) = _writes(rest)
    assert write["address"] == 43000
    assert write["hub"] == "solis_control"
    value = write["value"]
    assert isinstance(value, list)
    yy, mm, dd, hh, mi, ss = value
    written = datetime(2000 + yy, mm, dd, hh, mi, ss, tzinfo=_LONDON)
    assert abs(datetime.now(_LONDON) - written) < timedelta(seconds=2)
    assert ("homeassistant", "update_entity", {"entity_id": [CLOCK]}) in rest.calls
    assert lines[0] == "inverter clock before: 60 min behind Europe/London"
    assert lines[-1].startswith("[APPLIED] sync inverter clock to Europe/London")
    assert "now 0 s ahead" in lines[-1]


async def test_sync_syncs_whatever_the_error_beforehand() -> None:
    rest = ClockRest(offset=timedelta(0))

    outcome, lines = await _device(rest).sync_clock()

    assert outcome == "synced"
    assert len(_writes(rest)) == 1
    assert lines[0] == "inverter clock before: 0 s ahead Europe/London"


async def test_sync_reports_an_unreadable_clock_beforehand_and_still_syncs() -> None:
    rest = ClockRest()
    rest.unreadable = True
    device = _device(rest)

    async def recover(*args: object, **kwargs: object) -> list[EntityState]:
        rest.unreadable = False
        return await ClockRest.call_service(rest, *args, **kwargs)  # type: ignore[arg-type]

    rest.call_service = recover  # type: ignore[method-assign]
    outcome, lines = await device.sync_clock()

    assert outcome == "synced"
    assert lines[0] == "inverter clock before: unreadable"


async def test_sync_fails_when_the_read_back_still_disagrees() -> None:
    rest = ClockRest(takes_write=False)

    outcome, lines = await _device(rest).sync_clock()

    assert outcome == "failed"
    assert lines[-1].startswith("[WARNING] sync inverter clock to Europe/London")
    assert "60 min behind" in lines[-1]


async def test_sync_fails_when_the_write_raises() -> None:
    rest = ClockRest()

    async def boom(*args: object, **kwargs: object) -> list[EntityState]:
        raise RuntimeError("modbus down")

    rest.call_service = boom  # type: ignore[method-assign]
    outcome, lines = await _device(rest).sync_clock()

    assert outcome == "failed"
    assert lines[-1] == "[FAILED] sync inverter clock to Europe/London"


@pytest.mark.parametrize("mode", ["simulate", "off"])
async def test_sync_never_writes_unless_proactive_mode_is_on(mode: str) -> None:
    rest = ClockRest()

    outcome, lines = await _device(rest, mode=mode).sync_clock()

    assert outcome == "not_written"
    assert _writes(rest) == []
    assert "not written" in lines[-1]


async def test_a_confirmed_sync_is_not_undone_by_a_later_read_failure() -> None:
    """Only the first read after the write succeeds; that read is the confirmation."""
    rest = ClockRest()
    reads_after_write = 0
    original_get = rest.get_state

    async def get_state(entity_id: str) -> EntityState:
        nonlocal reads_after_write
        if _writes(rest):
            reads_after_write += 1
            if reads_after_write > 1:
                raise RuntimeError("HA blip")
        return await original_get(entity_id)

    rest.get_state = get_state  # type: ignore[method-assign]
    outcome, lines = await _device(rest).sync_clock()

    assert outcome == "synced"
    assert lines[-1].startswith("[APPLIED]")
