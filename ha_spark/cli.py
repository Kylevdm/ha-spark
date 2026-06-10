"""Command-line interface for ha-spark."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ha_spark.config import ConfigError, Settings, load_settings
from ha_spark.energy.chargers import SolisCharger
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
from ha_spark.energy.sources import gather_inputs
from ha_spark.energy.store import ConsumptionStore
from ha_spark.ha.models import StateChangedEvent
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.ha.state_cache import StateCache
from ha_spark.ha.statistics import list_statistic_ids
from ha_spark.ha.websocket import HomeAssistantWebSocket
from ha_spark.health import Status, check_load_history, exit_code, format_report, run_health
from ha_spark.logging import get_logger, setup_logging

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ha-spark", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_states = sub.add_parser("states", help="List Home Assistant entity states")
    p_states.add_argument("--domain", help="Filter by domain (e.g. light, sensor)")
    p_states.add_argument(
        "--watch", action="store_true", help="Stream live state changes over WebSocket"
    )

    sub.add_parser("health", help="Probe HA, Ollama and storage; exit non-zero on failure")

    p_plan = sub.add_parser("plan", help="Compute the battery charge plan")
    p_plan.add_argument(
        "--apply", action="store_true", help="Run the charger (simulate/on per PROACTIVE_MODE)"
    )

    p_run = sub.add_parser(
        "run", help="Run the planner daemon: compute & apply the plan once per day"
    )
    p_run.add_argument(
        "--once", action="store_true", help="Run immediately and exit (skip the daily schedule)"
    )

    sub.add_parser("onboard", help="Check load-history readiness for the slot-profile forecast")

    p_bf = sub.add_parser(
        "backfill-load",
        help="Backfill house-load history (ha_spark:house_load) from an existing statistic",
    )
    p_bf.add_argument(
        "--from",
        dest="source",
        metavar="ENTITY_ID",
        help="Source statistic to build history from (default: BACKFILL_SOURCE_ENTITY)",
    )
    p_bf.add_argument(
        "--list", dest="list_only", action="store_true", help="List backfill-capable statistics"
    )

    p_csv = sub.add_parser(
        "import-csv", help="Import Octopus half-hourly grid-import CSV export(s) (cost data)"
    )
    p_csv.add_argument("paths", nargs="+", metavar="PATH", help="CSV file(s) to import")

    p_pull = sub.add_parser(
        "pull-consumption", help="Pull half-hourly grid import from the Octopus API (cost data)"
    )
    p_pull.add_argument(
        "--days", type=int, default=30, help="History window to fetch (default 30)"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # `health` is the tool you reach for when config is broken, so it must run and
    # diagnose rather than fail fast: build raw Settings (no credential validation)
    # and let the checks themselves report what's wrong. Quiet logs keep the report
    # clean — failure detail is carried in each check result.
    if args.command == "health":
        settings = Settings()
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

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
