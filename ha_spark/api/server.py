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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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
    state: AppState = getattr(request.app.state, STATE_ATTR)
    return state


def _agent_router(state: AppState) -> APIRouter:
    """Build the ``/agent/*`` router, gated by ``state.settings.agent_exposure``."""
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
    async def health_() -> tools.HealthResult:
        return await tools.get_health(state.settings)

    @router.get("/context")
    async def context() -> tools.ContextResult:
        return await tools.get_context(state.settings)

    if exposure in ("read_act", "read_write"):

        @router.post("/context")
        async def add_context(body: dict[str, object]) -> tools.ContextResult:
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

    else:
        # GET /context is always registered, so without an explicit POST
        # handler here Starlette would answer POST /agent/context with 405
        # (path matches, method doesn't) instead of 404. Register one that
        # 404s, so "absent below this tier" reads the same for every route.
        @router.post("/context", include_in_schema=False)
        async def add_context_unavailable(body: dict[str, object] | None = None) -> None:
            raise HTTPException(status_code=404)

    if exposure == "read_write":

        @router.post("/config")
        async def config(body: dict[str, object]) -> dict[str, Any]:
            try:
                state.apply_options(body)
            except Exception as exc:  # noqa: BLE001 - validation failure -> 400
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return state.current_options()

    return router


def build_app(state: AppState, *, require_token: bool = False, token: str = "") -> FastAPI:
    """Build the FastAPI app with the API routes bound to ``state``.

    ``require_token``/``token`` gate every route (including ``/api/*``) behind
    bearer auth -- used by the published port (Task 9). The ingress app omits
    it, since ingress already authenticates the caller.
    """
    # Local import: mcp_server does ``from ha_spark.api.server import AppState``,
    # so a module-top import would cycle.
    from ha_spark.agent.mcp_server import build_mcp

    # FastMCP's streamable-HTTP app owns its own lifespan (a StreamableHTTP
    # session manager); FastAPI does NOT auto-run a mounted sub-app's lifespan,
    # so we adopt it as the app's lifespan or /mcp 500s with "Task group is not
    # initialized". Build the app and read its lifespan BEFORE creating FastAPI.
    mcp_app = build_mcp(state).streamable_http_app()
    app = FastAPI(
        title="ha-spark",
        docs_url=None,
        redoc_url=None,
        lifespan=mcp_app.router.lifespan_context,
    )
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
        except Exception:  # noqa: BLE001 - malformed body is a client error
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
    # FastAPI router dependencies (the ``_auth`` gate above) do NOT propagate to a
    # mounted ASGI sub-app, so on the published port /mcp would otherwise be
    # reachable without the bearer token while /api/* and /agent/* require it.
    # Wrap the mount in the same check so every inbound surface is gated alike.
    app.mount("/mcp", _token_gated(mcp_app, token) if require_token else mcp_app)
    return app


def _token_gated(asgi_app: Any, token: str) -> Any:
    """Wrap an ASGI app so HTTP requests must carry a valid bearer token.

    FastAPI route dependencies don't reach mounted sub-apps, so the published
    port gates ``/mcp`` here instead, reusing :func:`auth.verify`. Non-HTTP
    scopes (lifespan) pass through untouched.
    """

    async def gated(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            header = next(
                (v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"authorization"),
                None,
            )
            if not auth.verify(header, token):
                await JSONResponse({"error": "invalid or missing token"}, status_code=401)(
                    scope, receive, send
                )
                return
        await asgi_app(scope, receive, send)

    return gated


def make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    return uvicorn.Server(config)


async def _serve_or_raise(server: uvicorn.Server) -> None:
    """Run ``server.serve()``, converting a failed-bind ``SystemExit`` to a normal error.

    Uvicorn calls ``sys.exit(1)`` internally when the socket bind fails (e.g. the
    port is already in use). ``SystemExit`` is a ``BaseException``: if it escapes
    this task uncaught, asyncio re-raises it straight out of the event loop the
    moment the task is stepped -- before any poll loop watching ``task.done()``
    gets a chance to observe it -- which would crash the daemon instead of letting
    it degrade gracefully. Catching it here lets the task store a normal
    ``Exception`` that callers' ``except Exception`` can handle as usual.
    """
    try:
        await server.serve()
    except SystemExit as exc:
        raise RuntimeError(f"HTTP server exited before startup: {exc!r}") from exc


async def serve_in_background(server: uvicorn.Server) -> asyncio.Task[None]:
    task = asyncio.ensure_future(_serve_or_raise(server))
    while not server.started:  # noqa: ASYNC110 - uvicorn flips this once the socket is bound
        if task.done():
            # The serve task finished before binding -- raise its stored
            # exception now instead of polling forever.
            exc = task.exception()
            if exc is not None:
                raise exc
            return task
        await asyncio.sleep(0.01)
    return task


async def stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    await task
