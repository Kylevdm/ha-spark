"""Solis inverter driver: native timed-slot charge control over a thin HA
``modbus:`` overlay (the ``solis_control`` hub stood up in #90), plus the
power-switch stop-discharge hold (unchanged, ADR-0003 rule 2).

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

from typing import TYPE_CHECKING

from ha_spark.devices.base import Capability, effective_mode, fmt_hhmm
from ha_spark.devices.registry import register
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
# Work-mode bitfield: bit 5 (mask 32) == grid charging permitted. A forced grid
# charge is refused by firmware when this is unset, so assert it, never write it.
_GRID_CHARGE_BIT = 1 << 5


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
        # SoC-unreadable guard: soc_now==0 from a dead sensor would size a max charge.
        if mode == "on" and not intent.soc_valid:
            line = f"[BLOCKED] SoC unreadable; not charging to {intent.target_soc_pct:.0f}%"
            log.warning(line)
            return [line]
        lines: list[str] = []
        # Grid-charge gate for the whole forced-charge program: read the work
        # mode once. A real write is refused when bit 5 is unset (firmware would
        # ignore the force anyway). Non-"on" modes don't read/gate.
        blocked = await self._assert_grid_charge_allowed() if mode == "on" else None
        # Window and current are separate register writes, each isolated so a
        # failure on one is reported distinctly and never masks the other.
        lines.append(await self._write_charge_window(intent, blocked))
        lines.append(await self._write_charge_current(intent, blocked))
        # Zero-guard the slots the planner does not drive so a stale manual
        # window (charge 2/3, any discharge) can't actuate behind the plan.
        for slot in (2, 3):
            lines.append(await self._zero_guard_slot(slot))
        for start, end in intent.holds:
            lines.append(
                await self._stop_discharge(
                    f"turn inverter off (stop discharge) during dispatch "
                    f"{start:%H:%M}-{end:%H:%M}"
                )
            )
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

    async def _write_charge_window(self, intent: ChargeIntent, blocked: str | None) -> str:
        """Program charge slot 1's window block, write-if-changed, read-back verified.

        Writing the 8-register block at 43143 (discharge half held at 0) *is* the
        commit — there is no separate commit step."""
        desc = (
            f"set charge window {fmt_hhmm(intent.window_start)}-{fmt_hhmm(intent.window_end)} "
            f"for the {window_hours(intent.window_start, intent.window_end):.1f} h window"
        )
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            log.info("[SIMULATE] would %s", desc)
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        if blocked:
            log.warning("[BLOCKED] %s: %s", desc, blocked)
            return f"[BLOCKED] {desc}: {blocked}"
        want_block = [
            intent.window_start.hour,
            intent.window_start.minute,
            intent.window_end.hour,
            intent.window_end.minute,
            0, 0, 0, 0,  # discharge half of slot 1: always zeroed here
        ]
        try:
            wrote = await self._apply_slot_block(1, want_block)
            mismatch = await self._verify_slot_block(1, want_block)
        except Exception as exc:  # noqa: BLE001 - isolate per action
            log.error("[FAILED] %s: %r", desc, exc)
            return f"[FAILED] {desc}: {exc!r}"
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return f"[WARNING] {desc}, but {mismatch}"
        return f"[APPLIED] {desc}" if wrote else f"[SKIP] {desc} (already set)"

    async def _write_charge_current(self, intent: ChargeIntent, blocked: str | None) -> str:
        """Program slot 1's charge current (43141), a standalone single register."""
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
            return f"[BLOCKED] {desc}: {blocked}"
        return await self._set_current(amps, desc)

    async def _zero_guard_slot(self, slot: int) -> str:
        """Zero a non-driven slot's window block, but only if it is non-zero
        (register endurance: steady state costs zero writes)."""
        desc = f"zero timed slot {slot} window"
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            return f"[SIMULATE] would {desc} (if non-zero)"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc} (if non-zero)"
        zeros = [0] * len(_WINDOW_FIELDS)
        try:
            current = await self._read_slot_block(slot)
            if current == zeros:
                return f"[SKIP] slot {slot} already zeroed"
            await self._write_register(_SLOT_BLOCK_REG[slot], zeros)
            mismatch = await self._verify_slot_block(slot, zeros)
        except Exception as exc:  # noqa: BLE001
            log.error("[FAILED] %s: %r", desc, exc)
            return f"[FAILED] {desc}: {exc!r}"
        return f"[WARNING] {desc}, but {mismatch}" if mismatch else f"[APPLIED] {desc}"

    async def _set_current(self, amps: float, desc: str) -> str:
        """Live charge-rate write (rate tier / supply guard). Single register,
        no commit block needed."""
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            log.info("[SIMULATE] would %s", desc)
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        try:
            await self._apply_current(round(amps * _CURRENT_SCALE), amps)
            mismatch = await self._verify_current(amps)
        except Exception as exc:  # noqa: BLE001 - isolate per write
            log.error("[FAILED] %s: %r", desc, exc)
            return f"[FAILED] {desc}: {exc!r}"
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return f"[WARNING] {desc}, but {mismatch}"
        return f"[APPLIED] {desc}"

    async def _stop_discharge(self, desc: str) -> str:
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        entity = self._config.entities.get("power_switch", "")
        if not entity:
            return "[SKIP] no power_switch entity configured; discharge left as-is"
        try:
            await self._rest.call_service(
                "select", "select_option", {"entity_id": entity, "option": "Off"}
            )
            mismatch = await self._read_back_option(entity, "Off")
        except Exception as exc:  # noqa: BLE001
            return f"[FAILED] {desc}: {exc!r}"
        return f"[WARNING] {desc}, but {mismatch}" if mismatch else f"[APPLIED] {desc}"

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

    async def _verify_slot_block(self, slot: int, want: list[int]) -> str | None:
        try:
            got = await self._read_slot_block(slot)
        except Exception as exc:  # noqa: BLE001
            return f"read-back failed: {exc!r}"
        return None if got == want else f"read back slot {slot} {got} (wanted {want})"

    async def _verify_current(self, amps: float) -> str | None:
        try:
            got = await self._read_current_a()
        except Exception as exc:  # noqa: BLE001
            return f"read-back failed: {exc!r}"
        return None if abs(got - amps) <= 0.5 else f"read back {got:g} A (wanted {amps:g} A)"

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
