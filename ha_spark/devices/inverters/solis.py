"""Solis inverter driver: native timed-slot charge control over a thin HA
``modbus:`` overlay (the ``solis_control`` hub stood up in #90), plus the
declarative power-switch reconcile (ADR-0003 rule 2, #140): ha-spark owns both
edges of the whole-inverter enable, driving it to ``Off`` while a dispatch hold
is active and ``On`` otherwise.

Control surface (decided in #82, validated by live-fire #83, register map from
#100/#80 — **no tier-A source; cross-checked live 2026-09-08**): the timed-slot
holding registers, written *natively* via ``modbus.write_register`` and read
back through the overlay's own ``sensor.<hub>_*`` entities. ha-spark never
imports pymodbus — HA owns the connection; this is a normal HA service call.

The solax integration's "update charge/discharge times" button is itself a
multi-register block write starting at 43143, so there is **no separate commit
step**: writing the 8-register window block *is* the commit (see
``docs/solis-control-modbus-overlay.yaml`` and RUN-83-log). The charge-current
register (43141) is a standalone single-register write that applies on write.

PROACTIVE_MODE + control authority (via ``effective_mode``) gate side effects:
``simulate``/``observe`` -> log intended writes only; ``on`` -> real
``call_service``; ``off`` -> compute only. Each write isolates its own failure
and reads back to confirm the device took it. The forced-charge program (window
+ current) additionally refuses when grid charging is not permitted by the
inverter's work mode (bit 5); the live rate-tier throttle is not so gated.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar
from zoneinfo import ZoneInfo

from ha_spark.devices.base import Capability, effective_mode, fmt_hhmm
from ha_spark.devices.registry import register
from ha_spark.energy.export_store import ExportEventStore
from ha_spark.energy.models import ChargeIntent, window_hours
from ha_spark.logging import get_logger

if TYPE_CHECKING:  # avoid an import cycle: config imports devices.base at runtime
    from ha_spark.config import DeviceConfig, Settings
    from ha_spark.ha.rest import HomeAssistantRest

log = get_logger(__name__)

# --- Solis S5 AC-coupled register map (holding registers). No tier-A source;
# from #80/#100 reverse-engineering, cross-checked against the live inverter
# 2026-09-08. The window block matches solax-modbus's WRITE_MULTI for the
# "update charge/discharge times" button (plugin_solis.py). ---
_CHARGE_CURRENT_REG = 43141  # DC amps x10 (raw 600 == 60.0 A)
_DISCHARGE_CURRENT_REG = 43142  # DC amps x10 (raw 625 == 62.5 A)
# WRITE_MULTIPLE base per slot; 8 consecutive registers, in this order:
#   charge start h/m, charge end h/m, discharge start h/m, discharge end h/m.
_SLOT_BLOCK_REG = {1: 43143, 2: 43153, 3: 43163}
# Overlay sensor suffixes for the 8-register window block, block order.
_WINDOW_FIELDS = (
    "timed_charge_start_hours",
    "timed_charge_start_minutes",
    "timed_charge_end_hours",
    "timed_charge_end_minutes",
    "timed_discharge_start_hours",
    "timed_discharge_start_minutes",
    "timed_discharge_end_hours",
    "timed_discharge_end_minutes",
)
_SLOT_SUFFIX = {1: "", 2: "_2", 3: "_3"}
# Current register scale: the overlay sensor decodes raw/10 to amps; encode x10.
_CURRENT_SCALE = 10.0
# HA's update_entity service is asynchronous with respect to the Modbus
# overlay sensors. Retry a small, fixed number of times after each write; the
# bound is deliberately finite so a stale overlay can never hold the caller.
_READ_BACK_ATTEMPTS = 3
_READ_BACK_DELAY_SECONDS = 0.1
# Work-mode bitfield: bit 5 (mask 32) == grid charging permitted. A forced grid
# charge is refused by firmware when this is unset, so assert it, never write it.
_GRID_CHARGE_BIT = 1 << 5
_EXPORT_CURRENT_A = 62.5
_EXPORT_CURRENT_RAW = 625
T = TypeVar("T")


@register("solis")
class SolisDevice:
    """Solis: native timed-slot charge (modbus overlay) + power-switch hold."""

    capabilities = frozenset(
        {Capability.CHARGE_WINDOW, Capability.CHARGE_RATE, Capability.STOP_DISCHARGE}
    )
    # Transitional `Charger`-protocol compat (`chargers.charger_for`);
    # superseded by `Capability.CHARGE_RATE in capabilities` in Task 6.
    supports_live_rate = True

    def __init__(self, config: DeviceConfig, settings: Settings, rest: HomeAssistantRest) -> None:
        self._config = config
        self._settings = settings
        self._rest = rest
        self._hub = settings.solis_control_hub
        self._slave = settings.solis_modbus_slave

    # --- planning ---

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return solis_current_a(intent, self._settings) * self._settings.battery_voltage_v

    # --- apply: the forced-charge sequence ---

    async def apply(self, intent: ChargeIntent) -> list[str]:
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        tz = ZoneInfo(self._settings.timezone)
        now = datetime.now(tz)
        # The power-switch reconcile runs first and unconditionally (#140). It is
        # exempt from the SoC guard below — it commands no SoC-derived magnitude,
        # and freezing the inverter `Off` on an unrelated dead sensor is the
        # failure it exists to remove — and it must settle before anything else
        # reads the switch, or `_require_power_switch_on` would refuse export on
        # the very tick a hold ends, against a state already being corrected.
        lines: list[str] = [await self._reconcile_power_switch(intent, now)]
        # SoC-unreadable guard: soc_now==0 from a dead sensor would size a max charge.
        if mode == "on" and not intent.soc.ok:
            line = (
                f"[BLOCKED] {intent.soc.reason}; not charging to "
                f"{intent.target_soc_pct:.0f}%"
            )
            log.warning(line)
            lines.append(line)
            return lines
        raw_export = getattr(intent, "export", None)
        export_store = ExportEventStore(self._settings.db_path)
        export = _export_window(intent, tz)
        export_ready = export is not None
        if raw_export is not None and export is None:
            export_ready = False
            lines.append("[BLOCKED] export refused: malformed export window")
        elif export_ready:
            assert export is not None
            export_error = _validate_export(intent, export)
            if export_error is None and intent.hold_overlaps(*export):
                # Hold beats export on overlap: the reconcile will hold the
                # inverter `Off` inside the event, so refuse the whole window
                # rather than program a discharge that is cut mid-slot. Tested
                # against the window, not the clock: a morning dispatch must not
                # refuse an evening event, and an evening dispatch must refuse it
                # even when the plan is computed hours earlier.
                export_error = "a dispatch hold overlaps the export window"
            if mode == "on" and export_error is None:
                export_error = await self._require_power_switch_on()
            if export_error is not None:
                export_ready = False
                lines.append(f"[BLOCKED] export refused: {export_error}")
            else:
                discharge_ok, discharge_line = await self._write_discharge_current_result()
                lines.append(discharge_line)
                export_ready = discharge_ok
                if not discharge_ok:
                    lines.append("[BLOCKED] export refused: discharge current was not confirmed")
        # Grid-charge gate for the whole forced-charge program: read the work
        # mode once. A real write is refused when bit 5 is unset (firmware would
        # ignore the force anyway). Non-"on" modes don't read/gate.
        blocked = await self._assert_grid_charge_allowed() if mode == "on" else None
        # Current is the safety prerequisite for slot 1. If an existing window
        # is active at another current, deactivate it and confirm zero before
        # attempting the current transition. This leaves a failed transition
        # safe at zero rather than continuing an old, higher-rate window.
        deactivation_line: str | None = None
        if mode == "on" and blocked is None:
            current_ok, current_line, deactivation_line = await self._prepare_slot_one_current(
                intent
            )
        else:
            current_ok, current_line = await self._write_charge_current_result(intent, blocked)
        if deactivation_line is not None:
            lines.append(deactivation_line)
        lines.append(current_line)
        window_block = None if current_ok else (blocked or "planned current was not confirmed")
        window_line = await self._write_charge_window(
            intent,
            window_block,
            export_window=export if export_ready else None,
        )
        lines.append(window_line)
        # Zero-guard the slots the planner does not drive so a stale manual
        # window (charge 2/3, any discharge) can't actuate behind the plan.
        for slot in (2, 3):
            lines.append(await self._zero_guard_slot(slot))
        if mode == "on":
            if raw_export is not None or export_store.exists:
                await self._persist_export_state(raw_export, export, export_ready, window_line)
        return lines

    async def set_charge_rate(self, watts: float) -> str:
        amps = (
            round(watts / self._settings.battery_voltage_v)
            if self._settings.battery_voltage_v > 0
            else 0
        )
        return await self._set_current(amps, f"set charge current to {amps} A ({watts:.0f} W)")

    async def read_charge_rate(self) -> float:
        """Read the live charge-current register (via the overlay sensor).

        Does not catch: callers isolate read failures (the supply guard skips
        the tick)."""
        state = await self._rest.get_state(self._sensor("timed_charge_current"))
        return float(state.state) * self._settings.battery_voltage_v

    # --- internal writes (PROACTIVE_MODE-gated, failure-isolated, read-back verified) ---

    async def _write_charge_window(
        self,
        intent: ChargeIntent,
        blocked: str | None,
        *,
        export_window: tuple[datetime, datetime] | None = None,
    ) -> str:
        """Program charge slot 1's window block, write-if-changed, read-back verified.

        Writing the 8-register block at 43143, including its optional discharge
        half, is the commit. There is no separate commit step."""
        desc = (
            f"set charge window {fmt_hhmm(intent.window_start)}-{fmt_hhmm(intent.window_end)} "
            f"for the {window_hours(intent.window_start, intent.window_end):.1f} h window"
        )
        if export_window is not None:
            export_start, export_end = export_window
            desc += (
                f" and export {export_start:%H:%M}-{export_end:%H:%M}"
                f" at {_EXPORT_CURRENT_A:g} A"
            )
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            log.info("[SIMULATE] would %s", desc)
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        if blocked and export_window is None:
            # Grid-charge permission is not a prerequisite for clearing a
            # resident discharge schedule. Inspect the block first so an
            # ordinary blocked charge request still reports BLOCKED when no
            # export window is resident. A failed inspection fails closed.
            try:
                existing = await self._read_slot_block(1)
            except Exception:
                return f"[BLOCKED] {desc}: {blocked}"
            if any(existing[4:]):
                return await self._clear_discharge_window(desc)
            return f"[BLOCKED] {desc}: {blocked}"
        zero_charge = export_window is not None and solis_current_a(intent, self._settings) <= 0
        if blocked and export_window is not None and not zero_charge:
            # An export write must not be gated by the charge-only work-mode
            # bit. Preserve the resident charge half and replace the discharge
            # half in one atomic Slot 1 block.
            try:
                existing = await self._read_slot_block(1)
            except Exception as exc:  # noqa: BLE001 - fail closed
                return f"[FAILED] {desc}: slot 1 state unreadable: {exc!r}"
            charge_values = existing[:4]
        elif zero_charge:
            charge_values = [0, 0, 0, 0]
        else:
            charge_values = [
                intent.window_start.hour,
                intent.window_start.minute,
                intent.window_end.hour,
                intent.window_end.minute,
            ]
        discharge_values = (
            [
                export_window[0].hour,
                export_window[0].minute,
                export_window[1].hour,
                export_window[1].minute,
            ]
            if export_window is not None
            else [0, 0, 0, 0]
        )
        want_block = [*charge_values, *discharge_values]
        try:
            wrote = await self._apply_slot_block(1, want_block)
            mismatch = await self._verify_slot_block(1, want_block, refresh=wrote)
        except Exception as exc:  # noqa: BLE001 - isolate per action
            log.error("[FAILED] %s: %r", desc, exc)
            return f"[FAILED] {desc}: {exc!r}"
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return f"[WARNING] {desc}, but {mismatch}"
        return f"[APPLIED] {desc}" if wrote else f"[SKIP] {desc} (already set)"

    async def _clear_discharge_window(self, desc: str) -> str:
        """Clear only Slot 1's discharge half, preserving any charge schedule."""
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        safe_desc = desc.replace("set charge window", "set timed charge schedule")
        clear_desc = f"clear timed export window ({safe_desc})"
        if mode == "simulate":
            return f"[SIMULATE] would {clear_desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {clear_desc}"
        try:
            existing = await self._read_slot_block(1)
            want = existing[:4] + [0, 0, 0, 0]
            wrote = await self._apply_slot_block(1, want)
            mismatch = await self._verify_slot_block(1, want, refresh=wrote)
        except Exception as exc:  # noqa: BLE001 - cleanup is failure-isolated
            log.error("[FAILED] %s: %r", clear_desc, exc)
            return f"[FAILED] {clear_desc}: {exc!r}"
        if mismatch:
            return f"[WARNING] {clear_desc}, but {mismatch}"
        return f"[APPLIED] {clear_desc}" if wrote else f"[SKIP] {clear_desc} (already clear)"

    async def _write_charge_current_result(
        self, intent: ChargeIntent, blocked: str | None
    ) -> tuple[bool, str]:
        """Write and freshly verify the planned current, returning success separately."""
        # solis_current_a already clamps to max_charge_current_a (<= the 62.5 A
        # DC hardware ceiling); round to the integer register value.
        amps = round(solis_current_a(intent, self._settings))
        desc = (
            f"set timed charge current to {amps} A for the "
            f"{window_hours(intent.window_start, intent.window_end):.1f} h window"
        )
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "on" and blocked:
            log.warning("[BLOCKED] %s: %s", desc, blocked)
            return False, f"[BLOCKED] {desc}: {blocked}"
        return await self._set_current_result(amps, desc)

    async def _prepare_slot_one_current(
        self, intent: ChargeIntent
    ) -> tuple[bool, str, str | None]:
        """Make slot 1 safe before changing its planned charge current."""
        amps = round(solis_current_a(intent, self._settings))
        current_desc = (
            f"set timed charge current to {amps} A for the "
            f"{window_hours(intent.window_start, intent.window_end):.1f} h window"
        )
        zeros = [0] * len(_WINDOW_FIELDS)
        try:
            active = await self._read_slot_block(1) != zeros
        except Exception as exc:  # noqa: BLE001 - do not guess at an active window
            line = f"[FAILED] {current_desc}: slot 1 state unreadable: {exc!r}"
            log.error(line)
            return False, line, None
        if not active:
            ok, line = await self._set_current_result(amps, current_desc)
            return ok, line, None

        try:
            current = await self._read_current_a()
            needs_deactivation = abs(current - amps) > 0.5
        except Exception:
            needs_deactivation = True
        if needs_deactivation:
            deactivated, deactivation_line = await self._zero_slot(
                1, "deactivate timed slot 1 window"
            )
            if not deactivated:
                line = f"[BLOCKED] {current_desc}: slot 1 was not safely deactivated"
                log.warning(line)
                return False, line, deactivation_line
            ok, line = await self._set_current_result(amps, current_desc)
            return ok, line, deactivation_line

        ok, line = await self._set_current_result(amps, current_desc)
        return ok, line, None

    async def _zero_guard_slot(self, slot: int) -> str:
        """Zero a non-driven slot's window block, but only if it is non-zero
        (register endurance: steady state costs zero writes)."""
        desc = f"zero timed slot {slot} window"
        _ok, line = await self._zero_slot(slot, desc)
        return line

    async def _zero_slot(self, slot: int, desc: str) -> tuple[bool, str]:
        """Zero a slot and confirm it, returning whether zero was confirmed."""
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            return True, f"[SIMULATE] would {desc} (if non-zero)"
        if mode in ("off", "observe"):
            return True, f"[{mode.upper()}] computed: {desc} (if non-zero)"
        zeros = [0] * len(_WINDOW_FIELDS)
        try:
            current = await self._read_slot_block(slot)
            if current == zeros:
                return True, f"[SKIP] slot {slot} already zeroed"
            await self._write_register(_SLOT_BLOCK_REG[slot], zeros)
            mismatch = await self._verify_slot_block(slot, zeros, refresh=True)
        except Exception as exc:  # noqa: BLE001
            log.error("[FAILED] %s: %r", desc, exc)
            return False, f"[FAILED] {desc}: {exc!r}"
        if mismatch:
            return False, f"[WARNING] {desc}, but {mismatch}"
        return True, f"[APPLIED] {desc}"

    async def _set_current(self, amps: float, desc: str) -> str:
        """Live charge-rate write (rate tier / supply guard). Single register,
        no commit block needed."""
        _ok, line = await self._set_current_result(amps, desc)
        return line

    async def _set_current_result(self, amps: float, desc: str) -> tuple[bool, str]:
        """Set current and return whether its post-write value was confirmed."""
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            log.info("[SIMULATE] would %s", desc)
            return True, f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return True, f"[{mode.upper()}] computed: {desc}"
        try:
            wrote = await self._apply_current(round(amps * _CURRENT_SCALE), amps)
            mismatch = await self._verify_current(amps, refresh=wrote)
        except Exception as exc:  # noqa: BLE001 - isolate per write
            log.error("[FAILED] %s: %r", desc, exc)
            return False, f"[FAILED] {desc}: {exc!r}"
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return False, f"[WARNING] {desc}, but {mismatch}"
        return True, f"[APPLIED] {desc}"

    async def _reconcile_power_switch(self, intent: ChargeIntent, now: datetime) -> str:
        """Converge the whole-inverter enable on the state the clock implies (#140).

        Desired state is a pure function of ``now`` and ``intent.holds``: ``Off``
        while a hold is active, ``On`` otherwise. Nothing is remembered, so a
        restart or a crash mid-hold converges on the next tick, and ha-spark owns
        both edges rather than only ever subtracting (ADR-0003).

        It must never read export state. If the switch is ``On`` during a paid
        export that is because no hold is active, not because export asked —
        ``_require_power_switch_on`` stays a read-only refusal.
        """
        wanted = "Off" if intent.hold_active(now) else "On"
        desc = f"set inverter power switch to {wanted}"
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        entity = self._config.entities.get("power_switch", "")
        if not entity:
            return "[SKIP] no power_switch entity configured; discharge left as-is"
        try:
            if (await self._rest.get_state(entity)).state.strip().lower() == wanted.lower():
                return f"[SKIP] {desc} (already set)"
            await self._rest.call_service(
                "select", "select_option", {"entity_id": entity, "option": wanted}
            )
            mismatch = await self._read_back_option(entity, wanted)
        except Exception as exc:  # noqa: BLE001 - isolate this action's failure
            log.error("[FAILED] %s: %r", desc, exc)
            return f"[FAILED] {desc}: {exc!r}"
        return f"[WARNING] {desc}, but {mismatch}" if mismatch else f"[APPLIED] {desc}"

    async def _write_discharge_current_result(self) -> tuple[bool, str]:
        """Set and verify the fixed full-rate command used for paid export."""
        desc = f"set timed discharge current to {_EXPORT_CURRENT_A:g} A for export"
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            return True, f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return True, f"[{mode.upper()}] computed: {desc}"
        try:
            wrote = await self._apply_discharge_current(_EXPORT_CURRENT_RAW)
            mismatch = await self._verify_discharge_current(
                _EXPORT_CURRENT_A, refresh=wrote
            )
        except Exception as exc:  # noqa: BLE001 - isolate export action
            log.error("[FAILED] %s: %r", desc, exc)
            return False, f"[FAILED] {desc}: {exc!r}"
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return False, f"[WARNING] {desc}, but {mismatch}"
        return True, f"[APPLIED] {desc}" if wrote else f"[SKIP] {desc} (already set)"

    async def _require_power_switch_on(self) -> str | None:
        entity = self._config.entities.get("power_switch", "")
        if not entity:
            return "power_switch entity is not configured"
        try:
            state = await self._rest.get_state(entity)
        except Exception as exc:  # noqa: BLE001 - fail closed
            return f"power_switch state unreadable: {exc!r}"
        return None if state.state.strip().lower() == "on" else f"power_switch is {state.state!r}"

    async def _persist_export_state(
        self,
        raw_export: object,
        export: tuple[datetime, datetime] | None,
        export_ready: bool,
        window_line: str,
    ) -> None:
        """Record only a fully verified event; clear state for cancellation/failure."""
        try:
            async with ExportEventStore(self._settings.db_path) as store:
                if (
                    export is not None
                    and export_ready
                    and window_line.startswith(("[APPLIED]", "[SKIP]"))
                ):
                    start, end = export
                    await store.save(_export_identity(raw_export, start, end), end)
                elif export is None and window_line.startswith(("[APPLIED]", "[SKIP]")):
                    await store.clear()
        except Exception as exc:  # noqa: BLE001 - persistence cannot undo HA writes
            log.error("Export event state persistence failed: %r", exc)

    # --- modbus helpers ---

    def _sensor(self, field: str) -> str:
        return f"sensor.{self._hub}_{field}"

    async def _write_register(self, address: int, value: int | list[int]) -> None:
        await self._rest.call_service(
            "modbus",
            "write_register",
            {"hub": self._hub, "slave": self._slave, "address": address, "value": value},
        )

    async def _apply_slot_block(self, slot: int, want: list[int]) -> bool:
        """Write the slot's window block only when it differs; return whether written."""
        if await self._read_slot_block(slot) == want:
            return False
        await self._write_register(_SLOT_BLOCK_REG[slot], want)
        return True

    async def _apply_current(self, want_raw: int, amps: float) -> bool:
        """Write the charge-current register only when it differs; return whether written."""
        if abs(await self._read_current_a() - amps) <= 0.5:
            return False
        await self._write_register(_CHARGE_CURRENT_REG, want_raw)
        return True

    async def _apply_discharge_current(self, want_raw: int) -> bool:
        """Write the fixed export current only when its read-back differs."""
        if abs(await self._read_discharge_current_a() - _EXPORT_CURRENT_A) <= 0.5:
            return False
        await self._write_register(_DISCHARGE_CURRENT_REG, want_raw)
        return True

    async def _read_slot_block(self, slot: int) -> list[int]:
        # Parse strictly (not via the tolerant _to_float): an unreadable slot
        # register must raise so the caller degrades to [FAILED]/read-back
        # mismatch, never coerce to a default that could mis-decide a write.
        suffix = _SLOT_SUFFIX[slot]
        out: list[int] = []
        for field in _WINDOW_FIELDS:
            state = await self._rest.get_state(self._sensor(field + suffix))
            out.append(int(float(state.state)))
        return out

    async def _read_current_a(self) -> float:
        state = await self._rest.get_state(self._sensor("timed_charge_current"))
        return float(state.state)

    async def _read_discharge_current_a(self) -> float:
        state = await self._rest.get_state(self._sensor("timed_discharge_current"))
        return float(state.state)

    async def _verify_slot_block(
        self, slot: int, want: list[int], *, refresh: bool = False
    ) -> str | None:
        entities = tuple(
            self._sensor(field + _SLOT_SUFFIX[slot]) for field in _WINDOW_FIELDS
        )
        return await self._verify_read_back(
            lambda: self._read_slot_block(slot),
            lambda got: got == want,
            lambda got: f"read back slot {slot} {got} (wanted {want})",
            refresh_entities=entities if refresh else None,
        )

    async def _verify_current(self, amps: float, *, refresh: bool = False) -> str | None:
        return await self._verify_read_back(
            self._read_current_a,
            lambda got: abs(got - amps) <= 0.5,
            lambda got: f"read back {got:g} A (wanted {amps:g} A)",
            refresh_entities=(self._sensor("timed_charge_current"),) if refresh else None,
        )

    async def _verify_discharge_current(
        self, amps: float, *, refresh: bool = False
    ) -> str | None:
        return await self._verify_read_back(
            self._read_discharge_current_a,
            lambda got: abs(got - amps) <= 0.5,
            lambda got: f"read back discharge {got:g} A (wanted {amps:g} A)",
            refresh_entities=(self._sensor("timed_discharge_current"),) if refresh else None,
        )

    async def _verify_read_back(
        self,
        read: Callable[[], Awaitable[T]],
        matches: Callable[[T], bool],
        describe_mismatch: Callable[[T], str],
        *,
        refresh_entities: tuple[str, ...] | None = None,
    ) -> str | None:
        """Refresh once, then perform a small bounded read-back observation."""
        if refresh_entities is not None:
            try:
                await self._refresh_entities(refresh_entities)
            except Exception as exc:  # noqa: BLE001 - verification must degrade safely
                return f"read-back refresh failed: {exc!r}"
        mismatch: str | None = None
        last_exc: Exception | None = None
        for attempt in range(_READ_BACK_ATTEMPTS):
            try:
                got = await read()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            else:
                last_exc = None
                mismatch = None if matches(got) else describe_mismatch(got)
                if mismatch is None:
                    return None
            if attempt + 1 < _READ_BACK_ATTEMPTS:
                await asyncio.sleep(_READ_BACK_DELAY_SECONDS)
        if last_exc is not None:
            return f"read-back failed: {last_exc!r}"
        return mismatch or "read-back failed"

    async def _refresh_entities(self, entity_ids: tuple[str, ...]) -> None:
        await self._rest.call_service(
            "homeassistant", "update_entity", {"entity_id": list(entity_ids)}
        )

    async def _assert_grid_charge_allowed(self) -> str | None:
        """None when grid charging is permitted; else a reason string (refuse)."""
        try:
            state = await self._rest.get_state(self._sensor("work_mode_bitfield"))
            bitfield = int(float(state.state))
        except Exception as exc:  # noqa: BLE001
            return f"work mode unreadable: {exc!r}"
        if not bitfield & _GRID_CHARGE_BIT:
            return f"grid charging not permitted (work mode bitfield {bitfield}, bit 5 unset)"
        return None

    async def _read_back_option(self, entity: str, wanted: str) -> str | None:
        try:
            got = str((await self._rest.get_state(entity)).state)
        except Exception as exc:  # noqa: BLE001
            return f"read-back failed: {exc!r}"
        return None if got.lower() == wanted.lower() else f"read back {got!r} (wanted {wanted!r})"


def _export_window(intent: ChargeIntent, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """Read the optional planner export value without coupling to its model type.

    Resolved to ``tz`` — the household clock the inverter runs on. The Slot 1
    window registers hold local wall-clock time, and the charge half of that
    same block is written from ``intent.window_start``, a local ``time``. Both
    halves must share one basis or a BST event is programmed an hour early.
    """
    value = getattr(intent, "export", None)
    if value is None:
        return None
    get = (
        value.get
        if isinstance(value, dict)
        else lambda name, default=None: getattr(value, name, default)
    )
    start = get("window_start", get("start", get("selected_start")))
    end = get("window_end", get("end", get("selected_end")))
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    return start.astimezone(tz), end.astimezone(tz)


def _validate_export(intent: ChargeIntent, export: tuple[datetime, datetime]) -> str | None:
    """Fail closed on malformed export values and untrusted SoC observations."""
    start, end = export
    value = getattr(intent, "export", None)
    get = (
        value.get
        if isinstance(value, dict)
        else lambda name, default=None: getattr(value, name, default)
    )
    planned_kw = get("planned_export_kw")
    dno_kw = get("dno_export_limit_kw")
    if not isinstance(planned_kw, (int, float)) or not math.isfinite(float(planned_kw)):
        return "planned export power is not finite"
    if float(planned_kw) <= 0:
        return "planned export power is not positive"
    if dno_kw is not None and (
        not isinstance(dno_kw, (int, float))
        or not math.isfinite(float(dno_kw))
        or float(dno_kw) <= 0
    ):
        return "DNO export limit is invalid"
    if end <= start or end <= datetime.now(UTC):
        return "export window is stale or empty"
    soc = intent.soc.soc_now
    if not intent.soc.ok or not math.isfinite(soc) or not 0 <= soc <= 100:
        return intent.soc.reason if not intent.soc.ok else "SoC is outside 0-100%"
    return None


def _export_identity(value: object, start: datetime, end: datetime) -> str:
    """Return the original Axle identity, not a shortened selected window."""
    identity = (
        value.get("event_identity")
        if isinstance(value, dict)
        else getattr(value, "event_identity", None)
    )
    if isinstance(identity, tuple) and len(identity) == 3:
        direction, event_start, event_end = identity
        if (
            isinstance(direction, str)
            and isinstance(event_start, datetime)
            and isinstance(event_end, datetime)
        ):
            return (
                f"{direction}|{event_start.astimezone(UTC).isoformat()}|"
                f"{event_end.astimezone(UTC).isoformat()}"
            )
    # Compatibility for a third-party adapter that has not yet supplied the
    # resolved identity; planner-owned ExportIntent always does.
    return f"export|{start.astimezone(UTC).isoformat()}|{end.astimezone(UTC).isoformat()}"


def solis_current_a(intent: ChargeIntent, settings: Settings) -> float:
    """DC charge current (A) for the intent — the legacy planner sizing, inverted."""
    needed_kwh = max(
        0.0, (intent.target_soc_pct - intent.soc_now) / 100.0 * settings.battery_capacity_kwh
    )
    eff = settings.charge_efficiency if settings.charge_efficiency > 0 else 1.0
    purchase = needed_kwh / eff
    kwh_per_amp = (
        window_hours(intent.window_start, intent.window_end) * settings.battery_voltage_v / 1000.0
    )
    if kwh_per_amp <= 0:
        return 0.0
    return min(settings.max_charge_current_a, purchase / kwh_per_amp)
