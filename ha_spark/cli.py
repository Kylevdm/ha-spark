"""Command-line interface for ha-spark."""

from __future__ import annotations

import argparse
import asyncio
import sys

from ha_spark.config import ConfigError, Settings, load_settings
from ha_spark.ha.models import StateChangedEvent
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.ha.state_cache import StateCache
from ha_spark.ha.websocket import HomeAssistantWebSocket
from ha_spark.health import exit_code, format_report, run_health
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ha-spark", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_states = sub.add_parser("states", help="List Home Assistant entity states")
    p_states.add_argument("--domain", help="Filter by domain (e.g. light, sensor)")
    p_states.add_argument(
        "--watch", action="store_true", help="Stream live state changes over WebSocket"
    )

    sub.add_parser("health", help="Probe HA, Ollama and storage; exit non-zero on failure")

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

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
