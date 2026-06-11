"""ha-spark: a local-first energy planner and agent for Home Assistant.

Reads live state over the HA REST/WebSocket APIs, plans the overnight battery
charge deterministically, and (per PROACTIVE_MODE) applies it to the inverter.
Configure via .env / add-on options; see .env.example for every setting.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ha_spark.config import ConfigError, Settings, load_settings
from ha_spark.energy.backtest import backtest_cost, format_backtest
from ha_spark.energy.chargers import SolisCharger
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.models import ConsumptionInterval
from ha_spark.energy.octopus import OctopusApiError, fetch_consumption, parse_octopus_csv
from ha_spark.energy.onboarding import (
    BACKFILL_STATISTIC_ID,
    SUPPORTED_UNITS,
    backfill_load,
    statistic_unit,
)
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.report import format_plan
from ha_spark.energy.scheduler import run_forever, run_once
from ha_spark.energy.sources import gather_inputs, parse_time
from ha_spark.energy.store import ConsumptionStore
from ha_spark.ha.models import StateChangedEvent
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.ha.state_cache import StateCache
from ha_spark.ha.statistics import list_statistic_ids
from ha_spark.ha.websocket import HomeAssistantWebSocket
from ha_spark.health import Status, check_load_history, exit_code, format_report, run_health
from ha_spark.logging import get_logger, setup_logging
from ha_spark.router import route_message

log = get_logger(__name__)


async def _cmd_states(settings: Settings, *, domain: str | None, watch: bool) -> int:
    """Seed the state cache from HA and print it; optionally stream live updates."""
    cache = StateCache()
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        await cache.seed(rest)

    entities = cache.by_domain(domain) if domain else cache.all()
    for state in entities:
        print(f"{state.entity_id:<45} {state.state:<20} {state.friendly_name}")
    print(f"\n{len(entities)} entit{'y' if len(entities) == 1 else 'ies'} shown.")

    if not watch:
        return 0

    ws = HomeAssistantWebSocket(settings.ha_websocket_url, settings.auth_token)
    ws.add_listener(cache.on_state_changed)

    async def _print_change(event: StateChangedEvent) -> None:
        if domain and not event.entity_id.startswith(f"{domain}."):
            return
        new = event.new_state.state if event.new_state else "<removed>"
        print(f"  ~ {event.entity_id} -> {new}")

    ws.add_listener(_print_change)
    ws.start()
    print("\nWatching for changes (Ctrl-C to stop)...")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await ws.stop()
    return 0


async def _cmd_health(settings: Settings) -> int:
    """Run the dependency checks, print the report, and return its exit code."""
    results = await run_health(settings)
    print(format_report(results))
    return exit_code(results)


async def _cmd_plan(settings: Settings, *, apply: bool) -> int:
    """Compute the battery charge plan, print it, optionally run the charger."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        inputs, cfg, load_source = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
        print(format_plan(plan, load_source))
        if apply:
            lines = await SolisCharger(settings, rest).apply(plan)
            print(f"\nActions (PROACTIVE_MODE={settings.proactive_mode}):")
            for line in lines:
                print(f"  {line}")
    return 0


async def _cmd_ask(settings: Settings, message: str) -> int:
    """Route a natural-language message: Ollama if reachable, offline parser if not."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        result = await route_message(message, settings, rest)
    print(f"[{result.source}] {result.text}")
    return 0


async def _cmd_run(settings: Settings, *, once: bool) -> int:
    """Run the planner daemon: compute & apply once, or loop daily."""
    if once:
        await run_once(settings)
        return 0
    try:
        await run_forever(settings)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0


async def _cmd_onboard(settings: Settings) -> int:
    """Report load-history readiness for the slot-profile forecast."""
    result = await check_load_history(settings)
    print(format_report([result]))
    return 0 if result.status is Status.OK else 2


async def _cmd_backfill_load(settings: Settings, *, source: str | None, list_only: bool) -> int:
    """Backfill ha_spark:house_load from an existing statistic, or list candidates."""
    if list_only:
        metas = await list_statistic_ids(
            settings.ha_websocket_url, settings.auth_token, timeout=settings.ha_timeout
        )
        candidates = [m for m in metas if statistic_unit(m) in SUPPORTED_UNITS]
        for meta in sorted(candidates, key=lambda m: str(m.get("statistic_id"))):
            kind = "mean power" if meta.get("has_mean") else "energy sum"
            print(f"{meta['statistic_id']:<70} {statistic_unit(meta):<4} ({kind})")
        print(f"\n{len(candidates)} backfill-capable statistics.")
        return 0
    entity = source or settings.backfill_source_entity
    if not entity:
        print(
            "No source entity: pass --from <entity_id> or set BACKFILL_SOURCE_ENTITY "
            "(use --list to see candidates).",
            file=sys.stderr,
        )
        return 2
    try:
        count, span = await backfill_load(settings, entity)
    except (ValueError, RuntimeError) as exc:
        print(f"Backfill failed: {exc}", file=sys.stderr)
        return 2
    print(f"Imported {count} hourly stats ({span}) from {entity} into {BACKFILL_STATISTIC_ID}.")
    print(
        f"Set CONSUMPTION_ENERGY_ENTITY={BACKFILL_STATISTIC_ID} to use it for the load "
        "forecast, then run `ha-spark onboard` to confirm readiness."
    )
    return 0


async def _store_and_report(
    settings: Settings, intervals: list[ConsumptionInterval], source: str
) -> int:
    """Upsert intervals into the consumption store and print a summary."""
    async with ConsumptionStore(settings.db_path) as store:
        changed = await store.upsert(intervals, source)
        count, first, last = await store.summary()
    print(f"Imported {len(intervals)} intervals ({changed} new/updated).")
    if first and last:
        print(
            f"Store now holds {count} intervals: "
            f"{first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC"
        )
    return 0


def _cmd_import_csv(settings: Settings, paths: list[str]) -> int:
    """Ingest Octopus dashboard CSV export(s) into the consumption store."""
    intervals: list[ConsumptionInterval] = []
    for path in paths:
        try:
            intervals.extend(parse_octopus_csv(Path(path).read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as exc:
            print(f"Could not import {path}: {exc}", file=sys.stderr)
            return 2
    return asyncio.run(_store_and_report(settings, intervals, "csv"))


async def _cmd_backtest(settings: Settings, *, days: int) -> int:
    """Rate stored grid import under the two-rate tariff and print the summary."""
    since = datetime.now(UTC) - timedelta(days=days)
    async with ConsumptionStore(settings.db_path) as store:
        intervals = await store.load_since(since)
    summary = backtest_cost(
        intervals,
        window_start=parse_time(settings.charge_window_start),
        window_end=parse_time(settings.charge_window_end),
        rate_offpeak=settings.rate_offpeak_gbp_kwh,
        rate_peak=settings.rate_peak_gbp_kwh,
        tz=load_timezone(settings.timezone),
    )
    if summary is None:
        print(
            "No stored consumption in the window; run `import-csv` or "
            "`pull-consumption` first.",
            file=sys.stderr,
        )
        return 2
    print(format_backtest(summary))
    return 0


async def _cmd_pull_consumption(settings: Settings, *, days: int) -> int:
    """Pull half-hourly consumption from the Octopus API (incremental)."""
    period_from = datetime.now(UTC) - timedelta(days=days)
    async with ConsumptionStore(settings.db_path) as store:
        latest = await store.latest_interval_start()
    if latest is not None and latest > period_from:
        period_from = latest  # incremental: re-fetch the newest interval onward
    try:
        intervals = await fetch_consumption(settings, period_from=period_from)
    except OctopusApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return await _store_and_report(settings, intervals, "api")


_EPILOG = """\
examples:
  ha-spark states --domain sensor          list all sensor entities
  ha-spark states --watch                  list entities, then stream live changes
  ha-spark health                          probe HA / Ollama / SQLite / load history
  ha-spark onboard                         check load-history readiness for the forecast
  ha-spark plan                            compute tonight's charge plan
  ha-spark plan --apply                    ...and run the charger (per PROACTIVE_MODE)
  ha-spark ask "what's tonight's plan"     answer via Ollama, or offline if unreachable
  ha-spark run                             daemon: plan + apply daily at PLAN_RUN_TIME
  ha-spark run --once                      plan + apply immediately, then exit
  ha-spark backfill-load --list            show statistics usable as a load source
  ha-spark backfill-load --from sensor.x   rebuild house-load history from sensor.x
  ha-spark import-csv export.csv           import an Octopus dashboard CSV (cost data)
  ha-spark pull-consumption --days 60      pull grid import from the Octopus API
  ha-spark backtest --days 30              rate stored grid import under the tariff
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha-spark",
        description=__doc__,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_states = sub.add_parser(
        "states",
        help="List Home Assistant entity states",
        description="List every entity HA knows about (entity_id, state, friendly name), "
        "seeded over REST. With --watch, keep streaming state changes over WebSocket "
        "until Ctrl-C.",
    )
    p_states.add_argument(
        "--domain",
        metavar="DOMAIN",
        help="Only show entities of this domain (e.g. light, sensor, number)",
    )
    p_states.add_argument(
        "--watch",
        action="store_true",
        help="After listing, stream live state changes over WebSocket (Ctrl-C to stop)",
    )

    sub.add_parser(
        "health",
        help="Probe HA, Ollama, storage and load history; exit non-zero on failure",
        description="Doctor command: checks the HA REST API, the HA WebSocket auth "
        "handshake, the Ollama endpoint, that the SQLite path is writable, and whether "
        "the load forecast has enough history. Exit 0 = all green, 1 = a critical "
        "dependency (HA/SQLite) failed, 2 = degraded (e.g. Ollama down, thin history).",
    )

    sub.add_parser(
        "onboard",
        help="Check load-history readiness for the slot-profile forecast",
        description="Report whether CONSUMPTION_ENERGY_ENTITY has enough hourly history "
        "for the slot-profile forecast (PROFILE_MIN_DAYS distinct days incl. 2+ weekend "
        "days). Exit 0 when ready, 2 otherwise — fix gaps with `backfill-load`.",
    )

    p_plan = sub.add_parser(
        "plan",
        help="Compute the overnight battery charge plan",
        description="Read live HA state (SoC, Solcast solar, Octopus dispatches, load "
        "forecast), compute the overnight charge deterministically, and print the plan "
        "with projected two-rate costs.",
    )
    p_plan.add_argument(
        "--apply",
        action="store_true",
        help="Run the charger with the computed plan. PROACTIVE_MODE gates side effects: "
        "off = compute only, simulate = log intended writes (default), on = real "
        "service calls to the inverter",
    )

    p_ask = sub.add_parser(
        "ask",
        help="Ask a natural-language question (Ollama, with offline fallback)",
        description="Route a message through the LLM router: a fast /api/tags probe "
        "checks the remote Ollama endpoint; if reachable the message is answered by "
        "OLLAMA_MODEL, otherwise the deterministic offline parser answers energy "
        "queries (plan, soc, solar, strategy, mode, window). The output is prefixed "
        "with [ollama] or [offline] to show which tier answered.",
    )
    p_ask.add_argument(
        "message",
        nargs="+",
        metavar="MESSAGE",
        help="The question to ask (multiple words are joined with spaces)",
    )

    p_run = sub.add_parser(
        "run",
        help="Daemon: compute & apply the plan once per day",
        description="Long-running loop that computes and applies the charge plan once "
        "per local calendar day at PLAN_RUN_TIME (default 22:00), retrying on failure "
        "until the day rolls over. Writes are still gated by PROACTIVE_MODE.",
    )
    p_run.add_argument(
        "--once",
        action="store_true",
        help="Compute & apply immediately and exit (skip the daily schedule)",
    )

    p_bf = sub.add_parser(
        "backfill-load",
        help="Rebuild house-load history from an existing HA statistic",
        description="Read hourly long-term statistics from a source entity (mean-power "
        "W/kW or energy Wh/kWh — unit auto-detected), convert to hourly kWh, and import "
        "them as the external statistic ha_spark:house_load via the recorder WS API. "
        "Afterwards set CONSUMPTION_ENERGY_ENTITY=ha_spark:house_load. Idempotent.",
    )
    p_bf.add_argument(
        "--from",
        dest="source",
        metavar="ENTITY_ID",
        help="Source statistic to build history from (default: BACKFILL_SOURCE_ENTITY)",
    )
    p_bf.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="List backfill-capable statistics (with units) instead of importing",
    )

    p_csv = sub.add_parser(
        "import-csv",
        help="Import Octopus grid-import CSV export(s) into the cost store",
        description="Parse half-hourly consumption CSV(s) exported from the Octopus "
        "dashboard into the local store. This is grid *import* (cost/backtest data) — "
        "it does not feed the load forecast.",
    )
    p_csv.add_argument("paths", nargs="+", metavar="PATH", help="CSV file(s) to import")

    p_pull = sub.add_parser(
        "pull-consumption",
        help="Pull grid import from the Octopus API into the cost store",
        description="Fetch half-hourly grid import from the Octopus REST API "
        "(needs OCTOPUS_API_KEY, OCTOPUS_MPAN, OCTOPUS_METER_SERIAL). Incremental: "
        "resumes from the newest stored interval.",
    )
    p_pull.add_argument(
        "--days",
        type=int,
        default=30,
        metavar="N",
        help="History window to fetch when the store is empty (default: 30)",
    )

    p_bt = sub.add_parser(
        "backtest",
        help="Rate stored grid import under the configured two-rate tariff",
        description="Summarise what the stored half-hourly grid import cost under "
        "RATE_OFFPEAK/RATE_PEAK, classifying each interval by the fixed charge window. "
        "Populate the store with `import-csv` or `pull-consumption` first.",
    )
    p_bt.add_argument(
        "--days",
        type=int,
        default=30,
        metavar="N",
        help="How far back to rate stored intervals (default: 30)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # `health` is the tool you reach for when config is broken, so it must run and
    # diagnose rather than fail fast: skip credential validation (but keep the
    # add-on options overlay so the configured endpoints are probed) and let the
    # checks themselves report what's wrong. Quiet logs keep the report clean —
    # failure detail is carried in each check result.
    if args.command == "health":
        settings = load_settings(validate=False)
        setup_logging("WARNING")
        return asyncio.run(_cmd_health(settings))

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)

    if args.command == "states":
        return asyncio.run(_cmd_states(settings, domain=args.domain, watch=args.watch))

    if args.command == "plan":
        return asyncio.run(_cmd_plan(settings, apply=args.apply))

    if args.command == "ask":
        return asyncio.run(_cmd_ask(settings, " ".join(args.message)))

    if args.command == "run":
        return asyncio.run(_cmd_run(settings, once=args.once))

    if args.command == "onboard":
        return asyncio.run(_cmd_onboard(settings))

    if args.command == "backfill-load":
        return asyncio.run(
            _cmd_backfill_load(settings, source=args.source, list_only=args.list_only)
        )

    if args.command == "import-csv":
        return _cmd_import_csv(settings, args.paths)

    if args.command == "pull-consumption":
        return asyncio.run(_cmd_pull_consumption(settings, days=args.days))

    if args.command == "backtest":
        return asyncio.run(_cmd_backtest(settings, days=args.days))

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
