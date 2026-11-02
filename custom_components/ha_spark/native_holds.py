"""Durable native protection ownership, release transactions and transition steps.

Requests are verified on disk before Off. Only a verified owned hold can write
On, after restoration reads under guard.lock then native.lock. Shutdown never
releases; an explicit removal additionally needs fresh clear evidence.
"""
import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid5

from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util

from .authority_store import AuthorityState, HoldRecord
from .native_evidence import EvidenceRead, NativeEvidence
from .native_hold import (
    DEGRADED_AFTER,
    RELEASE_BACKOFF,
    RELEASE_BACKOFF_AFTER,
    REPEAT_AFTER,
    advance_ev,
    hold_device,
    hold_identity_valid,
    remapped,
)
from .native_power import async_write_power_locked, decode_power, polled_power
from .solis_frames import Reading
from .solis_transport import valid_words
from .topology import Inverter, Topology, validate_topology

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator


class NativeHolds:
    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator
        self.evidence = NativeEvidence(coordinator)
        self.lock = asyncio.Lock()
        self.backoff: dict[str, tuple[int, datetime]] = {}
        self.notices: dict[str, tuple[datetime, bool]] = {}

    def topology(self) -> Topology | None:
        topology = validate_topology(self.coordinator.entry.options.get("topology"))
        return topology if isinstance(topology, Topology) else None

    def remapped(self) -> bool:
        state = self.coordinator.authority.state
        return state is not None and any(remapped(h.device, self.topology()) for h in state.holds)

    def _refusal(self, inverter: Inverter, state: AuthorityState, *, release: bool) -> str | None:
        c = self.coordinator
        current = c.authority.state
        if current is None:
            return "authority_unknown"
        if current.epoch != state.epoch or current.store_id != state.store_id:
            return "stale_authority"
        if not c.control.owned():
            return "not_owner"
        if self.remapped():
            return "held_identity_remapped"
        if not c._running or not c.native.polling or c.native.closed:
            return "not_available"
        if c.native.reason(inverter) is not None:
            return "read_only"
        if current.committed_mode not in ("protect", "optimise"):
            return "mode"
        manual = current.manual_session
        if not release and (manual is not None and not manual.keep_protection or
                            current.transition is not None
                            and current.transition.target == "observe"):
            return "mode"
        return None

    async def _save(self, records: tuple[HoldRecord, ...], expected: AuthorityState) -> bool:
        c = self.coordinator
        state = c.authority.state
        if (state is None or state.epoch != expected.epoch or state.store_id != expected.store_id
                or not c.control.owned()):
            return False
        return records == state.holds or await c.authority.async_save(
            state.model_copy(update={"holds": records}))

    async def _replace(self, inverter: Inverter, records: tuple[HoldRecord, ...],
                       expected: AuthorityState) -> bool:
        state = self.coordinator.authority.state
        return state is not None and await self._save(tuple(
            h for h in state.holds if h.device != hold_device(inverter)) + records, expected)

    def _notice(self, key: str, now: datetime, message: str) -> None:
        """Fixed text and opaque ids only; one update after thirty minutes."""
        previous = self.notices.get(key)
        if previous is not None and (previous[1] or now - previous[0] < REPEAT_AFTER):
            return
        self.notices[key] = (previous[0] if previous else now, previous is not None)
        identifier = str(uuid5(NAMESPACE_URL, f"{self.coordinator.entry.entry_id}:{key}"))
        persistent_notification.async_create(self.coordinator.hass, message,
                                             title="ha-spark protection",
                                             notification_id=f"ha_spark_hold_{identifier}")

    async def _async_notice(self, inverter: Inverter, state: AuthorityState, now: datetime,
                            kind: Literal["apply", "degraded", "release"], message: str) -> None:
        current = self.coordinator.authority.state
        if current is None:
            return
        records = tuple(h for h in current.holds if h.device == hold_device(inverter))
        at_key, repeated_key = f"{kind}_notice_at", f"{kind}_notice_repeated"
        sent = [getattr(h, at_key) for h in records if getattr(h, at_key) is not None]
        at = min(sent) if sent else None
        if at is not None and (now - at < REPEAT_AFTER
                              or any(getattr(h, repeated_key) for h in records)):
            return
        changes = {at_key: at or now, repeated_key: at is not None}
        if kind == "degraded":
            changes["degraded_notified"] = True
        elif kind == "release":
            changes["release_refused_notified"] = True
        if records and not await self._replace(inverter, tuple(
                h.model_copy(update=changes) for h in records), state):
            return
        self._notice(kind + ":" + inverter.id, now, message)

    def _clear_notice(self, key: str) -> None:
        self.notices.pop(key, None)
        identifier = str(uuid5(NAMESPACE_URL, f"{self.coordinator.entry.entry_id}:{key}"))
        persistent_notification.async_dismiss(self.coordinator.hass, f"ha_spark_hold_{identifier}")

    def _clear_notices(self, inverter: Inverter) -> None:
        for kind in ("apply", "degraded", "release"):
            self._clear_notice(kind + ":" + inverter.id)

    async def async_reconcile(self, now: datetime) -> bool:
        """Serialize whole-record saves with transitions and new add-on sessions."""
        async with self.coordinator.control.lock:
            return await self.async_reconcile_held(now)

    async def async_reconcile_held(self, now: datetime) -> bool:
        """One evaluator; the caller holds the authority controller lock."""
        async with self.lock:
            c = self.coordinator
            state = c.authority.state
            if state is None or not c.control.owned() or state.committed_mode == "observe":
                return False
            if state.manual_session is not None and not state.manual_session.keep_protection:
                return True
            if self.remapped():
                records = tuple(h.model_copy(update={"remap_notified": True})
                                if remapped(h.device, self.topology()) else h for h in state.holds)
                if (await self._save(records, state)
                        and any(not h.remap_notified for h in state.holds)):
                    self._notice("remap", now, "Held control mapping changed; the original hold is "
                                 "retained. Move to Observe before using the replacement mapping.")
                return False
            self._clear_notice("remap")
            evidence = self.evidence.read(now)
            success = True
            for inverter in c.native.inverters.values():
                if not hold_identity_valid(inverter):
                    self._notice("apply:" + inverter.id, now,
                                 "A protection hold cannot represent the configured "
                                 "gateway identity.")
                    success = False
                    continue
                try:
                    verified = await self._reconcile(inverter, state, now, evidence)
                    current = c.authority.state
                    needed = (evidence.charging is True
                        or any(w.start <= now < w.end for w in evidence.dispatches)
                        or current is not None and any(
                            h.device == hold_device(inverter) and h.owned and h.confirmed
                            and not h.pending_release and (h.cause == "ev"
                                or h.trusted_end is not None and now < h.trusted_end)
                            for h in current.holds))
                    if not verified and needed:
                        await self._async_notice(inverter, state, now, "apply",
                                                 "A protection hold could not be applied.")
                    success = verified and success
                except Exception:
                    # Fixed outcome; one bad inverter never skips protection on peers.
                    await self._async_notice(inverter, state, now, "apply",
                                             "A protection hold could not be applied.")
                    success = False
            return success

    async def _reconcile(self, inverter: Inverter, expected: AuthorityState, now: datetime,
                         evidence: EvidenceRead) -> bool:
        # Read each pass from one shared evidence observation, not one per inverter.
        c = self.coordinator
        state = c.authority.state
        if state is None:
            return False
        records = tuple(h for h in state.holds if h.device == hold_device(inverter))
        updated: list[HoldRecord] = []
        for h in records:
            h = h.model_copy(update={"remap_notified": False})
            if h.cause == "ev":
                h = advance_ev(h, evidence.charging, evidence.ev_report)
            elif evidence.dispatch_trusted:
                ends = [w.end for w in evidence.dispatches if w.start <= now < w.end]
                h = h.model_copy(update={"pending_release": not bool(ends),
                                         "trusted_end": max(ends) if ends else h.trusted_end})
            else:
                # Unknown evidence re-arms an unfinished release until the last trusted end.
                h = h.model_copy(update={"pending_release": h.trusted_end is not None
                                         and now >= h.trusted_end})
            fresh = evidence.charging is not None if h.cause == "ev" else evidence.dispatch_trusted
            since = None if fresh else h.evidence_stale_since or now
            h = h.model_copy(update={"evidence_stale_since": since,
                "degraded_notified": h.degraded_notified if not fresh else False,
                "degraded_notice_at": h.degraded_notice_at if not fresh else None,
                "degraded_notice_repeated": h.degraded_notice_repeated if not fresh else False})
            updated.append(h)
        records = tuple(updated)
        if not await self._replace(inverter, records, expected):
            return False
        if not any(h.evidence_stale_since for h in records):
            self._clear_notice("degraded:" + inverter.id)
        if any(h.evidence_stale_since and now - h.evidence_stale_since >= DEGRADED_AFTER
               for h in records):
            await self._async_notice(inverter, expected, now, "degraded",
                                     "Retaining a protection hold without fresh evidence; "
                                     "check its source.")
            state = c.authority.state
            if state is None:
                return False
            records = tuple(h for h in state.holds if h.device == hold_device(inverter))
        ends = [w.end for w in evidence.dispatches if w.start <= now < w.end]
        ev_wanted = evidence.charging is True and not evidence.overnight
        active = any(h.owned and not h.pending_release and (
            h.confirmed or h.cause == "dispatch" and h.trusted_end is not None
            and now < h.trusted_end) for h in records)
        wanted = ev_wanted or bool(ends) or active
        if wanted:
            self.backoff.pop(inverter.id, None)
            mismatch = False
            async with c.native.lock:
                if self._refusal(inverter, expected, release=False) is not None:
                    return False
                before = polled_power(c.native, inverter, now)
                if before is None or before != "off":
                    read = await c.native.transport(inverter).read_holding(
                        43007, 1, device_id=inverter.gateway_unit_id)
                    before = decode_power(list(read.registers)) if read.error is None else None
                # An owner's Off is not our hold. Nor can an unconfirmed request
                # be promoted by finding Off after a restart.
                may_create = before != "off" or any(h.confirmed and h.owned for h in records)
                requests = {h.cause: h for h in records}
                if may_create:
                    if ev_wanted and "ev" not in requests:
                        requests["ev"] = HoldRecord(cause="ev", started_at=now,
                                                     device=hold_device(inverter))
                    if ends:
                        old = requests.get("dispatch")
                        requests["dispatch"] = (old.model_copy(update={"trusted_end": max(ends),
                                                                         "pending_release": False})
                            if old else HoldRecord(cause="dispatch", started_at=now,
                                trusted_end=max(ends), device=hold_device(inverter)))
                records = tuple(h.model_copy(update={"confirmed": False}) if before != "off"
                                else h for h in requests.values())
                if not records:
                    return True
                if not await self._replace(inverter, records, expected):
                    return False
                if before == "off":
                    if any(h.confirmed and h.owned for h in records):
                        self._clear_notice("apply:" + inverter.id)
                        return await self._replace(inverter, tuple(h.model_copy(update={
                            "confirmed": True, "release_refused_since": None,
                            "release_refused_reason": None, "apply_notice_at": None,
                            "apply_notice_repeated": False}) for h in records), expected)
                    await self._async_notice(inverter, expected, now, "apply",
                                             "A protection hold could not be confirmed as owned.")
                    return False
                result = await async_write_power_locked(c.native, inverter, "off", recheck=lambda:
                    self._refusal(inverter, expected, release=False))
                mismatch = result.mismatch
                confirmed = result.outcome == "applied" or (
                    result.outcome == "unchanged" and any(h.confirmed and h.owned for h in records))
                if confirmed:
                    records = tuple(h.model_copy(update={"confirmed": True,
                        "release_refused_since": None, "release_refused_reason": None,
                        "apply_notice_at": None, "apply_notice_repeated": False})
                        for h in records)
                    confirmed = await self._replace(inverter, records, expected)
                    if confirmed:
                        self._clear_notice("apply:" + inverter.id)
            if mismatch:
                await c.native.async_readback_mismatch(inverter.id)
            if not confirmed:
                await self._async_notice(inverter, expected, now, "apply",
                                         "A protection hold could not be applied.")
            return confirmed
        return await self._release(inverter, expected, now)

    async def async_release(self, now: datetime, *, removal: bool = False) -> bool:
        async with self.coordinator.control.lock:
            return await self.async_release_held(now, removal=removal)

    async def async_release_held(self, now: datetime, *, removal: bool = False) -> bool:
        """Transition entry: authority lock precedes the guard and bus locks."""
        async with self.lock:
            c = self.coordinator
            state = c.authority.state
            if state is None or not c.control.owned() or self.remapped():
                return False
            if not state.holds:
                return True
            evidence = self.evidence.read(now)
            success = True
            for inverter in c.native.inverters.values():
                records = [h for h in state.holds if h.device == hold_device(inverter)]
                if removal and any(
                    evidence.charging is not False if h.cause == "ev" else
                    not (h.trusted_end is not None and now >= h.trusted_end or
                         evidence.dispatch_trusted and not any(
                             w.start <= now < w.end for w in evidence.dispatches))
                    for h in records
                ):
                    success = False
                    continue
                success = await self._release(inverter, state, now, handover=True) and success
            return success

    async def _release(self, inverter: Inverter, expected: AuthorityState, now: datetime,
                       *, handover: bool = False) -> bool:
        c = self.coordinator
        state = c.authority.state
        if state is None:
            return False
        records = tuple(h for h in state.holds if h.device == hold_device(inverter))
        if not records:
            return True
        guard = c.guard
        if guard is None:
            return False
        backoff = self.backoff.get(inverter.id)
        if backoff and backoff[0] >= RELEASE_BACKOFF_AFTER and now - backoff[1] < RELEASE_BACKOFF:
            return False
        mismatch = False
        reason: Literal["switch_unreadable", "schedule_unverified"] | None = None
        async with guard.lock:
            async with c.native.lock:
                if self._refusal(inverter, expected, release=True) is not None:
                    return False
                read = await c.native.transport(inverter).read_holding(
                    43007, 1, device_id=inverter.gateway_unit_id)
                before = decode_power(list(read.registers)) if read.error is None else None
                if before == "on":
                    return await self._replace(inverter, (), expected)
                if before == "off" and not any(h.confirmed and h.owned for h in records):
                    return await self._replace(inverter, (), expected) if handover or all(
                        h.pending_release for h in records) else False
                if before is None:
                    reason = "switch_unreadable"
                elif not await self._restoration(inverter, expected, handover=handover):
                    reason = "schedule_unverified"
                if reason is None:
                    if not await self._replace(inverter, tuple(h.model_copy(update={
                            "pending_release": True}) for h in records), expected):
                        return False
                    result = await async_write_power_locked(
                        c.native, inverter, "on", recheck=lambda:
                        self._refusal(inverter, expected, release=True))
                    mismatch = result.mismatch
                    if result.outcome in ("applied", "unchanged"):
                        saved = await self._replace(inverter, (), expected)
                        if saved:
                            self.backoff.pop(inverter.id, None)
                            self._clear_notices(inverter)
                        return saved
                    reason = "switch_unreadable"
        if mismatch:
            await c.native.async_readback_mismatch(inverter.id)
        count = self.backoff.get(inverter.id, (0, now))[0] + 1
        self.backoff[inverter.id] = count, now
        state = c.authority.state
        if state is not None:
            records = tuple(h.model_copy(update={
                "release_refused_since": h.release_refused_since or now,
                "release_refused_reason": reason,
            }) for h in state.holds if h.device == hold_device(inverter))
            if await self._replace(inverter, records, expected) and any(
                h.release_refused_since and now - h.release_refused_since >= DEGRADED_AFTER
                for h in records
            ):
                await self._async_notice(inverter, expected, now, "release",
                                         "A protection hold could not be released; "
                                         "restoration is not verified.")
        return False

    async def _restoration(self, inverter: Inverter, expected: AuthorityState, *,
                           handover: bool = False) -> bool:
        """Q9/Q10: no charging edits are replayed; require fresh resident read-back."""
        c = self.coordinator
        return (await c.writes.async_restoration_held(inverter, epoch=expected.epoch,
                                                       handover=handover)
                and self._refusal(inverter, expected, release=True) is None)


class NativeTransitionWriter:
    """Only protection and startup steps are implemented; other steps fail closed."""
    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator

    async def async_barrier(self) -> None:
        await self.coordinator.writes.async_barrier()

    async def async_step(self, step: str, state: AuthorityState, *, removal: bool) -> bool:
        if step == "evaluate_protection":
            return await self.coordinator.holds.async_reconcile_held(dt_util.utcnow())
        if step == "release_holds":
            return await self.coordinator.holds.async_release_held(
                dt_util.utcnow(), removal=removal)
        return False

    async def async_startup(self, stage: str) -> bool:
        c = self.coordinator
        if stage == "connect":
            return not c.native.closed
        if stage == "read_device_state":
            if c.authority.state is not None and c.authority.state.committed_mode != "observe":
                async with c.native.lock:
                    for inverter in c.native.inverters.values():
                        transport = c.native.transport(inverter)
                        for address, count in ((43007, 1), (43141, 30), (33132, 21)):
                            epoch = getattr(transport, "connection_epoch", 0)
                            result = await (transport.read_input if address < 40000 else
                                transport.read_holding)(address, count,
                                                        device_id=inverter.gateway_unit_id)
                            key = f"{'input' if address < 40000 else 'holding'}_{address}"
                            c.native.readings[inverter.id][key] = Reading(
                                dt_util.utcnow(), result.registers if result.error is None and
                                len(result.registers) == count and valid_words(result.registers)
                                else None, epoch)
            return True
        if stage == "read_evidence":
            c.holds.evidence.read(dt_util.utcnow())
            return True
        if stage == "reconcile_holds":
            await c.holds.async_reconcile(dt_util.utcnow())
            # Refused holds remain durable and visible; do not stop their retries.
            return c.authority.state is not None
        return False
