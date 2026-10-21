"""Half-hourly scheduled plan + apply loop.

`run_once` computes and applies a single charge plan (the same path as
`ha-spark plan --apply`). `run_forever` wakes once a minute and calls
`run_once` once per local half-hour slot. A failed run is retried on the next
tick because the completed slot is not recorded until the run succeeds. An
unchanged plan is skipped only while it is known resident: a failed or blocked
apply, or a run that raised, leaves no baseline, so the next replan applies
again (#168).

When `grid_power_entity` is set, every tick inside the charge window also runs
the supply guard: throttle the battery's charge-current setpoint while
whole-house draw exceeds `supply_max_current_a`, restoring toward the plan's
current as headroom returns. Outside the window the timed-charge setpoint is
inert (and there is nothing else ha-spark can shed), so the guard stays quiet.

Every tick also reconciles the inverter's hold state (`reconcile_tick`, #143):
a read-first pass that converges the whole-inverter enable on what the clock and
the dispatch holds imply. On the HA-entity path it re-reads the dispatch entity
each minute; the Octopus API path re-rates the last plan's dispatch windows
from local evidence without fetching plans from Kraken. It is deliberately
independent of `setpoint_changed` — a hold boundary is a clock event, not a
plan change, and dispatch bounds need not land on a half-hour. A tick that
replans reconciles inside `run_once` against its new plan; if replan raises,
the per-minute pass uses the last plan so a failure never starves the clock
(#147).

Every tick also makes exactly one checked SoC observation (`soc_monitor_tick`,
#114): the daemon loop is the sole SoC observation cadence, and that one
measurement is reused by planning, device application, and guard work. The
first failed observation enters pending failure — new SoC-based programming
and charge-rate increases are blocked while valid supply-guard reductions
remain available — and consecutive failures are counted toward the configured
fallback-entry threshold (`soc_failure_threshold`, default 3). With an
explicit `solis_fallback_current_a`, Solis programs only the configured cheap
window and reports fallback as confirmed after current and window read-backs;
failed attempts retry on the next minute. The active fallback is left in place
on a passing observation until `soc_recovery_minutes` of stable reports make
recovery ready (#117); each tick then computes a fresh plan from that tick's
measurement and applies it, leaving fallback only once the apply reads back
clean. A recovered apply that is not fully verified re-applies the fallback on
the same tick and records its verdict (#423), so the unconfirmed or verified
state is published at once, and the next recovery attempt waits five minutes;
an unconfirmed re-apply is retried every minute of that wait, because the
per-minute fallback path deliberately stays quiet while recovery is ready.
AlphaESS has no fallback request: after the threshold it stays blocked until
the same stable evidence qualifies a fresh plan and its apply succeeds, and a
failed recovered apply there still retries the next minute. The
fallback schedule is validated as one pure schedule before every apply (#422):
an above-limit current is blocked with the case named, never clamped. The
authority store owns *fallback active* and its causes (phase 2: the only cause
is SoC integrity) and is saved, with a bumped epoch, before the fallback write
is requested; entry and exit each notify once, and exit clears the store only
after the recovered plan applied and read back. Observe and Protect still
count, persist and publish pending failure and threshold crossings but never
request a fallback write.

Owner mode requests are accepted only here, persisted under the inverter bus
lock before handover work. Stored steps resume after restart; partial failures
remain visible and retry on the next tick. Optimise activation always applies a
fresh plan without a preview baseline, and completes only on typed verification.
Leaving Optimise cancels export, verifies the fallback starting schedule, then
releases owned holds only for Observe before committing the target mode.

With `inverter_clock_dst_sync` on, the first tick after the household zone's
UTC offset changes also syncs the inverter clock (`DstClockSync`, #161).

Readiness captures the revision and rates in force on each boundary tick, in
all modes, and separate read-only guard evidence in Observe. Missed boundaries
stay gaps; mid-slot requests never rewrite a slot. Recording failures are isolated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import NamedTuple
from uuid import uuid4

import httpx
import uvicorn

from ha_spark.agent.auth import resolve_token
from ha_spark.api.server import (
    AGENT_PORT,
    INGRESS_PORT,
    INGRESS_PROXY_IP,
    OPTIONS_PATH,
    AppState,
    build_app,
    make_server,
    serve_in_background,
    stop_server,
)
from ha_spark.authority import (
    FALLBACK_CAUSE_SOC_INTEGRITY,
    AuthorityStore,
    FallbackState,
    RequesterKind,
)
from ha_spark.config import Settings, actuation_settings_changed
from ha_spark.daemon_channel import DaemonChannel, Pending, Reply, action_lines
from ha_spark.devices import Capability, Device, inverter_device
from ha_spark.devices.execution import (
    ExecutionContext,
    Ownership,
    execution_context,
    held_identity_remapped,
)
from ha_spark.devices.inverters.solis import (
    SolisDevice,
    validated_fallback_current_a,
)
from ha_spark.devices.outcomes import ActionLine, ActionOutcome, is_verified, legacy_action_line
from ha_spark.energy.derived_base_load import (
    BACKFILL_NAME,
    BACKFILL_STATISTIC_ID,
    derive_specs_from_settings,
    rerive_trailing_window,
)
from ha_spark.energy.dispatch_evidence import partition_dispatches
from ha_spark.energy.dst_clock_sync import DstClockSync
from ha_spark.energy.evidence_freshness import EvidenceFreshness, check_report_time
from ha_spark.energy.forecast import forecast_model_tag, load_timezone
from ha_spark.energy.horizon import HorizonGeometry
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ChargeIntent, ChargePlan, DispatchSlot, LoadForecast
from ha_spark.energy.orchestrator import orchestrate
from ha_spark.energy.plan_run import current_plan
from ha_spark.energy.publish import (
    plan_to_payload,
    publish_operating_mode,
    publish_plan,
    publish_predictions,
    publish_soc_integrity,
    republish_last,
)
from ha_spark.energy.readiness import readiness_tick
from ha_spark.energy.readiness_ledger import PlanRevision, record_guard, record_slot, slot_start
from ha_spark.energy.readiness_measurements import record_closed_days
from ha_spark.energy.report import format_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.soc_monitor import SocMonitor, SocMonitorSnapshot, observe_soc
from ha_spark.energy.sources import (
    ev_hold_status_from_read,
    parse_time,
    read_dispatch_evidence,
    read_dispatches_and_live,
)
from ha_spark.energy.supply_guard import SupplyGuard
from ha_spark.energy.tariff import _controlled_windows
from ha_spark.energy.tariff import _in_overnight_window as in_window
from ha_spark.energy.v2l import run_v2l_tick, run_v2l_topup, warn_deprecated_notify_target
from ha_spark.ha.notifier import send_notification
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger
from ha_spark.mode_preconditions import check_preconditions
from ha_spark.settings_service import SettingsService
from ha_spark.transitions import PreconditionReport, TransitionRequest, propose_transition

log = get_logger(__name__)

# How often the signal sampler records occupancy/heat-pump/temperature signals.
SIGNAL_SAMPLE_INTERVAL = timedelta(minutes=30)


REPLAN_INTERVAL = timedelta(minutes=30)

# #423: after a recovered apply that is not fully verified re-applied the
# fallback on the same tick, the next recovery attempt waits this long.
RECOVERY_RETRY_DELAY = timedelta(minutes=5)


def _slot_start(now: datetime) -> datetime:
    """Return the local half-hour slot containing ``now``."""
    slot_minutes = int(REPLAN_INTERVAL.total_seconds() // 60)
    minute = now.minute - now.minute % slot_minutes
    return now.replace(minute=minute, second=0, microsecond=0)


def should_run(now: datetime, last_run_slot: datetime | None) -> bool:
    """True when the current local half-hour slot has not run successfully."""
    return _slot_start(now) != last_run_slot


# Asymmetric deadband on the programmed charge current, in whole register amps
# (#173): any rise re-applies so the night never undercharges, but a reduction
# must reach 5 A, so SoC drift does not rewrite the inverter every half hour.
_CURRENT_RAISE_A = 1
_CURRENT_LOWER_A = 5


def charge_current_changed(applied_a: float, planned_a: float) -> bool:
    """Whether the planned charge current leaves the deadband around the applied one."""
    delta = round(planned_a) - round(applied_a)
    return delta >= _CURRENT_RAISE_A or delta <= -_CURRENT_LOWER_A


def needs_apply(
    previous_plan: ChargePlan | None,
    intent: ChargeIntent,
    rate_w: Callable[[ChargeIntent], float],
    voltage_v: float,
) -> bool:
    """Whether ``intent`` must be applied, given the last *applied* plan.

    ``previous_plan`` must be the plan last applied, not the last one computed:
    a skipped plan never reached the inverter, so it cannot be the baseline the
    current deadband is measured from (#173). ``rate_w`` is the active device's
    pure ``planned_rate_w``. The amps are compared only when this plan's SoC is
    trusted: an untrusted plan is sized from 0% and is refused at the gate anyway.
    """
    if previous_plan is None or not previous_plan.soc.ok:
        return True
    previous = previous_plan.charge_intent
    if previous is None or setpoint_changed(previous, intent):
        return True
    if not intent.soc.ok or voltage_v <= 0:
        return False
    return charge_current_changed(rate_w(previous) / voltage_v, rate_w(intent) / voltage_v)


def setpoint_changed(
    previous: ChargeIntent,
    current: ChargeIntent,
) -> bool:
    """Return whether a plan changes the command sent to an inverter.

    SoC is deliberately excluded. The daemon measures SoC every minute, so a
    fresh observation alone must not turn an unchanged plan into another device
    write.

    A pending export event is always a changed command because its device
    programming depends on the clock as well as the plan. Otherwise, a change
    in the charge target, window, holds, or export intent is a changed command.
    An unreadable Axle event is also always a changed command so the device gets
    a chance to preserve a verified resident export until the read recovers or
    the verified window ends (#148).
    """
    if not getattr(current, "export_trusted", True):
        return True
    if getattr(current, "export", None) is not None:
        return True
    return (
        previous.target_soc_pct != current.target_soc_pct
        or previous.window_start != current.window_start
        or previous.window_end != current.window_end
        or previous.holds != current.holds
        # The export value is an optional planner extension.  Comparing the
        # value itself catches changed/cancelled event windows while keeping
        # legacy inverter intents source-compatible.
        or getattr(previous, "export", None) != getattr(current, "export", None)
    )


class RunResult(NamedTuple):
    """One ``run_once``: the plan it computed and the plan now resident on the device."""

    plan: ChargePlan
    # The baseline the next `needs_apply` compares against: `plan` after a clean
    # apply, the previous baseline after a skip, and `None` after a failed or
    # blocked apply, which leaves the device state unknown (#168).
    applied_plan: ChargePlan | None
    # The device's `apply` lines; empty when the apply was skipped or held.
    apply_lines: tuple[str, ...] = ()
    text: str = ""
    action_lines: tuple[str, ...] = ()
    revision: PlanRevision | None = None


def _apply_failed(lines: list[str]) -> bool:
    """An unknown or non-verified result cannot establish a resident baseline."""
    return not lines or not all(is_verified(line) for line in lines)


def _preview_accepted(lines: tuple[str, ...]) -> bool:
    """Legacy non-real-mode bookkeeping is not hardware verification.

    Plain strings are accepted only here for callers predating typed outcomes.
    """
    return bool(lines) and all(
        legacy_action_line(line).outcome
        in (
            ActionOutcome.VERIFIED,
            ActionOutcome.UNCHANGED_VERIFIED,
            ActionOutcome.SIMULATED,
        )
        for line in lines
    )


def _real_write_mode(settings: Settings, execution: ExecutionContext | None = None) -> bool:
    context = execution or execution_context()
    return any(
        device.type == "inverter"
        and context.gate.mode_for(Ownership.CHARGING, device, settings) == "on"
        for device in settings.devices
    )


async def _record_forecast(
    settings: Settings, plan: ChargePlan, forecast: LoadForecast,
    geometry: HorizonGeometry | None = None,
) -> None:
    """Log the plan's load forecast for its served day so `forecast-eval` can
    score it later.

    Records the forecast's own unrotated day-order ``slots`` and its served
    date (#397), never the horizon-order ``inputs.load_slots``; a forecast
    built without a date (hand-built tests) reads as the pre-#394 tomorrow.
    """
    tz = load_timezone(settings.timezone)
    target_date = forecast.target_date or (datetime.now(tz) + timedelta(days=1)).date()
    try:
        async with ForecastLedger(settings.db_path) as ledger:
            await ledger.record_forecast(
                datetime.now(UTC),
                target_date,
                forecast_model_tag(forecast.source),
                plan.load_kwh,
                forecast.slots,
                forecast.source,
                horizon_start=geometry.horizon_start if geometry is not None else None,
                horizon_end=geometry.horizon_end if geometry is not None else None,
                drove_plan=True,
                median_total_kwh=forecast.median_total_kwh,
                median_slots=forecast.median_slots,
            )
    except Exception:
        log.exception("Recording forecast failed")


async def run_once(
    settings: Settings,
    *,
    soc: SocMeasurement | None = None,
    previous_plan: ChargePlan | None = None,
    trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
    ev_hold_state: EvHoldState | None = None,
    fallback_active: bool = False,
    execution: ExecutionContext | None = None,
) -> RunResult:
    """Compute the charge plan, log it, and apply it under site authority.

    ``soc`` is the daemon tick's checked measurement, reused as-is so the
    plan and its charge intent carry the exact observation the loop made
    (no independent reread); a failed measurement blocks real writes at the
    charger gate. Without one (CLI/agent callers) the plan makes its own
    observation through the same shared path.

    ``trusted_holds`` is the daemon's last trusted hold set, used by the
    reconcile when this plan's hold data is untrusted (#143 §3).

    ``execution`` defaults to the process-shared driver context, as do the
    guard, reconcile, fallback, DST and shutdown entry points. Per-call REST
    clients and driver instances therefore never create independent bus locks.
    """
    context = execution or execution_context()
    generation = context.planned_generation.get()
    if generation is None:
        generation = context.config_generation
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        run = await current_plan(settings, rest, soc=soc)
        plan, inputs, load_source = run.plan, run.inputs, run.load_source
        log.info(
            "Charge plan:\n%s",
            format_plan(plan, load_source, forecast_date=run.forecast.target_date, tz=run.tz),
        )
        intent = plan.charge_intent
        assert intent is not None  # planner always sets it
        device = (
            inverter_device(settings, rest) if execution is None
            else inverter_device(settings, rest, execution=execution)
        )
        now = datetime.now(load_timezone(settings.timezone))
        revision = None
        try:
            revision = PlanRevision(
                uuid4().hex, now.astimezone(UTC), run,
                device.planned_rate_w(plan.charge_intent)
                if Capability.CHARGE_RATE in device.capabilities else None,
                generation,
            )
        except Exception:
            log.exception("Readiness plan provenance unavailable")
        ev_hold_state = ev_hold_state or EvHoldState()
        evidence_freshness = getattr(inputs, "evidence_freshness", None) or {}
        ev_hold_active = await _observe_ev_hold(
            ev_hold_state,
            inputs.ev_hold_charging,
            evidence_freshness.get("ev_status"),
            now=now,
            device=device,
            battery_freshness=evidence_freshness.get("battery_power"),
            battery_direction_value=getattr(inputs, "battery_direction", "unknown"),
        )
        intent = replace(intent, ev_hold_active=ev_hold_active)
        # One reconcile pass before anything else, on every device-driving
        # caller (#143): an owner that abdicates when invoked from the CLI is
        # not one (ADR-0003), and the switch must have settled before `apply`
        # reads it as an export precondition. The daemon repeats this every
        # minute; here it also covers the skipped-setpoint path below, which
        # would otherwise leave the clock unserved for a whole slot.
        reconcile_intent = hold_reconcile_intent(intent, trusted_holds)
        lines: list[str] = (
            list(await device.reconcile_holds(reconcile_intent, now))
            if reconcile_intent is not None
            else [UNTRUSTED_HOLDS_LINE]
        )
        # Lazy: the rate is only consulted when the setpoint itself is unchanged.
        rate_w = lambda i: device.planned_rate_w(i)  # noqa: E731
        applied_plan = previous_plan
        apply_lines: list[str] = []
        if fallback_active:
            reason = (
                "Solis fallback is active"
                if any(d.type == "inverter" and d.driver == "solis" for d in settings.devices)
                else "SoC integrity recovery is pending"
            )
            lines.append(ActionLine(
                f"[SKIP] normal programming held while {reason}", ActionOutcome.BLOCKED
            ))
            applied_plan = None
        elif needs_apply(previous_plan, intent, rate_w, settings.battery_voltage_v):
            apply_lines = list(await device.apply(intent))
            lines.extend(apply_lines)
            accepted = (
                not _apply_failed(apply_lines)
                if _real_write_mode(settings, execution)
                else _preview_accepted(tuple(apply_lines))
            )
            applied_plan = plan if accepted else None
        else:
            lines.append("[SKIP] charge setpoint unchanged")
        for line in lines:
            log.info(line)
        await publish_plan(rest, plan, settings)
        if settings.v2l_power_entity:
            try:
                await run_v2l_topup(settings, rest, plan, inputs.flexibility_event, now)
            except Exception:  # noqa: BLE001 - a request never breaks a plan run
                log.warning("V2L top-up request failed", exc_info=True)
    await _record_forecast(settings, plan, run.forecast, getattr(run.inputs, "geometry", None))
    await _run_orchestrator(settings)
    await _run_derived_rerive(settings)
    return RunResult(
        plan, applied_plan, tuple(apply_lines),
        format_plan(plan, load_source, forecast_date=run.forecast.target_date, tz=run.tz),
        tuple(lines),
        revision,
    )


async def _run_orchestrator(settings: Settings) -> None:
    """Compute proactive decisions for tomorrow and publish them (best-effort).

    Isolated so a failure here never aborts the daily run. Nothing is executed
    yet — decisions are advisory; this is the seam later proactivity hangs off.
    """
    try:
        decisions = await orchestrate(settings)
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            await publish_predictions(rest, decisions, settings)
    except Exception:
        log.exception("Proactive orchestrator failed")


async def _run_derived_rerive(settings: Settings) -> None:
    """Re-derive the trailing 48h of base-load history (best-effort).

    No-op when grid import is unconfigured (the source-entity backfill path
    is still available via ``backfill-load --from``). A failure here must
    never block the scheduled plan run; it logs + reports so the operator can
    inspect the daemon log.
    """
    specs = derive_specs_from_settings(settings)
    if not specs.get("grid_import") or not specs["grid_import"].entity_id:
        return
    try:
        result = await rerive_trailing_window(
            settings,
            specs,
            statistic_id=BACKFILL_STATISTIC_ID,
            statistic_name=BACKFILL_NAME,
        )
    except Exception:
        log.exception("Derived base-load rerive failed; will retry next tick")
        return
    if result is None:
        return
    if result.rows_imported:
        log.info(
            "Derived base-load rerive: %d rows upserted (%s)",
            result.rows_imported,
            result.span,
        )
    if result.negative_clamped:
        log.warning(
            "Derived base-load rerive: %d hour(s) clamped to 0 — check invert flags",
            result.negative_clamped,
        )
    for note in result.degradation:
        log.info("Derived base-load rerive: %s", note)


async def sample_signals(settings: Settings, now: datetime) -> None:
    """Record occupancy/heat-pump/temperature signals for one sample tick.

    Each signal is independently best-effort: an unreadable entity logs a
    warning and is skipped, it never aborts the others or the daemon loop.
    """
    async with (
        HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest,
        ForecastLedger(settings.db_path) as ledger,
    ):
        person_entities = [e.strip() for e in settings.person_entities.split(",") if e.strip()]
        if person_entities:
            home = 0
            for entity in person_entities:
                try:
                    state = await rest.get_state(entity)
                except httpx.HTTPError as exc:
                    log.warning("Signal sampler: %s unreadable (%s)", entity, exc)
                    continue
                if state.state == "home":
                    home += 1
            await ledger.record_signal(now, "occupancy_home_frac", home / len(person_entities))

        if settings.heatpump_energy_entity:
            try:
                state = await rest.get_state(settings.heatpump_energy_entity)
                await ledger.record_signal(now, "heatpump_kwh", float(state.state))
            except (httpx.HTTPError, ValueError) as exc:
                log.warning(
                    "Signal sampler: %s unreadable (%s)", settings.heatpump_energy_entity, exc
                )

        if settings.outdoor_weather_entity:
            try:
                state = await rest.get_state(settings.outdoor_weather_entity)
                temp = state.attributes.get("temperature")
                if temp is not None:
                    await ledger.record_signal(now, "temp_out_c", float(temp))
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                log.warning(
                    "Signal sampler: %s unreadable (%s)", settings.outdoor_weather_entity, exc
                )


async def guard_tick(
    settings: Settings,
    target_w: float | None,
    *,
    soc: SocMeasurement | None = None,
    fallback_active: bool = False,
    fallback_confirmed: bool = False,
) -> float:
    """One supply-guard pass; returns the target charge power (W) used.

    No-op for inverters without a settable charge rate (the guard has nothing
    to throttle). A daemon (re)started mid-window has no plan yet; adopt the
    current charge-rate setpoint (W) as the restore target rather than guessing.
    ``soc`` is the tick's checked measurement: while it failed the guard can
    only reduce. An unconfirmed fallback also blocks increases; a verified one
    caps restoration at its configured current.
    """
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        charger = inverter_device(settings, rest)
        if Capability.CHARGE_RATE not in charger.capabilities:
            return target_w or 0.0
        if fallback_active and fallback_confirmed:
            fallback_a = validated_fallback_current_a(settings)
            if fallback_a is not None:
                # The fallback setting is the restoration target; a plan made
                # while SoC was rejected is not authority for this value.
                target_w = fallback_a * settings.battery_voltage_v
        if target_w is None:
            target_w = await charger.read_charge_rate()
            log.info("Supply guard: adopted current setpoint %.0f W as target", target_w)
        await SupplyGuard(settings, rest).tick(
            target_w,
            soc=soc,
            fallback_active=fallback_active,
            fallback_confirmed=fallback_confirmed,
        )
    return target_w


UNTRUSTED_HOLDS_LINE = (
    "[SKIP] hold data untrusted and no trusted hold set yet; power switch left as-is"
)
UNREADABLE_DISPATCH_LINE = "[SKIP] dispatch entity unreadable; using the last trusted holds"
STALE_DISPATCH_LINE = "[SKIP] dispatch evidence not fresh; using the last trusted holds"
UNREADABLE_EV_STATUS_LINE = "[SKIP] EV status unreadable; no EV hold evidence"
UNTRUSTED_EV_RELINQUISH_LINE = (
    "[SKIP] dispatch holds ended, but EV status is unreadable; inverter state left unchanged"
)
EV_RELEASE_CONFIRMATION_PASSES = 5


@dataclass
class EvHoldState:
    """EV hold snapshot shared by plan and minute passes (#420/#421).

    Small and serialisable on purpose: ``active``, the strictly advancing
    clear-report count, and the last counted effective report time. #421
    persists it in the authority store, so it must stay free of behaviour.
    """

    active: bool = False
    clear_count: int = 0
    last_clear_report_at: datetime | None = None


def ev_hold_overnight_exception(
    now: datetime,
    resident_window: tuple[time, time] | None,
    battery_validity: str,
    battery_direction_value: str,
) -> bool:
    """Q4: whether the overnight exception stops a *new* EV hold from starting.

    It applies only when all of: ``now`` is inside the *resident* slot-1
    charge window read back from the inverter (unknown is not verified, so it
    means no exception), the battery-direction evidence is fresh, and the
    direction shows no discharge (``charging`` or ``idle``). Missing, stale or
    unknown direction, or ``discharging``, means the hold applies. A confirmed
    EV hold is unaffected by the window: this predicate only gates a hold
    *start*, never a release.
    """
    if resident_window is None:
        return False
    if battery_validity != "fresh":
        return False
    if battery_direction_value not in ("charging", "idle"):
        return False
    return in_window(now.time(), resident_window[0], resident_window[1])


def next_ev_hold_state(
    previous: EvHoldState,
    charging: bool | None,
    evidence: EvidenceFreshness | None,
    *,
    overnight_exception: bool = False,
) -> EvHoldState:
    """Advance the EV hold state machine by one status read (#420).

    Only fresh evidence decides. Stale, future, unknown, unavailable or
    unreadable evidence never starts a hold and never releases one, and it
    resets the clearance run (Q5). Fresh charging starts (or keeps) the hold
    and resets the clearance run — except that the overnight exception (Q4)
    stops a *new* hold; a confirmed hold is never released by the window.
    Fresh not-charging advances a run of strictly advancing effective report
    times: the fifth counted report releases, an equal report time neither
    advances nor resets, and a backwards one resets the run (Q5).
    """
    if evidence is None or evidence.validity != "fresh" or charging is None:
        return EvHoldState(active=previous.active)
    if charging:
        if previous.active or not overnight_exception:
            return EvHoldState(active=True)
        return EvHoldState()
    if not previous.active:
        return EvHoldState()
    reported_at = evidence.reported_at
    if reported_at is None:
        return EvHoldState(active=True)
    last = previous.last_clear_report_at
    if last is None:
        return EvHoldState(active=True, clear_count=1, last_clear_report_at=reported_at)
    if reported_at < last:
        # A backwards report resets the run; the hold stays until clear evidence.
        return EvHoldState(active=True)
    if reported_at == last:
        # Equal does not advance and does not reset: repeated GETs of one
        # cached report never manufacture five distinct clear reports.
        return EvHoldState(
            active=True, clear_count=previous.clear_count, last_clear_report_at=last
        )
    count = previous.clear_count + 1
    if count >= EV_RELEASE_CONFIRMATION_PASSES:
        return EvHoldState()
    return EvHoldState(active=True, clear_count=count, last_clear_report_at=reported_at)


async def _resident_charge_window(device: Device, now: datetime) -> tuple[time, time] | None:
    """The resident slot-1 charge window read back from the inverter (#420).

    Reuses #418's overlay reads (GETs only, no refresh, no write), counted
    only when every field reports fresh (#444). ``None`` means unknown or
    unsupported: unknown is not verified, so the overnight exception never
    applies on a ``None`` read.
    """
    reader: Callable[[datetime], Awaitable[tuple[time, time] | None]] | None = getattr(
        device, "verified_resident_charge_window", None
    )
    if reader is None:
        return None
    try:
        return await reader(now)
    except Exception:  # noqa: BLE001 - an unreadable window is not verified
        log.debug("Resident charge-window read failed; the overnight exception is off")
        return None


async def _observe_ev_hold(
    state: EvHoldState,
    charging: bool | None,
    evidence: EvidenceFreshness | None,
    *,
    now: datetime,
    device: Device,
    battery_freshness: EvidenceFreshness | None,
    battery_direction_value: str,
) -> bool:
    """Update the caller-owned state and return whether the EV hold is active.

    The resident window is read back only where the exception can matter: a
    *new* hold on fresh charging evidence. Every other pass keeps the steady
    state of one cached ``GET`` and zero extra reads.
    """
    exception = False
    if (
        charging is True
        and not state.active
        and evidence is not None
        and evidence.validity == "fresh"
    ):
        resident = await _resident_charge_window(device, now)
        exception = ev_hold_overnight_exception(
            now,
            resident,
            battery_freshness.validity if battery_freshness is not None else "unknown",
            battery_direction_value,
        )
    context = execution_context()
    store = context.authority
    record = next((h for h in store.state.holds if h.cause == "ev"), None) if (
        store is not None and store.state is not None
    ) else None
    if record is not None:
        # A release still pending (it failed or is retrying) needs the clear
        # evidence to stay fresh: anything but a fresh not-charging report
        # re-arms the confirmed hold and restarts the clearance run (Q4/Q5).
        rearm = record.confirmed and record.pending_release and not (
            evidence is not None and evidence.validity == "fresh" and charging is False
        )
        state.active = (record.confirmed and not record.pending_release) or rearm
        state.clear_count = 0 if rearm else record.clear_count
        state.last_clear_report_at = None if rearm else record.last_clear_report_at
    next_state = next_ev_hold_state(state, charging, evidence, overnight_exception=exception)
    if record is not None and store is not None and store.state is not None:
        # Every EV record follows the one EV evidence stream, so a record left
        # under an older device identity can never outlive the clearance.
        store.save_holds(tuple(
            h.model_copy(update={
                "clear_count": next_state.clear_count,
                "last_clear_report_at": next_state.last_clear_report_at,
                "pending_release": not next_state.active if h.confirmed else h.pending_release,
            }) if h.cause == "ev" else h
            for h in store.state.holds
        ))
    state.active = next_state.active
    state.clear_count = next_state.clear_count
    state.last_clear_report_at = next_state.last_clear_report_at
    return state.active


async def notify_remapped_holds(settings: Settings, rest: HomeAssistantRest) -> None:
    """Keep old identities and persist one degraded notice per mapping mismatch episode."""
    store = execution_context().authority
    if store is None or not store.known or store.save_failed or store.state is None:
        return
    records = []
    notify = False
    for record in store.state.holds:
        remapped = held_identity_remapped(record.device, settings)
        notify |= remapped and not record.remap_notified
        records.append(record.model_copy(update={"remap_notified": remapped}))
    if tuple(records) != store.state.holds:
        store.save_holds(tuple(records))
    if notify:
        try:
            await send_notification(
                settings, rest, "ha-spark degraded hold",
                "Held control mapping changed; the original hold is retained. "
                "Move to Observe before using the replacement mapping.",
            )
        except Exception:  # noqa: BLE001 - notification failure must not block protection
            log.warning("Remapped hold notification failed")


async def _hold_degraded_notice(
    settings: Settings, rest: HomeAssistantRest,
    freshness: dict[str, EvidenceFreshness], now: datetime, *, dispatch_trusted: bool,
) -> None:
    store = execution_context().authority
    if store is None or store.state is None:
        return
    records = []
    notices = []
    ev = freshness.get("ev_status")
    for record in store.state.holds:
        # A hold that no longer holds anything is not retained, so never degraded.
        holding = not record.pending_release and (
            record.trusted_end is None or now < record.trusted_end
        )
        # Dispatch evidence is whatever this pass trusts for holds: the fresh
        # HA dispatch entity, or the plan's freshness-gated Kraken fetch.
        fresh = not holding or (
            ev is not None and ev.validity == "fresh" if record.cause == "ev"
            else dispatch_trusted
        )
        since = None if fresh else record.evidence_stale_since or now
        notified = False if fresh else record.degraded_notified
        if since is not None and not notified and (
            now - since >= timedelta(minutes=settings.hold_degraded_minutes)
        ):
            notified = True
            notices.append(record.cause)
        records.append(record.model_copy(update={
            "evidence_stale_since": since, "degraded_notified": notified,
        }))
    # Persist the once-only notice before sending, including across restarts.
    store.save_holds(tuple(records))
    for cause in notices:
        try:
            await send_notification(
                settings, rest, "ha-spark degraded hold",
                f"Retaining {cause} hold without fresh evidence; check its source.",
            )
        except Exception:  # noqa: BLE001 - notice failure must not block protection
            log.warning("Degraded hold notification failed")


_RELEASE_REFUSED_REASONS = {
    "switch_unreadable": "the power switch is unreadable",
    "schedule_unverified": "the schedule to restore is not verified",
}


async def _release_refused_notice(
    settings: Settings, rest: HomeAssistantRest, now: datetime
) -> None:
    """Notify once per episode when an owned hold's release stays refused (#449).

    The flag is persisted before sending, so a restart doesn't repeat it; a
    verified release clears the record and with it the flag. Never blocks.
    """
    store = execution_context().authority
    if store is None or store.state is None:
        return
    due = [
        h for h in store.state.holds
        if h.release_refused_since is not None and not h.release_refused_notified
        and now - h.release_refused_since >= timedelta(minutes=settings.hold_degraded_minutes)
    ]
    if not due:
        return
    try:
        store.save_holds(tuple(
            h.model_copy(update={"release_refused_notified": True}) if h in due else h
            for h in store.state.holds
        ))
    except Exception:  # noqa: BLE001 - bookkeeping never blocks the tick
        log.warning("Recording the refused-release notice failed; not sending it")
        return
    for record in due:
        reason = _RELEASE_REFUSED_REASONS.get(record.release_refused_reason or "", "refused")
        try:
            await send_notification(
                settings, rest, "ha-spark hold not released",
                f"The {record.cause} hold has ended but the battery is still held: "
                f"{reason}. Check the inverter power switch.",
            )
        except Exception:  # noqa: BLE001 - notice failure must not block protection
            log.warning("Refused-release notification failed")


def hold_reconcile_intent(
    intent: ChargeIntent, trusted_holds: tuple[tuple[datetime, datetime], ...] | None
) -> ChargeIntent | None:
    """The intent a reconcile pass may act on, or ``None`` when it must do nothing.

    Degraded hold data is ignored, never believed (#143 §3). A trusted intent is
    used as-is. An untrusted one, whose empty holds mean "unreadable" rather than
    "no dispatch", is evaluated against the last trusted hold set instead, so a
    hold still ends on its own known end time: no expiry timer, no stuck ``Off``.
    With no trusted set yet, durable records provide the dispatch end or an
    already-cleared owned release. An unconfirmed dispatch request counts here
    (#443): its window can only retry the Off, because a release still needs a
    confirmed owned record. No picture and no record never writes On.
    """
    if intent.hold_trusted:
        return intent
    if trusted_holds is None:
        store = execution_context().authority
        records = store.state.holds if store is not None and store.state is not None else ()
        recorded_dispatch = tuple(
            (h.started_at, h.trusted_end) for h in records
            if h.cause == "dispatch" and h.trusted_end is not None
        )
        if recorded_dispatch:
            return replace(intent, holds=recorded_dispatch)
        owned_release = any(h.owned and h.confirmed and h.pending_release for h in records)
        if not intent.ev_hold_active and not owned_release:
            return None
        # EV charging is independent evidence. It may close a hold while the
        # dispatch picture is absent, but keeps dispatch trust false for export.
        return replace(intent, holds=())
    return replace(intent, holds=trusted_holds)


def _shutdown_hold_reason(
    trusted_holds: tuple[tuple[datetime, datetime], ...] | None, now: datetime
) -> str | None:
    """Why a clean shutdown must leave the switch Off, or ``None`` to relinquish (#426)."""
    store = execution_context().authority
    records = store.state.holds if store is not None and store.state is not None else ()
    # A pending EV release has not verified yet, so its record still holds.
    if any(h.cause == "ev" for h in records):
        return "an EV hold is recorded"
    ends = [end for _, end in trusted_holds or ()] + [
        h.trusted_end for h in records if h.cause == "dispatch" and h.trusted_end is not None
    ]
    if any(now < end for end in ends):
        return "a known dispatch hold is still ahead"
    return None


class ReconcileResult(NamedTuple):
    """One reconcile pass: its action lines, the caller's trusted hold set after it,
    and whether it relinquished control (so the next replan re-applies the plan)."""

    lines: list[str]
    trusted_holds: tuple[tuple[datetime, datetime], ...] | None
    relinquished: bool = False


async def reconcile_tick(
    settings: Settings,
    plan: ChargePlan | None,
    now: datetime,
    *,
    previous: list[str] | None = None,
    trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
    ev_hold_state: EvHoldState | None = None,
) -> ReconcileResult:
    """One per-minute power-switch reconcile pass. Never raises.

    The clock seam (#143). ``apply`` is a plan diff on the half-hourly replan
    cadence, but whether a dispatch hold is active *right now* is a function of
    the clock, and Octopus dispatch bounds are whatever HA reports — need not be
    half-hour aligned, and can open and close inside one slot. Driving the
    reconcile from here converges a sub-slot hold, a failed write, and an
    external change (the select flipped in the HA UI, a leftover automation)
    within a minute rather than at the next plan change.

    It is cheap by construction: ``reconcile_holds`` is read-first, so steady
    state is one *cached* ``GET`` of the select per minute and zero register
    writes. The cadence is not a new precedent — ``soc_monitor_tick`` and the
    writing ``guard_tick`` already run here.

    With no plan yet (boot, or ``last_plan`` cleared by a hot reload) this does
    nothing at all: no picture is not evidence that no hold is active, and
    writing ``On`` merely because a plan has not loaded would release a live
    dispatch on every restart.

    Returns the pass's action lines so the caller can pass them back as
    ``previous``: only lines that changed since the last pass are logged. A
    per-minute pass that logged unconditionally would put 1440 identical lines a
    day into the add-on log — most of them ``[SKIP] ... (already set)``, or in
    Observe a write that never happens — and bury the plan
    and guard activity an operator actually reads it for.

    ``trusted_holds`` is the caller's last trusted dispatch hold set, substituted
    when dispatch data is untrusted. EV status is read separately and cannot
    make dispatch data trusted or untrusted.

    On the HA-entity dispatch path the pass re-reads the dispatch entity (#143
    §4) — a cached ``GET`` of what BottlecapDave's integration last polled —
    so a dispatch published *and* started inside one slot holds within a
    minute rather than being missed. The holds are folded through the same
    ``_controlled_windows`` the planner uses, so an overnight dispatch stays
    cheap charge coverage, never a hold. A trusted read replaces the returned
    trusted set; a failed one falls back to it. The ``octopus_intelligent``
    path re-rates the last plan's original daytime dispatch windows each minute
    from fresh local evidence. It reads the HA dispatch entity only for its live
    state, never for planned windows; an untrusted plan keeps the existing
    last-trusted-holds behavior, and so does a plan whose Kraken read is older
    than ``octopus_dispatch_max_age_seconds`` (or has no read time at all).

    Relinquishing control (#143 §5): once the clock is past every known hold end
    and the hold data is *still* untrusted, ha-spark has no picture left to steer
    by, so it writes the device's safe state instead of reconciling — and
    returns an empty trusted set so it fires once: the one blind write is the
    bound, and any retry goes through the read-first reconcile and ``apply``,
    which never write blind (register endurance). Inside a known hold an
    untrusted read never relinquishes, a trusted one never does, and with no
    known holds (``None`` or ``()``) there is nothing to relinquish from. This is
    the bound on the reconcile's refused blind release: no counter, no tunable.
    """
    if plan is None or plan.charge_intent is None:
        return ReconcileResult([], trusted_holds)
    relinquished = False
    ev_hold_state = ev_hold_state or EvHoldState()
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            # Read the device before evidence, including re-verification of a
            # durable request whose process died between write and read-back.
            store = execution_context().authority
            if store is not None and store.state is not None:
                verified = []
                for record in store.state.holds:
                    try:
                        switch = await rest.get_state(record.device.power_switch)
                        if switch.state.strip().lower() == "off" and not record.confirmed:
                            record = record.model_copy(update={"confirmed": True})
                    except Exception:  # noqa: BLE001 - no inferred confirmation
                        pass
                    verified.append(record)
                store.save_holds(tuple(verified))
                if trusted_holds is None:
                    recorded = tuple(
                        (h.started_at, h.trusted_end) for h in store.state.holds
                        if h.cause == "dispatch" and h.confirmed and h.trusted_end is not None
                    )
                    trusted_holds = recorded or None
            fresh = plan.charge_intent
            read_lines: list[str] = []
            dispatches: tuple[DispatchSlot, ...] = ()
            live_dispatch: bool | None = None
            report_times: dict[str, datetime | None] = {}
            refresh_dispatches = (
                settings.tariff_provider != "octopus_intelligent"
                and bool(settings.dispatch_entity)
            )
            rerate_octopus_dispatches = (
                settings.tariff_provider == "octopus_intelligent" and fresh.hold_trusted
            )
            if rerate_octopus_dispatches:
                # The plan's Kraken read is the only source of these windows,
                # and the reconcile never re-polls Kraken, so the plan's trust
                # lasts only as long as that read's validated age.
                kraken = check_report_time(
                    fresh.octopus_dispatch_reported_at,
                    now=now,
                    max_age_s=settings.octopus_dispatch_max_age_seconds,
                    option="octopus_dispatch_max_age_seconds",
                )
                if kraken.validity != "fresh":
                    rerate_octopus_dispatches = False
                    fresh = replace(fresh, hold_trusted=False)
                    read_lines = [STALE_DISPATCH_LINE]
            dispatch_trusted = True
            if refresh_dispatches:
                dispatches, dispatch_trusted, live_dispatch = await read_dispatches_and_live(
                    settings, rest, report_times=report_times
                )
                if not dispatch_trusted:
                    fresh = replace(fresh, hold_trusted=False)
                    read_lines = [UNREADABLE_DISPATCH_LINE]
            elif rerate_octopus_dispatches:
                # Live state only matters when there is a window to rate.
                if settings.dispatch_entity and fresh.unrated_holds:
                    _entity_dispatches, _entity_trusted, live_dispatch = (
                        await read_dispatches_and_live(
                            settings, rest, report_times=report_times
                        )
                    )
                dispatches = tuple(
                    DispatchSlot(start=start, end=end)
                    for start, end in fresh.unrated_holds
                )
            should_rate_dispatches = (refresh_dispatches and dispatch_trusted) or (
                rerate_octopus_dispatches
            )
            evidence_read = await read_dispatch_evidence(
                settings,
                rest,
                dispatches if should_rate_dispatches else (),
                live_dispatch=live_dispatch,
                forecast_house_kw=plan.load_kwh / 24,
                report_times=report_times,
                now=now,
            )
            if refresh_dispatches and dispatch_trusted:
                live_freshness = evidence_read.freshness.get("live_dispatch")
                if live_freshness is None or live_freshness.validity != "fresh":
                    # #420: a dispatch picture whose source has not reported
                    # within its validated age is untrusted evidence — a
                    # cached HA state is not a live picture. The last trusted
                    # hold set stands and ends on its own known end; a stale
                    # read is never turned into a trusted-empty schedule.
                    dispatch_trusted = False
                    should_rate_dispatches = False
                    fresh = replace(fresh, hold_trusted=False)
                    read_lines = read_lines + [STALE_DISPATCH_LINE]
            if should_rate_dispatches:
                dispatches, _dropped = partition_dispatches(
                    dispatches, evidence_read.evidence, now
                )
                trusted_holds = _controlled_windows(
                    dispatches, fresh.window_start, fresh.window_end
                )
                fresh = replace(fresh, holds=trusted_holds, hold_trusted=True)
            await _hold_degraded_notice(
                settings, rest, evidence_read.freshness, now, dispatch_trusted=fresh.hold_trusted
            )
            ev_charging = ev_hold_status_from_read(
                evidence_read.ev_status, evidence_read.ev_status_trusted
            )
            if settings.ev_status_entity and ev_charging is None:
                read_lines.append(UNREADABLE_EV_STATUS_LINE)
            device = inverter_device(settings, rest)
            ev_hold_active = await _observe_ev_hold(
                ev_hold_state,
                ev_charging,
                evidence_read.freshness.get("ev_status"),
                now=now,
                device=device,
                battery_freshness=evidence_read.freshness.get("battery_power"),
                battery_direction_value=evidence_read.battery_direction,
            )
            fresh = replace(fresh, ev_hold_active=ev_hold_active)
            intent = hold_reconcile_intent(fresh, trusted_holds)
            if intent is None:
                lines = read_lines + [UNTRUSTED_HOLDS_LINE]
            elif (
                settings.ev_status_entity
                and ev_charging is None
                and not fresh.hold_trusted
                and trusted_holds
                and now >= max(end for _, end in trusted_holds)
            ):
                # An unreadable EV source is not evidence that charging ended.
                # Leave the switch untouched until the EV read recovers; it must
                # not turn a prior trusted EV hold into a safe-state relinquish.
                lines = read_lines + [UNTRUSTED_EV_RELINQUISH_LINE]
            elif (
                not fresh.hold_trusted
                and not intent.ev_hold_active
                and trusted_holds
                and now >= max(end for _, end in trusted_holds)
            ):
                log.warning(
                    "[WARNING] relinquishing control: past every known hold end and hold "
                    "data still untrusted; writing the inverter safe state"
                )
                relinquished = True
                trusted_holds = ()
                lines = read_lines + list(await device.write_safe_state())
            else:
                lines = read_lines + list(await device.reconcile_holds(intent, now))
            # After this pass, so a hold that is legitimately back (which
            # ends the refused episode) never reads as "ended but still held".
            await _release_refused_notice(settings, rest, now)
    except Exception:
        log.exception("Hold reconcile tick failed; will retry next minute")
        return ReconcileResult(
            [ActionLine("[FAILED] hold reconciliation failed", ActionOutcome.FAILED)],
            trusted_holds, relinquished,
        )
    for line in lines:
        if previous is not None and line in previous:
            log.debug(line)
        else:
            log.info(line)
    return ReconcileResult(lines, trusted_holds, relinquished)


def _save_fallback_authority(active: bool) -> bool:
    """Enter or leave fallback in the authority store, bumping the epoch (#422).

    The store owns *fallback active* and its causes (phase 2: the only cause
    is SoC integrity; the SoC monitor stays the sole judge of that cause and
    keeps its counter, recovery progress and confirmation flags — the store
    only mirrors its verdict). Returns whether this call changed the store;
    raises (with ``save_failed`` set, so the write gate refuses every write)
    when the durable save fails.
    """
    context = execution_context()
    store = context.authority
    if store is None:
        return False
    changed = store.save_fallback(active=active, cause=FALLBACK_CAUSE_SOC_INTEGRITY)
    if changed:
        # Work on the rest of this tick plans under the bumped epoch, so the
        # fallback apply itself is not refused as stale; anything planned
        # before the bump still is.
        assert store.state is not None
        context.planned_epoch.set(store.state.epoch)
    return changed


async def _notify_fallback_entered(settings: Settings, rest: HomeAssistantRest) -> None:
    await send_notification(
        settings, rest, "ha-spark fallback entered",
        f"SoC integrity failure: fallback charging entered "
        f"(cause: {FALLBACK_CAUSE_SOC_INTEGRITY}); the configured cheap window is "
        "programmed at the configured fallback current",
    )


async def _notify_fallback_left(settings: Settings, rest: HomeAssistantRest) -> None:
    store = execution_context().authority
    state = store.state if store is not None else None
    leaving_optimise = state is not None and (
        state.committed_mode != "optimise"
        or (state.transition is not None and state.transition.target != "optimise")
    )
    await send_notification(
        settings, rest, "ha-spark fallback left",
        "Fallback charging ownership ended: the owner is leaving Optimise"
        if leaving_optimise else
        "SoC integrity recovered: fallback charging left; normal programming resumed",
    )


async def _mirror_fallback_authority(
    settings: Settings, rest: HomeAssistantRest, monitor: SocMonitor, *, solis: bool
) -> None:
    """Mirror the monitor's fallback verdict into the authority store (#422).

    Entry: a fallback-active monitor (a status persisted by an earlier process
    reconciles into the store here, on the first tick of this version, as an
    active cause that is not confirmed — the monitor's confirmation stays
    false until this process reads back) is saved active before any fallback
    write is requested. Exit: a store left active after a failed or
    interrupted exit save is cleared here, so the store never claims a
    fallback the monitor has already left and the next entry still bumps the
    epoch and notifies once. One notice each way, only on the change.
    """
    store = execution_context().authority
    state = store.state if store is not None else None
    active = bool(
        monitor.fallback_active and solis and state is not None
        and state.committed_mode == "optimise"
        and (state.transition is None or state.transition.target == "optimise")
    )
    try:
        changed = _save_fallback_authority(active)
    except (OSError, ValueError):
        log.warning("Authority save failed; fallback state not persisted; writes refused")
        return
    if not changed:
        return
    if active:
        log.info(
            "SoC integrity: fallback entered in the authority store (cause %s)",
            FALLBACK_CAUSE_SOC_INTEGRITY,
        )
        await _notify_fallback_entered(settings, rest)
    else:
        log.info("SoC integrity: fallback exit persisted in the authority store")
        await _notify_fallback_left(settings, rest)


def _fallback_verdict_line(action_lines: list[ActionLine | str]) -> ActionLine | str:
    """The line that speaks for a fallback attempt's resident outcome."""
    return next(
        (
            line
            for line in reversed(action_lines)
            if isinstance(line, ActionLine)
            or line.startswith(
                (
                    # Legacy fallback boundary only; production drivers
                    # emit typed lines. Never used for normal apply.
                    "[FALLBACK]",
                    "[FAILED]",
                    "[BLOCKED]",
                    "[SIMULATE]",
                    "[SKIP]",
                    "[OFF]",
                    "[OBSERVE]",
                )
            )
        ),
        ActionLine(
            "[FAILED] Solis fallback produced no confirmation action",
            ActionOutcome.FAILED,
        ),
    )


def _log_action_lines(action_lines: list[ActionLine | str]) -> None:
    for line in action_lines:
        if legacy_action_line(line).outcome is ActionOutcome.FAILED:
            log.warning(line)
        else:
            log.info(line)


async def soc_monitor_tick(settings: Settings, monitor: SocMonitor) -> SocMeasurement | None:
    """One per-minute SoC observation: check, count once, publish. Never raises.

    The daemon loop's sole SoC observation cadence (#114): the returned
    measurement is the one every consumer this tick (planning, device
    application, guard work) must reuse. ``None`` when no ``soc_entity`` is
    configured (nothing to observe) or when the tick itself failed — callers
    then fall back to their own observation path and the next minute retries.
    """
    if not settings.soc_entity:
        return None
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            measurement = await observe_soc(settings, rest, monitor=monitor)
            snapshot = monitor.record(
                measurement,
                failure_threshold=settings.soc_failure_threshold,
                recovery_duration=timedelta(minutes=settings.soc_recovery_minutes),
                recover_without_fallback=any(
                    device.type == "inverter" and device.driver == "alphaess"
                    for device in settings.devices
                ),
            )
            inverter = next(
                (device for device in settings.devices if device.type == "inverter"), None
            )
            await _mirror_fallback_authority(
                settings, rest, monitor, solis=inverter is not None and inverter.driver == "solis"
            )
            if (
                inverter is not None
                and inverter.driver == "solis"
                and settings.solis_fallback_current_a is not None
                and _real_write_mode(settings)
            ):
                requested = monitor.request_fallback()
                if requested is not None:
                    entered = False
                    try:
                        # #422: the store (active + cause + bumped epoch) is
                        # saved BEFORE the fallback write is requested from the
                        # device; a failed save leaves ``save_failed`` set and
                        # the gate refuses the write.
                        entered = _save_fallback_authority(True)
                    except (OSError, ValueError):
                        log.warning(
                            "Authority save failed; fallback entry not persisted; "
                            "hardware writes refused"
                        )
                    action_lines: list[str]
                    try:
                        device = inverter_device(settings, rest)
                        if isinstance(device, SolisDevice):
                            action_lines = list(await device.apply_fallback(measurement))
                        else:
                            log.error(
                                "Solis fallback skipped: inverter factory returned %s",
                                type(device).__name__,
                            )
                            action_lines = [
                                ActionLine(
                                    "[SKIP] Solis fallback skipped: inverter factory returned "
                                    f"non-Solis device ({type(device).__name__})",
                                    ActionOutcome.SIMULATED,
                                )
                            ]
                    except Exception:
                        log.exception("Solis fallback attempt failed; will retry next minute")
                        action_lines = [
                            ActionLine(
                                "[FAILED] Solis fallback attempt raised; unconfirmed",
                                ActionOutcome.FAILED,
                            )
                        ]
                    action_line = _fallback_verdict_line(action_lines)
                    snapshot = monitor.complete_fallback(action_line=action_line)
                    _log_action_lines(action_lines)
                    if entered:
                        log.info(
                            "SoC integrity: fallback entered in the authority store "
                            "(cause %s)",
                            FALLBACK_CAUSE_SOC_INTEGRITY,
                        )
                        await _notify_fallback_entered(settings, rest)
            await publish_soc_integrity(rest, snapshot, settings)
            return measurement
    except Exception:
        log.exception("SoC monitor tick failed; will retry next minute")
        return None


async def _reapply_fallback(
    settings: Settings, monitor: SocMonitor, measurement: SocMeasurement | None
) -> bool:
    """#423: re-apply the fallback now and publish its verdict. Never raises.

    Only the recovery path calls this: ``request_fallback`` returns ``None``
    while recovery is ready, so the per-minute path cannot re-program a
    fallback a failed recovered apply may have disturbed. This is the same
    device action, gate, schedule validation and read-back as entry; the
    store is already active, so there is no epoch bump and no notice.
    Returns whether an attempt was made (Optimise, Solis, a configured
    fallback current): the verdict, verified or failed, is recorded and
    published either way, and a failed attempt is retried by the next call.
    """
    inverter = next((device for device in settings.devices if device.type == "inverter"), None)
    if (
        measurement is None
        or inverter is None
        or inverter.driver != "solis"
        or settings.solis_fallback_current_a is None
        or not _real_write_mode(settings)
    ):
        return False
    snapshot: SocMonitorSnapshot | None = None
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            action_lines: list[ActionLine | str]
            device = inverter_device(settings, rest)
            if isinstance(device, SolisDevice):
                action_lines = list(await device.apply_fallback(measurement))
            else:
                log.error(
                    "Solis fallback re-apply skipped: inverter factory returned %s",
                    type(device).__name__,
                )
                action_lines = [
                    ActionLine(
                        "[SKIP] Solis fallback re-apply skipped: inverter factory "
                        f"returned non-Solis device ({type(device).__name__})",
                        ActionOutcome.SIMULATED,
                    )
                ]
            snapshot = monitor.complete_fallback(action_line=_fallback_verdict_line(action_lines))
            _log_action_lines(action_lines)
            await publish_soc_integrity(rest, snapshot, settings)
            return True
    except Exception:
        log.exception("Solis fallback re-apply failed; will retry next minute")
    if snapshot is None:
        # The attempt raised before recording a verdict: keep the failure
        # visible and the fallback unconfirmed until the next retry.
        try:
            snapshot = monitor.complete_fallback(
                action_line=ActionLine(
                    "[FAILED] Solis fallback re-apply raised; unconfirmed",
                    ActionOutcome.FAILED,
                )
            )
        except Exception:
            log.exception("Recording the failed fallback re-apply failed")
            return True
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            await publish_soc_integrity(rest, snapshot, settings)
    except Exception:
        log.exception("Publishing the fallback re-apply verdict failed")
    return True


def recovery_verified(apply_lines: tuple[str, ...]) -> bool:
    """Whether a recovered normal apply may end the fallback (#117).

    Like the resident baseline, this needs explicit verification for every
    action. A read-back warning or an empty result is never verification.
    """
    return bool(apply_lines) and all(is_verified(line) for line in apply_lines)


async def finish_recovery(
    settings: Settings,
    monitor: SocMonitor,
    apply_lines: tuple[str, ...] | None,
    measurement: SocMeasurement | None = None,
) -> bool:
    """Leave fallback after a verified recovered apply, else keep it truthfully.

    ``apply_lines`` is ``None`` when the recovery run raised; ``measurement``
    is this tick's observation, carried by the same-tick fallback re-apply.
    A failed apply may have changed the resident program, so a verified
    fallback loses its confirmation and is re-applied and re-verified on this
    same tick (#423), with its verdict published; the store's fallback stays
    active (no epoch bump, no notice). Returns whether the recovery backoff
    started: the next recovery attempt then waits five minutes, while an
    unconfirmed re-apply is retried every minute of that wait by the loop.
    Sites without a Solis fallback (AlphaESS threshold block, or no charging
    authority) keep today's behaviour: keep the block, retry next minute.
    """
    preview = (
        apply_lines is not None
        and not _real_write_mode(settings)
        and _preview_accepted(apply_lines)
    )
    if apply_lines is not None and (recovery_verified(apply_lines) or preview):
        line = next(
            (line for line in reversed(apply_lines) if is_verified(line)),
            apply_lines[-1],
        )
        if monitor.recovery_blocked and preview and not is_verified(line):
            action = f"[RECOVERED] plan computed without hardware programming: {line}"
        else:
            action = f"[RECOVERED] normal programming resumed: {line}"
        log.info(action)
        snapshot = monitor.complete_recovery(action_line=action)
        left = False
        try:
            # #422: the store's fallback active + cause are cleared and the
            # epoch bumped only here, after the recovered plan applied and read
            # back (today's exit point). A failed save is repaired by the next
            # tick's mirror.
            left = _save_fallback_authority(False)
        except (OSError, ValueError):
            log.warning("Authority save failed; fallback exit not persisted; writes refused")
    else:
        retained = "SoC integrity block" if monitor.recovery_blocked else "fallback"
        monitor.invalidate_fallback_confirmation()
        if await _reapply_fallback(settings, monitor, measurement):
            log.warning(
                "[FAILED] recovered normal program not verified; %s kept and "
                "re-applied on this tick; next recovery attempt in five minutes",
                retained,
            )
            return True
        log.warning(
            "[FAILED] recovered normal program not verified; %s kept, retrying next minute",
            retained,
        )
        return False
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            await publish_soc_integrity(rest, snapshot, settings)
            if left:
                log.info("SoC integrity: fallback exit persisted in the authority store")
                await _notify_fallback_left(settings, rest)
    except Exception:
        log.exception("Publishing the SoC recovery transition failed")
    return False


async def _planned_rate_w(settings: Settings, plan: ChargePlan) -> float | None:
    """The plan's charge rate (W) for the active charger, or None if unset.

    ``inverter_device``/``planned_rate_w`` perform no I/O; the rest client just
    satisfies the constructor and is closed straight away.
    """
    if plan.charge_intent is None:
        return None
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        return inverter_device(settings, rest).planned_rate_w(plan.charge_intent)


async def _charger_supports_live_rate(settings: Settings) -> bool:
    """Whether the configured inverter exposes a settable charge rate (no I/O)."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        return Capability.CHARGE_RATE in inverter_device(settings, rest).capabilities


async def run_forever(settings: Settings, *, poll_seconds: int = 60) -> None:
    """Claim the local executor before starting any services or device work."""
    channel = DaemonChannel(settings)
    try:
        await _run_forever(settings, channel, poll_seconds=poll_seconds)
    finally:
        context = execution_context()
        if channel.authority is not None and context.authority is channel.authority:
            context.authority = None
            context.planned_epoch.set(None)
        await channel.__aexit__(None, None, None)


async def _accept_mode(
    settings: Settings, channel: DaemonChannel, pending: Pending,
    measurement: SocMeasurement | None, now: datetime, *, soc_programming_held: bool,
) -> Reply:
    store = channel.authority
    assert store is not None
    request = pending.request
    if request.target is None:
        return Reply(error="invalid mode request")
    known = store.known and not store.save_failed
    if known and request.expected_epoch is None:
        return Reply(error="stale_epoch", text="authority known; use mode set observe")
    report = PreconditionReport()
    if (
        known and store.state is not None
        and request.expected_epoch == store.state.epoch and store.state.transition is None
        and (request.target == "optimise" or (
            request.target == "protect" and store.state.committed_mode == "observe"
        ))
    ):
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout,
        ) as rest:
            report = await check_preconditions(
                settings, rest, measurement, soc_programming_held=soc_programming_held,
            )
    kind: RequesterKind = "ingress" if pending.ingress_user is not None else "cli"
    decision = propose_transition(
        store.state if known else None,
        TransitionRequest(target=request.target, expected_epoch=request.expected_epoch,
                          requester_kind=kind, requester_id=pending.ingress_user or "cli"),
        report, now=now,
    )
    if decision.refusal:
        return Reply(error=decision.refusal, text="; ".join(decision.reasons),
                     reasons=list(decision.reasons))
    old_mode = store.state.committed_mode if known and store.state else "authority_unknown"
    if decision.confirm_observe:
        store.confirm_observe(kind)
    elif decision.state is store.state:
        return Reply(authority=channel.authority_status())
    else:
        assert decision.state is not None
        store.save(decision.state)
    assert store.state is not None
    log.info("Operating mode requester=%s old=%s target=%s epoch=%d",
             pending.ingress_user or "cli", old_mode, request.target, store.state.epoch)
    return Reply(authority=channel.authority_status())


async def _mode_lifecycle(
    settings: Settings, channel: DaemonChannel, pending: Pending | None,
    measurement: SocMeasurement | None, now: datetime, plan: ChargePlan | None,
    ev_hold_state: EvHoldState, *, soc_programming_held: bool,
) -> None:
    store = channel.authority
    assert store is not None
    context = execution_context()
    async with AsyncExitStack() as stack:
        config = next((d for d in settings.devices if d.type == "inverter"), None)
        if config is not None:
            await stack.enter_async_context(context.coordinator_for(config, settings).transition())
        if pending is not None:
            try:
                reply = await _accept_mode(
                    settings, channel, pending, measurement, now,
                    soc_programming_held=soc_programming_held,
                )
            except (OSError, ValueError):
                reply = Reply(error="authority persistence failed")
            except Exception:
                reply = Reply(error="mode request failed")
            pending.future.set_result(reply)
        if not store.known or store.save_failed:
            return
        while store.state is not None and store.state.transition is not None:
            state = store.state
            transition = state.transition
            assert transition is not None
            step = transition.steps[0] if transition.steps else ""
            lines: list[str] = []
            try:
                if transition.target == "observe":
                    remapped = [h for h in state.holds
                                if held_identity_remapped(h.device, settings)]
                    if remapped:
                        # Drop obsolete ownership before any charging handover:
                        # it cannot authorise the replacement mapping.
                        store.save_holds(tuple(h for h in state.holds if h not in remapped))
                        log.warning("Dropped %d hold(s) on a device no longer configured; "
                                    "its power switch may still be Off", len(remapped))
                        async with HomeAssistantRest(
                            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout,
                        ) as rest:
                            await send_notification(
                                settings, rest, "ha-spark hold dropped",
                                "Switching to Observe dropped a hold on an inverter that is "
                                "no longer configured. Its power switch may still be Off; "
                                "check it and turn it On yourself if it should run.",
                            )
                if step == "verify_fresh_plan":
                    # The normal tick runs the fresh plan and protection every
                    # minute inside this handover, even after partial failure.
                    return
                with context.after_authority_save(), context.handover(step):
                    if step == "evaluate_protection":
                        # The reconcile reads the switch, and the resident window
                        # for the overnight exception, with fresh evidence.
                        if plan is None:
                            unknown = SocMeasurement(SocStatus.UNAVAILABLE, now)
                            plan = ChargePlan(
                                soc=unknown, capacity_kwh=0, solar_kwh=0, effective_solar_kwh=0,
                                load_kwh=0, cheap_covered_kwh=0, usable_now_kwh=0, deficit_kwh=0,
                                buffer_pct=0, required_kwh=0, target_soc=0, window_hours=0,
                                ev_charging=False, ha_template_needed=None,
                                charge_intent=ChargeIntent(
                                    0, unknown, parse_time(settings.charge_window_start),
                                    parse_time(settings.charge_window_end), hold_trusted=False,
                                ),
                            )
                        result = await reconcile_tick(settings, plan, now,
                                                      ev_hold_state=ev_hold_state)
                        lines = result.lines
                        # Evaluated, and any hold it needs is verified: an
                        # unconfirmed request (a write not read back) is not.
                        if store.state is not None and any(
                            not h.confirmed for h in store.state.holds
                        ):
                            lines.append("[FAILED] hold request not confirmed by read-back")
                    elif step == "release_holds":
                        if store.state is not None and store.state.holds:
                            async with HomeAssistantRest(
                                settings.ha_rest_url, settings.auth_token,
                                timeout=settings.ha_timeout,
                            ) as rest:
                                device = inverter_device(settings, rest)
                                if isinstance(device, SolisDevice):
                                    lines = list(await device.release_holds())
                                if store.state is not None and store.state.holds:
                                    # Observe commits only once no hold of ours remains,
                                    # e.g. one on an inverter the driven device isn't.
                                    lines.append("[FAILED] hold records remain unreleased")
                    elif step in ("cancel_export", "write_fallback"):
                        async with HomeAssistantRest(
                            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout,
                        ) as rest:
                            device = inverter_device(settings, rest)
                            if not isinstance(device, SolisDevice):
                                lines = ["[FAILED] no validated charging handover"]
                            elif step == "cancel_export":
                                lines = list(await device.cancel_export())
                            else:
                                lines = list(await device.write_fallback())
                    elif step != "commit_observe":
                        lines = ["[FAILED] unsupported handover step"]
                if step in ("cancel_export", "write_fallback") and not lines:
                    lines = ["[FAILED] charging handover not verified"]
                failures = tuple(str(line) for line in lines if (
                    not is_verified(line) if step in (
                        "release_holds", "cancel_export", "write_fallback",
                    )
                    else legacy_action_line(line).outcome in (
                        ActionOutcome.REFUSED, ActionOutcome.FAILED, ActionOutcome.BLOCKED,
                    )
                ))
            except Exception:
                failures = ("[FAILED] handover step failed",)
            if step != "evaluate_protection":  # reconcile_tick logs its own lines
                _log_action_lines([*lines, *(f for f in failures if f not in lines)])
            if store.state is None:
                return
            current = store.state
            if failures:
                store.save(current.model_copy(update={
                    "transition": transition.model_copy(update={"failing_actions": failures}),
                }))
                return
            remaining = transition.steps[1:]
            updates: dict[str, object] = {
                "transition": transition.model_copy(update={"steps": remaining,
                                                            "failing_actions": ()})
                if remaining else None,
            }
            if not remaining and current.committed_mode != transition.target:
                updates.update(committed_mode=transition.target, epoch=current.epoch + 1)
                if transition.target != "optimise":
                    updates["fallback"] = FallbackState()
            store.save(current.model_copy(update=updates))
            if current.fallback.active and "fallback" in updates:
                async with HomeAssistantRest(
                    settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout,
                ) as rest:
                    await _notify_fallback_left(settings, rest)


async def _run_forever(
    settings: Settings, channel: DaemonChannel, *, poll_seconds: int
) -> None:
    """Loop: recompute and apply the plan once per local half-hour slot.

    Serves the add-on HTTP API (behind ingress) sharing an :class:`AppState`
    with this loop: ``POST /api/config`` commits a settings pair and the loop
    picks up the active snapshot on its next tick (hot reload, no restart).
    """
    await channel.__aenter__()
    service = SettingsService(OPTIONS_PATH, settings=settings)
    settings = service.load(writable=True)
    warn_deprecated_notify_target(settings)
    state = AppState(settings=settings, options_path=OPTIONS_PATH, executor=channel,
                     settings_service=service)
    # Add-on mode: listen on all interfaces (the ingress proxy connects over the
    # hassio network) but answer only the proxy's address. Standalone/dev has no
    # ingress proxy, so bind to loopback and let the OS keep the network out.
    if settings.is_standalone:
        server = make_server(build_app(state), "127.0.0.1", INGRESS_PORT)
    else:
        server = make_server(
            build_app(state, ingress_peer=INGRESS_PROXY_IP),
            "0.0.0.0",  # noqa: S104 - peer-restricted to the ingress proxy
            INGRESS_PORT,
        )
    authority = AuthorityStore(settings)
    try:
        authority.start_process()
    except (OSError, ValueError):
        authority.save_failed = True
        log.warning("Authority persistence failed; hardware writes refused")
    context = execution_context()
    context.authority = authority
    channel.authority = authority
    faults: set[str] = set()
    serve_task: asyncio.Task[None] | None = None
    try:
        serve_task = await serve_in_background(server)
        log.info("HTTP API listening on :%d (ingress)", INGRESS_PORT)
    except Exception:
        log.exception("HTTP API failed to start; continuing without it")

    port_server: uvicorn.Server | None = None
    port_task: asyncio.Task[None] | None = None
    if settings.agent_surface == "on" and settings.agent_expose_port:
        token = resolve_token(settings)
        port_server = make_server(
            build_app(state, require_token=True, token=token), "0.0.0.0", AGENT_PORT  # noqa: S104
        )
        try:
            port_task = await serve_in_background(port_server)
            log.info("Agent surface listening on :%d (token-protected)", AGENT_PORT)
        except Exception:
            log.exception("Agent port failed to start; continuing")

    # Guard only inverters with a live charge rate (AlphaESS self-regulates);
    # re-checked only when the inverter or grid entity changes (it needs a client).
    guard_cfg = (settings.grid_power_entity, settings.inverter)
    guard_enabled = bool(settings.grid_power_entity) and await _charger_supports_live_rate(settings)
    if guard_enabled:
        log.info(
            "Supply guard enabled: watching %s (limit %g A)",
            settings.grid_power_entity,
            settings.supply_max_current_a,
        )
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            await republish_last(rest, settings)
    except Exception:
        log.exception("Republishing last known states failed")
    last_run_slot: datetime | None = None
    last_plan: ChargePlan | None = None
    last_revision: PlanRevision | None = None
    last_readiness_slot: datetime | None = None
    readiness_evaluated: date | None = None
    readiness_task: asyncio.Task[None] | None = None

    async def record_readiness(tick_settings: Settings, tick_now: datetime) -> None:
        nonlocal readiness_evaluated
        try:
            await record_closed_days(tick_settings, tick_now)
            readiness_evaluated = await readiness_tick(
                tick_settings, tick_now, readiness_evaluated, authority,
            )
        except Exception:
            log.warning("Readiness background recording failed")

    # The plan known resident on the device: the baseline `needs_apply`
    # compares against, `None` when unknown after a failed apply or run (#168).
    # `last_plan` is the latest computed plan, applied or skipped.
    last_applied_plan: ChargePlan | None = None
    # The previous reconcile pass's lines, so a per-minute pass logs only what
    # changed rather than the same line 1440 times a day.
    last_reconcile_lines: list[str] = []
    # The hold set from the last trusted dispatch read (#143 §3): a trusted plan,
    # or on the HA-entity path the per-minute reconcile's own read (§4). A failed
    # read's empty holds mean "unreadable", so the reconcile evaluates the clock
    # against this instead. `None` until a trusted read exists.
    last_trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None
    ev_hold_state = EvHoldState()
    # Set when a reconcile pass relinquished control (#143 §5): the safe state
    # overwrote Slot 1, so the next replan must apply even an unchanged plan.
    reapply = False
    last_settings = settings
    target_w: float | None = None
    last_signal_at: datetime | None = None
    monitor = SocMonitor.load(settings)
    # #423: loop-local recovery backoff — no recovery attempt before this
    # timestamp. Not persisted: like recovery progress, downtime is not
    # evidence; a restart already requires a fresh observation and read-back.
    recovery_retry_not_before: datetime | None = None
    clock_sync = DstClockSync()
    pending = None
    startup_reconciled = False
    try:
        while True:
            settings = state.settings  # hot-reloaded by POST /api/config
            # Capture with the settings snapshot, before any I/O can accept a save.
            context.planned_generation.set(context.config_generation)
            current_faults = set()
            if not authority.known:
                current_faults.add("authority unknown")
            if authority.save_failed:
                current_faults.add("authority save failure")
                try:
                    authority.retry_pending()
                except (OSError, ValueError):
                    log.warning("Authority save retry failed; hardware writes refused")
            try:
                async with HomeAssistantRest(
                    settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
                ) as rest:
                    for fault in current_faults - faults:
                        log.warning("%s; hardware writes refused", fault)
                        await send_notification(settings, rest, "ha-spark authority fault", fault)
                    await notify_remapped_holds(settings, rest)
                    await publish_operating_mode(rest, authority)
            except Exception:
                log.warning("Authority publication failed")
            faults = current_faults
            context.planned_epoch.set(authority.state.epoch if authority.state else None)
            if settings is not last_settings:
                if actuation_settings_changed(last_settings, settings):
                    monitor.invalidate_fallback_confirmation()
                # Never reuse a previous plan's command across a hot reload.
                last_plan = None
                last_revision = None
                last_applied_plan = None
                last_trusted_holds = None
                target_w = None
                last_settings = settings
            tz = load_timezone(settings.timezone)
            window_start = parse_time(settings.charge_window_start)
            window_end = parse_time(settings.charge_window_end)
            if (settings.grid_power_entity, settings.inverter) != guard_cfg:
                guard_cfg = (settings.grid_power_entity, settings.inverter)
                guard_enabled = bool(settings.grid_power_entity) and (
                    await _charger_supports_live_rate(settings)
                )
            now = datetime.now(tz)
            if not startup_reconciled:
                startup_reconciled = True
                # No planner or fallback write may precede restart reconciliation.
                if authority.state is not None and (
                    authority.state.holds or authority.state.committed_mode != "observe"
                ):
                    if not authority.state.holds:
                        async with HomeAssistantRest(
                            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
                        ) as rest:
                            for config in settings.devices:
                                entity = config.entities.get("power_switch", "")
                                if config.type == "inverter" and entity:
                                    try:
                                        await rest.get_state(entity)
                                    except Exception:  # noqa: BLE001 - direction decides later
                                        pass
                    unknown_soc = SocMeasurement(SocStatus.UNAVAILABLE, now)
                    startup_plan = ChargePlan(
                        soc=unknown_soc, capacity_kwh=0, solar_kwh=0, effective_solar_kwh=0,
                        load_kwh=0, cheap_covered_kwh=0, usable_now_kwh=0, deficit_kwh=0,
                        buffer_pct=0, required_kwh=0, target_soc=0, window_hours=0,
                        ev_charging=False, ha_template_needed=None,
                        charge_intent=ChargeIntent(
                            0, unknown_soc, window_start, window_end, hold_trusted=False,
                        ),
                    )
                    startup_result = await reconcile_tick(
                        settings, startup_plan, now, ev_hold_state=ev_hold_state,
                    )
                    last_reconcile_lines, last_trusted_holds = startup_result[:2]
            # Sole SoC observation cadence (#114): one checked measurement per
            # minute in every operating state, reused by everything below.
            measurement = await soc_monitor_tick(settings, monitor)
            solis_fallback_active = monitor.fallback_active and any(
                device.type == "inverter" and device.driver == "solis"
                for device in settings.devices
            )
            alphaess_recovery_blocked = any(
                device.type == "inverter" and device.driver == "alphaess"
                for device in settings.devices
            ) and monitor.recovery_blocked
            fallback_confirmed = solis_fallback_active and monitor.fallback_confirmed
            # Recovery (#117): a fresh plan from this tick's measurement, applied
            # every minute until verified; the fallback stays effective till then.
            recovering = (solis_fallback_active or alphaess_recovery_blocked) and (
                monitor.recovery_ready
                and (solis_fallback_active or (measurement is not None and measurement.ok))
            )
            # #423: a failed recovered apply re-applied and re-verified the
            # fallback on the same tick; the next recovery attempt waits five
            # minutes instead of churning the resident program every minute.
            # The backoff belongs to one recovery attempt: it lapses as soon as
            # recovery is no longer ready (a new SoC failure reset progress, or
            # the fallback left), never blocking a later episode.
            if not monitor.recovery_ready:
                recovery_retry_not_before = None
            elif (
                recovering
                and solis_fallback_active
                and recovery_retry_not_before is not None
                and now < recovery_retry_not_before
            ):
                recovering = False
                # `request_fallback` returns None while recovery is ready, so
                # the scheduler itself retries an unconfirmed fallback
                # re-apply every minute of the backoff, through the same
                # gated, read-back-verified path. Only in Optimise.
                if not monitor.fallback_confirmed:
                    await _reapply_fallback(settings, monitor, measurement)
                    fallback_confirmed = solis_fallback_active and monitor.fallback_confirmed
            # Set once `run_once` has *returned*, because it makes its own
            # reconcile pass with the plan it just computed. A run that raises
            # after that pass (`apply` or `publish_plan`) therefore falls back
            # below as well: one extra read-first pass against `last_plan`,
            # which is what every tick in that failure window steers by anyway,
            # so it costs a duplicate read rather than new stale exposure.
            replanned = False
            pending = channel.next_request()
            mode_request = pending if pending is not None and pending.request.op == "mode" else None
            if mode_request is not None or (
                authority.state is not None and authority.state.transition is not None
            ):
                try:
                    await _mode_lifecycle(
                        settings, channel, mode_request, measurement, now, last_plan, ev_hold_state,
                        soc_programming_held=monitor.fallback_active or monitor.recovery_blocked,
                    )
                except (OSError, ValueError):
                    log.warning("Authority handover save failed; hardware writes refused")
                context.planned_epoch.set(authority.state.epoch if authority.state else None)
                if mode_request is not None:
                    pending = None
            if pending is not None and pending.request.op == "sync_clock":
                try:
                    async with HomeAssistantRest(
                        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
                    ) as rest:
                        device = inverter_device(settings, rest)
                        if isinstance(device, SolisDevice):
                            outcome, clock_lines = await device.sync_clock()
                            reply = Reply(
                                lines=action_lines(clock_lines), synced=outcome == "synced"
                            )
                        else:
                            reply = Reply(error="sync-clock is Solis-only")
                    pending.future.set_result(reply)
                except Exception:
                    last_applied_plan = None
                    pending.future.set_result(Reply(error="daemon clock request failed"))
                pending = None
            transition = authority.state.transition if authority.state is not None else None
            activating = transition is not None and transition.steps == ("verify_fresh_plan",)
            if activating:
                # A preview-accepted Observe/Protect plan is not resident hardware.
                last_applied_plan = None
            if pending is not None or recovering or activating or should_run(now, last_run_slot):
                try:
                    # Recovery never compares against a baseline: the resident
                    # program is the fallback, not any earlier plan.
                    previous_plan = None if reapply or recovering else last_applied_plan
                    async with AsyncExitStack() as activation:
                        if activating:
                            config = next(d for d in settings.devices if d.type == "inverter")
                            await activation.enter_async_context(
                                context.coordinator_for(config, settings).transition()
                            )
                            activation.enter_context(context.after_authority_save())
                            activation.enter_context(context.handover("verify_fresh_plan"))
                        result = await run_once(
                            settings,
                            soc=measurement,
                            previous_plan=previous_plan,
                            trusted_holds=last_trusted_holds,
                            ev_hold_state=ev_hold_state,
                            # A failed first tick after restart cannot delegate the
                            # observation to run_once and lift a restored block.
                            fallback_active=(
                                solis_fallback_active
                                or alphaess_recovery_blocked
                                or monitor.awaiting_fresh_observation
                            )
                            and not recovering,
                        )
                        if activating and authority.state is not None and transition is not None:
                            # The plan's own actions must all verify, as recovery
                            # demands; around them, only a typed failure or a hold
                            # request that never read back holds activation open
                            # (an owner-Off switch or untrusted-holds note does not).
                            failures = tuple(dict.fromkeys(
                                [str(line) for line in result.apply_lines
                                 if not is_verified(line)]
                                + [str(line) for line in result.action_lines
                                   if legacy_action_line(line).outcome in (
                                       ActionOutcome.REFUSED, ActionOutcome.FAILED,
                                   )]
                                + (["[FAILED] hold request not confirmed by read-back"]
                                   if any(not h.confirmed for h in authority.state.holds)
                                   else [])
                            ))
                            if not recovery_verified(result.apply_lines) and not failures:
                                failures = ("[FAILED] fresh plan apply not verified",)
                            authority.save(authority.state.model_copy(update={
                                "transition": transition.model_copy(update={
                                    "failing_actions": failures,
                                }) if failures else None,
                            }))
                            if not failures:
                                log.warning(
                                    "Optimise active: disable any pre-existing automations or "
                                    "manual schedules that write the same devices"
                                )
                    plan, last_applied_plan = result.plan, result.applied_plan
                    if recovering:
                        backed_off = await finish_recovery(
                            settings, monitor, result.apply_lines, measurement
                        )
                        solis_fallback_active = monitor.fallback_active
                        fallback_confirmed = solis_fallback_active and monitor.fallback_confirmed
                        if backed_off:
                            recovery_retry_not_before = now + RECOVERY_RETRY_DELAY
                    replanned = True
                    state.set_plan(plan)
                    last_run_slot = _slot_start(now)
                    last_plan = plan
                    last_revision = result.revision
                    reapply = False
                    if plan.charge_intent is not None and plan.charge_intent.hold_trusted:
                        last_trusted_holds = plan.charge_intent.holds
                    if plan.soc.ok:
                        target_w = await _planned_rate_w(settings, plan)
                    else:
                        # The plan was blocked on an untrusted SoC: its rate was
                        # sized from soc_now == 0 and must not become the guard's
                        # restore target. The guard adopts the live setpoint
                        # instead (reductions only) until a plan is computed from
                        # a trusted measurement (#117 recomputes on recovery).
                        target_w = None
                    if pending is not None:
                        pending.future.set_result(Reply(
                            text=result.text,
                            lines=action_lines(result.action_lines),
                            error=(
                                "device action refused"
                                if any(
                                    legacy_action_line(line).outcome is ActionOutcome.REFUSED
                                    for line in result.action_lines
                                ) else None
                            ),
                            plan=[{"entity_id": eid, "state": value, "attributes": attrs}
                                  for eid, value, attrs in plan_to_payload(plan, settings)],
                        ))
                except Exception:
                    if pending is not None and not pending.future.done():
                        pending.future.set_result(Reply(error="daemon plan request failed"))
                    # It may have raised mid-apply: the device state is unknown,
                    # so the retry must not skip an unchanged plan (#168).
                    last_applied_plan = None
                    if activating and authority.state is not None and transition is not None:
                        try:
                            authority.save(authority.state.model_copy(update={
                                "transition": transition.model_copy(update={
                                    "failing_actions": ("[FAILED] fresh plan run failed",),
                                }),
                            }))
                        except (OSError, ValueError):
                            log.warning("Authority activation save failed; hardware writes refused")
                    if pending is not None:
                        log.error("Daemon plan request failed; will retry next tick")
                    else:
                        log.exception("Scheduled plan run failed; will retry next tick")
                    if recovering:
                        backed_off = await finish_recovery(settings, monitor, None, measurement)
                        solis_fallback_active = monitor.fallback_active
                        fallback_confirmed = solis_fallback_active and monitor.fallback_confirmed
                        if backed_off:
                            recovery_retry_not_before = now + RECOVERY_RETRY_DELAY
            # The clock cadence (#143), independent of `setpoint_changed`: the
            # power switch converges within a minute, not at the next plan
            # change. Skipped when `run_once` returned, having made its own pass
            # with the freshly computed plan; reconciling as well would drive the
            # switch to the *stale* plan's state and then immediately back, two
            # writes and a momentarily wrong whole-inverter enable whenever a
            # dispatch is announced or cancelled between slots. A replan that
            # raised made no such pass, and `last_run_slot` does not advance
            # until one succeeds, so every later tick is a replan tick too:
            # without this fallback nothing would reconcile for as long as the
            # failures last, and a hold ending meanwhile would leave the switch
            # `Off` — the house on grid import — though `last_plan` still holds
            # its known end time (#147).
            if not replanned:
                with context.handover("verify_fresh_plan" if activating else ""):
                    reconciled = await reconcile_tick(
                        settings,
                        last_plan,
                        now,
                        previous=last_reconcile_lines,
                        trusted_holds=last_trusted_holds,
                        ev_hold_state=ev_hold_state,
                    )
                last_reconcile_lines, last_trusted_holds = reconciled[:2]
                reapply = reapply or reconciled.relinquished
            if slot_start(now) != last_readiness_slot:
                last_readiness_slot = slot_start(now)
                await record_slot(
                    settings.db_path, now, last_revision,
                    authority.state.committed_mode
                    if authority.known and authority.state is not None else "authority_unknown",
                    measurement,
                    ev_hold_active=ev_hold_state.active,
                )
            if guard_enabled and in_window(now.time(), window_start, window_end):
                try:
                    # While activating, a fresh plan may already be charging: the
                    # guard's reductions run under the activation's charging grant.
                    with context.handover("verify_fresh_plan" if activating else ""):
                        target_w = await guard_tick(
                            settings,
                            target_w,
                            soc=measurement,
                            fallback_active=(
                                solis_fallback_active or monitor.awaiting_fresh_observation
                            ),
                            fallback_confirmed=fallback_confirmed,
                        )
                except Exception:
                    log.exception("Supply guard tick failed; will retry next tick")
                await record_guard(
                    settings, now, last_revision,
                    authority.state.committed_mode
                    if authority.known and authority.state is not None else "authority_unknown",
                )
            # Recorder and notice I/O must never delay the next protection tick.
            if readiness_task is None or readiness_task.done():
                readiness_task = asyncio.create_task(record_readiness(settings, now))
            if last_signal_at is None or now - last_signal_at >= SIGNAL_SAMPLE_INTERVAL:
                try:
                    await sample_signals(settings, now)
                    last_signal_at = now
                except Exception:
                    log.exception("Signal sampling failed; will retry next tick")
            if settings.inverter_clock_dst_sync and clock_sync.due(now) is not None:
                try:
                    async with HomeAssistantRest(
                        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
                    ) as rest:
                        await clock_sync.run(settings, rest, now)
                except Exception:
                    log.exception("Clock-change sync failed; will retry next tick")
            if settings.v2l_power_entity:
                try:
                    await run_v2l_tick(settings, now)
                except Exception:
                    log.exception("V2L tick failed; will retry next tick")
            await asyncio.sleep(poll_seconds)
    finally:
        if readiness_task is not None:
            readiness_task.cancel()
            await asyncio.gather(readiness_task, return_exceptions=True)
        state.executor = None
        if pending is not None and not pending.future.done():
            pending.future.set_result(Reply(error="daemon stopped during request"))
        if serve_task is not None:
            await stop_server(server, serve_task)
        if port_server is not None and port_task is not None:
            await stop_server(port_server, port_task)
        # One best-effort relinquish on a clean shutdown (#143 §5), in Optimise
        # only: the charging gate is "on" in no other mode, so Protect, Observe
        # and unknown authority write nothing (#426). No plan means ha-spark never
        # steered, so there is nothing to hand back. A held switch stays Off while
        # an EV hold is recorded or a known dispatch end is still ahead, or every
        # restart mid-hold would drain the battery into the car until the next
        # process holds it again (#143 §1); only the charge window is restored,
        # and the next start reconciles the hold first.
        # SIGKILL/OOM never reach here; that is the runbook's (#131/#134).
        if last_plan is not None and _real_write_mode(settings):
            try:
                shutdown_at = datetime.now(load_timezone(settings.timezone))
                kept = _shutdown_hold_reason(last_trusted_holds, shutdown_at)
                async with HomeAssistantRest(
                    settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
                ) as rest:
                    device = inverter_device(settings, rest)
                    if kept is not None:
                        log.warning(
                            "[WARNING] keeping the hold on shutdown: %s; restoring the "
                            "charge window only", kept,
                        )
                        lines = await device.write_safe_window()
                    else:
                        log.warning("[WARNING] relinquishing control on shutdown")
                        lines = await device.write_safe_state()
                    for line in lines:
                        log.info(line)
                    if kept is not None:
                        try:
                            await send_notification(
                                settings, rest, "ha-spark hold kept on shutdown",
                                f"ha-spark stopped with the inverter held Off: {kept}. "
                                "It is reconciled on the next start.",
                            )
                        except Exception:  # noqa: BLE001 - a notice never blocks shutdown
                            log.warning("Shutdown hold notification failed")
            except Exception:
                log.exception("Writing the safe state on shutdown failed")
        context.authority = None
        context.planned_epoch.set(None)
        channel.authority = None
