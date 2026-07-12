"""Solis inverter driver: timed charge current (number) + window (time/select) +
power switch (select).

PROACTIVE_MODE + control authority (via `effective_mode`) gate side effects:
``simulate``/``observe`` -> log intended writes only; ``on`` -> real
``call_service``; ``off`` -> compute only. Each write isolates its own failure
and reads back to confirm the device took it.
"""
from __future__ import annotations

from datetime import time

from ha_spark.config import DeviceConfig, Settings
from ha_spark.devices.base import Capability, effective_mode
from ha_spark.devices.registry import register
from ha_spark.energy.models import ChargeIntent, window_hours
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)


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


def _fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


@register("solis")
class SolisDevice:
    """Solis: timed charge current (number) + window (time/select) + power switch (select)."""

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

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return solis_current_a(intent, self._settings) * self._settings.battery_voltage_v

    async def apply(self, intent: ChargeIntent) -> list[str]:
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        lines: list[str] = []
        current = round(solis_current_a(intent, self._settings))
        # SoC-unreadable guard: soc_now==0 from a dead sensor would size a max charge.
        if mode == "on" and not intent.soc_valid:
            line = f"[BLOCKED] SoC unreadable; not charging to {intent.target_soc_pct:.0f}%"
            log.warning(line)
            return [line]
        lines.append(await self._write_window(intent))
        lines.append(
            await self._set_current(
                current,
                f"set timed charge current to {current} A for the "
                f"{window_hours(intent.window_start, intent.window_end):.1f} h window",
            )
        )
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
        """Does not catch: callers isolate read failures (the supply guard skips the tick)."""
        state = await self._rest.get_state(self._config.entities["charge_current"])
        return float(state.state) * self._settings.battery_voltage_v

    # --- internal writes (PROACTIVE_MODE-gated, failure-isolated, read-back verified) ---

    async def _set_current(self, amps: float, desc: str) -> str:
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            log.info("[SIMULATE] would %s", desc)
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        try:
            entity = self._config.entities["charge_current"]
            await self._rest.call_service(
                "number", "set_value", {"entity_id": entity, "value": amps}
            )
            mismatch = await self._read_back_number(entity, amps)
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
        try:
            entity = self._config.entities["power_switch"]
            await self._rest.call_service(
                "select", "select_option", {"entity_id": entity, "option": "Off"}
            )
            mismatch = await self._read_back_option(entity, "Off")
        except Exception as exc:  # noqa: BLE001
            return f"[FAILED] {desc}: {exc!r}"
        return f"[WARNING] {desc}, but {mismatch}" if mismatch else f"[APPLIED] {desc}"

    async def _write_window(self, intent: ChargeIntent) -> str:
        start_e = self._config.entities.get("window_start", "")
        end_e = self._config.entities.get("window_end", "")
        if not (start_e and end_e):
            return "[SKIP] no window entities configured; window left as-is"
        desc = f"set charge window {_fmt_hhmm(intent.window_start)}-{_fmt_hhmm(intent.window_end)}"
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "simulate":
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        try:
            await self._rest.call_service(
                "time",
                "set_value",
                {"entity_id": start_e, "time": _fmt_hhmm(intent.window_start) + ":00"},
            )
            await self._rest.call_service(
                "time",
                "set_value",
                {"entity_id": end_e, "time": _fmt_hhmm(intent.window_end) + ":00"},
            )
        except Exception as exc:  # noqa: BLE001
            return f"[FAILED] {desc}: {exc!r}"
        return f"[APPLIED] {desc}"

    async def _read_back_number(self, entity: str, wanted: float) -> str | None:
        try:
            got = float((await self._rest.get_state(entity)).state)
        except Exception as exc:  # noqa: BLE001
            return f"read-back failed: {exc!r}"
        return None if abs(got - wanted) <= 0.5 else f"read back {got:g} A (wanted {wanted:g} A)"

    async def _read_back_option(self, entity: str, wanted: str) -> str | None:
        try:
            got = str((await self._rest.get_state(entity)).state)
        except Exception as exc:  # noqa: BLE001
            return f"read-back failed: {exc!r}"
        return None if got.lower() == wanted.lower() else f"read back {got!r} (wanted {wanted!r})"
