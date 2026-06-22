"""Add-on HTTP API served behind add-on ingress.

The companion HA integration drives ha-spark through this API instead of the
daemon pushing states directly: it reads the latest plan (to create entities)
and reads/writes user options (onboarding + settings). Runs in the daemon's
event loop (see :mod:`ha_spark.energy.scheduler`).

Auth: in add-on mode the only route in is HA's ingress proxy, which
authenticates the user and is not mapped to the host network (no ``ports:``),
so the handlers trust their caller. ``POST /api/config`` is therefore the only
mutation and is reachable only through that authenticated proxy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from ha_spark.config import _OPTION_KEYS, Settings, load_settings
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.logging import get_logger

log = get_logger(__name__)


@dataclass
class AppState:
    """State shared between the daemon and the API handlers.

    ``reload`` is injectable so tests can rebuild ``Settings`` without the full
    add-on/dev credential machinery; in production it is :func:`load_settings`.
    """

    settings: Settings
    options_path: Path
    reload: Callable[[], Settings] = load_settings
    plan: ChargePlan | None = None
    plan_at: datetime | None = None

    def set_plan(self, plan: ChargePlan) -> None:
        """Record the latest computed plan (called by the daemon each run)."""
        self.plan = plan
        self.plan_at = datetime.now(UTC)

    def current_options(self) -> dict[str, Any]:
        """The user-facing options subset of the current settings."""
        return {key: getattr(self.settings, key) for key in _OPTION_KEYS}

    def apply_options(self, updates: dict[str, Any]) -> Settings:
        """Merge ``updates`` into the persisted options, then reload settings.

        Only keys in ``_OPTION_KEYS`` are accepted; unknown keys are ignored.
        Raises if the merged config fails validation (the caller maps that to 400).
        """
        current: dict[str, Any] = {}
        if self.options_path.exists():
            current = json.loads(self.options_path.read_text(encoding="utf-8"))
        current.update({k: v for k, v in updates.items() if k in _OPTION_KEYS})
        self.options_path.parent.mkdir(parents=True, exist_ok=True)
        self.options_path.write_text(json.dumps(current), encoding="utf-8")
        self.settings = self.reload()
        return self.settings


STATE_KEY = web.AppKey("ha_spark_state", AppState)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


async def _health(request: web.Request) -> web.Response:
    """Liveness + whether a plan has been computed yet (full doctor stays in the CLI)."""
    state = request.app[STATE_KEY]
    return web.json_response({"status": "ok", "plan_at": _iso(state.plan_at)})


async def _get_plan(request: web.Request) -> web.Response:
    """The latest plan as the same sensor payload the daemon would push."""
    state = request.app[STATE_KEY]
    if state.plan is None:
        return web.json_response({"plan": None, "generated_at": None})
    entities = [
        {"entity_id": entity_id, "state": value, "attributes": attrs}
        for entity_id, value, attrs in plan_to_payload(state.plan, state.settings)
    ]
    return web.json_response({"plan": entities, "generated_at": _iso(state.plan_at)})


async def _get_config(request: web.Request) -> web.Response:
    """Current user options."""
    return web.json_response(request.app[STATE_KEY].current_options())


async def _post_config(request: web.Request) -> web.Response:
    """Merge posted options, persist, and hot-reload the daemon's settings."""
    try:
        updates = await request.json()
    except Exception:  # noqa: BLE001 - any malformed body is a client error
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(updates, dict):
        return web.json_response({"error": "expected a JSON object"}, status=400)
    try:
        request.app[STATE_KEY].apply_options(updates)
    except Exception as exc:  # noqa: BLE001 - validation/reload failure -> client error
        log.warning("Rejecting config update: %r", exc)
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response(request.app[STATE_KEY].current_options())


def create_app(state: AppState) -> web.Application:
    """Build the aiohttp app with the API routes bound to ``state``."""
    app = web.Application()
    app[STATE_KEY] = state
    app.add_routes(
        [
            web.get("/api/health", _health),
            web.get("/api/plan", _get_plan),
            web.get("/api/config", _get_config),
            web.post("/api/config", _post_config),
        ]
    )
    return app
