"""Read-only native Solis discovery, shared gateways and live validation fences.

Claims are configuration, never evidence of identity or a grant of authority.
Serial words are discarded immediately; no raw replies or household identifiers
leave this module. There is deliberately no hardware write path here.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .solis_frames import Reading, Schedule
from .solis_identity import caching_on, identity_reason
from .solis_transport import PymodbusTcpTransport, Transport, TransportError, valid_words
from .topology import Inverter, Topology, validate_topology

transport_factory: Callable[[str, int], Transport] = PymodbusTcpTransport


@dataclass
class Probe:
    reason: str | None = "unvalidated_identity"
    caching: bool | None = None
    identity: tuple[int, ...] = ()


async def async_probe(transport: Transport, unit: int) -> Probe:
    """Read the entire required identity slice, then prime and time five repeats."""
    words: list[int] = []
    epoch: int | None = None
    for address, count in ((33000, 20), (35000, 1), (33289, 1), (34502, 1)):
        result = await transport.read_input(address, count, device_id=unit)
        if result.error is not None:
            return Probe("unreachable" if result.error in (
                TransportError.DISCONNECTED, TransportError.TIMEOUT,
            ) else "identity_read_failed")
        current_epoch = getattr(transport, "connection_epoch", 0)
        if (len(result.registers) != count or not valid_words(result.registers)
                or epoch is not None and current_epoch != epoch):
            return Probe("identity_read_failed")
        epoch = current_epoch
        # Never retain the serial in 33004-33019.
        words.append(result.registers[0])
    reason = identity_reason(*words[:3])
    times: list[float] = []
    for index in range(6):
        result = await transport.read_input(35000, 1, device_id=unit)
        if (result.error is not None or len(result.registers) != 1
                or not valid_words(result.registers)
                or getattr(transport, "connection_epoch", 0) != epoch):
            return Probe(reason or "identity_read_failed")
        if index:
            times.append(result.elapsed_s)
    cached = caching_on(times)
    return Probe(reason or ("identity_read_failed" if cached is None else
                            "caching_on" if cached else None), cached, tuple(words[:3]))


class NativeDevices:
    """One connection per host/port; validation is recomputed after each reconnect."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry[Any], *,
        owned: Callable[[], bool], overlay: Callable[[], dict[str, bool] | None],
        changed: Callable[[], None],
    ) -> None:
        self.hass, self.entry = hass, entry
        self.owned, self.overlay, self.changed = owned, overlay, changed
        topology = validate_topology(entry.options.get("topology"))
        self.inverters = {i.id: i for i in topology.inverters if i.transport == "native"} if (
            isinstance(topology, Topology)) else {}
        self.schedule = Schedule(tuple(self.inverters))
        self.readings: dict[str, dict[str, Reading]] = {key: {} for key in self.inverters}
        self.transports: dict[tuple[str, int], Transport] = {}
        self.probes: dict[str, Probe] = {}
        self.epochs: dict[str, int] = {}
        # Gateway-wide caching fence: set on any probe that sees caching, cleared
        # only by a completed probe that establishes it off (never by a failure).
        self.gateway_caching: dict[str, bool] = {}
        self.closed = False
        self.poll_task: asyncio.Task[None] | None = None
        # Cleared when unloading starts, before platforms go and well before close.
        self.polling = True
        self.lock = asyncio.Lock()
        for inverter in self.inverters.values():
            self.transport(inverter)

    def transport(self, inverter: Inverter) -> Transport:
        assert inverter.gateway_host is not None
        key = (inverter.gateway_host, inverter.gateway_port)
        if key not in self.transports:
            self.transports[key] = transport_factory(*key)
        return self.transports[key]

    def gateway_id(self, inverter: Inverter) -> str:
        """Opaque stable gateway key, without host, port, unit or serial text."""
        return str(uuid5(NAMESPACE_URL, (
            f"{self.entry.entry_id}:{inverter.gateway_host}:{inverter.gateway_port}")))

    def connected(self, inverter: Inverter) -> bool:
        return bool(getattr(self.transport(inverter), "connected", False))

    def claimed(self, inverter: Inverter) -> bool:
        claims = self.entry.options.get("gateway_claims")
        return isinstance(claims, list) and any(
            isinstance(c, dict) and c == {
                "host": inverter.gateway_host, "port": inverter.gateway_port,
                "unit_id": inverter.gateway_unit_id,
            } and type(c.get("port")) is int and type(c.get("unit_id")) is int
            for c in claims
        )

    def reason(self, inverter: Inverter) -> str | None:
        if self.inverters.get(inverter.id) != inverter:
            # Options can change before their reload runs. Old probe evidence
            # never validates a new unit or gateway in that interval.
            return "unvalidated_identity"
        probe = self.probes.get(inverter.id, Probe())
        if not self.connected(inverter):
            return "unreachable"
        epoch = getattr(self.transport(inverter), "connection_epoch", 0)
        if self.epochs.get(inverter.id) != epoch:
            return "unvalidated_identity"
        if probe.reason is not None:
            return probe.reason
        if self.gateway_caching.get(self.gateway_id(inverter)) is True:
            return "caching_on"
        if probe.caching is not False:
            return "identity_read_failed"
        overlay = self.overlay()
        if (not self.claimed(inverter) or not self.owned() or overlay is None
                or overlay.get(inverter.id) is not False):
            return "gateway_not_owned"
        return None

    async def async_reprobe(self, inverter_id: str) -> bool:
        """Admin command/service seam; also used for startup and reconnect reads."""
        inverter = self.inverters.get(inverter_id)
        if inverter is None:
            return False
        if self.closed:
            return False
        async with self.lock:
            if self.closed:
                return False
            self.probes[inverter_id] = Probe()
            probe = await async_probe(self.transport(inverter), inverter.gateway_unit_id)
            if self.closed:
                return False
            self.probes[inverter_id] = probe
            self.record_caching(inverter, probe)
            self.epochs[inverter_id] = getattr(self.transport(inverter), "connection_epoch", 0)
        self.recheck()
        return True

    def record_caching(self, inverter: Inverter, probe: Probe) -> None:
        """Fold one probe's caching result into its gateway's fence."""
        if probe.caching is not None:
            self.gateway_caching[self.gateway_id(inverter)] = probe.caching

    async def async_readback_mismatch(self, inverter_id: str) -> None:
        """One probe per mismatch, called by the later verified-write boundary."""
        await self.async_reprobe(inverter_id)

    async def async_start(self) -> None:
        current = {self.issue_id(i) for i in self.inverters.values()}
        for domain, issue in list(ir.async_get(self.hass).issues):
            # Repairs for inverters no longer in the topology must not linger.
            if domain == DOMAIN and issue.startswith("native_device_") and issue not in current:
                ir.async_delete_issue(self.hass, DOMAIN, issue)
        for inverter_id in self.inverters:
            await self.async_reprobe(inverter_id)

    async def async_check_connections(self) -> None:
        for inverter in self.inverters.values():
            if (not self.connected(inverter) or self.epochs.get(inverter.id) !=
                    getattr(self.transport(inverter), "connection_epoch", 0)):
                await self.async_reprobe(inverter.id)
        self.recheck()

    def issue_id(self, inverter: Inverter) -> str:
        return f"native_device_{uuid5(NAMESPACE_URL, f'{self.entry.entry_id}:{inverter.id}')}"

    def recheck(self) -> None:
        """Run on each accepted summary, even when identity remains unchanged."""
        if self.closed:
            return
        for inverter in self.inverters.values():
            issue = self.issue_id(inverter)
            reason = self.reason(inverter)
            if reason is None:
                ir.async_delete_issue(self.hass, DOMAIN, issue)
            else:
                ir.async_create_issue(
                    self.hass, DOMAIN, issue, is_fixable=False,
                    severity=ir.IssueSeverity.ERROR, translation_key=f"native_{reason}",
                )
        self.changed()

    async def async_poll(self, now: datetime) -> None:
        """One due read, never interleaved with a probe and never after close."""
        if self.closed or not self.polling or self.lock.locked():
            return
        async with self.lock:
            if self.closed or not self.polling:
                return
            item = self.schedule.next(now.timestamp())
            if item is None:
                return
            inverter_id, frame = item
            inverter = self.inverters[inverter_id]
            transport = self.transport(inverter)
            read = transport.read_input if frame.bank == "input" else transport.read_holding
            registers = None
            self.poll_task = asyncio.current_task()
            try:
                result = await read(frame.address, frame.count, device_id=inverter.gateway_unit_id)
                if (result.error is None and len(result.registers) == frame.count
                        and valid_words(result.registers)):
                    registers = result.registers
            except Exception:
                # Fixed unknown result, with no untrusted transport exception text.
                pass
            finally:
                self.poll_task = None
            self.readings[inverter_id][frame.key] = Reading(
                dt_util.utcnow(), registers, getattr(transport, "connection_epoch", 0))
        self.changed()

    def reading(self, inverter: Inverter, frame_key: str) -> Reading | None:
        """A frame read on the current connection; nothing from before a reconnect."""
        reading = self.readings.get(inverter.id, {}).get(frame_key)
        if (self.closed or reading is None or reading.registers is None
                or not self.connected(inverter)
                or reading.epoch != getattr(self.transport(inverter), "connection_epoch", 0)):
            return None
        return reading

    async def async_close(self) -> None:
        self.closed = True
        if self.poll_task is not None:
            self.poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.poll_task
        for transport in self.transports.values():
            await transport.close()
