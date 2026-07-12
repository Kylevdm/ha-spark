"""ha-spark: a local-first energy planner and agent for Home Assistant.

Reads live state over the HA REST/WebSocket APIs, plans the overnight battery
charge deterministically, and (per PROACTIVE_MODE) applies it to the inverter.
Configure via .env / add-on options; see .env.example for every setting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from ha_spark.config import ConfigError, Settings, load_settings
from ha_spark.dashboard import build_dashboard
from ha_spark.energy import habits
from ha_spark.energy.backtest import backtest_cost, format_backtest
from ha_spark.energy.chargers import charger_for
from ha_spark.energy.context import KINDS, ContextStore
from ha_spark.energy.eval import actual_kwh_by_date, evaluate, format_eval
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ConsumptionInterval, window_hours
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
from ha_spark.energy.sources import build_schedule, gather_inputs, parse_time
from ha_spark.energy.store import ConsumptionStore
from ha_spark.energy.tariff import TariffSchedule
from ha_spark.ha.models import StateChangedEvent
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.ha.state_cache import StateCache
from ha_spark.ha.statistics import list_statistic_ids, statistics_during_period
from ha_spark.ha.websocket import HomeAssistantWebSocket
from ha_spark.health import Status, check_load_history, exit_code, format_report, run_health
from ha_spark.logging import setup_logging
from ha_spark.onboarding_discover import FieldProposal, propose
from ha_spark.presets import get_preset, preset_names
from ha_spark.router import route_message


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
        plan = compute_plan(inputs, cfg, build_schedule(settings, inputs, cfg))
        print(format_plan(plan, load_source))
        if apply:
            intent = plan.charge_intent
            assert intent is not None  # planner always sets it
            lines = await charger_for(settings, rest).apply(intent)
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


_STATUS_GLYPH = {"match": "✓", "differs": "≠", "missing": "·"}


def _resolve_field(prop: FieldProposal, preset: dict[str, str]) -> tuple[str, str] | None:
    """The entity to propose for a field and where it came from, or None."""
    if prop.best is not None:
        return prop.best.entity_id, "discovered"
    if prop.config_field in preset:
        return preset[prop.config_field], "preset"
    return None


async def _cmd_onboard(
    settings: Settings, *, as_json: bool, write: bool, preset_name: str | None
) -> int:
    """Propose entity mappings from a live state dump, then check load readiness."""
    preset: dict[str, str] = {}
    if preset_name:
        try:
            preset = get_preset(preset_name)
        except KeyError:
            print(
                f"Unknown preset {preset_name!r}; available: {', '.join(preset_names())}",
                file=sys.stderr,
            )
            return 2

    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        states = await rest.get_states()
    proposals = propose(states, settings)

    if as_json:
        payload = {
            p.config_field: {
                "current": p.current,
                "status": p.status,
                "optional": p.optional,
                "candidates": [
                    {"entity_id": c.entity_id, "score": c.score, "reasons": list(c.reasons)}
                    for c in p.candidates
                ],
            }
            for p in proposals
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Entity discovery ({len(states)} entities scanned):")
    for p in proposals:
        glyph = _STATUS_GLYPH[p.status]
        opt = " (optional)" if p.optional else ""
        print(f"  {glyph} {p.config_field}{opt}")
        print(f"      configured: {p.current or '<unset>'}")
        if p.best is not None:
            print(f"      proposed:   {p.best.entity_id}  [{', '.join(p.best.reasons)}]")
            for alt in p.candidates[1:]:
                print(f"      alt:        {alt.entity_id}")
        elif p.config_field in preset:
            print(f"      preset:     {preset[p.config_field]}  [{preset_name}]")
        else:
            print("      no candidate found")

    if write:
        print("\n# Proposed options (paste into the add-on Configuration / .env):")
        for p in proposals:
            resolved = _resolve_field(p, preset)
            if resolved is None:
                continue
            entity_id, origin = resolved
            print(f"{p.config_field}: {entity_id}    # {origin}")

    print()
    result = await check_load_history(settings)
    print(format_report([result]))
    return 0 if result.status is Status.OK else 2


async def _cmd_generate_dashboard(settings: Settings, *, output: str) -> int:
    """Render a Lovelace dashboard from configured entity fields and write it to disk."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        dashboard = await build_dashboard(settings, rest)
    Path(output).write_text(  # noqa: ASYNC240 (one-shot CLI write, not a server loop)
        yaml.safe_dump(dashboard, sort_keys=False), encoding="utf-8"
    )
    print(f"Wrote dashboard to {output}")
    return 0


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
    window_start = parse_time(settings.charge_window_start)
    window_end = parse_time(settings.charge_window_end)
    schedule = TariffSchedule(
        cheap_rate=settings.rate_offpeak_gbp_kwh,
        standard_rate=settings.rate_peak_gbp_kwh,
        export_rate=settings.rate_export_gbp_kwh,
        window_hours=window_hours(window_start, window_end),
    )
    summary = backtest_cost(
        intervals,
        window_start=window_start,
        window_end=window_end,
        schedule=schedule,
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


async def _cmd_forecast_eval(settings: Settings, *, days: int) -> int:
    """Score recorded load forecasts against actual consumption (MAE/MAPE per model)."""
    since = (datetime.now(UTC) - timedelta(days=days)).date()
    async with ForecastLedger(settings.db_path) as ledger:
        forecasts = await ledger.forecasts_since(since)
    if not forecasts:
        print(
            "No recorded forecasts yet; the daemon writes one nightly (`ha-spark run`).",
            file=sys.stderr,
        )
        return 2
    rows = await statistics_during_period(
        settings.ha_websocket_url,
        settings.auth_token,
        settings.consumption_energy_entity,
        datetime.combine(since, datetime.min.time(), tzinfo=UTC),
        period="day",
        timeout=settings.ha_timeout,
    )
    results = evaluate(forecasts, actual_kwh_by_date(rows))
    print(format_eval(results, days))
    return 0


async def _cmd_learn_factors(settings: Settings) -> int:
    """Report what the habit learner derives from recorded history (6E)."""
    tz = load_timezone(settings.timezone)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    since = datetime.now(UTC) - timedelta(days=settings.profile_history_days)

    rows = await statistics_during_period(
        settings.ha_websocket_url,
        settings.auth_token,
        settings.consumption_energy_entity,
        since,
        period="day",
        timeout=settings.ha_timeout,
    )
    daily_actuals = actual_kwh_by_date(rows)

    away_dates: set[date] = set()
    async with ContextStore(settings.db_path) as store:
        for e in await store.list_all():
            if e.kind != "away":
                continue
            d = e.start_date
            while d <= e.end_date:
                away_dates.add(d)
                d += timedelta(days=1)
    async with ForecastLedger(settings.db_path) as ledger:
        occ_samples = await ledger.signal_history("occupancy_home_frac", since)

    factor, n = habits.learn_away_factor(daily_actuals, away_dates)
    predicted_occ = habits.predict_occupancy(occ_samples, tomorrow, tz)

    print("Learned habits:")
    if factor is not None:
        print(f"  Away load factor   {factor:.2f}  (from {n} past away days; auto-applied)")
    else:
        print(
            f"  Away load factor   not enough away history ({n} usable day(s); "
            f"need {habits.MIN_AWAY_SAMPLES}) — using configured "
            f"{settings.away_load_factor:.2f}"
        )
    if predicted_occ is not None:
        print(f"  Occupancy tomorrow ~{predicted_occ * 100:.0f}% home ({tomorrow:%a %d %b})")
    else:
        print("  Occupancy tomorrow not enough occupancy history yet")

    ctx = habits.HabitContext(
        target_date=tomorrow,
        predicted_occupancy=predicted_occ,
        away_active=any(d == tomorrow for d in away_dates),
        learned_away_factor=factor,
    )
    actions = habits.predict_actions(ctx)
    if actions:
        print("Advisory predictions for tomorrow:")
        for a in actions:
            print(f"  - {a.action} ({a.confidence * 100:.0f}%): {a.reason}")
    return 0


async def _cmd_context(settings: Settings, args: argparse.Namespace) -> int:
    """Add, list, or remove date-ranged context facts the planner consumes."""
    async with ContextStore(settings.db_path) as store:
        if args.context_command == "add":
            try:
                start = date.fromisoformat(args.start)
                end = date.fromisoformat(args.end) if args.end else start
            except ValueError as exc:
                print(f"Bad date (expected YYYY-MM-DD): {exc}", file=sys.stderr)
                return 2
            try:
                entry_id = await store.add(
                    args.kind, start, end, note=args.note or "", factor=args.factor
                )
            except ValueError as exc:
                print(f"Could not add context: {exc}", file=sys.stderr)
                return 2
            print(f"Added context [{entry_id}] {args.kind} {start} .. {end}.")
            return 0

        if args.context_command == "remove":
            removed = await store.remove(args.id)
            if not removed:
                print(f"No context with id {args.id}.", file=sys.stderr)
                return 2
            print(f"Removed context [{args.id}].")
            return 0

        entries = await store.list_all()

    if not entries:
        print("No context facts stored. Add one with `ha-spark context add`.")
        return 0
    today = datetime.now(load_timezone(settings.timezone)).date()
    print(f"{len(entries)} context fact(s):")
    for e in entries:
        active = e.start_date <= today <= e.end_date
        factor = e.factor(settings)
        span = (
            f"{e.start_date}"
            if e.start_date == e.end_date
            else f"{e.start_date} .. {e.end_date}"
        )
        note = f"  — {e.note}" if e.note else ""
        flag = "active" if active else "      "
        print(f"  [{e.id}] {flag} {e.kind:<10} {span}  ×{factor:.2f}{note}")
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
  ha-spark forecast-eval --days 14         score recorded forecasts vs actual load
  ha-spark learn-factors                   show learned away factor + occupancy
  ha-spark context add away --from 2026-07-01 --to 2026-07-14   record a holiday
  ha-spark context list                    show stored context facts
  ha-spark context remove 3                delete a context fact by id
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

    p_onboard = sub.add_parser(
        "onboard",
        help="Propose entity mappings from live HA state, then check load readiness",
        description="Scan Home Assistant's entities and propose which one maps to each "
        "ha-spark config field (battery SoC, solar forecast, dispatch sensor, charge "
        "current control, ...), ranked by device class / unit / name. Then report "
        "whether CONSUMPTION_ENERGY_ENTITY has enough hourly history for the slot-profile "
        "forecast. Exit 0 when ready, 2 otherwise — fix gaps with `backfill-load`. "
        "Proposals are advisory: review and set the options yourself.",
    )
    p_onboard.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit the discovery proposals as JSON (skips the readiness check output)",
    )
    p_onboard.add_argument(
        "--write", action="store_true",
        help="Also print a ready-to-paste options fragment for the proposed mapping",
    )
    p_onboard.add_argument(
        "--preset", dest="preset", metavar="NAME", choices=preset_names(),
        help=f"Fill unmatched fields from a vendor preset ({', '.join(preset_names())})",
    )

    p_dash = sub.add_parser(
        "generate-dashboard",
        help="Render a Lovelace dashboard YAML file from configured entity fields",
        description="Build a Lovelace dashboard from whichever entity-id fields are "
        "already set in config (battery SoC, solar, EV/charger, grid/tariff, ...), "
        "labelling each with its live HA friendly_name where reachable. Re-run any "
        "time config changes — no onboarding re-run needed.",
    )
    p_dash.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="File path to write the Lovelace YAML to",
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
        "queries (plan, soc, solar, strategy, mode, window). A context statement "
        "(\"I'm away next week\") is extracted into a context fact, and a context "
        "question lists stored facts — both before plain chat. The output is "
        "prefixed with [ollama] or [offline] to show which tier answered.",
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

    p_fe = sub.add_parser(
        "forecast-eval",
        help="Score recorded load forecasts against actual consumption",
        description="Join forecasts recorded nightly by the daemon (`ha-spark run`) "
        "against actual daily consumption from HA statistics, and report MAE/MAPE "
        "per model. A model must beat the `median` baseline here before "
        "LOAD_MODEL=auto can use it.",
    )
    p_fe.add_argument(
        "--days",
        type=int,
        default=14,
        metavar="N",
        help="How many days of recorded forecasts to score (default: 14)",
    )

    sub.add_parser(
        "learn-factors",
        help="Report habits learned from recorded history (away factor, occupancy)",
        description="Show what the habit learner derives from the signal/forecast/context "
        "history: the away load factor learned from past away periods (auto-applied to the "
        "plan once enough away days exist), tomorrow's predicted occupancy fraction, and "
        "the advisory habit predictions for tomorrow.",
    )

    p_ctx = sub.add_parser(
        "context",
        help="Manage date-ranged context facts (away/guests) the planner uses",
        description="Record household context the deterministic planner consumes as a "
        "load multiplier: an `away` holiday lightens the forecast, `guests` heightens "
        "it, and `high_usage`/`low_usage` apply a custom --factor. Active facts are "
        "printed with each plan.",
    )
    ctx_sub = p_ctx.add_subparsers(dest="context_command", required=True, metavar="ACTION")
    p_ctx_add = ctx_sub.add_parser("add", help="Add a context fact")
    p_ctx_add.add_argument("kind", choices=KINDS, help="The kind of fact")
    p_ctx_add.add_argument(
        "--from", dest="start", required=True, metavar="YYYY-MM-DD", help="First day (inclusive)"
    )
    p_ctx_add.add_argument(
        "--to", dest="end", metavar="YYYY-MM-DD", help="Last day (inclusive; default: same day)"
    )
    p_ctx_add.add_argument("--note", metavar="TEXT", help="Optional free-text note")
    p_ctx_add.add_argument(
        "--factor",
        type=float,
        metavar="X",
        help="Load multiplier for high_usage/low_usage facts (away/guests use config)",
    )
    ctx_sub.add_parser("list", help="List all stored context facts")
    p_ctx_rm = ctx_sub.add_parser("remove", help="Remove a context fact by id")
    p_ctx_rm.add_argument("id", type=int, metavar="ID", help="The fact id (see `context list`)")

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
        return asyncio.run(
            _cmd_onboard(
                settings, as_json=args.as_json, write=args.write, preset_name=args.preset
            )
        )

    if args.command == "generate-dashboard":
        return asyncio.run(_cmd_generate_dashboard(settings, output=args.output))

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

    if args.command == "forecast-eval":
        return asyncio.run(_cmd_forecast_eval(settings, days=args.days))

    if args.command == "learn-factors":
        return asyncio.run(_cmd_learn_factors(settings))

    if args.command == "context":
        return asyncio.run(_cmd_context(settings, args))

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
