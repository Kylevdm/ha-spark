"""Add-on HTTP API with separate ingress and integration auth on port 8099.

The companion integration uses a per-boot bearer credential on the isolated
``/integration`` surface, which refuses ingress headers. Ingress retains its
peer and owner-header checks, including for mounted apps. Runs in the daemon's
event loop (see :mod:`ha_spark.energy.scheduler`).

Auth: the ingress listener is not mapped to the host network, but other
containers on the Supervisor's internal network can reach it, so the handlers
do not trust their caller by default. In add-on mode the ingress app is built
with ``ingress_peer=INGRESS_PROXY_IP`` and answers 403 to ingress requests whose
socket peer is not HA's ingress proxy (which authenticates the user);
forwarding headers cannot bypass the peer check. In standalone/dev mode the listener binds to
loopback only. The published agent port is gated by a bearer token instead.

Owner-only routes (the app shell, ``/api/mode`` and ``/api/config``) exist
only on the ingress app, never on the published port or the standalone
listener: an agent token must never reach an owner write (#457).

In configuration repair only the shell assets and mode status, the config read,
``POST /api/config/import`` and health answer; everything else is 503.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from ha_spark.agent import auth, tools
from ha_spark.agent.exposure import OPERATION_TIERS, allowed, surface_on
from ha_spark.api.contract import (
    SUPPORTED_API_VERSIONS,
    AuthorityInputs,
    IntegrationLink,
    SiteSnapshot,
    link_view,
    plan_summary,
    version_refusal,
)
from ha_spark.api.static_assets import packaged_assets
from ha_spark.authority import OperatingMode, authority_view, load_authority
from ha_spark.config import ConfigError, Settings
from ha_spark.daemon_channel import DaemonChannel
from ha_spark.energy import v2l
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.plan_run import StoredPlanRun
from ha_spark.energy.publish import plan_to_payload
from ha_spark.integration_discovery import IntegrationDiscovery
from ha_spark.logging import get_logger
from ha_spark.mode_preconditions import entry_preconditions
from ha_spark.settings_service import OPTIONS_PATH as OPTIONS_PATH
from ha_spark.settings_service import RepairTokenConflict, RevisionConflict, SettingsService

log = get_logger(__name__)

# Add-on ingress serves on this fixed internal port (must match config.yaml
# `ingress_port`). Not mapped to the host network, but other containers on the
# Supervisor network can reach it, hence the peer check (see _PeerGate).
INGRESS_PORT = 8099
# The Supervisor's ingress proxy address on the internal hassio network. HA's
# add-on docs: "Only connections from 172.30.32.2 must be allowed."
INGRESS_PROXY_IP = "172.30.32.2"
# Optional published port (Task 9): bound only when the user opts in via
# `agent_surface == "on"` and `agent_expose_port`. Mapped to the host in config.yaml.
AGENT_PORT = 8098
STATE_ATTR = "ha_spark_state"
# The fixed answer to every agent config change (``/agent/config`` and the MCP
# ``set_config`` tool) until owner-approved config proposals exist (#457).
AGENT_CONFIG_REFUSAL: dict[str, str] = {
    "error": "unavailable",
    "detail": "agent config changes need owner approval, which is not available in this version",
}


# Above one 60 s daemon tick, below HA's ingress proxy timeout.
_MODE_REPLY_TIMEOUT_S = 90
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: OperatingMode
    expected_epoch: int | None = Field(ge=1, strict=True)


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)
    changes: dict[str, Any]


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The revision, or in configuration repair the repair token.
    expected: StrictInt | StrictStr = Field(union_mode="left_to_right")
    document: dict[str, Any]


# The routes configuration repair serves (method, path); everything else is 503.
_REPAIR_ROUTES = frozenset({
    ("GET", "/api/mode"), ("GET", "/api/config"),
    ("POST", "/api/config/import"), ("GET", "/api/health"),
})


class AppState:
    """Daemon/API state; all settings operations delegate to the settings service."""

    def __init__(
        self,
        settings: Settings,
        options_path: Path,
        settings_service: SettingsService | None = None,
        plan: ChargePlan | None = None,
        plan_at: datetime | None = None,
        executor: DaemonChannel | None = None,
    ) -> None:
        self.settings_service = settings_service or SettingsService(options_path, settings=settings)
        self.plan = plan
        self.plan_run: StoredPlanRun | None = None
        self.plan_at = plan_at
        self.executor = executor
        self.integration_link = IntegrationLink(None, None, False)
        self.loop_tick: datetime | None = None
        self.soc_observation_ok: bool = False

    def integration_link_view(self) -> dict[str, object]:
        """Read the current link without affecting authority or planning."""
        return link_view(self.integration_link, datetime.now(UTC))

    @property
    def settings(self) -> Settings:
        return self.settings_service.settings

    @settings.setter
    def settings(self, settings: Settings) -> None:
        # Compatibility for tests; production publishes only through save.
        self.settings_service.settings = settings

    def set_plan(self, plan: ChargePlan, plan_run: StoredPlanRun | None = None) -> None:
        """Record the latest computed plan (called by the daemon each run).

        ``plan_run`` is the run the plan came from, kept for view-only readers
        (#496); plans from no run (synthetic startup or fallback plans, test
        fakes) store none, which later readers treat as "no timeline".
        """
        self.plan = plan
        self.plan_run = plan_run
        self.plan_at = datetime.now(UTC)

    def current_options(self) -> dict[str, Any]:
        return self.settings_service.read_view()

    def apply_options(
        self, updates: dict[str, Any], expected_revision: int, actor: str,
    ) -> dict[str, Any]:
        return self.settings_service.save(updates, expected_revision, actor)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _state_of(request: Request) -> AppState:
    state: AppState = getattr(request.app.state, STATE_ATTR)
    return state


class _Gate:
    """FastAPI dependency enforcing :func:`~ha_spark.agent.exposure.allowed`.

    Resolves the operation's tier from :data:`OPERATION_TIERS` and evaluates the
    live ``state.settings`` per request, so runtime exposure changes apply
    without a rebuild. A denied tier 404s, reading the same as a route that was
    never registered -- in particular never a 405, since the path/method always
    matches and the dependency rejects before the handler runs.
    """

    def __init__(self, state: AppState, operation: str) -> None:
        self._state = state
        # Public: the schema filter reads the operation back off the registered
        # route's own gate rather than restating the binding (see
        # :func:`_gate_bindings`).
        self.operation = operation

    async def __call__(self) -> None:
        if not allowed(self._state, OPERATION_TIERS[self.operation]):
            raise HTTPException(status_code=404)


# (path, lowercase method, operation) per gated route -- lowercase to match the
# schema's method keys.
_GateBindings = tuple[tuple[str, str, str], ...]


def _gate_bindings(router: APIRouter) -> _GateBindings:
    """The route -> operation binding, read back off each route's own :class:`_Gate`.

    The gate the request path evaluates is the only place a route's operation is
    named, so the schema filter cannot drift from what is enforced and adding a
    route needs no second edit (#93 flagged this matrix being written twice).
    Only the *binding* is static here; the exposure decision it feeds is still
    taken per request against the live settings.
    """
    bindings: list[tuple[str, str, str]] = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dependency in route.dependencies:
            gate = dependency.dependency
            if isinstance(gate, _Gate):
                bindings.extend(
                    (route.path, method.lower(), gate.operation) for method in route.methods or ()
                )
    return tuple(bindings)


class _SparkAPI(FastAPI):
    """App whose ``/openapi.json`` reflects the *current* gate.

    Routes are registered once and gated per request (:class:`_Gate`), so the
    advertised schema must apply the same predicate per call: an operation
    denied at the live tier is filtered out, keeping "absent below this tier"
    true for discovery as well as execution.
    """

    def __init__(self, *args: Any, gate_bindings: _GateBindings, **kwargs: Any) -> None:
        self._gate_bindings = gate_bindings
        super().__init__(*args, **kwargs)

    def openapi(self) -> dict[str, Any]:
        self.openapi_schema = None  # settings may have changed since the last call
        schema = super().openapi()
        state: AppState = getattr(self.state, STATE_ATTR)
        paths: dict[str, Any] = schema.get("paths", {})
        for path, method, operation in self._gate_bindings:
            if allowed(state, OPERATION_TIERS[operation]):
                continue
            ops: dict[str, Any] | None = paths.get(path)
            if ops is not None:
                ops.pop(method, None)
                if not ops:
                    paths.pop(path, None)
        return schema


def _agent_router(state: AppState) -> APIRouter:
    """Build the ``/agent/*`` router: every route registered once, each behind a
    request-time :class:`_Gate`.

    ``state.settings`` is read per request (never captured here), so
    ``POST /api/config`` exposure changes take effect without a restart, and
    ``agent_surface == "off"`` denies every route (#93).
    """
    router = APIRouter(prefix="/agent")

    @router.get("/plan", dependencies=[Depends(_Gate(state, "get_plan"))])
    async def plan() -> tools.PlanResult:
        return await tools.get_plan(state.settings)

    @router.get("/state", dependencies=[Depends(_Gate(state, "get_state"))])
    async def state_() -> tools.StateResult:
        return await tools.get_state(state.settings)

    @router.get("/forecast", dependencies=[Depends(_Gate(state, "get_forecast"))])
    async def forecast() -> tools.ForecastResult:
        return await tools.get_forecast(state.settings)

    @router.get("/predictions", dependencies=[Depends(_Gate(state, "get_predictions"))])
    async def predictions() -> tools.PredictionsResult:
        return await tools.get_predictions(state.settings)

    @router.get("/health", dependencies=[Depends(_Gate(state, "get_health"))])
    async def health_() -> tools.HealthResult:
        return await tools.get_health(state.settings)

    @router.get("/context", dependencies=[Depends(_Gate(state, "get_context"))])
    async def context() -> tools.ContextResult:
        return await tools.get_context(state.settings)

    @router.post("/context", dependencies=[Depends(_Gate(state, "add_context"))])
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

    @router.post("/run", dependencies=[Depends(_Gate(state, "run_plan"))])
    async def run() -> tools.PlanResult:
        return await tools.run_plan(state.settings, executor=state.executor)

    @router.post("/config", dependencies=[Depends(_Gate(state, "set_config"))])
    async def config() -> JSONResponse:
        # Agents may not change configuration until owner-approved config
        # proposals exist (ADR-0006, #457): refuse without reading the body.
        return JSONResponse(AGENT_CONFIG_REFUSAL, status_code=409)

    return router


def build_app(
    state: AppState,
    *,
    require_token: bool = False,
    token: str = "",
    ingress_peer: str | None = None,
    integration: IntegrationDiscovery | None = None,
) -> FastAPI:
    """Build the FastAPI app with the API routes bound to ``state``.

    ``require_token``/``token`` gate every route (including ``/api/*``) behind
    bearer auth -- used by the published port (Task 9). ``ingress_peer``, set
    for the add-on ingress listener, answers 403 to ingress requests (``/mcp``
    included) whose socket peer is not that address. ``integration`` isolates
    the heartbeat namespace under its per-boot credential before those checks.
    """
    if integration is not None and (
        require_token or ingress_peer is None or state.settings_service.bootstrap.is_standalone
    ):
        raise ValueError("Integration requires the add-on ingress listener")

    # Local import: mcp_server does ``from ha_spark.api.server import AppState``,
    # so a module-top import would cycle.
    from ha_spark.agent.mcp_server import build_mcp

    # FastMCP's streamable-HTTP app owns its own lifespan (a StreamableHTTP
    # session manager); FastAPI does NOT auto-run a mounted sub-app's lifespan,
    # so we adopt it as the app's lifespan or /mcp 500s with "Task group is not
    # initialized". Build the app and read its lifespan BEFORE creating FastAPI.
    mcp_app = build_mcp(state).streamable_http_app()
    # Built before the app so the schema filter is handed the bindings of the
    # very router that gets included below -- one object, no restated table.
    agent_router = _agent_router(state)
    app = _SparkAPI(
        title="ha-spark",
        docs_url=None,
        redoc_url=None,
        lifespan=mcp_app.router.lifespan_context,
        gate_bindings=_gate_bindings(agent_router),
    )
    setattr(app.state, STATE_ATTR, state)
    ingress = ingress_peer is not None and not require_token
    app.state.ingress = ingress
    assets = packaged_assets() if ingress else {}
    ui_paths = {"/", *(f"/static/{name}" for name in assets)} if ingress else set()
    # Added first so the ingress peer check runs before it.
    app.add_middleware(_RepairGate, state=state, ui_paths=ui_paths)
    if ingress_peer is not None:
        app.add_middleware(_PeerGate, peer=ingress_peer)
    if integration is not None:
        app.add_middleware(_IntegrationGate, state=state, integration=integration)

    if require_token:

        async def _auth(authorization: str | None = Header(default=None)) -> None:
            if not auth.verify(authorization, token):
                raise HTTPException(status_code=401, detail="invalid or missing token")

        app.router.dependencies.append(Depends(_auth))

    if ingress:
        def register_asset(path: str, name: str, content: bytes) -> None:
            media_type = {
                ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
            }.get(Path(name).suffix, "text/plain")

            async def asset() -> Response:
                return Response(content, media_type=media_type, headers=_PAGE_HEADERS)

            app.add_api_route(path, asset, methods=["GET"], include_in_schema=False)

        register_asset("/", "index.html", assets["index.html"])
        for name, content in assets.items():
            register_asset(f"/static/{name}", name, content)

        @app.get("/api/mode")
        async def get_mode(request: Request) -> JSONResponse:
            st = _state_of(request)
            if st.executor is None:
                return JSONResponse({"error": "no daemon executor"}, status_code=503)
            if st.settings_service.repair is not None:
                # The stored authority, read only; no settings to check entry against.
                return JSONResponse(
                    st.executor.authority_status(), headers={"Cache-Control": "no-store"},
                )
            preconditions = await entry_preconditions(st.settings)
            # Snapshot after the diagnostic reads; preconditions never grant authority.
            view = dict(st.executor.authority_status())
            view["preconditions"] = preconditions
            return JSONResponse(view, headers={"Cache-Control": "no-store"})

        @app.post("/api/mode")
        async def post_mode(request: Request) -> JSONResponse:
            user = request.headers.get("X-Remote-User-Id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user) is None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if request.headers.get("X-HA-Spark-Request") != "mode":
                return JSONResponse({"error": "forbidden"}, status_code=403)
            content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return JSONResponse({"error": "application/json required"}, status_code=415)
            try:
                body = ModeBody.model_validate(await request.json())
            except (ValueError, ValidationError):
                return JSONResponse({"error": "invalid mode request"}, status_code=400)
            executor = _state_of(request).executor
            if executor is None:
                return JSONResponse({"error": "no daemon executor"}, status_code=503)
            try:
                # Accepted within one daemon tick; a later verdict still lands in the store.
                reply = await asyncio.wait_for(executor.submit_mode(
                    body.target, body.expected_epoch, ingress_user=user,
                ), timeout=_MODE_REPLY_TIMEOUT_S)
            except TimeoutError:
                return JSONResponse(
                    {"error": "pending", "detail": "request queued; reload the mode status"},
                    status_code=504,
                )
            if reply.error:
                status = 409 if reply.error in ("stale_epoch", "transition_in_progress") else 400
                return JSONResponse(
                    {"error": reply.error, "detail": reply.text, "reasons": reply.reasons},
                    status_code=status,
                )
            return JSONResponse(reply.authority or {})

        @app.get("/api/config")
        async def get_config(request: Request) -> dict[str, Any]:
            """Current user options."""
            return _state_of(request).current_options()

        @app.post("/api/config")
        async def post_config(request: Request) -> JSONResponse:
            """Commit a revisioned partial update and publish the daemon's new snapshot."""
            user = request.headers.get("X-Remote-User-Id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user) is None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if request.headers.get("X-HA-Spark-Request") != "config":
                return JSONResponse({"error": "forbidden"}, status_code=403)
            content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return JSONResponse({"error": "application/json required"}, status_code=415)
            try:
                updates = await request.json()
            except Exception:  # noqa: BLE001 - malformed body is a client error
                return JSONResponse({"error": "invalid JSON body"}, status_code=400)
            try:
                body = ConfigBody.model_validate(updates)
            except ValidationError:
                return JSONResponse({"error": "invalid config envelope"}, status_code=400)
            try:
                result = _state_of(request).apply_options(
                    body.changes, body.expected_revision, f"ingress:{user}",
                )
            except RevisionConflict as exc:
                return JSONResponse(
                    {"error": "conflict", "revision": exc.revision}, status_code=409,
                )
            except ConfigError as exc:
                # ConfigError reasons name fields only, never an input value (#412).
                log.warning("Rejecting config update: %s", exc)
                return JSONResponse({"error": str(exc)}, status_code=400)
            except Exception as exc:  # noqa: BLE001 - save/reload failure -> client error
                # Any other exception's text could carry input; report its type only.
                log.warning("Config update failed: %s", type(exc).__name__)
                return JSONResponse({"error": "config update failed"}, status_code=400)
            return JSONResponse(result)

        @app.post("/api/config/import")
        async def post_config_import(request: Request) -> JSONResponse:
            """Replace the configuration; in repair the only write, with the repair token."""
            user = request.headers.get("X-Remote-User-Id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user) is None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if request.headers.get("X-HA-Spark-Request") != "config":
                return JSONResponse({"error": "forbidden"}, status_code=403)
            content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return JSONResponse({"error": "application/json required"}, status_code=415)
            try:
                body = ImportBody.model_validate(await request.json())
            except Exception:  # noqa: BLE001 - malformed body is a client error
                return JSONResponse({"error": "invalid config import"}, status_code=400)
            try:
                result = _state_of(request).settings_service.import_document(
                    body.document, body.expected, f"ingress:{user}",
                )
            except RevisionConflict as exc:
                return JSONResponse(
                    {"error": "conflict", "revision": exc.revision}, status_code=409,
                )
            except RepairTokenConflict as exc:
                return JSONResponse(
                    {"error": "conflict", "repair_token": exc.token}, status_code=409,
                )
            except ConfigError as exc:
                # ConfigError reasons name fields only, never an input value (#412).
                log.warning("Rejecting config import: %s", exc)
                return JSONResponse({"error": str(exc)}, status_code=400)
            except Exception as exc:  # noqa: BLE001 - save failure -> client error
                log.warning("Config import failed: %s", type(exc).__name__)
                return JSONResponse({"error": "config import failed"}, status_code=400)
            return JSONResponse(result)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Liveness + whether a plan has been computed yet (full doctor stays in the CLI)."""
        st = _state_of(request)
        repair = st.settings_service.repair
        if st.executor is not None:
            authority = st.executor.authority_status()
        else:
            loaded = load_authority(
                st.settings if repair is None else st.settings_service.bootstrap
            )
            authority = authority_view(loaded.state, known=loaded.status == "known")
        failed = authority["mode"] == "authority_unknown" or authority["save_failed"]
        result: dict[str, Any] = {
            "status": "fail" if failed or repair is not None else "ok",
            "plan_at": _iso(st.plan_at),
            "daemon": "running" if st.executor is not None else "unavailable",
            "integration_link": st.integration_link_view(),
            "operating_mode": authority["mode"], "authority": authority,
        }
        if repair is not None:
            result["configuration"] = {"status": "configuration_repair", "reasons": repair}
        return result

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

    app.include_router(agent_router)
    # FastAPI router dependencies (the ``_auth`` gate above) do NOT propagate to a
    # mounted ASGI sub-app, so on the published port /mcp would otherwise be
    # reachable without the bearer token while /api/* and /agent/* require it.
    # Wrap the mount in the same check so every inbound surface is gated alike.
    # The surface gate is applied first (checked on every request), then the
    # Origin gate only on the published port, then the token gate outermost so
    # auth 401s before Origin 403s and surface 404s. This matches the /agent/*
    # ordering (router-level _auth runs before the route _Gate).
    mcp_mount = _surface_gated(mcp_app, state)
    app.mount(
        "/mcp",
        _token_gated(_origin_gated(mcp_mount), token) if require_token else mcp_mount,
    )
    return app


def _surface_gated(asgi_app: Any, state: AppState) -> Any:
    """Wrap an ASGI app so ``agent_surface == "off"`` 404s it like an unmounted path.

    The MCP-level gate denies tools individually (see ``mcp_server._Gated``);
    this removes the surface itself, so with the master switch off ``/mcp``
    reads exactly as if it were never mounted. Checked per request against the
    live ``state.settings``. Non-HTTP scopes (lifespan) pass through untouched.
    """

    async def gated(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and not surface_on(state):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        await asgi_app(scope, receive, send)

    return gated


def _origin_gated(asgi_app: Any) -> Any:
    """Reject browser origins on the published MCP port after token authentication."""

    async def gated(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and _has_origin(scope):
            await JSONResponse({"error": "forbidden"}, status_code=403)(scope, receive, send)
            return
        await asgi_app(scope, receive, send)

    return gated


def _has_origin(scope: Any) -> bool:
    return any(name.lower() == b"origin" for name, _ in scope.get("headers", []))


_HEARTBEAT_MAX_BODY = 16 * 1024
# A body that never finishes must not hold the handler: bytes and time are both bounded.
_HEARTBEAT_BODY_TIMEOUT_S = 5.0


def _heartbeat_now() -> datetime:
    return datetime.now(UTC)


def _reject_constant(name: str) -> object:
    """``NaN`` and ``Infinity`` aren't JSON; ``json.loads`` accepts them by default."""
    raise ValueError(name)


def _integration_app(state: AppState, integration: IntegrationDiscovery) -> FastAPI:
    # No slash redirect: its Location would echo the request's query string.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.post("/integration/heartbeat")
    async def heartbeat(request: Request) -> JSONResponse:
        invalid = JSONResponse({"error": "invalid heartbeat"}, status_code=400)
        try:
            length = request.headers.get("content-length")
            if length is not None and int(length) > _HEARTBEAT_MAX_BODY:
                return invalid
            body = bytearray()
            async with asyncio.timeout(_HEARTBEAT_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > _HEARTBEAT_MAX_BODY:
                        return invalid
                    body.extend(chunk)
            payload = json.loads(body, parse_constant=_reject_constant)
            if not isinstance(payload, dict):
                return invalid
        except (ValueError, RecursionError, TimeoutError):
            return invalid
        version = payload.get("api_version")
        release = payload.get("release_version")
        if (type(version) is not int or version < 1 or not isinstance(release, str)
                or not 1 <= len(release) <= 64):
            return invalid
        if version not in SUPPORTED_API_VERSIONS:
            previous = state.integration_link
            state.integration_link = IntegrationLink(previous.snapshot, previous.received_at, True)
            return JSONResponse(version_refusal(), status_code=409,
                                headers={"Cache-Control": "no-store"})
        try:
            snapshot = SiteSnapshot.model_validate(payload)
        except ValidationError:
            return invalid
        now = _heartbeat_now()
        state.integration_link = IntegrationLink(snapshot, now, False)
        if state.executor is not None:
            authority = state.executor.authority_inputs()
        else:
            loaded = load_authority(state.settings)
            authority = AuthorityInputs(loaded.state, loaded.status == "known", False)
        return JSONResponse(
            plan_summary(
                session_id=integration.session_id, built_at=now, loop_tick=state.loop_tick,
                plan=state.plan, plan_at=state.plan_at, soc_observation_ok=state.soc_observation_ok,
                session=(
                    v2l.load_session(state.settings) if state.settings.v2l_power_entity else None
                ),
                settings=state.settings, authority=authority,
            ),
            headers={"Cache-Control": "no-store"},
        )

    return app


class _IntegrationGate:
    """Own the integration namespace before ingress peer and repair checks.

    Classifies on the raw ``scope["path"]``, as the router does only while
    ``root_path`` is empty; :func:`make_server` never sets one.
    """

    def __init__(self, app: Any, state: AppState, integration: IntegrationDiscovery) -> None:
        self.app = app
        self.state = state
        self.integration = integration
        self.heartbeat_app = _integration_app(state, integration)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket") or not (
            scope["path"] == "/integration" or scope["path"].startswith("/integration/")
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = [(k.lower(), v) for k, v in scope.get("headers", [])]
        authorization = next((v.decode("latin-1") for k, v in headers
                              if k == b"authorization"), None)
        if any(k in (b"x-ingress-path", b"x-hass-source") or k.startswith(b"x-remote-user-")
               for k, _ in headers):
            response = JSONResponse({"error": "forbidden"}, status_code=403)
        elif (authorization is not None and not authorization.isascii()) or not auth.verify(
            authorization, self.integration.auth_token,
        ):
            response = JSONResponse({"error": "invalid or missing token"}, status_code=401)
        elif self.state.settings_service.repair is not None:
            response = JSONResponse({"error": "configuration_repair"}, status_code=503)
        else:
            await self.heartbeat_app(scope, receive, send)
            return
        await response(scope, receive, send)


class _RepairGate:
    """ASGI middleware: in configuration repair, 503 everything but :data:`_REPAIR_ROUTES`."""

    def __init__(self, app: Any, state: AppState, ui_paths: set[str]) -> None:
        self.app = app
        self.state = state
        self.routes = _REPAIR_ROUTES | frozenset(("GET", path) for path in ui_paths)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] in ("http", "websocket")
            and self.state.settings_service.repair is not None
            and (scope.get("method"), scope["path"]) not in self.routes
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse({"error": "configuration_repair"}, status_code=503)(
                    scope, receive, send
                )
            return
        await self.app(scope, receive, send)


class _PeerGate:
    """ASGI middleware: only socket peer ``peer`` may make HTTP/WebSocket requests.

    Checks the connection's peer address (``scope["client"]``), never headers
    such as ``X-Forwarded-For``, which any caller can set. Lifespan passes through.
    """

    def __init__(self, app: Any, peer: str) -> None:
        self.app = app
        self.peer = peer

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if not client or client[0] != self.peer:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"error": "forbidden"}, status_code=403)(
                        scope, receive, send
                    )
                return
        await self.app(scope, receive, send)


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
