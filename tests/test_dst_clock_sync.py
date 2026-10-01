"""Opt-in inverter-clock sync at a daylight-saving change (#161 addendum)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.base import ControlAuthority
from ha_spark.energy.dst_clock_sync import DstClockSync
from ha_spark.ha.models import EntityState

_LONDON = ZoneInfo("Europe/London")
# 25 Oct 2026: BST ends at 02:00 BST (01:00 UTC).
BEFORE = datetime(2026, 10, 25, 0, 59, tzinfo=UTC).astimezone(_LONDON)
AFTER = datetime(2026, 10, 25, 1, 0, tzinfo=UTC).astimezone(_LONDON)


def _after(minutes: int) -> datetime:
    """Step in UTC: adding a timedelta to an aware datetime drops `fold`, which
    would put a fall-back-night 01:01 back on BST. The daemon's `now` is always
    a fresh `datetime.now(tz)`, so this only matters to the test."""
    return (AFTER.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(_LONDON)


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


def test_the_first_observation_is_not_a_change() -> None:
    sync = DstClockSync()
    assert sync.observe(AFTER) is False


def test_an_unchanged_offset_is_not_a_change() -> None:
    sync = DstClockSync()
    sync.observe(BEFORE - timedelta(hours=3))
    assert sync.observe(BEFORE) is False


def test_an_offset_change_makes_a_sync_due() -> None:
    sync = DstClockSync()
    sync.observe(BEFORE)
    assert sync.observe(AFTER) is True


async def test_a_due_sync_writes_reads_back_and_notifies_before_and_after() -> None:
    rest = FakeRest()
    sync = DstClockSync()
    sync.observe(BEFORE)
    sync.observe(AFTER)

    await sync.run(_settings(), rest, AFTER)  # type: ignore[arg-type]

    assert len(rest.writes()) == 1
    (notice,) = rest.notices()
    assert notice["title"] == "Inverter clock synced at the clock change"
    assert "before: 60 min ahead" in str(notice["message"])
    assert "now 0 s ahead" in str(notice["message"])
    assert sync.observe(_after(1)) is False


async def test_a_failing_sync_retries_each_pass_and_notifies_once_after_30_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    rest = FakeRest(takes_write=False)
    sync = DstClockSync()
    sync.observe(BEFORE)
    sync.observe(AFTER)

    for minute in range(0, 32):
        now = _after(minute)
        assert sync.observe(now) is True
        await sync.run(_settings(), rest, now)  # type: ignore[arg-type]

    assert len(rest.writes()) == 32
    (notice,) = rest.notices()
    assert notice["title"] == "Inverter clock sync failing"
    assert "since the clock change" in str(notice["message"])


async def test_a_retry_that_succeeds_clears_the_pending_sync() -> None:
    rest = FakeRest(takes_write=False)
    sync = DstClockSync()
    sync.observe(BEFORE)
    sync.observe(AFTER)
    await sync.run(_settings(), rest, AFTER)  # type: ignore[arg-type]

    rest.takes_write = True
    await sync.run(_settings(), rest, _after(1))  # type: ignore[arg-type]

    assert sync.observe(_after(2)) is False
    assert [n["title"] for n in rest.notices()] == ["Inverter clock synced at the clock change"]


@pytest.mark.parametrize("mode", ["simulate", "off"])
async def test_outside_on_it_logs_would_sync_once_and_writes_nothing(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    rest = FakeRest()
    sync = DstClockSync()
    sync.observe(BEFORE)
    sync.observe(AFTER)

    with caplog.at_level("INFO"):
        await sync.run(_settings(mode), rest, AFTER)  # type: ignore[arg-type]

    assert rest.writes() == []
    assert rest.notices() == []
    assert "would sync inverter clock" in caplog.text
    assert sync.observe(_after(1)) is False


async def test_without_control_authority_it_never_writes() -> None:
    rest = FakeRest()
    settings = _settings()
    observe = {"control": ControlAuthority.OBSERVE}
    settings.devices[0] = settings.devices[0].model_copy(update=observe)
    sync = DstClockSync()
    sync.observe(BEFORE)
    sync.observe(AFTER)

    await sync.run(settings, rest, AFTER)  # type: ignore[arg-type]

    assert rest.writes() == []


def test_the_option_defaults_off() -> None:
    assert Settings().inverter_clock_dst_sync is False
