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

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ha_spark.agent import auth, tools
from ha_spark.config import _OPTION_KEYS, Settings, load_settings
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.logging import get_logger

log = get_logger(__name__)

# Add-on ingress serves on this fixed internal port (must match config.yaml
# `ingress_port`). Not mapped to the host network, so it isn't externally reachable.
INGRESS_PORT = 8099
# Where the add-on persists user options (HA add-on convention).
OPTIONS_PATH = Path("/data/options.json")
# Attribute under which the shared AppState is stashed on the FastAPI app.
STATE_ATTR = "ha_spark_state"


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


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _state_of(request: Request) -> AppState:
    return cast(AppState, getattr(request.app.state, STATE_ATTR))


def _agent_router(state: AppState) -> APIRouter:
    """Build the ``/agent/*`` tool routes, gated by the current exposure level.

    Routes are registered per ``state.settings.agent_exposure`` at build time
    (read always; act at read_act+; config write at read_write). Each handler
    reads ``state.settings`` live, so hot-reloaded values are picked up; only the
    set of registered routes is fixed until the app is rebuilt.
    """
    router = APIRouter(prefix="/agent")
    exposure = state.settings.agent_exposure

    @router.get("/plan")
    async def plan() -> tools.PlanResult:
        return await tools.get_plan(state.settings)

    @router.get("/state")
    async def state_() -> tools.StateResult:
        return await tools.get_state(state.settings)

    @router.get("/forecast")
    async def forecast() -> tools.ForecastResult:
        return await tools.get_forecast(state.settings)

    @router.get("/predictions")
    async def predictions() -> tools.PredictionsResult:
        return await tools.get_predictions(state.settings)

    @router.get("/health")
    async def health() -> tools.HealthResult:
        return await tools.get_health(state.settings)

    @router.get("/context")
    async def context() -> tools.ContextResult:
        return await tools.get_context(state.settings)

    if exposure in ("read_act", "read_write"):

        @router.post("/context")
        async def add_context(body: dict[str, object]) -> tools.ContextResult:
            from datetime import date

            try:
                return await tools.add_context(
                    state.settings,
                    str(body["kind"]),
                    date.fromisoformat(str(body["start_date"])),
                    date.fromisoformat(str(body["end_date"])),
                    note=str(body.get("note", "")),
                )
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @router.post("/run")
        async def run() -> tools.PlanResult:
            return await tools.run_plan(state.settings)

    if exposure == "read_write":

        @router.post("/config")
        async def config(body: dict[str, object]) -> dict[str, object]:
            try:
                state.apply_options(body)
            except Exception as exc:  # noqa: BLE001 - validation failure -> 400
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return state.current_options()

    return router


def build_app(state: AppState, *, require_token: bool = False, token: str = "") -> FastAPI:
    """Build the FastAPI app with the API + agent routes bound to ``state``.

    When ``require_token`` is set (the published port), every request must carry
    ``Authorization: Bearer <token>``; the ingress app leaves it unset and trusts
    HA's authenticated proxy.
    """
    app = FastAPI(title="ha-spark", docs_url=None, redoc_url=None)
    setattr(app.state, STATE_ATTR, state)

    if require_token:

        async def _auth(authorization: str | None = Header(default=None)) -> None:
            if not auth.verify(authorization, token):
                raise HTTPException(status_code=401, detail="invalid or missing token")

        app.router.dependencies.append(Depends(_auth))

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Liveness + whether a plan has been computed yet (full doctor stays in the CLI)."""
        st = _state_of(request)
        return {"status": "ok", "plan_at": _iso(st.plan_at)}

    @app.get("/api/plan")
    async def get_plan(request: Request) -> dict[str, Any]:
        """The latest plan as the same sensor payload the daemon would push."""
        st = _state_of(request)
        if st.plan is None:
            return {"plan": None, "generated_at": None}
        entities = [
            {"entity_id": eid, "state": value, "attributes": attrs}
            for eid, value, attrs in plan_to_payload(st.plan, st.settings)
        ]
        return {"plan": entities, "generated_at": _iso(st.plan_at)}

    @app.get("/api/config")
    async def get_config(request: Request) -> dict[str, Any]:
        """Current user options."""
        return _state_of(request).current_options()

    @app.post("/api/config")
    async def post_config(request: Request) -> JSONResponse:
        """Merge posted options, persist, and hot-reload the daemon's settings."""
        try:
            updates = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a client error
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(updates, dict):
            return JSONResponse({"error": "expected a JSON object"}, status_code=400)
        try:
            _state_of(request).apply_options(updates)
        except Exception as exc:  # noqa: BLE001 - validation/reload failure -> client error
            log.warning("Rejecting config update: %r", exc)
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_state_of(request).current_options())

    app.include_router(_agent_router(state))
    return app


def make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    """Wrap ``app`` in a uvicorn server bound to ``host:port`` (not yet running)."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    return uvicorn.Server(config)


async def serve_in_background(server: uvicorn.Server) -> asyncio.Task[None]:
    """Start ``server.serve()`` as a task and return once the socket is bound.

    If ``serve()`` exits before binding (e.g. the port is already taken),
    re-raise its error to the caller instead of waiting forever.
    """
    task = asyncio.ensure_future(server.serve())
    # ponytail: poll uvicorn's `started` flag; it exposes no awaitable for readiness.
    while not server.started and not task.done():  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    if task.done():  # serve() returned/raised before binding -> surface it
        await task
    return task


async def stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    """Ask the server to exit and await the serve task."""
    server.should_exit = True
    await task
