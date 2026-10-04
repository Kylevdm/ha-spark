"""Opt-in inverter-clock sync at a daylight-saving change (#161, addendum to #155).

The Solis inverter clock does not reliably follow a clock change: in spring 2026
it stayed on GMT for about seven weeks, so the 23:30–05:30 charge window fired
an hour late. With ``inverter_clock_dst_sync`` on, the first per-minute pass
after the household zone's UTC offset changes runs the same write and read-back
as ``python -m ha_spark solis sync-clock``. Both UK clock changes fall inside
the charge window, so syncing at once keeps the stored window on the local-time
tariff for the rest of that night.

The change is found from the zone rules alone (:func:`last_clock_change`), not
from an offset remembered between passes. So a restart across the change still
syncs, a ``timezone`` hot-reload is never mistaken for a change, and a stale
memory can never fire weeks later. The only state is which change has already
been handled. The six-hour lookback bounds the whole episode: a failing sync
retries every pass until then (one notification after 30 minutes), and a switch
from ``simulate`` to ``on`` inside it still syncs.

Daylight saving only, never drift: nothing here reads the clock error to decide
whether to sync, and it syncs whatever the error beforehand. Every write
invariant applies through ``SolisDevice.sync_clock``, the one place the write
is gated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ha_spark.config import Settings
from ha_spark.devices import inverter_device
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.ha.rest import HomeAssistantRest, notify
from ha_spark.logging import get_logger

log = get_logger(__name__)

LOOKBACK = timedelta(hours=6)
_NOTIFY_FAILURE_AFTER = timedelta(minutes=30)


def last_clock_change(now: datetime, lookback: timedelta = LOOKBACK) -> datetime | None:
    """The UTC instant ``now``'s zone last changed offset, if within ``lookback``.

    Found by bisection over whole UTC seconds, so every pass inside the lookback
    returns the identical instant. Assumes at most one change per lookback.
    """
    tz = now.tzinfo
    end = int(now.astimezone(UTC).timestamp())
    start = end - int(lookback.total_seconds())

    def offset(second: int) -> timedelta | None:
        return datetime.fromtimestamp(second, UTC).astimezone(tz).utcoffset()

    if offset(start) == offset(end):
        return None
    lo, hi = start, end  # offset(lo) is the old one, offset(hi) the new
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if offset(mid) == offset(start):
            lo = mid
        else:
            hi = mid
    return datetime.fromtimestamp(hi, UTC)


@dataclass
class DstClockSync:
    """Drives the sync each clock change calls for, once."""

    handled: datetime | None = None
    simulated: datetime | None = None
    failure_notified: datetime | None = None

    def due(self, now: datetime) -> datetime | None:
        """The clock change still awaiting a sync, or ``None``."""
        change = last_clock_change(now)
        return change if change is not None and change != self.handled else None

    async def run(self, settings: Settings, rest: HomeAssistantRest, now: datetime) -> None:
        """One attempt at the due sync. Raises only what the caller isolates."""
        change = self.due(now)
        if change is None:
            return
        device = inverter_device(settings, rest)
        if not isinstance(device, SolisDevice):
            self.handled = change
            return
        outcome, lines = await device.sync_clock()
        if outcome == "not_written":
            # Not handled: switching to `on` inside the lookback still syncs.
            if self.simulated != change:
                self.simulated = change
                for line in lines:
                    log.info("Clock change: %s", line)
            return
        for line in lines:
            log.info("Clock change: %s", line)
        if outcome == "synced":
            self.handled = change
            await _notify(
                settings, rest, "Inverter clock synced at the clock change", "\n".join(lines)
            )
            return
        if self.failure_notified != change and now - change >= _NOTIFY_FAILURE_AFTER:
            self.failure_notified = change
            await _notify(
                settings,
                rest,
                "Inverter clock sync failing",
                "The inverter clock has not been synced since the clock change at "
                f"{change.astimezone(now.tzinfo):%H:%M %Z}; ha-spark retries every minute "
                f"for {LOOKBACK.total_seconds() / 3600:g} h and export windows are refused "
                "until the clock agrees.\n" + "\n".join(lines),
            )


async def _notify(settings: Settings, rest: HomeAssistantRest, title: str, message: str) -> None:
    if not settings.notify_service:
        return
    try:
        await notify(rest, settings.notify_service, title, message)
    except Exception:  # noqa: BLE001 - a notice is never a gate
        log.warning("Clock-change notification failed")
