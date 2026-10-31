"""Independent HA report-driven supply guard, with no production device writer.

HA 2026.10.3 source: helpers/event.py async_track_state_report_event documents
that unchanged updates fire state_reported, opposite of state_changed. core.py
State.last_reported documents using event.data['last_reported'] for reports
(the State object can be mutated again), and last_updated for changed events.
Both subscriptions are required. Positive grid watts mean import.

The current summary has neither epoch nor manual session: mirror_from deliberately
supplies None/False. Persisted cuts remain ceilings but cannot be replayed with an
unknown epoch. Phase 8 must provide authority and a native writer, not HA services.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Annotated, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .clamp import ChargeWriter, ClampedWriter, DeviceIntent
from .const import DOMAIN
from .coordinator import SparkCoordinator
from .supply_guard import (
    AuthorityMirror,
    GuardDecision,
    GuardInputs,
    GuardReading,
    GuardState,
    InverterMeasurement,
    mirror_permits,
    step,
)
from .topology import (
    MajorLoad,
    Topology,
    driver_capabilities,
    effective_limits,
    is_running,
    major_load_reserve_kw,
)


class StoredGuard(BaseModel):
    """Strict durable subset; transient report counters never survive restart."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    cut_kw: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] | None
    latched: Annotated[bool, Field(strict=True)]
    missing_since: AwareDatetime | None
    restores_used: Annotated[int, Field(strict=True, ge=0, le=48)]
    restore_day: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    epoch: Annotated[int, Field(strict=True, ge=0)] | None


def mirror_from(coordinator: SparkCoordinator) -> AuthorityMirror | None:
    """Only a counted, still-connected summary is authority evidence."""
    if not coordinator.connected or coordinator.summary is None:
        return None
    authority = coordinator.summary.authority
    return AuthorityMirror(authority.mode, None, False, authority.save_failed,
                           authority.transition_target)


class SupplyGuardLoop:
    """Per-entry state, storage and one serialised clamp/write boundary."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry[Any], coordinator: SparkCoordinator,
        topology: Topology, *, writer: ChargeWriter | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.topology = topology
        self.limits = effective_limits(topology)
        self.lock = asyncio.Lock()
        self.last_requested_a: dict[str, float] = {}
        self.states: dict[str, GuardState] = {}
        self.decisions: dict[str, GuardDecision] = {}
        self.store: Store[dict[str, object]] = Store(
            hass, 1, f"{DOMAIN}.supply_guard.{entry.entry_id}", atomic_writes=True,
        )
        self.writer = ClampedWriter(writer, self) if writer is not None else None
        self._unsubs: list[Callable[[], None]] = []
        self._readings: dict[str, tuple[float | None, datetime | None]] = {}
        # Bounded in-memory event trail for the diagnostics download only.
        self._history: deque[dict[str, object]] = deque(maxlen=48)
        self._active = False
        self._stopped = False
        self._save_failed = False

    def allowance_for(self, bank_id: str) -> float | None:
        """Connection allowance, split by the clamp rather than counted per bank."""
        bank = next((b for b in self.topology.banks if b.id == bank_id), None)
        if bank is None:
            return 0
        state = self.states.get(bank.connection_id)
        return state.allowance_kw if state else 0

    def permits_writes(self) -> bool:
        """Warn-only unless a connected, permitting mirror and a working store."""
        return self._active and not self._save_failed and mirror_permits(
            mirror_from(self.coordinator))

    def diagnostics(self) -> dict[str, object]:
        """Bounded state, event history and raw readings for the diagnostics download.

        No secret, token or site identifier is held here; entity ids are consumed
        HA state references, as everywhere else in this integration.
        """
        connections: list[dict[str, object]] = []
        for key, guard_state in self.states.items():
            decision = self.decisions.get(key)
            connections.append({
                "id": key,
                "cut_kw": guard_state.cut_kw,
                "latched": guard_state.latched,
                "restores_left": guard_state.restores_left,
                "dormant_reason": guard_state.dormant_reason,
                "warn_reason": decision.warn_reason if decision is not None else None,
            })
        readings = {
            entity: {"power_w": power,
                     "reported_at": reported.isoformat() if reported is not None else None}
            for entity, (power, reported) in self._readings.items()
        }
        return {"connections": connections, "history": list(self._history),
                "readings": readings}

    @property
    def started(self) -> bool:
        """Entity availability: the loop runs without the add-on, but not before start."""
        return self._active

    @property
    def limiting(self) -> bool:
        """Site visibility for ticket 11; raw readings stay internal."""
        return any(s.limiting for s in self.states.values())

    @property
    def allowance_kw(self) -> dict[str, float | None]:
        """Per-connection ceilings for snapshot and diagnostics consumers."""
        return {key: s.allowance_kw for key, s in self.states.items()}

    @property
    def restores_left(self) -> dict[str, int]:
        """Per-connection daily restore budget."""
        return {key: s.restores_left for key, s in self.states.items()}

    def _now(self) -> datetime:
        summary = self.coordinator.summary
        zone_name = summary.household_timezone if summary else None
        zone = dt_util.get_time_zone(zone_name or self.hass.config.time_zone)
        if zone is None:
            zone = dt_util.get_time_zone(self.hass.config.time_zone)
        return dt_util.utcnow().astimezone(zone)

    def _power(self, entity: str | None) -> tuple[float | None, datetime | None]:
        if entity in self._readings:
            return self._readings[entity]
        state = self.hass.states.get(entity) if entity else None
        return self._state_power(state, state.last_reported if state else None)

    @staticmethod
    def _state_power(state: State | None, reported: datetime | None) -> tuple[
        float | None, datetime | None,
    ]:
        if state is None:
            return None, None
        try:
            value = float(state.state)
            unit = state.attributes.get("unit_of_measurement")
            if isfinite(value) and unit in ("W", "kW"):
                return value * 1000 if unit == "kW" else value, reported
        except (ValueError, TypeError, OverflowError):
            pass
        return None, reported

    async def async_start(self) -> None:
        """Load cuts before subscribing; entry unload can fence a pending load."""
        async with self.lock:
            if self._stopped or self._active:
                return
            now = self._now()
            self.states = {c.id: GuardState(restore_day=now.date(),
                                          dormant_reason="awaiting_reading")
                           for c in self.topology.connections}
            try:
                raw = await self.store.async_load()
                if raw is not None:
                    if not isinstance(raw, dict):
                        raise ValueError("invalid_guard_store")
                    # A topology edit keeps the cuts of connections that still
                    # exist; new ones start fresh and removed ones are dropped.
                    loaded = dict(self.states)
                    for key, value in raw.items():
                        if key not in self.states:
                            continue
                        record = StoredGuard.model_validate(value)
                        loaded[key] = GuardState(
                            cut_kw=record.cut_kw, latched=record.latched,
                            missing_since=record.missing_since, restores_used=record.restores_used,
                            restore_day=date.fromisoformat(record.restore_day), epoch=record.epoch,
                            restored=record.cut_kw is not None, dormant_reason="awaiting_reading",
                        )
                    self.states = loaded
            except Exception:
                # Corruption is a fresh, dormant machine until a good reading.
                pass
            if self._stopped:
                return
            self._active = True
            entities = {c.grid_power_entity for c in self.topology.connections}
            entities.update(i.grid_port_power_entity or i.battery_power_entity
                            for i in self.topology.inverters)
            entities.update(load.power_entity for load in self.topology.major_loads)
            ids = [entity for entity in entities if entity is not None]
            self._unsubs = [
                async_track_state_change_event(self.hass, ids, self._changed),
                async_track_state_report_event(self.hass, ids, self._reported),
                async_track_time_interval(self.hass, self._tick, timedelta(seconds=5)),
            ]
            self.entry.async_on_unload(self._cancel_subscriptions)
        await self.async_evaluate()
        if self._active:
            # Entities become available the moment the loop runs, before any change.
            self.coordinator.async_update_listeners()

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        self._readings[event.data["entity_id"]] = self._state_power(
            state, state.last_updated if state else None,
        )
        self._schedule()

    @callback
    def _reported(self, event: Event[EventStateReportedData]) -> None:
        self._readings[event.data["entity_id"]] = self._state_power(
            event.data["new_state"], event.data["last_reported"],
        )
        self._schedule()

    @callback
    def _tick(self, now: datetime) -> None:
        self._schedule()

    @callback
    def _schedule(self) -> None:
        if self._active:
            # Capture each event now: queued coroutines must not all see the last
            # state when several reports arrive before the boundary lock is free.
            entities = {c.grid_power_entity for c in self.topology.connections}
            entities.update(i.grid_port_power_entity or i.battery_power_entity
                            for i in self.topology.inverters)
            entities.update(load.power_entity for load in self.topology.major_loads)
            readings = {entity: self._power(entity) for entity in entities if entity is not None}
            self.entry.async_create_background_task(self.hass, self.async_evaluate(readings),
                                                    "Supply guard reading")

    async def _save(self) -> None:
        data: dict[str, object] = {
            key: {"cut_kw": s.cut_kw, "latched": s.latched,
                  "missing_since": s.missing_since.isoformat() if s.missing_since else None,
                  "restores_used": s.restores_used, "restore_day": s.restore_day.isoformat(),
                  "epoch": s.epoch}
            for key, s in self.states.items()
        }
        try:
            await self.store.async_save(data)
            persisted = await Store[dict[str, object]](
                self.hass, 1, self.store.key, atomic_writes=True,
            ).async_load()
            if persisted != data:
                self._save_failed = True
        except Exception:
            self._save_failed = True

    async def async_evaluate(
        self, readings: dict[str, tuple[float | None, datetime | None]] | None = None,
    ) -> None:
        """Timer and reports share the write boundary, independently of polling."""
        async with self.lock:
            if not self._active:
                return
            now = self._now()

            def read(entity: str | None) -> tuple[float | None, datetime | None]:
                if readings is not None:
                    return readings.get(entity, (None, None)) if entity else (None, None)
                return self._power(entity)

            mirror = mirror_from(self.coordinator) if not self._save_failed else None
            before = self.states.copy()
            writes: set[str] = set()
            for connection in self.topology.connections:
                banks = [b for b in self.topology.banks if b.connection_id == connection.id]
                bank_ids = {b.id for b in banks}
                inverters = [i for i in self.topology.inverters if i.bank_id in bank_ids]
                measurements = []
                for inv in inverters:
                    entity = inv.grid_port_power_entity or inv.battery_power_entity
                    if entity:
                        power, reported = read(entity)
                        if power is not None and inv.power_sign == "negative_import":
                            power = -power
                        unadjusted = power
                        if power is not None and not inv.grid_port_power_entity:
                            bank = next(b for b in banks if b.id == inv.bank_id)
                            eta = bank.one_way_charge_efficiency
                            power = power / eta if power >= 0 else power * eta
                        measurements.append(InverterMeasurement(
                            inv.id, power, reported, unadjusted,
                        ))
                loads = [load for load in self.topology.major_loads
                         if load.connection_id == connection.id]

                def running(load: MajorLoad) -> bool:
                    power, reported = read(load.power_entity)
                    return is_running(load, power / 1000 if power is not None else None,
                                      reported, now)

                reserve = major_load_reserve_kw(loads, running)
                lossless_requested = sum(
                    max(0, self.last_requested_a.get(i.id, 0)) * b.nominal_voltage_v / 1000
                    for b in banks for i in inverters if i.bank_id == b.id
                )
                requested = sum(
                    max(0, self.last_requested_a.get(i.id, 0)) * b.nominal_voltage_v
                    / b.one_way_charge_efficiency / 1000
                    for b in banks for i in inverters if i.bank_id == b.id
                )
                power, reported = read(connection.grid_power_entity)
                limit = next(c for c in self.limits.connections if c.id == connection.id)
                state, decision = step(self.states[connection.id], GuardInputs(
                    limit_kw=limit.import_kw, requested_kw=requested, reserve_kw=reserve,
                    lossless_requested_kw=lossless_requested,
                    reading=GuardReading(power, reported), measurements=tuple(measurements),
                    mirror=mirror, grid_configured=connection.grid_power_entity is not None,
                    controllable=any(driver_capabilities(i.driver_kind).setpoint_step_a is not None
                                     for i in inverters), household_day=now.date(),
                    observe_only=self.writer is None,
                ), now)
                self.states[connection.id] = state
                self.decisions[connection.id] = decision
                if decision.write:
                    writes.update(i.id for i in inverters)
            durable_fields = ("cut_kw", "latched", "missing_since", "restores_used",
                              "restore_day", "epoch")
            if any(any(getattr(before[key], field) != getattr(state, field)
                       for field in durable_fields) for key, state in self.states.items()):
                await self._save()
                if self._save_failed:
                    # A failed restore save must not remove the retained ceiling
                    # at the clamp boundary. New cuts are still kept in memory.
                    for key, previous in before.items():
                        current = self.states[key]
                        if previous.cut_kw is not None and (
                            current.cut_kw is None or current.cut_kw > previous.cut_kw
                        ):
                            self.states[key] = replace(current, cut_kw=previous.cut_kw,
                                                       latched=previous.latched,
                                                       dormant_reason="guard_save_failed")
                    self.decisions = {
                        key: replace(decision, write=False, warn_reason="guard_save_failed")
                        for key, decision in self.decisions.items()
                    }
            if self.writer is not None and not self._save_failed:
                for inv in self.topology.inverters:
                    requested_a = self.last_requested_a.get(inv.id, 0)
                    if inv.id in writes and requested_a > 0 and self._active:
                        try:
                            await self.writer.async_write_locked(DeviceIntent(
                                inv.id, inv.bank_id, requested_a,
                            ))
                        except Exception:
                            # A writer's exception may contain device data. Keep
                            # the cut; publish only a fixed failure outcome.
                            bank = next(b for b in self.topology.banks if b.id == inv.bank_id)
                            decision = self.decisions[bank.connection_id]
                            self.decisions[bank.connection_id] = replace(
                                decision, write=False, warn_reason="guard_write_failed",
                            )
            for key, guard_state in self.states.items():
                prior = before.get(key)
                if prior is None:
                    continue
                if prior.cut_kw is not None and guard_state.cut_kw is None:
                    self._history.append({"at": now.isoformat(), "connection_id": key,
                                          "event": "restore",
                                          "allowance_kw": guard_state.allowance_kw})
                if (guard_state.cut_kw is not None
                        and (prior.cut_kw is None or guard_state.cut_kw < prior.cut_kw)):
                    self._history.append({"at": now.isoformat(), "connection_id": key,
                                          "event": "cut", "allowance_kw": guard_state.cut_kw})
                if guard_state.latched and not prior.latched:
                    self._history.append({"at": now.isoformat(), "connection_id": key,
                                          "event": "latch",
                                          "allowance_kw": guard_state.allowance_kw})
            if any(
                (prior.limiting, prior.allowance_kw, prior.restores_left)
                != (guard_state.limiting, guard_state.allowance_kw, guard_state.restores_left)
                for key, guard_state in self.states.items()
                if (prior := before.get(key)) is not None
            ):
                # Entities and the snapshot follow the guard, never the 30 s poll
                # clock. The heartbeat task is never awaited inside this lock.
                # async_refresh rather than async_request_refresh: the debouncer
                # would only batch a second change, and its cooldown timer must
                # not outlive the loop's own change notifications.
                self.coordinator.async_update_listeners()
                self.entry.async_create_background_task(
                    self.hass, self.coordinator.async_refresh(),
                    "Supply guard heartbeat",
                )

    @callback
    def _cancel_subscriptions(self) -> None:
        # HA also calls this on unload: unsubscribe exactly once even when the
        # platforms fail to unload after our explicit stop.
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def async_stop(self) -> None:
        """Fence queued callbacks and finish any in-flight fake write before return."""
        self._stopped = True
        self._active = False
        self._cancel_subscriptions()
        async with self.lock:
            if self.states:
                await self._save()
