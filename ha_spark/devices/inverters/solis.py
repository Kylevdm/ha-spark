"""Solis inverter driver: native timed-slot charge control over a thin HA
``modbus:`` overlay (the ``solis_control`` hub stood up in #90), plus the
declarative power-switch reconcile (ADR-0003 rule 2, #140): ha-spark owns both
edges of the whole-inverter enable, driving it to ``Off`` while a dispatch hold
is active and ``On`` otherwise. That reconcile is ``reconcile_holds``, a seam of
its own rather than a step of ``apply`` (#143) — it answers to the clock, not to
a plan diff, so it runs every minute while ``apply`` stays half-hourly.

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

Slot windows carry a clock face and no date, so an export window is only
programmed once that clock face next comes round *at* its own event (#144).
Until then the event is deferred and re-offered each tick; without this, an
Axle event with a day's notice would discharge the battery a day early, outside
the paid window. The inverter fires that face on its *own* clock, so arming also
requires a fresh inverter-clock reading that agrees with the household clock
(#161).

The shared authority gate decides side effects for each ownership class:
``simulate`` logs intended writes, ``on`` permits real ``call_service``, and
refused authority emits a non-verified ``[REFUSED]`` line without writing.
Each write isolates its own failure and reads back to confirm the device took it.
The forced-charge program (window + current) additionally refuses when grid charging
is not permitted by the
inverter's work mode (bit 5); the live rate-tier throttle is not so gated. The
SoC fallback uses the same forced-charge sequence with an explicitly configured
current and the configured cheap-rate window, validated as one schedule before
every apply (#422): an above-limit current is blocked with the case named,
never silently clamped, and the schedule it applies is charge-only through
controls that read back.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple, TypeVar
from zoneinfo import ZoneInfo

import httpx

from ha_spark.authority import AuthorityStore, HoldDevice, HoldRecord
from ha_spark.devices.base import Capability, HoldContract, HoldValidation, fmt_hhmm
from ha_spark.devices.execution import (
    ExecutionContext,
    Ownership,
    device_action,
    execution_context,
)
from ha_spark.devices.inverters.solis_clock import (
    CLOCK_SYNC_REG,
    STALE_AFTER,
    clock_entity,
    clock_error,
    clock_refusal,
    clock_registers,
    describe_error,
)
from ha_spark.devices.outcomes import ActionLine, ActionOutcome, is_verified
from ha_spark.devices.registry import register
from ha_spark.energy.export_notifications import (
    ExportNotificationStore,
    make_notice,
    parse_event_id,
    send_once,
)
from ha_spark.energy.export_store import ExportEventStore
from ha_spark.energy.models import ChargeIntent, ExportIntent, window_hours
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.logging import get_logger

if TYPE_CHECKING:  # avoid an import cycle: config imports devices.base at runtime
    from ha_spark.config import DeviceConfig, Settings
    from ha_spark.ha.models import EntityState
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
_MAX_CHARGE_CURRENT_A = 62.5
# HA's update_entity service is asynchronous with respect to the Modbus
# overlay sensors. The ~5 s write-to-read-back latency first seen (#164) came
# from the RS485 gateway's storage mode, not the overlay's scan_interval; with
# storage off a landed write reads back in ~1.5 s median, <=7 s (#287). Observe
# for up to ~15 s after each write: it costs nothing once a write lands, it is
# the headroom the zero-confirm loop runs inside, and it is deliberately finite
# so a stale overlay can never hold the caller.
_READ_BACK_ATTEMPTS = 16
_READ_BACK_DELAY_SECONDS = 1.0
# The gap must outlast the ~4 s spurious 0.0 the overlay reports around program
# writes (#178); five reads assume _READ_BACK_DELAY_SECONDS stays at 1.0 s.
_ZERO_CONFIRM_GAP_READS = 5
# Work-mode bitfield: bit 5 (mask 32) == grid charging permitted. A forced grid
# charge is refused by firmware when this is unset, so assert it, never write it.
_GRID_CHARGE_BIT = 1 << 5
_EXPORT_CURRENT_A = 62.5
_EXPORT_CURRENT_RAW = 625
# A sync is confirmed only when the read-back is this close to the household
# clock: the write truncates to whole seconds and a poll adds a little latency.
_CLOCK_SYNC_READ_BACK = timedelta(seconds=10)
T = TypeVar("T")
ClockSyncOutcome = Literal["synced", "failed", "not_written"]


def _refused(mode: str) -> ActionLine:
    return ActionLine(f"[REFUSED] {mode.removeprefix('refuse:')}", ActionOutcome.REFUSED)


class _ApplyResult(NamedTuple):
    """Action lines and read-back verdicts from one forced-charge apply pass."""

    lines: list[ActionLine]
    current_ok: bool
    window_verified: bool
    cleanup_ok: bool


def _is_zero_target(target: float | list[int]) -> bool:
    if isinstance(target, list):
        return not any(target)
    return target == 0


def _discharge_half(
    export_window: tuple[datetime, datetime] | None, kept_discharge: list[int] | None
) -> list[int]:
    """Slot 1's discharge half: the export window, a kept one, or zeros."""
    if export_window is not None:
        start, end = export_window
        return [start.hour, start.minute, end.hour, end.minute]
    return kept_discharge or [0, 0, 0, 0]


@dataclass(frozen=True)
class FallbackSchedule:
    """A validated fallback charge schedule (#422): current and window, export off.

    Export/discharge is disabled by construction and the controls are the
    fixed read-back-capable set, so the schedule the device applies can only
    be a bounded charge in the configured cheap window.
    """

    current_a: int
    window_start: time
    window_end: time


# The fallback's own controls, each one written and read back through the
# overlay (43141 current, 43143 window block). A control outside this set may
# not carry a fallback: it has no verified read-back path here.
_FALLBACK_CONTROLS = frozenset({Capability.CHARGE_WINDOW, Capability.CHARGE_RATE})


def validate_fallback_schedule(
    settings: Settings,
    *,
    export: ExportIntent | None = None,
    controls: frozenset[Capability] = _FALLBACK_CONTROLS,
) -> FallbackSchedule | str:
    """Validate the fallback charge schedule; one named reason per invalid case.

    Pure: settings (plus the candidate's own export and controls, which the
    fallback's construction never varies) → either a valid schedule or the
    named reason it is invalid, so the caller blocks with the case named
    instead of clamping (#422). Re-run immediately before every apply against
    the settings in force then: a limit lowered below the fallback current
    blocks the next apply.
    """
    if export is not None:
        return "the fallback schedule must not program export or discharge"
    unsupported = sorted(str(control) for control in controls - _FALLBACK_CONTROLS)
    if unsupported:
        return f"fallback control {', '.join(unsupported)} has no read-back support"
    configured = settings.solis_fallback_current_a
    if configured is None:
        return "solis_fallback_current_a is not configured"
    if not math.isfinite(configured):
        return "solis_fallback_current_a is not a finite current"
    if configured < 1:
        return "solis_fallback_current_a must be a current of at least 1 A"
    if not math.isfinite(settings.max_charge_current_a) or settings.max_charge_current_a <= 0:
        return "max_charge_current_a is not a positive finite limit"
    if configured > settings.max_charge_current_a:
        return (
            f"solis_fallback_current_a ({configured:g} A) is above max_charge_current_a "
            f"({settings.max_charge_current_a:g} A)"
        )
    if configured > _MAX_CHARGE_CURRENT_A:
        return (
            f"solis_fallback_current_a ({configured:g} A) is above the inverter "
            f"register limit ({_MAX_CHARGE_CURRENT_A:g} A)"
        )
    if not math.isfinite(settings.battery_voltage_v) or settings.battery_voltage_v <= 0:
        return "battery_voltage_v is not a positive finite limit"
    if not math.isfinite(settings.supply_max_current_a) or settings.supply_max_current_a <= 0:
        return "supply_max_current_a is not a positive finite limit"
    if not math.isfinite(settings.supply_voltage_v) or settings.supply_voltage_v <= 0:
        return "supply_voltage_v is not a positive finite limit"
    charge_w = configured * settings.battery_voltage_v
    supply_w = settings.supply_max_current_a * settings.supply_voltage_v
    if charge_w > supply_w:
        return (
            f"fallback charge power ({charge_w:.0f} W) is above the site supply limit "
            f"({supply_w:.0f} W)"
        )
    window: dict[str, time] = {}
    for side, raw in (
        ("start", settings.charge_window_start),
        ("end", settings.charge_window_end),
    ):
        try:
            parsed = time.fromisoformat(raw)
        except ValueError:
            return f"charge_window_{side} '{raw}' is not a valid HH:MM time"
        # The household clock face the Slot 1 registers encode is naive whole
        # minutes: an offset-attached or sub-minute time is not a time on it.
        if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
            return f"charge_window_{side} '{raw}' is not a time on the household clock"
        window[side] = parsed
    if window["start"] == window["end"]:
        return "the fallback charge window is zero-length"
    return FallbackSchedule(
        current_a=math.floor(configured + 1e-9),
        window_start=window["start"],
        window_end=window["end"],
    )


def validated_fallback_current_a(settings: Settings) -> int | None:
    """The validated fallback current, or ``None`` while the schedule is invalid."""
    result = validate_fallback_schedule(settings)
    return result.current_a if isinstance(result, FallbackSchedule) else None


@register("solis")
class SolisDevice:
    """Solis: native timed-slot charge (modbus overlay) + power-switch hold."""

    capabilities = frozenset(
        {Capability.CHARGE_WINDOW, Capability.CHARGE_RATE, Capability.STOP_DISCHARGE}
    )
    # Transitional `Charger`-protocol compat (`chargers.charger_for`);
    # superseded by `Capability.CHARGE_RATE in capabilities` in Task 6.
    supports_live_rate = True

    def __init__(
        self, config: DeviceConfig, settings: Settings, rest: HomeAssistantRest,
        *, execution: ExecutionContext | None = None,
    ) -> None:
        self._execution = execution or execution_context()
        self._coordinator = self._execution.coordinator_for(config, settings)
        self._config = config
        self._settings = settings
        self._rest = rest
        self._hub = settings.solis_control_hub
        self._slave = settings.solis_modbus_slave

    async def _diagnostic_number(self, field: str) -> float | None:
        """Read an overlay value without refreshing, writing, or substituting config."""
        try:
            state = await self._rest.get_state(self._sensor(field))
            value = float(state.state)
            return value if math.isfinite(value) else None
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def hold_validation(self) -> HoldValidation:
        """Qualify this model, never the Solis driver family as a whole."""
        identity: dict[str, int | None] = {}
        for address, field in ((33000, "model_word"), (35000, "inverter_type_definition"),
                               (33289, "tou_version")):
            value = await self._diagnostic_number(field)
            identity[str(address)] = (
                int(value) if value is not None and value.is_integer() and 0 <= value <= 65535
                else None
            )
        words = ", ".join(
            f"{address}=0x{value:04X}" if value is not None else f"{address}=unknown"
            for address, value in identity.items()
        )
        source = "inverter read"
        reason = "validated reference identity"
        mismatch = any(identity[key] is not None and identity[key] != expected
                       for key, expected in (("33000", 0x0026), ("35000", 0x2031)))
        if mismatch:
            reason = f"not validated: Solis model differs from reference ({words})"
        elif identity["33289"] == 0xAA55:
            reason = f"not validated: V2 TOU is incompatible with V1 slots ({words})"
        elif identity["33000"] is None or identity["35000"] is None:
            source = "owner confirmation" if self._settings.solis_model_confirmed else "unreadable"
            reason = ("validated reference identity by owner confirmation" if
                      self._settings.solis_model_confirmed else
                      f"not validated: identity unreadable, no owner confirmation ({words})")
        contract = None
        readable = identity["33000"] is not None and identity["35000"] is not None
        if (not mismatch and identity["33289"] != 0xAA55
                and (readable or self._settings.solis_model_confirmed)):
            contract = HoldContract(
                hold="whole-inverter power switch Off; preserve resident schedule",
                release="power switch On only after readable pre-read; restore confirmed schedule",
                read_back="verify switch, slot 1 and charge current through HA",
                evidence_freshness=(
                    "fresh source reports required; cached state alone is insufficient"
                ),
            )
        return HoldValidation(identity, source, contract, reason)

    async def resident_charge_window(self) -> tuple[time, time] | None:
        """The resident slot-1 charge window read back; unknown ⇒ ``None``.

        The same overlay GETs ``resident_diagnostic`` makes (#418): no refresh,
        no write, and the configured window is never substituted for the
        resident one. ``None`` is "unknown", and unknown is not verified (#420).
        """
        parts: list[int] = []
        for index, field in enumerate(_WINDOW_FIELDS[:4]):
            value = await self._diagnostic_number(field)
            maximum = 23 if index % 2 == 0 else 59
            if value is None or not value.is_integer() or not 0 <= value <= maximum:
                return None
            parts.append(int(value))
        return (time(parts[0], parts[1]), time(parts[2], parts[3]))

    async def resident_diagnostic(self) -> dict[str, object]:
        """Read slot 1, 43141 and the configured switch; unknown never means configured."""
        window = await self.resident_charge_window()
        current = await self._diagnostic_number("timed_charge_current")
        switch = "unknown"
        entity = self._config.entities.get("power_switch", "")
        if entity:
            try:
                state = (await self._rest.get_state(entity)).state.strip().lower()
                if state in {"on", "off"}:
                    switch = state
            except httpx.HTTPError:
                pass
        return {
            "charge_window": (
                f"{fmt_hhmm(window[0])}–{fmt_hhmm(window[1])}" if window else "unknown"
            ),
            "charge_current_a": current if current is not None and current >= 0 else "unknown",
            "power_switch": switch,
        }

    # --- planning ---

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return solis_current_a(intent, self._settings) * self._settings.battery_voltage_v

    # --- apply: the forced-charge sequence ---

    @device_action(Ownership.CHARGING)
    async def apply_fallback(self, soc: SocMeasurement) -> list[ActionLine]:
        """Apply the validated cheap-window fallback using the normal Solis gates.

        The SoC reading is carried for timing and diagnostics only. The plan
        target, plan window, and any planned export cannot affect this request.
        The schedule is re-validated here, immediately before every apply,
        against the settings in force now (#422): an above-limit current is
        blocked with the case named, never clamped.
        """
        validated = validate_fallback_schedule(self._settings)
        if isinstance(validated, str):
            return [
                ActionLine(
                    f"[BLOCKED] Solis fallback invalid: {validated}", ActionOutcome.BLOCKED
                )
            ]
        intent = ChargeIntent(
            target_soc_pct=0.0,
            soc=soc,
            window_start=validated.window_start,
            window_end=validated.window_end,
        )
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return [_refused(mode)]
        description = (
            f"Solis fallback at {validated.current_a} A in "
            f"{fmt_hhmm(validated.window_start)}-{fmt_hhmm(validated.window_end)}"
        )
        if mode == "simulate":
            return [
                ActionLine(
                    f"[SIMULATE] {description} would be programmed (not written)",
                    ActionOutcome.SIMULATED,
                )
            ]
        if mode != "on":
            return [
                ActionLine(
                    f"[SKIP] {description} not written (mode {mode})", ActionOutcome.SIMULATED
                )
            ]

        result = await self._apply(intent, validated.current_a)
        if result.current_ok and result.window_verified and result.cleanup_ok:
            result.lines.append(
                ActionLine(f"[FALLBACK] {description} read-back verified", ActionOutcome.VERIFIED)
            )
        else:
            detail = next(
                (line for line in result.lines if not is_verified(line)),
                "real read-back verification was not completed",
            )
            result.lines.append(
                ActionLine(f"[FAILED] {description} unconfirmed: {detail}", ActionOutcome.FAILED)
            )
        return result.lines

    async def apply(self, intent: ChargeIntent) -> list[ActionLine]:
        return (await self._apply(intent, None)).lines

    @device_action(Ownership.CHARGING)
    async def _apply(self, intent: ChargeIntent, fallback_current_a: float | None) -> _ApplyResult:
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return _ApplyResult([_refused(mode)], False, False, False)
        # load_timezone, not ZoneInfo: a minimal container without tzdata (or a
        # typo'd option) must degrade to UTC, not raise before the reconcile and
        # the SoC guard have run and cost the daemon every device write.
        # Imported here for the same reason as the TYPE_CHECKING block above:
        # forecast imports config, which imports devices.base at runtime.
        from ha_spark.energy.forecast import load_timezone

        tz = load_timezone(self._settings.timezone)
        now = datetime.now(tz)
        # The power-switch reconcile is not here: it is `reconcile_holds`, its own
        # per-minute seam (#143). Every caller makes one pass before it applies,
        # so the switch has already settled by the time `_require_power_switch_on`
        # reads it — otherwise export would be refused on the very tick a hold
        # ends, against a state already being corrected.
        lines: list[ActionLine] = []
        # SoC-unreadable guard: soc_now==0 from a dead sensor would size a max charge.
        # Apply this safety decision in every mode; the write helpers below
        # suppress side effects while simulate reports the block as a preview.
        if not intent.soc.ok and fallback_current_a is None:
            prefix = "[SIMULATE] " if mode == "simulate" else ""
            line = ActionLine(
                f"{prefix}[BLOCKED] {intent.soc.reason}; not charging to "
                f"{intent.target_soc_pct:.0f}%",
                (ActionOutcome.SIMULATED if prefix else ActionOutcome.BLOCKED),
            )
            log.warning(line)
            lines.append(line)
            return _ApplyResult(lines, False, False, False)
        current_a = (
            solis_current_a(intent, self._settings)
            if fallback_current_a is None
            else fallback_current_a
        )
        raw_export = getattr(intent, "export", None)
        export_store = ExportEventStore(self._settings.db_path)
        previous_export_record = None
        is_fallback = fallback_current_a is not None
        if mode == "on" and raw_export is None and export_store.exists and not is_fallback:
            async with export_store:
                previous_export_record = await export_store.load()
        export_error: str | None = None
        preserve_resident_export = False
        if (
            mode in ("on", "simulate")
            and raw_export is None
            and not getattr(intent, "export_trusted", True)
            and await self._has_live_verified_export(now)
        ):
            prefix = "[SIMULATE] " if mode == "simulate" else ""
            line = ActionLine(
                f"{prefix}[SKIP] Axle event read untrusted; preserving the verified export window",
                (ActionOutcome.SIMULATED if prefix else ActionOutcome.UNCHANGED_VERIFIED),
            )
            log.warning(line)
            return _ApplyResult([line], False, False, False)
        export = _export_window(intent, tz)
        export_ready = export is not None
        # Slot 1's verified discharge half, kept when an untrusted hold read
        # refuses a new window (#143 §3).
        kept_discharge: list[int] | None = None
        if (
            is_fallback
            and mode in ("on", "simulate")
            and export_store.exists
            and await self._has_live_verified_export(now)
        ):
            try:
                resident = await self._read_slot_block(1)
            except Exception:  # noqa: BLE001 - never clobber an unreadable live export
                line = ActionLine(
                    "[BLOCKED] Solis fallback held: live verified export slot 1 is unreadable",
                    ActionOutcome.BLOCKED,
                )
                log.info(line)
                return _ApplyResult([line], False, False, False)
            if any(resident[4:]):
                kept_discharge = resident[4:]
        if raw_export is not None and export is None:
            export_ready = False
            export_error = "malformed export window"
            lines.append(
                ActionLine(f"[BLOCKED] export refused: {export_error}", ActionOutcome.BLOCKED)
            )
        elif export_ready:
            assert export is not None
            export_error = _validate_export(intent, export)
            if export_error is None:
                export_error = _export_not_yet_armed(export, now)
                if export_error is not None and mode in ("on", "simulate"):
                    preserve_resident_export = await self._has_matching_live_verified_export(
                        raw_export, export, now
                    )
            if export_error is None and intent.hold_overlaps(*export):
                # Hold beats export on overlap: the reconcile will hold the
                # inverter `Off` inside the event, so refuse the whole window
                # rather than program a discharge that is cut mid-slot. Tested
                # against the window, not the clock: a morning dispatch must not
                # refuse an evening event, and an evening dispatch must refuse it
                # even when the plan is computed hours earlier.
                export_error = "a dispatch hold overlaps the export window"
            if export_error is None and not intent.hold_trusted:
                export_error, kept_discharge = await self._untrusted_holds_export(
                    export, mode, now
                )
            # After the hold checks, so a terminal refusal is never reported
            # as the retryable clock hold. A read, so `simulate` takes it too.
            if export_error is None and mode in ("on", "simulate"):
                export_error = await self._clock_refusal(raw_export, export, now, tz)
            if mode in ("on", "simulate") and export_error is None:
                export_error = await self._require_power_switch_on()
            if export_error is not None:
                export_ready = False
                prefix = "[SIMULATE] " if mode == "simulate" else ""
                lines.append(
                    ActionLine(
                        f"{prefix}[BLOCKED] export refused: {export_error}",
                        (ActionOutcome.SIMULATED if prefix else ActionOutcome.BLOCKED),
                    )
                )
            else:
                discharge_ok, discharge_line = await self._write_discharge_current_result()
                lines.append(discharge_line)
                export_ready = discharge_ok
                if not discharge_ok:
                    export_error = "discharge current was not confirmed"
                    lines.append(
                        ActionLine(
                            "[BLOCKED] export refused: discharge current was not confirmed",
                            ActionOutcome.BLOCKED,
                        )
                    )
        # Grid-charge gate for the whole forced-charge program: read the work
        # mode once. A real write is refused when bit 5 is unset (firmware would
        # ignore the force anyway), and simulate must preview the same decision.
        blocked = (
            await self._assert_grid_charge_allowed()
            if mode in ("on", "simulate")
            else None
        )
        # Current is the safety prerequisite for slot 1. If an existing window
        # is active at another current, deactivate it and confirm zero before
        # attempting the current transition. This leaves a failed transition
        # safe at zero rather than continuing an old, higher-rate window.
        deactivation_line: ActionLine | None = None
        preserve_prefix = "[SIMULATE] " if mode == "simulate" else ""
        preserve_line = ActionLine(
            f"{preserve_prefix}[SKIP] preserving the verified export window "
            "while it is not yet armed",
            (ActionOutcome.SIMULATED if preserve_prefix else ActionOutcome.UNCHANGED_VERIFIED),
        )
        if preserve_resident_export:
            current_ok = True
            current_line = preserve_line
        elif mode == "on" and blocked is None:
            current_ok, current_line, deactivation_line = await self._prepare_slot_one_current(
                intent,
                _discharge_half(export if export_ready else None, kept_discharge),
                current_a=current_a,
                fallback=is_fallback,
            )
        else:
            current_ok, current_line = await self._write_charge_current_result(
                intent, blocked, current_a=current_a, fallback=is_fallback
            )
        if deactivation_line is not None:
            lines.append(deactivation_line)
        lines.append(current_line)
        window_block = None if current_ok else (blocked or "planned current was not confirmed")
        if preserve_resident_export:
            window_line = preserve_line
        else:
            window_line = await self._write_charge_window(
                intent,
                window_block,
                export_window=export if export_ready else None,
                kept_discharge=kept_discharge,
                charge_current_a=current_a,
            )
        lines.append(window_line)
        window_verified = is_verified(window_line)
        if mode == "on" and raw_export is not None and export_ready and not window_verified:
            export_ready = False
            export_error = "timed export window was not confirmed"
        # Zero-guard the slots the planner does not drive so a stale manual
        # window (charge 2/3, any discharge) can't actuate behind the plan.
        cleanup_ok = True
        for slot in (2, 3):
            slot_ok, slot_line = await self._zero_guard_slot(slot)
            lines.append(slot_line)
            cleanup_ok = cleanup_ok and slot_ok
        if mode in ("on", "simulate") and not is_fallback:
            if raw_export is not None or export_store.exists:
                await self._persist_export_state(raw_export, export, export_ready, window_line)
            await self._notify_export_lifecycle(
                raw_export,
                export,
                export_ready,
                export_error,
                window_line,
                now,
                previous_export_record,
                mode,
            )
        return _ApplyResult(lines, current_ok, window_verified, cleanup_ok)

    async def set_charge_rate(self, watts: float) -> ActionLine:
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

    @device_action(Ownership.CHARGING)
    async def _write_charge_window(
        self,
        intent: ChargeIntent,
        blocked: str | None,
        *,
        export_window: tuple[datetime, datetime] | None = None,
        kept_discharge: list[int] | None = None,
        charge_current_a: float | None = None,
    ) -> ActionLine:
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
        elif kept_discharge is not None:
            desc += " and keep the verified export window"
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return _refused(mode)
        keeps_discharge = export_window is not None or kept_discharge is not None
        current_a = (
            solis_current_a(intent, self._settings)
            if charge_current_a is None
            else charge_current_a
        )
        zero_charge = keeps_discharge and current_a <= 0
        if mode == "simulate":
            if blocked:
                if not keeps_discharge:
                    line = ActionLine(
                        f"[SIMULATE] [BLOCKED] {desc}: {blocked}", ActionOutcome.SIMULATED
                    )
                    log.warning(line)
                    return line
                if not zero_charge:
                    try:
                        await self._read_slot_block(1)
                    except Exception:  # noqa: BLE001 - match the live fail-closed check
                        line = ActionLine(
                            f"[SIMULATE] [BLOCKED] {desc}: slot 1 state unreadable",
                            ActionOutcome.SIMULATED,
                        )
                        log.warning(line)
                        return line
                charge_action = (
                    "clear the charge schedule"
                    if zero_charge
                    else "preserve the resident charge schedule"
                )
                if export_window is not None:
                    export_start, export_end = export_window
                    export_action = (
                        f"set export {export_start:%H:%M}-{export_end:%H:%M}"
                        f" at {_EXPORT_CURRENT_A:g} A"
                    )
                else:
                    export_action = "keep the verified export window"
                line = ActionLine(
                    f"[SIMULATE] would {charge_action} and {export_action}", ActionOutcome.SIMULATED
                )
                log.info(line)
                return line
            log.info("[SIMULATE] would %s", desc)
            return ActionLine(f"[SIMULATE] would {desc}", ActionOutcome.SIMULATED)
        if mode in ("off", "observe"):
            return ActionLine(f"[{mode.upper()}] computed: {desc}", ActionOutcome.SIMULATED)
        if blocked and not keeps_discharge:
            # Grid-charge permission is not a prerequisite for clearing a
            # resident discharge schedule. Inspect the block first so an
            # ordinary blocked charge request still reports BLOCKED when no
            # export window is resident. A failed inspection fails closed.
            try:
                existing = await self._read_slot_block(1)
            except Exception:
                return ActionLine(f"[BLOCKED] {desc}: {blocked}", ActionOutcome.BLOCKED)
            if any(existing[4:]):
                return await self._clear_discharge_window(desc)
            return ActionLine(f"[BLOCKED] {desc}: {blocked}", ActionOutcome.BLOCKED)
        if blocked and keeps_discharge and not zero_charge:
            # An export write must not be gated by the charge-only work-mode
            # bit. Preserve the resident charge half and replace the discharge
            # half in one atomic Slot 1 block.
            try:
                existing = await self._read_slot_block(1)
            except Exception:  # noqa: BLE001 - fail closed
                return ActionLine(f"[FAILED] {desc}: slot 1 state unreadable", ActionOutcome.FAILED)
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
        discharge_values = _discharge_half(export_window, kept_discharge)
        want_block = [*charge_values, *discharge_values]
        try:
            wrote, zero_confirmed = await self._apply_slot_block(1, want_block)
            mismatch = (
                None
                if zero_confirmed
                else await self._verify_slot_block(1, want_block, refresh=wrote)
            )
        except Exception:  # noqa: BLE001 - isolate per action
            log.error("[FAILED] %s", desc)
            return ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED)
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
        return (
            ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)
            if wrote
            else ActionLine(f"[SKIP] {desc} (already set)", ActionOutcome.UNCHANGED_VERIFIED)
        )

    @device_action(Ownership.CHARGING)
    async def _clear_discharge_window(self, desc: str) -> ActionLine:
        """Clear only Slot 1's discharge half, preserving any charge schedule."""
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return _refused(mode)
        safe_desc = desc.replace("set charge window", "set timed charge schedule")
        clear_desc = f"clear timed export window ({safe_desc})"
        if mode == "simulate":
            return ActionLine(f"[SIMULATE] would {clear_desc}", ActionOutcome.SIMULATED)
        if mode in ("off", "observe"):
            return ActionLine(f"[{mode.upper()}] computed: {clear_desc}", ActionOutcome.SIMULATED)
        try:
            existing = await self._read_slot_block(1)
            want = existing[:4] + [0, 0, 0, 0]
            wrote, zero_confirmed = await self._apply_slot_block(1, want)
            mismatch = (
                None
                if zero_confirmed
                else await self._verify_slot_block(1, want, refresh=wrote)
            )
        except Exception:  # noqa: BLE001 - cleanup is failure-isolated
            log.error("[FAILED] %s", clear_desc)
            return ActionLine(f"[FAILED] {clear_desc}", ActionOutcome.FAILED)
        if mismatch:
            return ActionLine(f"[WARNING] {clear_desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
        return (
            ActionLine(f"[APPLIED] {clear_desc}", ActionOutcome.VERIFIED)
            if wrote
            else ActionLine(
                f"[SKIP] {clear_desc} (already clear)", ActionOutcome.UNCHANGED_VERIFIED
            )
        )

    @device_action(Ownership.CHARGING)
    async def _write_charge_current_result(
        self,
        intent: ChargeIntent,
        blocked: str | None,
        *,
        current_a: float | None = None,
        fallback: bool = False,
    ) -> tuple[bool, ActionLine]:
        """Write and freshly verify the planned current, returning success separately."""
        # solis_current_a already clamps to max_charge_current_a (<= the 62.5 A
        # DC hardware ceiling); round to the integer register value.
        amps = round(
            solis_current_a(intent, self._settings) if current_a is None else current_a
        )
        desc = _charge_current_description(
            intent, self._settings, amps, fallback=fallback
        )
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return False, _refused(mode)
        if blocked and mode in ("on", "simulate"):
            prefix = "[SIMULATE] " if mode == "simulate" else ""
            line = ActionLine(
                f"{prefix}[BLOCKED] {desc}: {blocked}",
                (ActionOutcome.SIMULATED if prefix else ActionOutcome.BLOCKED),
            )
            log.warning(line)
            return False, line
        return await self._set_current_result(amps, desc)

    @device_action(Ownership.CHARGING)
    async def _prepare_slot_one_current(
        self,
        intent: ChargeIntent,
        planned_discharge: list[int],
        *,
        current_a: float | None = None,
        fallback: bool = False,
    ) -> tuple[bool, ActionLine, ActionLine | None]:
        """Make slot 1 safe before changing its planned charge current.

        ``planned_discharge`` is the discharge half this apply will program."""
        amps = round(
            solis_current_a(intent, self._settings) if current_a is None else current_a
        )
        current_desc = _charge_current_description(
            intent, self._settings, amps, fallback=fallback
        )
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return False, _refused(mode), None
        zeros = [0] * len(_WINDOW_FIELDS)
        try:
            resident = await self._read_slot_block(1)
            active = resident != zeros
            if not active:
                zero_confirmed = await self._confirm_zero_target(
                    zeros, lambda: self._verify_slot_block(1, zeros)
                )
                active = zero_confirmed is not True
        except Exception:  # noqa: BLE001 - do not guess at an active window
            line = ActionLine(
                f"[FAILED] {current_desc}: slot 1 state unreadable", ActionOutcome.FAILED
            )
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
            # Only the charge half needs deactivating (#218): the planned export
            # window is kept so a mid-event replan doesn't interrupt it. Kept only
            # on an exact match, so a misread sensor (#178) is never written back.
            keep_discharge = None
            desc = "deactivate timed slot 1 window"
            if any(planned_discharge) and resident[4:] == planned_discharge:
                keep_discharge = planned_discharge
                desc = "deactivate timed slot 1 charge window, keeping its export window"
            deactivated, deactivation_line = await self._zero_slot(
                1, desc, keep_discharge=keep_discharge
            )
            if not deactivated:
                line = ActionLine(
                    f"[BLOCKED] {current_desc}: slot 1 was not safely deactivated",
                    ActionOutcome.BLOCKED,
                )
                log.warning(line)
                return False, line, deactivation_line
            ok, line = await self._set_current_result(amps, current_desc)
            return ok, line, deactivation_line

        ok, line = await self._set_current_result(amps, current_desc)
        return ok, line, None

    async def _zero_guard_slot(self, slot: int) -> tuple[bool, ActionLine]:
        """Zero a non-driven slot's window block, but only if it is non-zero
        (register endurance: steady state costs zero writes)."""
        desc = f"zero timed slot {slot} window"
        return await self._zero_slot(slot, desc)

    @device_action(Ownership.CHARGING)
    async def _zero_slot(
        self, slot: int, desc: str, *, keep_discharge: list[int] | None = None
    ) -> tuple[bool, ActionLine]:
        """Zero a slot and confirm it, returning whether zero was confirmed.

        ``keep_discharge`` zeroes only the charge half, writing these four
        registers back as the discharge half in the same atomic block."""
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return False, _refused(mode)
        if mode == "simulate":
            return True, ActionLine(
                f"[SIMULATE] would {desc} (if non-zero)", ActionOutcome.SIMULATED
            )
        if mode in ("off", "observe"):
            return True, ActionLine(
                f"[{mode.upper()}] computed: {desc} (if non-zero)", ActionOutcome.SIMULATED
            )
        want = [0] * 4 + (keep_discharge or [0] * 4)
        try:
            current = await self._read_slot_block(slot)
            if current == want:
                zero_confirmed = await self._confirm_zero_target(
                    want, lambda: self._verify_slot_block(slot, want)
                )
                if zero_confirmed is not False:
                    half = " charge half" if keep_discharge else ""
                    return True, ActionLine(
                        f"[SKIP] slot {slot}{half} already zeroed", ActionOutcome.UNCHANGED_VERIFIED
                    )
            await self._write_register(_SLOT_BLOCK_REG[slot], want)
            mismatch = await self._verify_slot_block(slot, want, refresh=True)
        except Exception:  # noqa: BLE001
            log.error("[FAILED] %s", desc)
            return False, ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED)
        if mismatch:
            return False, ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
        return True, ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)

    async def _set_current(self, amps: float, desc: str) -> ActionLine:
        """Live charge-rate write (rate tier / supply guard). Single register,
        no commit block needed."""
        _ok, line = await self._set_current_result(amps, desc)
        return line

    @device_action(Ownership.CHARGING)
    async def _set_current_result(self, amps: float, desc: str) -> tuple[bool, ActionLine]:
        """Set current and return whether its post-write value was confirmed."""
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return False, _refused(mode)
        if mode == "simulate":
            log.info("[SIMULATE] would %s", desc)
            return True, ActionLine(f"[SIMULATE] would {desc}", ActionOutcome.SIMULATED)
        if mode in ("off", "observe"):
            return True, ActionLine(f"[{mode.upper()}] computed: {desc}", ActionOutcome.SIMULATED)
        try:
            wrote, zero_confirmed = await self._apply_current(
                round(amps * _CURRENT_SCALE), amps
            )
            mismatch = (
                None if zero_confirmed else await self._verify_current(amps, refresh=wrote)
            )
        except Exception:  # noqa: BLE001 - isolate per write
            log.error("[FAILED] %s", desc)
            return False, ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED)
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return False, ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
        return True, ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)

    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[ActionLine]:
        """Converge the whole-inverter enable on the state the clock implies (#140).

        Desired state includes durable owned holds as well as the fresh intent.
        Persist a request before writing Off, and confirmation only after read-back.
        An already-Off switch without a record belongs to the owner: this seam
        neither claims it nor turns it On. Unconfirmed requests are re-read, not
        treated as proof that a write landed.

        Read-first, and deliberately cheap enough for the per-minute cadence it
        is driven at (#143): steady state is one *cached* ``GET`` of the select
        and zero register writes. The read hits HA's state machine, not modbus,
        and the read-back does not call ``homeassistant.update_entity``, so
        nothing here forces an inverter poll. The register is written only at a
        genuine hold edge, to correct an external change, or to retry a failure.

        It is exempt from the SoC guard in ``apply``: it commands no SoC-derived
        magnitude, and freezing the inverter ``Off`` on an unrelated dead sensor
        is exactly the failure it exists to remove.

        It must never read export state. If the switch is ``On`` during a paid
        export that is because no hold is active, not because export asked —
        ``_require_power_switch_on`` stays a read-only refusal.
        """
        return [await self._reconcile_power_switch(intent, now)]

    @device_action(Ownership.PROTECTION)
    async def _reconcile_power_switch(self, intent: ChargeIntent, now: datetime) -> ActionLine:
        store = self._execution.authority
        identity = HoldDevice(
            driver=self._config.driver,
            power_switch=self._config.entities.get("power_switch", ""),
            hub=self._settings.solis_control_hub, slave=self._settings.solis_modbus_slave,
        )
        records = tuple(h for h in store.state.holds if h.device == identity) if (
            store is not None and store.state is not None
        ) else ()
        retained = any(
            h.cause == "ev" and h.confirmed and not h.pending_release
            or h.cause == "dispatch" and h.trusted_end is not None and now < h.trusted_end
            and h.confirmed and not intent.hold_trusted
            for h in records
        )
        wanted = "Off" if intent.hold_active(now) or retained else "On"
        desc = f"set inverter power switch to {wanted}"
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return _refused(mode)
        if mode == "simulate":
            return ActionLine(f"[SIMULATE] would {desc}", ActionOutcome.SIMULATED)
        if mode in ("off", "observe"):
            return ActionLine(f"[{mode.upper()}] computed: {desc}", ActionOutcome.SIMULATED)
        entity = self._config.entities.get("power_switch", "")
        if not entity:
            return ActionLine(
                "[SKIP] no power_switch entity configured; discharge left as-is",
                ActionOutcome.UNVERIFIED,
            )
        try:
            # The pre-read suppresses a redundant write, and on a failure it
            # decides asymmetrically (#143): unreadable evidence may *close* a
            # hold, never open one. Falling through to `Off` keeps a flaky GET
            # on the tick a dispatch opens from dropping the hold; falling
            # through to `On` at the per-minute cadence would instead be up to
            # 60 blind, unverifiable releases an hour through an HA outage, each
            # one freeing a battery ha-spark can no longer see to discharge.
            try:
                current = (await self._rest.get_state(entity)).state.strip().lower()
                if current == wanted.lower():
                    if wanted == "Off" and records and store is not None:
                        # Confirm our existing requests by this read; never open
                        # a new cause on a switch someone else already turned Off.
                        # A confirmed record means the Off is ours, so a cause
                        # that starts under it (EV during a dispatch, or the
                        # reverse) is recorded and survives the other's release.
                        self._save_hold_request(
                            store, identity, intent, now, records, confirmed=True,
                            create=any(h.confirmed for h in records),
                        )
                    elif (
                        wanted == "On" and records and store is not None
                        and store.state is not None
                    ):
                        # Nothing retains these holds and the switch already reads
                        # On: they no longer hold anything, so drop them (no write).
                        store.save_holds(
                            tuple(h for h in store.state.holds if h.device != identity)
                        )
                    return ActionLine(
                        f"[SKIP] {desc} (already set)", ActionOutcome.UNCHANGED_VERIFIED
                    )
            except Exception:  # noqa: BLE001 - direction decides
                if wanted == "On":
                    # Not a benign skip: while the reads stay broken the inverter
                    # is left disabled, the house entirely on grid import. This
                    # direction is still the safe one, but it is unbounded here
                    # on purpose — the bound is the relinquish path (#143 §5),
                    # which writes the safe state once ha-spark is past every
                    # known hold end and still has no picture.
                    log.warning("Power-switch unreadable; not releasing a hold")
                    return ActionLine(
                        f"[WARNING] {desc} refused: state unreadable; "
                        "a hold is never released blind",
                        ActionOutcome.REFUSED,
                    )
                log.warning("Power-switch pre-read failed; writing %s anyway", wanted)
            if wanted == "On" and not any(h.owned and h.confirmed for h in records):
                # An unconfirmed request is not proof ha-spark turned it Off: its
                # write may never have landed and the owner switched Off since.
                return ActionLine(
                    "[SKIP] owner-Off switch has no confirmed owned hold; left as-is",
                    ActionOutcome.UNVERIFIED,
                )
            assert store is not None and store.state is not None
            if wanted == "Off":
                self._save_hold_request(store, identity, intent, now, records, confirmed=False)
            else:
                store.save_holds(tuple(
                    h.model_copy(update={"pending_release": True}) if h.device == identity else h
                    for h in store.state.holds
                ))
            mismatch = await self._select_option(entity, wanted)
            if not mismatch and store is not None and store.state is not None:
                if wanted == "Off":
                    self._save_hold_request(
                        store, identity, intent, now, store.state.holds, confirmed=True
                    )
                else:
                    store.save_holds(tuple(h for h in store.state.holds if h.device != identity))
        except Exception:  # noqa: BLE001 - isolate this action's failure
            log.error("[FAILED] %s", desc)
            return ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED)
        return (
            ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
            if mismatch
            else ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)
        )

    def _save_hold_request(
        self, store: AuthorityStore, identity: HoldDevice, intent: ChargeIntent,
        now: datetime, records: tuple[HoldRecord, ...], *, confirmed: bool,
        create: bool = True,
    ) -> None:
        assert store.state is not None
        existing = {h.cause: h for h in records if h.device == identity}
        if create and intent.ev_hold_active and "ev" not in existing:
            existing["ev"] = HoldRecord(cause="ev", started_at=now, device=identity)
        ends = [end if end.tzinfo else end.replace(tzinfo=now.tzinfo)
                for start, end in intent.holds
                if (start if start.tzinfo else start.replace(tzinfo=now.tzinfo)) <= now
                < (end if end.tzinfo else end.replace(tzinfo=now.tzinfo))]
        if ends and intent.hold_trusted and (create or "dispatch" in existing):
            previous = existing.get("dispatch")
            existing["dispatch"] = (
                previous.model_copy(update={"trusted_end": max(ends)}) if previous else
                HoldRecord(cause="dispatch", started_at=now, device=identity, trusted_end=max(ends))
            )
        store.save_holds(
            tuple(h for h in store.state.holds if h.device != identity)
            + tuple(h.model_copy(update={"confirmed": confirmed}) for h in existing.values())
        )

    async def write_safe_state(self) -> list[ActionLine]:
        """Leave the inverter self-managing when ha-spark stops steering (#143 §5).

        Inverter ``On``, Slot 1 charging across the configured cheap window, and
        a discharge window that is zeroed unless it is a verified export still
        running. The scheduler decides *when* (past every known hold end with no
        trusted picture, or a clean shutdown) and fires it once; this only writes.
        It is the designated bound on the reconcile's refused blind release, so an
        unreadable switch is written ``On`` here rather than refused. Retries are
        not this method's: they belong to the read-first reconcile and ``apply``,
        which never write blind, so broken reads cannot cost the inverter a
        register write a minute. Each action is gated and isolated like every
        other write; it never touches the charge or discharge current.
        """
        return [await self._write_safe_power_switch(), await self._write_safe_slot_block()]

    @device_action(Ownership.PROTECTION)
    async def _write_safe_power_switch(self) -> ActionLine:
        desc = "set inverter power switch to On (relinquishing control)"
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return _refused(mode)
        if mode == "simulate":
            return ActionLine(f"[SIMULATE] would {desc}", ActionOutcome.SIMULATED)
        if mode in ("off", "observe"):
            return ActionLine(f"[{mode.upper()}] computed: {desc}", ActionOutcome.SIMULATED)
        entity = self._config.entities.get("power_switch", "")
        if not entity:
            return ActionLine(
                "[SKIP] no power_switch entity configured; discharge left as-is",
                ActionOutcome.UNVERIFIED,
            )
        try:
            try:
                if (await self._rest.get_state(entity)).state.strip().lower() == "on":
                    return ActionLine(
                        f"[SKIP] {desc} (already set)", ActionOutcome.UNCHANGED_VERIFIED
                    )
            except Exception:  # noqa: BLE001 - unreadable is written, not refused
                log.warning("Power-switch pre-read failed; writing On anyway")
            mismatch = await self._select_option(entity, "On")
        except Exception:  # noqa: BLE001 - isolate this action's failure
            log.error("[FAILED] %s", desc)
            return ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED)
        return (
            ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
            if mismatch
            else ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)
        )

    @device_action(Ownership.CHARGING)
    async def _write_safe_slot_block(self) -> ActionLine:
        # Imported here for the same import-cycle reason as in `apply`.
        from ha_spark.energy.sources import parse_time

        try:
            start = parse_time(self._settings.charge_window_start)
            end = parse_time(self._settings.charge_window_end)
        except ValueError:
            line = ActionLine(
                "[FAILED] set charge window (relinquishing control)", ActionOutcome.FAILED
            )
            log.error(line)
            return line
        charge = [start.hour, start.minute, end.hour, end.minute]
        desc = f"set charge window {fmt_hhmm(start)}-{fmt_hhmm(end)} (relinquishing control)"
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return _refused(mode)
        if mode == "simulate":
            return ActionLine(f"[SIMULATE] would {desc}", ActionOutcome.SIMULATED)
        if mode in ("off", "observe"):
            return ActionLine(f"[{mode.upper()}] computed: {desc}", ActionOutcome.SIMULATED)
        try:
            async with ExportEventStore(self._settings.db_path) as store:
                record = await store.load()
            export_live = record is not None and record[1] > datetime.now(UTC)
            try:
                resident: list[int] | None = await self._read_slot_block(1)
            except Exception:  # noqa: BLE001 - decided below
                log.warning("Slot 1 pre-read failed")
                resident = None
            if resident is None:
                if export_live:
                    # A blind block write cannot carry a window it cannot see:
                    # it would zero a paid export mid-event.
                    line = ActionLine(
                        f"[BLOCKED] {desc}: slot 1 unreadable while a verified export is live",
                        ActionOutcome.BLOCKED,
                    )
                    log.warning(line)
                    return line
                discharge = [0, 0, 0, 0]
            else:
                # Same keep rule as #143 §3: only a window ha-spark verified and
                # recorded, whose end is still ahead. Anything else is zeroed.
                kept = resident[4:]
                discharge = kept if export_live and any(kept) else [0, 0, 0, 0]
            want = [*charge, *discharge]
            if resident == want:
                zero_confirmed = await self._confirm_zero_target(
                    want, lambda: self._verify_slot_block(1, want)
                )
                if zero_confirmed is not False:
                    return ActionLine(
                        f"[SKIP] {desc} (already set)", ActionOutcome.UNCHANGED_VERIFIED
                    )
            await self._write_register(_SLOT_BLOCK_REG[1], want)
            mismatch = await self._verify_slot_block(1, want, refresh=True)
        except Exception:  # noqa: BLE001 - isolate this action's failure
            log.error("[FAILED] %s", desc)
            return ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED)
        return (
            ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
            if mismatch
            else ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)
        )

    async def _select_option(self, entity: str, option: str) -> str | None:
        """Write a select option and confirm it; returns the read-back mismatch."""
        self._coordinator.confirm_write()
        await self._rest.call_service(
            "select", "select_option", {"entity_id": entity, "option": option}
        )
        return await self._read_back_option(entity, option)

    @device_action(Ownership.CHARGING)
    async def _write_discharge_current_result(self) -> tuple[bool, ActionLine]:
        """Set and verify the fixed full-rate command used for paid export."""
        desc = f"set timed discharge current to {_EXPORT_CURRENT_A:g} A for export"
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return False, _refused(mode)
        if mode == "simulate":
            return True, ActionLine(f"[SIMULATE] would {desc}", ActionOutcome.SIMULATED)
        if mode in ("off", "observe"):
            return True, ActionLine(f"[{mode.upper()}] computed: {desc}", ActionOutcome.SIMULATED)
        try:
            wrote = await self._apply_discharge_current(_EXPORT_CURRENT_RAW)
            mismatch = await self._verify_discharge_current(
                _EXPORT_CURRENT_A, refresh=wrote
            )
        except Exception:  # noqa: BLE001 - isolate export action
            log.error("[FAILED] %s", desc)
            return False, ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED)
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            return False, ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED)
        line = (
            ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)
            if wrote
            else ActionLine(f"[SKIP] {desc} (already set)", ActionOutcome.UNCHANGED_VERIFIED)
        )
        return True, line

    async def _untrusted_holds_export(
        self, export: tuple[datetime, datetime], mode: str, now: datetime
    ) -> tuple[str | None, list[int] | None]:
        """Refuse a *new* export window while hold data is untrusted (#143 §3).

        Returns the refusal (``None`` to proceed) and the Slot 1 discharge half
        to keep in place of the refused window.

        A failed dispatch read leaves the holds empty, so ``hold_overlaps``
        would admit a window across a dispatch ha-spark can no longer see, and
        the reconcile would then drive the switch ``Off`` mid-event and kill the
        export half-delivered. Same class of refusal as #132's underfunded
        event, and the next tick retries.

        An already-programmed window is left alone. When it is this very window,
        re-applying it is a write-if-changed no-op. When it differs — a trusted
        plan trimmed the event around a dispatch, and with the holds gone the
        planner now offers the whole event — refusing must not zero it, or the
        paid window is cleared on every tick the read stays down. Only a window
        ha-spark verified and recorded, and whose end is still ahead, is kept:
        anything else resident stays subject to the usual guard. It is captured
        here, before the charge-current step can deactivate Slot 1. Simulate
        reads the same resident state as ``on`` but suppresses its writes.
        """
        refusal = "hold data untrusted; not programming a new export window"
        if mode not in ("on", "simulate"):
            return refusal, None
        try:
            resident = (await self._read_slot_block(1))[4:]
        except Exception:  # noqa: BLE001 - fail closed
            return refusal, None
        start, end = export
        if resident == [start.hour, start.minute, end.hour, end.minute]:
            return None, None
        if any(resident):
            async with ExportEventStore(self._settings.db_path) as store:
                record = await store.load()
            if record is not None and record[1] > now.astimezone(UTC):
                return f"{refusal}; keeping the verified window already in Slot 1", resident
        return refusal, None

    async def _clock_refusal(
        self,
        raw_export: object,
        export: tuple[datetime, datetime],
        now: datetime,
        tz: ZoneInfo,
    ) -> str | None:
        """Refuse to *arm* an export window on a clock ha-spark cannot vouch for (#161).

        Gates arming only. A window already verified and recorded passed this
        check when it was armed, and a clock cannot drift minutes within one
        event, so a stale or unreadable blip mid-event must not clear a paid
        export (the same reasoning as #148). The runbook's abort covers the rest.
        Judged on the durable record alone, not a Slot 1 read: that read shares
        the hub with the clock sensor, so both can fail on the same blip.
        """
        start, end = export
        store = ExportEventStore(self._settings.db_path)
        if store.exists:
            async with store:
                record = await store.load()
            if (
                record is not None
                and record[0] == _export_identity(raw_export, start, end)
                and record[1] > now.astimezone(UTC)
            ):
                return None
        try:
            state = await self._rest.get_state(clock_entity(self._hub))
        except Exception:  # noqa: BLE001 - unreadable is refused below
            state = None
        tolerance = timedelta(minutes=self._settings.inverter_clock_tolerance_minutes)
        return clock_refusal(state, now, tz, tolerance)

    @device_action(Ownership.CLOCK)
    async def sync_clock(self) -> tuple[ClockSyncOutcome, list[ActionLine]]:
        """Set the inverter clock from the household clock and confirm it (#161).

        Run by the operator (``python -m ha_spark solis sync-clock``) and, when
        ``inverter_clock_dst_sync`` is on, by the daemon at a daylight-saving
        change; never to correct drift. Writes the six-register block at 43000
        whatever the error beforehand, then refreshes the clock sensor and reads
        it back. Returns ``synced``, ``failed``, or ``not_written`` (outside
        ``on``: this is the one place the write is gated) with lines reporting
        the error before and after. ``not_written`` is not a success, so the
        runbook step cannot pass by accident in ``simulate``.
        """
        from ha_spark.energy.forecast import load_timezone  # import cycle, see apply

        tz = load_timezone(self._settings.timezone)
        desc = f"sync inverter clock to {self._settings.timezone}"
        entity = clock_entity(self._hub)
        try:
            before = clock_error(await self._rest.get_state(entity), tz)
        except Exception:  # noqa: BLE001 - reported, and synced regardless
            before = None
        zone = self._settings.timezone
        before_text = "unreadable" if before is None else f"{describe_error(before)} {zone}"
        lines = [ActionLine(f"inverter clock before: {before_text}", ActionOutcome.UNVERIFIED)]
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return "not_written", [_refused(mode)]
        if mode != "on":
            lines.append(
                ActionLine(
                    f"[{mode.upper()}] would {desc}; not written (mode is {mode})",
                    ActionOutcome.SIMULATED,
                )
            )
            return "not_written", lines

        after: list[timedelta] = []  # the confirming reading, kept for the report

        def confirmed(state: EntityState) -> bool:
            error = clock_error(state, tz)
            ok = (
                error is not None
                and state.last_updated is not None
                and datetime.now(UTC) - state.last_updated <= STALE_AFTER
                and abs(error) <= _CLOCK_SYNC_READ_BACK
            )
            if ok and error is not None:
                after.append(error)
            return ok

        def describe(state: EntityState) -> str:
            error = clock_error(state, tz)
            return "read back unreadable" if error is None else f"read back {describe_error(error)}"

        try:
            await self._write_register(CLOCK_SYNC_REG, clock_registers(datetime.now(UTC), tz))
            mismatch = await self._verify_read_back(
                lambda: self._rest.get_state(entity),
                confirmed,
                describe,
                refresh_entities=(entity,),
            )
        except Exception:  # noqa: BLE001 - isolate this action's failure
            log.error("[FAILED] %s", desc)
            lines.append(ActionLine(f"[FAILED] {desc}", ActionOutcome.FAILED))
            return "failed", lines
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch)
            lines.append(ActionLine(f"[WARNING] {desc}, but {mismatch}", ActionOutcome.UNVERIFIED))
            return "failed", lines
        lines.append(
            ActionLine(
                f"[APPLIED] {desc} (now {describe_error(after[-1])})", ActionOutcome.VERIFIED
            )
        )
        return "synced", lines

    async def _require_power_switch_on(self) -> str | None:
        entity = self._config.entities.get("power_switch", "")
        if not entity:
            return "power_switch entity is not configured"
        try:
            state = await self._rest.get_state(entity)
        except Exception:  # noqa: BLE001 - fail closed
            return "power_switch state unreadable"
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
                if export is not None and export_ready and is_verified(window_line):
                    start, end = export
                    await store.save(_export_identity(raw_export, start, end), end)
                elif export is None and is_verified(window_line):
                    await store.clear()
        except Exception:  # noqa: BLE001 - persistence cannot undo HA writes
            log.error("Export event state persistence failed")

    async def _notify_export_lifecycle(
        self,
        raw_export: object,
        export: tuple[datetime, datetime] | None,
        export_ready: bool,
        export_error: str | None,
        window_line: str,
        now: datetime,
        previous_record: tuple[str, datetime] | None,
        mode: str,
    ) -> None:
        """Notify only verified export transitions; notification is never a gate."""
        service = self._settings.notify_service
        if not service:
            return
        store = ExportNotificationStore(self._settings.db_path)
        if mode != "on":
            context = _export_notification_context(raw_export, export)
            if context is None or not export_ready or export_error is not None:
                return
            event_id, start, end, planned_kw, dno_limit_kw = context
            await send_once(
                store,
                self._rest,
                service,
                make_notice(
                    "accepted",
                    event_id,
                    start,
                    end,
                    planned_export_kw=planned_kw,
                    dno_export_limit_kw=dno_limit_kw,
                ),
            )
            return
        if raw_export is None:
            if previous_record is None or not is_verified(window_line):
                return
            parsed = parse_event_id(previous_record[0])
            if parsed is None:
                return
            _, start, end = parsed
            await send_once(
                store,
                self._rest,
                service,
                make_notice("cleanup", previous_record[0], start, end),
            )
            return

        context = _export_notification_context(raw_export, export)
        if context is None:
            return
        event_id, start, end, planned_kw, dno_limit_kw = context
        if export_ready and is_verified(window_line):
            await send_once(
                store,
                self._rest,
                service,
                make_notice(
                    "accepted",
                    event_id,
                    start,
                    end,
                    planned_export_kw=planned_kw,
                    dno_export_limit_kw=dno_limit_kw,
                ),
            )
            if now.astimezone(UTC) >= start.astimezone(UTC):
                await send_once(
                    store,
                    self._rest,
                    service,
                    make_notice("started", event_id, start, end),
                )
            return
        if export_error is not None and "inverter clock" in export_error:
            # Retryable, not an abort: a sync before the event lets it arm.
            await send_once(
                store,
                self._rest,
                service,
                make_notice("clock", event_id, start, end, reason=export_error),
            )
            return
        if export_error is None or any(
            retryable in export_error
            for retryable in ("not yet armed", "hold data untrusted")
        ):
            return
        safe_state = (
            "no export window remains programmed"
            if is_verified(window_line)
            else "the Solis export state was not verified; remain in simulate and inspect it"
        )
        await send_once(
            store,
            self._rest,
            service,
            make_notice(
                "aborted",
                event_id,
                start,
                end,
                reason=export_error,
                safe_state=safe_state,
            ),
        )

    async def _has_live_verified_export(self, now: datetime) -> bool:
        """Return whether the last verified Axle export is still in progress.

        A failed Axle read is not a cancellation. Keep the whole apply pass
        away from Slot 1 while the durable record says a verified event has not
        ended; otherwise the ordinary no-export path would clear a paid event
        during a transient provider failure (#148).
        """
        async with ExportEventStore(self._settings.db_path) as store:
            record = await store.load()
        return record is not None and record[1] > now.astimezone(UTC)

    async def _has_matching_live_verified_export(
        self,
        raw_export: object,
        export: tuple[datetime, datetime],
        now: datetime,
    ) -> bool:
        """Return whether the refused event is already verified and resident in Slot 1."""
        start, end = export
        async with ExportEventStore(self._settings.db_path) as store:
            record = await store.load()
        if (
            record is None
            or record[0] != _export_identity(raw_export, start, end)
            or record[1] <= now.astimezone(UTC)
        ):
            return False
        try:
            resident = await self._read_slot_block(1)
        except Exception:  # noqa: BLE001 - fail closed around a resident write
            return False
        return resident[4:] == [start.hour, start.minute, end.hour, end.minute]

    # --- modbus helpers ---

    def _sensor(self, field: str) -> str:
        return f"sensor.{self._hub}_{field}"

    async def _write_register(self, address: int, value: int | list[int]) -> None:
        self._coordinator.confirm_write()
        await self._rest.call_service(
            "modbus",
            "write_register",
            {"hub": self._hub, "slave": self._slave, "address": address, "value": value},
        )

    async def _apply_slot_block(self, slot: int, want: list[int]) -> tuple[bool, bool]:
        """Return whether written and whether a zero was already confirmed."""
        if await self._read_slot_block(slot) == want:
            zero_confirmed = await self._confirm_zero_target(
                want, lambda: self._verify_slot_block(slot, want)
            )
            if zero_confirmed is not False:
                return False, zero_confirmed is True
        await self._write_register(_SLOT_BLOCK_REG[slot], want)
        return True, False

    async def _apply_current(self, want_raw: int, amps: float) -> tuple[bool, bool]:
        """Return whether written and whether a zero was already confirmed."""
        current = await self._read_current_a()
        if abs(current - amps) <= 0.5:
            zero_confirmed = await self._confirm_zero_target(
                amps, lambda: self._verify_current(amps)
            )
            if zero_confirmed is not False:
                return False, zero_confirmed is True
        await self._write_register(_CHARGE_CURRENT_REG, want_raw)
        return True, False

    async def _confirm_zero_target(
        self,
        target: float | list[int],
        verify: Callable[[], Awaitable[str | None]],
    ) -> bool | None:
        """Return None for non-zero targets, else whether the zero was confirmed."""
        if not _is_zero_target(target):
            return None
        return await verify() is None

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
            zero_target=want,
            refresh_entities=entities if refresh else None,
        )

    async def _verify_current(self, amps: float, *, refresh: bool = False) -> str | None:
        return await self._verify_read_back(
            self._read_current_a,
            lambda got: abs(got - amps) <= 0.5,
            lambda got: f"read back {got:g} A (wanted {amps:g} A)",
            zero_target=amps,
            refresh_entities=(self._sensor("timed_charge_current"),) if refresh else None,
        )

    async def _verify_discharge_current(
        self, amps: float, *, refresh: bool = False
    ) -> str | None:
        return await self._verify_read_back(
            self._read_discharge_current_a,
            lambda got: abs(got - amps) <= 0.5,
            lambda got: f"read back discharge {got:g} A (wanted {amps:g} A)",
            zero_target=amps,
            refresh_entities=(self._sensor("timed_discharge_current"),) if refresh else None,
        )

    async def _verify_read_back(
        self,
        read: Callable[[], Awaitable[T]],
        matches: Callable[[T], bool],
        describe_mismatch: Callable[[T], str],
        *,
        zero_target: float | list[int] | None = None,
        refresh_entities: tuple[str, ...] | None = None,
    ) -> str | None:
        """Refresh once, then perform a small bounded read-back observation."""
        if refresh_entities is not None:
            try:
                await self._refresh_entities(refresh_entities)
            except Exception:  # noqa: BLE001 - verification must degrade safely
                return "read-back refresh failed"
        mismatch: str | None = None
        last_exc: Exception | None = None
        first_match_at: int | None = None
        confirm_zero = zero_target is not None and _is_zero_target(zero_target)
        for attempt in range(_READ_BACK_ATTEMPTS):
            try:
                got = await read()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                first_match_at = None
            else:
                last_exc = None
                mismatch = None if matches(got) else describe_mismatch(got)
                if mismatch is None:
                    if not confirm_zero:
                        return None
                    if first_match_at is None:
                        first_match_at = attempt
                    elif attempt - first_match_at >= _ZERO_CONFIRM_GAP_READS:
                        return None
                else:
                    first_match_at = None
            if attempt + 1 < _READ_BACK_ATTEMPTS:
                await asyncio.sleep(_READ_BACK_DELAY_SECONDS)
        if last_exc is not None:
            return "read-back failed"
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
        except Exception:  # noqa: BLE001
            return "work mode unreadable"
        if not bitfield & _GRID_CHARGE_BIT:
            return f"grid charging not permitted (work mode bitfield {bitfield}, bit 5 unset)"
        return None

    async def _read_back_option(self, entity: str, wanted: str) -> str | None:
        """Confirm a select took the option, within the same bounded read-back
        window as every other read-back here.

        HA's state machine is asynchronous with respect to the overlay, so the
        first read after a write can still carry the old option. Without the
        retry the reconcile reports a mismatch it would have seen settle, and —
        because it remembers nothing and now runs every minute — writes the
        register again on the next pass, and the one after that. The bound stays
        finite so a permanently stale overlay can never hold the caller."""
        got = ""
        for attempt in range(_READ_BACK_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_READ_BACK_DELAY_SECONDS)
            try:
                got = str((await self._rest.get_state(entity)).state)
            except Exception:  # noqa: BLE001
                return "read-back failed"
            if got.lower() == wanted.lower():
                return None
        return f"read back {got!r} (wanted {wanted!r})"


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


def _next_wall_clock_occurrence(wall: datetime, now: datetime) -> datetime:
    """First moment at or after ``now`` showing ``wall``'s clock face.

    Recombines a date with the time of day rather than adding a ``timedelta``,
    so a DST boundary in between moves the UTC offset and leaves the clock face
    alone — which is what the Slot 1 registers actually hold.

    Comparisons are made on the absolute instant (``astimezone(UTC)``), never
    between two same-zone aware datetimes: Python compares those by clock face
    and ignores ``fold``, which would make the two distinct 01:30s of a
    fall-back night look identical. ``fold`` is then forced to 0 on the
    candidate — ``datetime.time()`` carries it through, so it has to be cleared
    deliberately — making this the *first* 01:30 of such a night, which is
    exactly the one the register fires on.
    """
    face = wall.time().replace(fold=0)
    today = datetime.combine(now.date(), face, tzinfo=now.tzinfo)
    if today.astimezone(UTC) >= now.astimezone(UTC):
        return today
    return datetime.combine(now.date() + timedelta(days=1), face, tzinfo=now.tzinfo)


def _clock_window_open(t: time, start: time, end: time) -> bool:
    """True when clock time ``t`` is inside ``[start, end)``, which may wrap midnight."""
    if start < end:
        return start <= t < end
    if start > end:
        return t >= start or t < end
    return False


def _export_not_yet_armed(export: tuple[datetime, datetime], now: datetime) -> str | None:
    """Refuse a window whose clock face would come round before its own event.

    The Slot 1 discharge registers carry a time of day and no date, while the
    planner's 24 h horizon offers an event as soon as it enters the horizon.
    Programming tomorrow's 18:30 event at noon today would discharge the battery
    at 18:30 *today* — intentional export outside a paid window, which costs a
    peak-rate refill and earns nothing.

    This is a deferral, not a terminal abort: the event is re-offered on every
    tick and arms itself once the next time its window opens is the event's own,
    so no scheduling state is needed. A window already under way stays armed, so a
    day-of pickup still delivers the remainder instead of waiting a day.

    Passing the start's clock face is not enough: until the clock also leaves
    the window, programming it puts the inverter inside it at once. Tuesday's
    18:30-19:30 programmed at Monday 18:31 would export unpaid until Monday
    19:30, so an earlier day's still-open window is refused too.

    An event inside a fall-back night's repeated hour is refused outright rather
    than armed: its clock face comes round twice, the register fires on the
    first, and the event may mean the second. Refusing loses one event an hour
    before the clocks go back; arming could export an hour early, unpaid.
    """
    start, end = export
    if now.astimezone(UTC) >= start.astimezone(UTC):
        return None
    if _clock_window_open(now.time(), start.time(), end.time()):
        return (
            f"the {start:%H:%M}-{end:%H:%M} window is already open on the clock at "
            f"{now:%a %d %b %H:%M}, before the event starts {start:%a %d %b %H:%M}; "
            "not yet armed"
        )
    occurrence = _next_wall_clock_occurrence(start, now)
    if occurrence.astimezone(UTC) == start.astimezone(UTC):
        return None
    return (
        f"the {start:%H:%M} window would next fire {occurrence:%a %d %b %H:%M}, "
        f"before the event starts {start:%a %d %b %H:%M}; not yet armed"
    )


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


def _export_notification_context(
    value: object, resolved: tuple[datetime, datetime] | None
) -> tuple[str, datetime, datetime, float | None, float | None] | None:
    """Extract notification-safe identity and planning details from an export value."""
    get = (
        value.get
        if isinstance(value, dict)
        else lambda name, default=None: getattr(value, name, default)
    )
    identity = get("event_identity")
    if isinstance(identity, tuple) and len(identity) == 3:
        direction, start, end = identity
        if (
            isinstance(direction, str)
            and isinstance(start, datetime)
            and isinstance(end, datetime)
            and start.tzinfo is not None
            and end.tzinfo is not None
        ):
            return (
                _export_identity(value, start, end),
                start,
                end,
                _finite_number(get("planned_export_kw")),
                _finite_number(get("dno_export_limit_kw")),
            )
    if resolved is None:
        return None
    start, end = resolved
    return (
        _export_identity(value, start, end),
        start,
        end,
        _finite_number(get("planned_export_kw")),
        _finite_number(get("dno_export_limit_kw")),
    )


def _finite_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _charge_current_description(
    intent: ChargeIntent, settings: Settings, amps: int, *, fallback: bool
) -> str:
    if fallback:
        return (
            f"set timed charge current to {amps} A for configured window "
            f"{fmt_hhmm(intent.window_start)}-{fmt_hhmm(intent.window_end)}"
        )
    return (
        f"set timed charge current to {amps} A over "
        f"{charge_hours(intent, settings):.1f} h of the window"
    )


def _window_elapsed(intent: ChargeIntent, settings: Settings) -> tuple[float, float]:
    """The window's length and the hours of it elapsed when the plan's SoC was
    observed (outside the window when not strictly between 0 and the length)."""
    from ha_spark.energy.forecast import load_timezone  # import cycle, see apply

    full = window_hours(intent.window_start, intent.window_end)
    local = intent.soc.observed_at.astimezone(load_timezone(settings.timezone))
    minute = local.hour * 60 + local.minute + local.second / 60
    start = intent.window_start.hour * 60 + intent.window_start.minute
    return full, (minute - start) % (24 * 60) / 60


def charge_hours(intent: ChargeIntent, settings: Settings) -> float:
    """Hours the charge current is sized over: what is left of the window when
    the plan's SoC was observed inside it, else the full window (#181).

    A mid-window replan spreading the remaining energy over the full window
    undercharges: at 03:30 it programmed 14 A where 41 A was needed.
    """
    full, elapsed = _window_elapsed(intent, settings)
    return full - elapsed if 0 < elapsed < full else full


def _sizing_soc(intent: ChargeIntent, settings: Settings) -> float:
    """The SoC the charge current is sized from (#402).

    Observed before the window, it is the plan's expected SoC at window start:
    sizing from the observed SoC would ignore the evening drain and undercharge.
    Inside the window it is the observed SoC (#181). An untrusted measurement
    always sizes from its 0, never from the projection, and is refused at the
    gate; a plan without a projection keeps the observed SoC.
    """
    full, elapsed = _window_elapsed(intent, settings)
    if (
        intent.soc.ok
        and intent.expected_window_start_soc is not None
        and math.isfinite(intent.expected_window_start_soc)
        and not 0 < elapsed < full
    ):
        return intent.expected_window_start_soc
    return intent.soc_now


def solis_current_a(intent: ChargeIntent, settings: Settings) -> float:
    """DC charge current (A) for the intent — the legacy planner sizing, inverted.

    A target at or below the sizing SoC (decision B can set one below the
    expected window-start SoC) sizes 0 A: the window then adds nothing.
    """
    needed_kwh = max(
        0.0,
        (intent.target_soc_pct - _sizing_soc(intent, settings))
        / 100.0
        * settings.battery_capacity_kwh,
    )
    eff = settings.charge_efficiency if settings.charge_efficiency > 0 else 1.0
    purchase = needed_kwh / eff
    kwh_per_amp = charge_hours(intent, settings) * settings.battery_voltage_v / 1000.0
    if kwh_per_amp <= 0:
        return 0.0
    return min(settings.max_charge_current_a, purchase / kwh_per_amp)
