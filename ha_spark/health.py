"""The ``health``/doctor command: probe everything ha-spark depends on.

Checks the HA REST API (via the Supervisor proxy or a dev URL), the HA WebSocket
auth handshake, the remote Ollama endpoint, and that the SQLite path is writable.
Output is human-readable. The exit code is ``0`` (all green), ``1`` (a critical
dependency failed — HA or SQLite), or ``2`` (degraded — only Ollama is down, for
which the deterministic offline parser is a valid fallback).
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum
from pathlib import Path

import aiosqlite
from pydantic import ValidationError

from ha_spark.authority import load_authority
from ha_spark.config import Settings
from ha_spark.daemon_channel import submit_socket
from ha_spark.devices.inverters.solis_clock import (
    FAIL_AT,
    UNREADABLE,
    clock_entity,
    clock_error,
    clock_refusal,
    describe_error,
)
from ha_spark.energy.axle import AxleApiError, read_axle_event
from ha_spark.energy.forecast import intervals_from_hourly_stats, load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.octopus import (
    OctopusApiError,
    fetch_planned_dispatches,
    fetch_standard_unit_rates,
)
from ha_spark.energy.planner import LIMITS_UNAVAILABLE_REASON
from ha_spark.energy.profile import history_coverage
from ha_spark.energy.solar_source import load_solar_source_kind
from ha_spark.energy.sources import (
    build_config,
    parse_time,
    read_dispatch_evidence,
    read_dispatches_and_live,
    read_ev_hold_status,
)
from ha_spark.energy.tariff import _in_overnight_window as in_window
from ha_spark.energy.topology import (
    Topology,
    charge_capacity_kw,
    effective_limits,
    legacy_topology,
    reserve_advisory,
)
from ha_spark.energy.topology_record import (
    PREVIEW_REASON,
    UNAVAILABLE_REASON,
    load_topology_record,
    select_limits,
)
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.ha.statistics import statistics_during_period
from ha_spark.ha.websocket import HomeAssistantWebSocket
from ha_spark.ollama import OllamaClient


class Status(Enum):
    """Outcome of a single health check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    """The result of one health check."""

    name: str
    status: Status
    detail: str


# Checks ha-spark cannot run without. Ollama is deliberately excluded: when it is
# unreachable the offline intent parser takes over, so that is degraded, not broken.
_CRITICAL = frozenset({"HA REST", "HA WS", "SQLite", "Operating mode", "Configuration"})

_GLYPH = {Status.OK: "✓", Status.WARN: "⚠", Status.FAIL: "✗"}


# Matches SiteSnapshot.release_version: version characters only, safe to print.
_RELEASE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")


async def check_ha_rest(settings: Settings) -> CheckResult:
    """Probe the HA REST API by fetching ``/config``."""
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            config = await rest.get_config()
        version = config.get("version", "?")
        return CheckResult("HA REST", Status.OK, f"HA {version} @ {settings.ha_rest_url}")
    except Exception as exc:  # noqa: BLE001 - a doctor reports failures, never raises
        return CheckResult("HA REST", Status.FAIL, f"{settings.ha_rest_url}: {exc!r}")


async def check_notify_service(settings: Settings) -> CheckResult:
    """Check that the configured notification service exists in Home Assistant."""
    if not settings.notify_service:
        return CheckResult("Notify service", Status.OK, "not configured")
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            domains = await rest.get_services()
        for domain in domains:
            if not isinstance(domain, dict) or domain.get("domain") != "notify":
                continue
            services = domain.get("services")
            if isinstance(services, dict) and settings.notify_service in services:
                return CheckResult("Notify service", Status.OK, "available in Home Assistant")
    except Exception:  # noqa: BLE001 - an unreadable registry degrades health
        return CheckResult("Notify service", Status.WARN, "could not read HA services")
    return CheckResult("Notify service", Status.WARN, "configured service is missing in HA")


async def check_ha_websocket(settings: Settings) -> CheckResult:
    """Probe the HA WebSocket API by completing the auth handshake."""
    try:
        version = await HomeAssistantWebSocket.probe(
            settings.ha_websocket_url, settings.auth_token, timeout=settings.ha_timeout
        )
        return CheckResult("HA WS", Status.OK, f"auth ok (HA {version})")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("HA WS", Status.FAIL, f"{settings.ha_websocket_url}: {exc!r}")


async def check_ollama(settings: Settings) -> CheckResult:
    """Probe the remote Ollama endpoint and that the configured model is present."""
    try:
        async with OllamaClient(
            settings.ollama_url, timeout=settings.ollama_health_timeout
        ) as client:
            models = await client.list_models()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "Ollama",
            Status.WARN,
            f"unreachable @ {settings.ollama_url} ({exc!r}); "
            "offline parser will be used, LLM features limited",
        )
    if settings.ollama_model in models:
        return CheckResult(
            "Ollama", Status.OK, f"{settings.ollama_model} @ {settings.ollama_url}"
        )
    return CheckResult(
        "Ollama",
        Status.WARN,
        f"reachable but '{settings.ollama_model}' not pulled "
        f"(ollama pull {settings.ollama_model}); have {models or 'none'}",
    )


async def check_sqlite(settings: Settings) -> CheckResult:
    """Confirm the SQLite path is writable (creates the data dir if absent)."""
    try:
        path = Path(settings.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(path)) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS _ha_spark_healthcheck (ts TEXT)")
            await db.execute("INSERT INTO _ha_spark_healthcheck (ts) VALUES ('ok')")
            await db.execute("DELETE FROM _ha_spark_healthcheck")
            await db.execute("DROP TABLE _ha_spark_healthcheck")
            await db.commit()
        return CheckResult("SQLite", Status.OK, f"writable @ {settings.db_path}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("SQLite", Status.FAIL, f"{settings.db_path}: {exc!r}")


async def check_ha_timezone(settings: Settings) -> CheckResult:
    """Compare HA's ``time_zone`` with the household clock (the ``timezone`` option).

    A mismatch only warns (#155): Axle times always carry an offset, so it
    touches times without one (Octopus dispatches) and the local day.
    """
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            config = await rest.get_config()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Household clock", Status.WARN, f"HA time_zone unreadable: {exc!r}")
    ha_zone = config.get("time_zone")
    if ha_zone == settings.timezone:
        return CheckResult("Household clock", Status.OK, f"{settings.timezone} (HA agrees)")
    return CheckResult(
        "Household clock",
        Status.WARN,
        f"HA time_zone is {ha_zone!r} but ha-spark timezone is {settings.timezone!r}; "
        "times without an offset and the local day may be misread",
    )


async def check_inverter_clock(settings: Settings) -> CheckResult:
    """Compare the Solis inverter clock with the household clock (#161)."""
    entity = clock_entity(settings.solis_control_hub)
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            state = await rest.get_state(entity)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "Inverter clock",
            Status.WARN,
            f"{entity}: {exc!r}; add the clock sensor from "
            "docs/solis-control-modbus-overlay.yaml; export windows are refused until it reads",
        )
    tz = load_timezone(settings.timezone)
    tolerance = timedelta(minutes=settings.inverter_clock_tolerance_minutes)
    error = clock_error(state, tz)
    refusal = clock_refusal(state, datetime.now(UTC), tz, tolerance)
    if error is None:
        return CheckResult(
            "Inverter clock", Status.WARN, f"{entity} {UNREADABLE}; export windows are refused"
        )
    if refusal is None:
        return CheckResult(
            "Inverter clock", Status.OK, f"{describe_error(error)} of {settings.timezone}"
        )
    status = Status.FAIL if abs(error) >= FAIL_AT else Status.WARN
    return CheckResult("Inverter clock", status, f"{refusal}; export windows are refused")


async def check_ev_status(settings: Settings) -> CheckResult:
    """Report whether the configured EV status can provide hold evidence."""
    entity = settings.ev_status_entity
    if not entity:
        return CheckResult("EV status", Status.OK, "not configured; EV charging holds disabled")
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            charging = await read_ev_hold_status(settings, rest)
    except Exception as exc:  # noqa: BLE001 - health reports failures, never raises
        return CheckResult("EV status", Status.WARN, f"{entity} unreadable: {exc!r}")
    if charging is None:
        return CheckResult("EV status", Status.WARN, f"{entity} unreadable")
    detail = "charging hold active" if charging else "readable; no charging hold"
    return CheckResult("EV status", Status.OK, f"{entity}: {detail}")


_REQUIRED_ENTITY_FIELDS = (
    "soc_entity",
    "battery_voltage_entity",
    "solar_tomorrow_entity",
    "octopus_rate_entity",
    "dispatch_entity",
    "ev_plug_entity",
    "ev_status_entity",
    "consumption_energy_entity",
    "charge_current_entity",
    "inverter_power_switch_entity",
)


def check_entity_config(settings: Settings) -> CheckResult:
    """Flag required entity fields left blank (ships unconfigured by default)."""
    native = load_solar_source_kind(settings) == "native"
    unset = [f for f in _REQUIRED_ENTITY_FIELDS if not getattr(settings, f)
             and not (native and f == "solar_tomorrow_entity")]
    if not settings.battery_power_entity:
        unset.append("battery_power_entity (glitch and stuck-SoC protection are off)")
    if not unset:
        return CheckResult("Entity config", Status.OK, "all required entities configured")
    return CheckResult(
        "Entity config",
        Status.WARN,
        f"unset: {', '.join(unset)} — run `ha-spark onboard` or set a preset",
    )


async def check_load_history(settings: Settings) -> CheckResult:
    """Report whether the load forecast has enough history for a slot profile."""
    entity = settings.consumption_energy_entity
    try:
        start = datetime.now(UTC) - timedelta(days=settings.profile_history_days)
        rows = await statistics_during_period(
            settings.ha_websocket_url,
            settings.auth_token,
            entity,
            start,
            period="hour",
            timeout=settings.ha_timeout,
        )
        intervals = intervals_from_hourly_stats(rows)
        if not intervals:
            return CheckResult(
                "Load history",
                Status.WARN,
                f"no hourly history for {entity}; see `ha-spark backfill-load`",
            )
        days, weekend_days = history_coverage(intervals, load_timezone(settings.timezone))
        if days >= settings.profile_min_days and weekend_days >= 2:
            return CheckResult(
                "Load history",
                Status.OK,
                f"slot profile ready ({days}d incl. {weekend_days} weekend) from {entity}",
            )
        return CheckResult(
            "Load history",
            Status.WARN,
            f"{days}/{settings.profile_min_days} days, {weekend_days}/2 weekend days "
            f"for {entity} — daily-median fallback; see `ha-spark backfill-load`",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Load history", Status.WARN, f"{entity}: {exc!r}")


async def check_supply_guard(settings: Settings) -> CheckResult:
    """Confirm the supply-guard sensor reads as a number (or report it disabled)."""
    entity = settings.grid_power_entity
    if not entity:
        return CheckResult(
            "Supply guard", Status.OK, "disabled (grid_power_entity not set)"
        )
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            state = await rest.get_state(entity)
        watts = float(state.state)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "Supply guard",
            Status.WARN,
            f"{entity}: {exc!r} — guard will skip ticks until it reads",
        )
    return CheckResult(
        "Supply guard",
        Status.OK,
        f"{watts:g} W from {entity} (limit {settings.supply_max_current_a:g} A)",
    )


async def _check_dynamic_tariff(settings: Settings) -> CheckResult:
    entity = settings.dynamic_rates_entity
    if not entity:
        return CheckResult("Tariff provider", Status.WARN, "dynamic_rates_entity not set")
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            state = await rest.get_state(entity)
        rates = state.attributes.get("rates")
        n = len(rates) if isinstance(rates, list) else 0
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "Tariff provider",
            Status.WARN,
            f"{entity}: {exc!r} — plans fall back to the fixed rate/window",
        )
    if not n:
        return CheckResult(
            "Tariff provider",
            Status.WARN,
            f"{entity}: no `rates` attribute — plans fall back to the fixed rate/window",
        )
    return CheckResult("Tariff provider", Status.OK, f"{n} rate slots from {entity}")


async def _check_octopus_intelligent_tariff(settings: Settings) -> CheckResult:
    """Confirm both API calls this provider needs succeed; never logs the API key."""
    try:
        dispatches = await fetch_planned_dispatches(settings)
        rates = await fetch_standard_unit_rates(
            settings,
            period_from=datetime.now(UTC),
            period_to=datetime.now(UTC) + timedelta(hours=48),
        )
    except OctopusApiError as exc:
        return CheckResult(
            "Tariff provider", Status.WARN, f"{exc} — plans fall back to the fixed rate/window"
        )
    return CheckResult(
        "Tariff provider",
        Status.OK,
        f"{len(rates)} rate slots, {len(dispatches)} planned dispatch(es)",
    )


async def check_export_event_provider(settings: Settings) -> CheckResult:
    """Confirm that the Axle event source can be read without exposing its token."""
    if settings.export_event_provider == "none":
        return CheckResult("Export event provider", Status.OK, "none (disabled)")
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            event = await read_axle_event(settings, rest)
    except AxleApiError as exc:
        return CheckResult(
            "Export event provider",
            Status.WARN,
            f"Axle event source unavailable: {exc}; plans keep the base schedule",
        )
    if event is None:
        return CheckResult("Export event provider", Status.OK, "Axle has no event scheduled")
    return CheckResult(
        "Export event provider",
        Status.OK,
        f"Axle export event {event.start.isoformat()} to {event.end.isoformat()}",
    )


async def check_tariff_provider(settings: Settings) -> CheckResult:
    """Confirm the configured tariff provider reads end-to-end (`fixed` needs no live read)."""
    if settings.tariff_provider == "dynamic":
        return await _check_dynamic_tariff(settings)
    if settings.tariff_provider == "octopus_intelligent":
        return await _check_octopus_intelligent_tariff(settings)
    return CheckResult("Tariff provider", Status.OK, "fixed (no live source)")


async def check_operating_mode(settings: Settings) -> CheckResult:
    """Read the store without initialisation; query only the daemon's live fault flag."""
    result = load_authority(settings)
    reply = await submit_socket(settings, "authority_status")
    if reply is not None and reply.authority is None:
        # A reachable daemon whose live status is unreadable may be hiding a save failure.
        return CheckResult("Operating mode", Status.FAIL, "daemon authority status unavailable")
    if reply is not None and reply.authority is not None:
        if reply.authority.get("save_failed"):
            return CheckResult("Operating mode", Status.FAIL, "authority save failure")
        if reply.authority.get("mode") == "authority_unknown":
            return CheckResult("Operating mode", Status.FAIL, "authority_unknown")
        if reply.authority.get("suspended"):
            return CheckResult("Operating mode", Status.FAIL, "suspended: configuration_repair")
    if result.status == "uninitialised":
        # No daemon of this version has started yet; nothing can write either way.
        return CheckResult("Operating mode", Status.WARN, "uninitialised (daemon not yet started)")
    if result.status != "known" or result.state is None:
        return CheckResult("Operating mode", Status.FAIL, result.status)
    return CheckResult("Operating mode", Status.OK, result.state.committed_mode)


async def check_integration_link(settings: Settings) -> CheckResult:
    """Read daemon-only liveness; a missing integration is never a failure.

    Only validated values are printed: the reply is another process's output.
    """
    try:
        reply = await submit_socket(settings, "integration_link")
    except OSError:
        return CheckResult("Integration link", Status.WARN, "unknown (daemon connection lost)")
    if reply is None:
        # The add-on's startup health runs before the daemon: no link to report is not a fault.
        return CheckResult("Integration link", Status.OK, "unknown (no daemon running)")
    view = reply.integration_link
    state = view.get("state") if view is not None else None
    if reply.error is not None or view is None or state not in (
        "connected", "never_connected", "unreachable", "version_skew",
    ):
        return CheckResult("Integration link", Status.WARN, "unknown")
    detail = str(state)
    version = view.get("integration_release_version")
    if isinstance(version, str) and _RELEASE_VERSION.fullmatch(version):
        detail += f"; integration {version}"
    age = view.get("snapshot_age_s")
    if state == "connected" and type(age) is int and age >= 0:
        detail += f"; snapshot age {age}s"
    topology_reason = view.get("topology_reason")
    if topology_reason in (
        "invalid_topology", "topology_not_configured", "topology_save_failed",
        "topology_record_unavailable",
    ):
        detail += f"; {topology_reason}"
    for field, code in (("major_loads_reason", "invalid_major_loads"),
                        ("supply_guard_reason", "invalid_supply_guard")):
        if view.get(field) == code:
            detail += f"; {code}"
    source = view.get("solar_source")
    if source in ("native", "entities"):
        detail += f"; solar: {source}"
    if source == "native":
        solar_status = view.get("solar_status")
        if solar_status in ("ok", "stale", "auth_failed", "quota_exhausted", "no_data"):
            detail += f"; {solar_status}"
        fetch_age = view.get("solar_fetch_age_s")
        if type(fetch_age) is int and fetch_age >= 0:
            detail += f"; fetch age {fetch_age}s"
    return CheckResult(
        "Integration link", Status.OK if state == "connected" else Status.WARN, detail,
    )


def check_hold_release(settings: Settings) -> list[CheckResult]:
    """A WARN per owned hold whose release has been refused (#449); nothing otherwise.

    Reads the stored records only; names the reason category, never a state value.
    """
    result = load_authority(settings)
    if result.status != "known" or result.state is None:
        return []
    reasons = {
        "switch_unreadable": "power switch unreadable",
        "schedule_unverified": "schedule to restore not verified",
    }
    return [
        CheckResult(
            "Hold release", Status.WARN,
            f"{h.cause} hold release refused since "
            f"{h.release_refused_since.astimezone(UTC):%Y-%m-%d %H:%M} UTC: "
            f"{reasons.get(h.release_refused_reason or '', 'refused')}; battery still held",
        )
        for h in result.state.holds if h.release_refused_since is not None
    ]


async def check_evidence_freshness(settings: Settings) -> list[CheckResult]:
    """Read-only source validity; no hardware or authority-store writes.

    Supplier validity describes this diagnostic's own fetch, not a cached
    planner fetch. PlannerInputs separately retains the gather_inputs fetch time.
    """
    reports: dict[str, datetime | None] = {}
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        dispatches, _trusted, live = await read_dispatches_and_live(
            settings, rest, report_times=reports
        )
        if settings.tariff_provider == "octopus_intelligent":
            try:
                await fetch_planned_dispatches(settings)
                reports["octopus_dispatch"] = datetime.now(UTC)
            except OctopusApiError:
                pass  # Unknown, never a trusted-empty schedule.
        snapshot = await read_dispatch_evidence(
            settings, rest, dispatches, live_dispatch=live,
            report_times=reports, read_all=True,
        )
    results = []
    for source, evidence in snapshot.freshness.items():
        detail = (
            f"{evidence.validity}; effective report={evidence.reported_at}; "
            f"age={evidence.age_s}s; max-age={evidence.max_age_s}s; "
            f"sibling={'yes' if evidence.via_sibling else 'no'}"
        )
        if evidence.reason:
            detail += f"; {evidence.reason}"
        if source == "battery_power":
            detail += f"; direction={snapshot.battery_direction}"
            if settings.battery_power_discharge_sign is None:
                detail += "; set battery_power_discharge_sign"
        results.append(CheckResult(
            f"Evidence {source}",
            Status.OK if evidence.validity == "fresh" else Status.WARN,
            detail,
        ))
    return results


async def check_effective_limits(
    settings: Settings, *, topology: Topology | None = None,
    overnight_base_load_kw: float | None = None,
    source: str = "legacy", note: str | None = None,
) -> CheckResult:
    """Show effective budgets and their source; forecast evidence adds a reserve advisory.

    Missing or unusable forecast evidence skips the advisory, never blocks health.
    """
    try:
        cfg = build_config(settings, settings.battery_voltage_v)
        topology = topology if topology is not None else legacy_topology(cfg, cfg.driver_kind)
        limits = effective_limits(topology)
        bank, connection = limits.banks[0], limits.connections[0]
        charge_capacity_kw(
            bank, connection,
            charge_efficiency=cfg.charge_efficiency if cfg.charge_efficiency > 0 else 1.0,
        )
    except (ValidationError, ValueError):
        return CheckResult("Effective limits", Status.WARN, LIMITS_UNAVAILABLE_REASON)
    if overnight_base_load_kw is not None:
        advisory = reserve_advisory(topology, connection, overnight_base_load_kw)
        if advisory is not None:
            return CheckResult("Effective limits", Status.WARN, advisory)
    detail = (
        f"{source}: charge {bank.charge_kw:.2f} kW DC; discharge {bank.discharge_kw:.2f} kW; "
        f"import {connection.import_kw:.2f} kW; site export {connection.site_export_kw:.2f} kW"
    )
    return CheckResult("Effective limits", Status.OK,
                       detail if note is None else f"{detail}; {note}")


async def _overnight_base_load_kw(settings: Settings) -> float | None:
    """The latest recorded load forecast's peak kW over the charge window, or None."""
    try:
        tz = load_timezone(settings.timezone)
        async with ForecastLedger(settings.db_path) as ledger:
            records = await ledger.forecasts_since(datetime.now(tz).date() - timedelta(days=1))
        slots = max(
            (r for r in records if r.slots is not None and len(r.slots) == 48),
            key=lambda r: r.made_at, default=None,
        )
        if slots is None or slots.slots is None:
            return None
        start, end = parse_time(settings.charge_window_start), parse_time(
            settings.charge_window_end)
        window = [
            float(kwh) * 2 for i, kwh in enumerate(slots.slots)
            if in_window(time(i // 2, 30 * (i % 2)), start, end)
        ]
        return max(window) if window and all(math.isfinite(v) for v in window) else None
    except Exception:  # noqa: BLE001 - advisory evidence only; never blocks health
        return None


async def check_limits(settings: Settings) -> CheckResult:
    """The limits source (#542): legacy, native or native (preview only), with advisory."""
    selection = select_limits(load_topology_record(settings), settings)
    if selection.source == "unavailable":
        return CheckResult("Effective limits", Status.WARN, UNAVAILABLE_REASON)
    overnight = await _overnight_base_load_kw(settings)
    if selection.source == "native (preview only)":
        if selection.preview is None:
            return CheckResult("Effective limits", Status.WARN, PREVIEW_REASON)
        writer = "native" if selection.actuation is not None else "legacy"
        return await check_effective_limits(
            settings, topology=selection.preview, overnight_base_load_kw=overnight,
            source=selection.source,
            note=f"{PREVIEW_REASON}; the writer keeps the {writer} limits",
        )
    return await check_effective_limits(
        settings, topology=selection.actuation, overnight_base_load_kw=overnight,
        source=selection.source,
    )


async def check_topology_record(settings: Settings) -> CheckResult:
    """Read the same durable acceptance record as daemon, CLI and agent previews."""
    loaded = load_topology_record(settings)
    detail: str = loaded.kind
    if loaded.revision is not None:
        detail += f"; revision {loaded.revision}"
    if loaded.reason is not None:
        detail += f"; {loaded.reason}"
    return CheckResult("Topology", Status.WARN if loaded.kind == "unavailable" else Status.OK,
                       detail)


async def run_health(settings: Settings) -> list[CheckResult]:
    """Run all checks concurrently, returning results in a stable order."""
    results = await asyncio.gather(
        check_operating_mode(settings),
        check_integration_link(settings),
        check_ha_rest(settings),
        *([check_notify_service(settings)] if settings.notify_service else []),
        check_ha_websocket(settings),
        check_ollama(settings),
        check_sqlite(settings),
        check_load_history(settings),
        check_supply_guard(settings),
        check_limits(settings),
        check_topology_record(settings),
        check_tariff_provider(settings),
        check_export_event_provider(settings),
        check_ha_timezone(settings),
        *([check_ev_status(settings)] if settings.ev_status_entity else []),
        *([check_inverter_clock(settings)] if settings.inverter == "solis" else []),
    )
    try:
        evidence = await check_evidence_freshness(settings)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never abort health
        evidence = [CheckResult(
            "Evidence freshness", Status.WARN, f"could not read ({type(exc).__name__})"
        )]
    return [*results, check_entity_config(settings), *check_hold_release(settings), *evidence]


async def run_repair_health(bootstrap: Settings, reasons: list[str]) -> list[CheckResult]:
    """Configuration repair: report the suspension and the stored authority, nothing live.

    Reasons name fields only. No HA, supplier or device check runs without a
    trusted configuration.
    """
    loaded = load_authority(bootstrap)
    stored = "authority unknown"
    if loaded.status == "known" and loaded.state is not None:
        transition = loaded.state.transition
        stored = (
            f"stored mode {loaded.state.committed_mode}, {len(loaded.state.holds)} hold(s), "
            f"transition {transition.target if transition else 'none'}"
        )
    detail = (
        "configuration repair: protection and optimisation suspended "
        f"({stored}); {'; '.join(reasons)}"
    )
    return [
        CheckResult("Configuration", Status.FAIL, detail),
        await check_operating_mode(bootstrap),
        await check_integration_link(bootstrap),
    ]


def format_report(results: list[CheckResult]) -> str:
    """Render results as aligned ``glyph name  detail`` lines."""
    width = max((len(r.name) for r in results), default=0)
    return "\n".join(f"{_GLYPH[r.status]} {r.name:<{width}}  {r.detail}" for r in results)


def exit_code(results: list[CheckResult]) -> int:
    """0 = all green, 1 = a critical check failed, 2 = degraded (Ollama only)."""
    if any(r.status is Status.FAIL and r.name in _CRITICAL for r in results):
        return 1
    if any(r.status is not Status.OK for r in results):
        return 2
    return 0
