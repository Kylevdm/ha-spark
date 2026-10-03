"""Opt-in inverter-clock sync at a daylight-saving change (#161 addendum)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.base import ControlAuthority
from ha_spark.energy.dst_clock_sync import DstClockSync, last_clock_change
from ha_spark.ha.models import EntityState


@pytest.fixture(autouse=True)
def _skip_read_back_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bounded read-back retry tests fast without changing production timing."""
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)


_LONDON = ZoneInfo("Europe/London")
# 25 Oct 2026: BST ends at 02:00 BST (01:00 UTC).
CHANGE = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    """Household time ``minutes`` after the change, stepped in UTC.

    Adding a timedelta to an aware datetime drops ``fold``, which would put a
    fall-back-night 01:01 back on BST. The daemon's ``now`` is always a fresh
    ``datetime.now(tz)``, so this only matters to the test.
    """
    return (CHANGE + timedelta(minutes=minutes)).astimezone(_LONDON)


class FakeRest:
    """Clock sensor plus write and notify capture. `takes_write` decides the read-back."""

    def __init__(self, *, takes_write: bool = True) -> None:
        self.takes_write = takes_write
        self.offset = timedelta(hours=1)  # the inverter still shows BST
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def get_state(self, entity_id: str) -> EntityState:
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
        self.calls.append((domain, service, data or {}))
        if (domain, service) == ("modbus", "write_register") and self.takes_write:
            self.offset = timedelta(0)
        return []

    def writes(self) -> list[dict[str, object]]:
        return [c[2] for c in self.calls if c[0:2] == ("modbus", "write_register")]

    def notices(self) -> list[dict[str, object]]:
        return [c[2] for c in self.calls if c[0:2] == ("notify", "mobile_app_phone")]


def _settings(mode: str = "on") -> Settings:
    return Settings(
        proactive_mode=mode,
        inverter_clock_dst_sync=True,
        notify_service="mobile_app_phone",
    )


# --- detection: stateless, from the zone rules alone ---


def test_the_change_instant_is_found_to_the_second() -> None:
    assert last_clock_change(_at(5)) == CHANGE


def test_the_spring_change_is_found_too() -> None:
    spring = datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    now = (spring + timedelta(minutes=1)).astimezone(_LONDON)
    assert last_clock_change(now) == spring


def test_no_change_in_the_lookback_is_none() -> None:
    assert last_clock_change(_at(-1)) is None
    assert last_clock_change(datetime(2026, 7, 1, 12, 0, tzinfo=_LONDON)) is None


def test_the_lookback_is_six_hours() -> None:
    assert last_clock_change(_at(6 * 60 - 1)) == CHANGE
    assert last_clock_change(_at(6 * 60)) is None


def test_a_fresh_process_after_the_change_still_finds_it() -> None:
    """A restart across the change must not skip the sync."""
    assert DstClockSync().due(_at(20)) == CHANGE


# --- the sync ---


async def test_a_due_sync_writes_reads_back_and_notifies_before_and_after() -> None:
    rest = FakeRest()
    sync = DstClockSync()

    await sync.run(_settings(), rest, _at(0))  # type: ignore[arg-type]

    assert len(rest.writes()) == 1
    (notice,) = rest.notices()
    assert notice["title"] == "Inverter clock synced at the clock change"
    assert "before: 60 min ahead" in str(notice["message"])
    assert "now 0 s ahead" in str(notice["message"])
    assert sync.due(_at(1)) is None


async def test_a_failing_sync_retries_each_pass_notifies_once_and_stops_after_the_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    rest = FakeRest(takes_write=False)
    sync = DstClockSync()

    for minute in range(0, 6 * 60 + 5):
        await sync.run(_settings(), rest, _at(minute))  # type: ignore[arg-type]

    assert len(rest.writes()) == 6 * 60
    (notice,) = rest.notices()
    assert notice["title"] == "Inverter clock sync failing"
    assert "since the clock change" in str(notice["message"])


async def test_a_retry_that_succeeds_is_not_repeated() -> None:
    rest = FakeRest(takes_write=False)
    sync = DstClockSync()
    await sync.run(_settings(), rest, _at(0))  # type: ignore[arg-type]

    rest.takes_write = True
    await sync.run(_settings(), rest, _at(1))  # type: ignore[arg-type]
    await sync.run(_settings(), rest, _at(2))  # type: ignore[arg-type]

    assert len(rest.writes()) == 2
    assert [n["title"] for n in rest.notices()] == ["Inverter clock synced at the clock change"]


@pytest.mark.parametrize("mode", ["simulate", "off"])
async def test_outside_on_it_logs_would_sync_once_and_writes_nothing(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    rest = FakeRest()
    sync = DstClockSync()

    with caplog.at_level("INFO"):
        for minute in range(3):
            await sync.run(_settings(mode), rest, _at(minute))  # type: ignore[arg-type]

    assert rest.writes() == []
    assert rest.notices() == []
    assert caplog.text.count("would sync inverter clock") == 1


async def test_switching_to_on_within_the_lookback_still_syncs() -> None:
    rest = FakeRest()
    sync = DstClockSync()
    await sync.run(_settings("simulate"), rest, _at(0))  # type: ignore[arg-type]

    await sync.run(_settings("on"), rest, _at(10))  # type: ignore[arg-type]

    assert len(rest.writes()) == 1


async def test_without_control_authority_it_never_writes() -> None:
    rest = FakeRest()
    settings = _settings()
    observe = {"control": ControlAuthority.OBSERVE}
    settings.devices[0] = settings.devices[0].model_copy(update=observe)

    await DstClockSync().run(settings, rest, _at(0))  # type: ignore[arg-type]

    assert rest.writes() == []


def test_the_option_defaults_off() -> None:
    assert Settings().inverter_clock_dst_sync is False
