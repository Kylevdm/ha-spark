"""Opt-in inverter-clock sync at a daylight-saving change (#161, addendum to #155).

The Solis inverter clock does not reliably follow a clock change: in spring 2026
it stayed on GMT for about seven weeks, so the 23:30–05:30 charge window fired
an hour late. With ``inverter_clock_dst_sync`` on, the first per-minute pass
after the household zone's UTC offset changes runs the same write and read-back
as ``python -m ha_spark solis sync-clock``. Both UK clock changes fall inside
the charge window, so syncing at once keeps the stored window on the local-time
tariff for the rest of that night.

Daylight saving only, never drift: nothing here reads the clock error to decide
whether to sync, and it syncs whatever the error beforehand. Every write
invariant applies through ``SolisDevice.sync_clock``. Outside ``on`` (or without
control authority) it logs "would sync" once and writes nothing. A failed sync
retries on every pass, with one notification if it is still failing after 30
minutes.

The last offset is held in memory. A process that is down across the change
starts with the new offset, sees no change, and does not sync; ``health`` and
the export arming gate still catch the error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ha_spark.config import Settings
from ha_spark.devices import inverter_device
from ha_spark.devices.base import effective_mode
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

_NOTIFY_FAILURE_AFTER = timedelta(minutes=30)


@dataclass
class DstClockSync:
    """Notices a household-zone offset change and drives the sync it calls for."""

    offset: timedelta | None = None
    pending_since: datetime | None = None
    failure_notified: bool = False

    def observe(self, now: datetime) -> bool:
        """Record ``now``'s UTC offset; True while a sync is due."""
        offset = now.utcoffset()
        if self.offset is not None and offset != self.offset:
            self.pending_since = now
            self.failure_notified = False
        self.offset = offset
        return self.pending_since is not None

    async def run(self, settings: Settings, rest: HomeAssistantRest, now: datetime) -> None:
        """One attempt at the due sync. Raises only what the caller isolates."""
        if self.pending_since is None:
            return
        device = inverter_device(settings, rest)
        if not isinstance(device, SolisDevice):
            self.pending_since = None
            return
        config = next(d for d in settings.devices if d.type == "inverter")
        mode = effective_mode(config.control, settings.proactive_mode)
        ok, lines = await device.sync_clock()
        for line in lines:
            log.info("Clock change: %s", line)
        if mode != "on":
            self.pending_since = None
            return
        if ok:
            self.pending_since = None
            await _notify(
                settings, rest, "Inverter clock synced at the clock change", "\n".join(lines)
            )
            return
        if not self.failure_notified and now - self.pending_since >= _NOTIFY_FAILURE_AFTER:
            self.failure_notified = True
            await _notify(
                settings,
                rest,
                "Inverter clock sync failing",
                "The inverter clock has not been synced since the clock change at "
                f"{self.pending_since:%H:%M %Z}; ha-spark retries every minute and export "
                "windows are refused until the clock agrees.\n" + "\n".join(lines),
            )


async def _notify(settings: Settings, rest: HomeAssistantRest, title: str, message: str) -> None:
    if not settings.notify_service:
        return
    try:
        await rest.call_service(
            "notify", settings.notify_service, {"title": title, "message": message}
        )
    except Exception:  # noqa: BLE001 - a notice is never a gate
        log.warning("Clock-change notification failed")
