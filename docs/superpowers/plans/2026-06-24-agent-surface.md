# Agent Surface (MCP + OpenAPI tool server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose ha-spark's computed data and a small set of gated actions to an external LLM (Claude via MCP, open-webui via OpenAPI), reachable through HA ingress (always) and an optional token-protected published port.

**Architecture:** Migrate the existing aiohttp `ha_spark/api/server.py` onto FastAPI (free OpenAPI + clean MCP mounting), porting `/api/health` `/api/plan` `/api/config` verbatim. Add a protocol-agnostic tool core (`ha_spark/agent/tools.py`) reused by the FastAPI routes and a FastMCP sub-app at `/mcp`. The daemon (`energy/scheduler.py`) serves the app via uvicorn instead of an aiohttp `AppRunner`.

**Tech Stack:** Python 3.11+ asyncio, FastAPI, uvicorn, `mcp` (official SDK / FastMCP), pydantic v2, pytest + respx.

## Global Constraints

- Python is `python3`; mypy runs `strict = true` (`disallow_untyped_defs`) — keep every new def typed and the suite clean.
- ruff lints `E,F,I,UP,B,ASYNC,W`, line length 100.
- pytest runs `asyncio_mode = "auto"` (no `@pytest.mark.asyncio`); mock HTTP with `respx`, use temp SQLite for stores.
- All three gates must be green before any commit lands: `ruff check .`, `mypy ha_spark`, `pytest -q`.
- Secrets (`agent_api_token`, `octopus_api_key`, `SUPERVISOR_TOKEN`, `HA_TOKEN`) must never appear in any response, log, error, or the OpenAPI schema.
- The LLM never reaches `call_service`: act tools route through `run_once`/the planner; real writes still require `PROACTIVE_MODE == on`.
- Dependencies are pinned and minimal; only `fastapi`, `uvicorn`, `mcp` are added.
- Keep `ha_spark_addon/config.yaml` `options`/`schema` and `config.py:_OPTION_KEYS` in sync (an existing test enforces this).
- Add-on ingress serves on fixed internal port `8099` (`api.server.INGRESS_PORT`, `config.yaml: ingress_port`).

---

### Task 1: Migrate `api/server.py` to FastAPI (port existing routes)

Replace the aiohttp app with a FastAPI app that reproduces the current behaviour exactly. `AppState` is unchanged. This task is self-contained: the ported routes pass a rewritten `tests/test_api.py`.

**Files:**
- Modify: `pyproject.toml` (add deps)
- Modify: `ha_spark/api/server.py` (aiohttp → FastAPI; keep `AppState`, `INGRESS_PORT`, `OPTIONS_PATH`)
- Modify: `tests/test_api.py` (aiohttp `TestClient` → FastAPI `TestClient`)

**Interfaces:**
- Produces:
  - `build_app(state: AppState) -> fastapi.FastAPI` (replaces `create_app`)
  - unchanged `AppState` with `.set_plan`, `.current_options`, `.apply_options`
  - constants `INGRESS_PORT: int`, `OPTIONS_PATH: Path`
  - `STATE_ATTR = "ha_spark_state"` stored on `app.state`
- Consumes: `energy/publish.py:plan_to_payload(plan, settings) -> list[Entity]` where each `Entity` is a 3-tuple `(entity_id, state, attributes)`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, extend the `dependencies` list:

```toml
dependencies = [
    "httpx>=0.27",
    "websockets>=12.0",
    "aiosqlite>=0.20",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "aiohttp>=3.9",
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "mcp>=1.2",
]
```

- [ ] **Step 2: Install and verify imports**

Run: `pip install -e ".[dev]" && python3 -c "import fastapi, uvicorn, mcp; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Rewrite `tests/test_api.py` to FastAPI TestClient (failing)**

Keep the `_plan(...)` and `_state(...)` helpers as-is (they build `ChargePlan`/`AppState`). Replace the aiohttp client usage. Representative test (port the existing three-route coverage):

```python
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app


def _client(state: AppState) -> TestClient:
    return TestClient(build_app(state))


def test_get_plan_returns_entities(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.set_plan(_plan())
    with _client(state) as client:
        resp = client.get("/api/plan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_at"] is not None
    assert any(e["entity_id"].startswith("sensor.ha_spark") for e in body["plan"])


def test_get_plan_empty(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.get("/api/plan")
    assert resp.json() == {"plan": None, "generated_at": None}


def test_get_config_returns_options(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    assert "min_soc" in resp.json()


def test_post_config_persists_and_reloads(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with _client(state) as client:
        resp = client.post("/api/config", json={"min_soc": 25.0})
    assert resp.status_code == 200
    assert resp.json()["min_soc"] == 25.0
    assert json.loads((tmp_path / "options.json").read_text())["min_soc"] == 25.0


def test_post_config_rejects_non_object(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.post("/api/config", json=[1, 2, 3])
    assert resp.status_code == 400


def test_health_ok(tmp_path: Path) -> None:
    with _client(_state(tmp_path)) as client:
        resp = client.get("/api/health")
    assert resp.json()["status"] == "ok"
```

Run: `pytest tests/test_api.py -q`
Expected: FAIL with `ImportError: cannot import name 'build_app'`.

- [ ] **Step 4: Rewrite `ha_spark/api/server.py` on FastAPI**

Keep the module docstring intent, `AppState`, `INGRESS_PORT`, `OPTIONS_PATH`, `_iso`. Replace the aiohttp app/handlers/`start_server`/`stop_server`:

```python
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ha_spark.config import _OPTION_KEYS, Settings, load_settings
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.logging import get_logger

log = get_logger(__name__)

INGRESS_PORT = 8099
OPTIONS_PATH = Path("/data/options.json")
STATE_ATTR = "ha_spark_state"


@dataclass
class AppState:
    settings: Settings
    options_path: Path
    reload: Callable[[], Settings] = load_settings
    plan: ChargePlan | None = None
    plan_at: datetime | None = None

    def set_plan(self, plan: ChargePlan) -> None:
        self.plan = plan
        self.plan_at = datetime.now(UTC)

    def current_options(self) -> dict[str, Any]:
        return {key: getattr(self.settings, key) for key in _OPTION_KEYS}

    def apply_options(self, updates: dict[str, Any]) -> Settings:
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
    return getattr(request.app.state, STATE_ATTR)


def build_app(state: AppState) -> FastAPI:
    app = FastAPI(title="ha-spark", docs_url=None, redoc_url=None)
    setattr(app.state, STATE_ATTR, state)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        st = _state_of(request)
        return {"status": "ok", "plan_at": _iso(st.plan_at)}

    @app.get("/api/plan")
    async def get_plan(request: Request) -> dict[str, Any]:
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
        return _state_of(request).current_options()

    @app.post("/api/config")
    async def post_config(request: Request) -> JSONResponse:
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

    return app
```

> Note: `start_server`/`stop_server` are intentionally removed here; Task 2 replaces the daemon's use of them with uvicorn. Do not reference them after this task.

- [ ] **Step 5: Run the ported API tests**

Run: `pytest tests/test_api.py -q`
Expected: PASS (all ported tests green).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml ha_spark/api/server.py tests/test_api.py
git commit -m "refactor(api): migrate add-on HTTP API from aiohttp to FastAPI"
```

---

### Task 2: Serve the FastAPI app from the daemon via uvicorn

Rewire `run_forever` to host `build_app(state)` on `INGRESS_PORT` using a `uvicorn.Server` task, replacing the deleted aiohttp `AppRunner` path.

**Files:**
- Modify: `ha_spark/api/server.py` (add uvicorn serve helpers)
- Modify: `ha_spark/energy/scheduler.py:run_forever` (lines ~24-30 imports, ~206-213 startup, ~267-269 teardown)
- Test: `tests/test_api_serve.py` (new)

**Interfaces:**
- Produces in `api/server.py`:
  - `def make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server`
  - `async def serve_in_background(server: uvicorn.Server) -> asyncio.Task[None]` — starts `server.serve()` as a task and waits until `server.started`
  - `async def stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None`
- Consumes: `build_app`, `AppState`, `INGRESS_PORT` from Task 1.

- [ ] **Step 1: Write the failing serve test**

```python
import asyncio
from datetime import time
from pathlib import Path

import httpx

from ha_spark.api.server import (
    AppState,
    build_app,
    make_server,
    serve_in_background,
    stop_server,
)
from ha_spark.config import Settings


def _state(tmp_path: Path) -> AppState:
    return AppState(settings=Settings(), options_path=tmp_path / "options.json")  # type: ignore[call-arg]


async def test_serves_health_on_a_port(tmp_path: Path) -> None:
    server = make_server(build_app(_state(tmp_path)), "127.0.0.1", 8123)
    task = await serve_in_background(server)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:8123/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        await stop_server(server, task)
```

Run: `pytest tests/test_api_serve.py -q`
Expected: FAIL with `ImportError: cannot import name 'make_server'`.

- [ ] **Step 2: Add uvicorn serve helpers to `api/server.py`**

```python
import asyncio

import uvicorn


def make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    return uvicorn.Server(config)


async def serve_in_background(server: uvicorn.Server) -> asyncio.Task[None]:
    task = asyncio.ensure_future(server.serve())
    while not server.started:  # uvicorn flips this once the socket is bound
        await asyncio.sleep(0.01)
    return task


async def stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    await task
```

Run: `pytest tests/test_api_serve.py -q`
Expected: PASS.

- [ ] **Step 3: Rewire `run_forever` to uvicorn**

In `ha_spark/energy/scheduler.py`, change the import block (was `from ha_spark.api.server import (INGRESS_PORT, OPTIONS_PATH, AppState, start_server, stop_server)`):

```python
from ha_spark.api.server import (
    INGRESS_PORT,
    OPTIONS_PATH,
    AppState,
    build_app,
    make_server,
    serve_in_background,
    stop_server,
)
```

Replace the startup block (currently builds `AppState`, `runner = await start_server(state)`):

```python
    state = AppState(settings=settings, options_path=OPTIONS_PATH)
    server = make_server(build_app(state), "0.0.0.0", INGRESS_PORT)  # noqa: S104 - ingress only
    serve_task: asyncio.Task[None] | None = None
    try:
        serve_task = await serve_in_background(server)
        log.info("HTTP API listening on :%d (ingress)", INGRESS_PORT)
    except Exception:
        log.exception("HTTP API failed to start; continuing without it")
```

Replace the `finally` teardown (currently `if runner is not None: await stop_server(runner)`):

```python
    finally:
        if serve_task is not None:
            await stop_server(server, serve_task)
```

- [ ] **Step 4: Run the scheduler + API suites**

Run: `pytest tests/test_api.py tests/test_api_serve.py tests/test_scheduler.py -q`
Expected: PASS (if `tests/test_scheduler.py` exists; otherwise omit it).

- [ ] **Step 5: Commit**

```bash
git add ha_spark/api/server.py ha_spark/energy/scheduler.py tests/test_api_serve.py
git commit -m "refactor(daemon): serve FastAPI app via uvicorn instead of aiohttp runner"
```

---

### Task 3: Tool core — read tools

Create `ha_spark/agent/tools.py` with the read functions and their pydantic result models. Each takes `settings: Settings`, builds its own HA client (mirroring `run_once`), and returns a typed model. Nested dataclasses are coerced with `fastapi.encoders.jsonable_encoder` (handles enums/dates).

**Files:**
- Create: `ha_spark/agent/__init__.py` (empty)
- Create: `ha_spark/agent/tools.py`
- Test: `tests/test_agent_tools_read.py`

**Interfaces:**
- Produces (all `async`, all take `settings: Settings`):
  - `get_plan(settings) -> PlanResult` — `PlanResult(plan: list[dict], generated_at: str | None)`
  - `get_state(settings) -> StateResult` — `StateResult(inputs: dict)`
  - `get_forecast(settings) -> ForecastResult` — `ForecastResult(load_kwh: float, slots: list[float] | None, source: str)`
  - `get_predictions(settings) -> PredictionsResult` — `PredictionsResult(decisions: list[dict])`
  - `get_health(settings) -> HealthResult` — `HealthResult(checks: list[dict])`
  - `get_context(settings) -> ContextResult` — `ContextResult(facts: list[dict])`
- Consumes: `gather_inputs`, `compute_plan`, `plan_to_payload`, `orchestrate`, `run_health`, `ContextStore`.

- [ ] **Step 1: Write failing read-tool tests**

```python
from datetime import date
from pathlib import Path

import httpx
import respx

from ha_spark.agent import tools
from ha_spark.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db")
    )


@respx.mock
async def test_get_plan_returns_entities(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await tools.get_plan(_settings(tmp_path))
    assert result.generated_at is not None
    assert isinstance(result.plan, list)


async def test_get_context_lists_added_facts(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    from ha_spark.energy.context import ContextStore

    async with ContextStore(s.db_path) as store:
        await store.add("away", date(2026, 7, 1), date(2026, 7, 5), note="holiday")
    result = await tools.get_context(s)
    assert result.facts[0]["kind"] == "away"
    assert result.facts[0]["note"] == "holiday"
```

Run: `pytest tests/test_agent_tools_read.py -q`
Expected: FAIL with `ModuleNotFoundError: ha_spark.agent`.

- [ ] **Step 2: Implement `ha_spark/agent/tools.py` read tools**

```python
"""Protocol-agnostic agent tool core.

Each function takes ``Settings`` and returns a pydantic model. The FastAPI
routes and the FastMCP server both call these; a future cloud-inference tier
would too. No HTTP/transport code lives here.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from ha_spark.config import Settings
from ha_spark.energy.context import ContextStore
from ha_spark.energy.orchestrator import orchestrate
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.health import run_health


class PlanResult(BaseModel):
    plan: list[dict[str, object]]
    generated_at: str | None


class StateResult(BaseModel):
    inputs: dict[str, object]


class ForecastResult(BaseModel):
    load_kwh: float
    slots: list[float] | None
    source: str


class PredictionsResult(BaseModel):
    decisions: list[dict[str, object]]


class HealthResult(BaseModel):
    checks: list[dict[str, object]]


class ContextResult(BaseModel):
    facts: list[dict[str, object]]


def _rest(settings: Settings) -> HomeAssistantRest:
    return HomeAssistantRest(settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout)


async def get_plan(settings: Settings) -> PlanResult:
    async with _rest(settings) as rest:
        inputs, cfg, _src = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
    entities = [
        {"entity_id": eid, "state": value, "attributes": attrs}
        for eid, value, attrs in plan_to_payload(plan, settings)
    ]
    return PlanResult(plan=entities, generated_at=datetime.now(UTC).isoformat())


async def get_state(settings: Settings) -> StateResult:
    async with _rest(settings) as rest:
        inputs, _cfg, _src = await gather_inputs(settings, rest)
    data = jsonable_encoder(dataclasses.asdict(inputs))
    data.pop("load_slots", None)  # large; available via get_forecast
    return StateResult(inputs=data)


async def get_forecast(settings: Settings) -> ForecastResult:
    async with _rest(settings) as rest:
        inputs, cfg, source = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
    slots = list(inputs.load_slots) if inputs.load_slots is not None else None
    return ForecastResult(load_kwh=plan.load_kwh, slots=slots, source=source)


async def get_predictions(settings: Settings) -> PredictionsResult:
    decisions = await orchestrate(settings)
    return PredictionsResult(decisions=[jsonable_encoder(dataclasses.asdict(d)) for d in decisions])


async def get_health(settings: Settings) -> HealthResult:
    checks = await run_health(settings)
    return HealthResult(
        checks=[{"name": c.name, "status": c.status.name, "detail": c.detail} for c in checks]
    )


async def get_context(settings: Settings) -> ContextResult:
    async with ContextStore(settings.db_path) as store:
        facts = await store.list_all()
    return ContextResult(facts=[jsonable_encoder(dataclasses.asdict(f)) for f in facts])
```

> If `PlannerInputs`/`Decision`/`ContextEntry` are pydantic models rather than dataclasses, replace `dataclasses.asdict(x)` with `x.model_dump()`. Verify with `python3 -c "import dataclasses, ha_spark.energy.models as m; print(dataclasses.is_dataclass(m.PlannerInputs))"` before implementing and pick the matching call.

- [ ] **Step 3: Run read-tool tests**

Run: `pytest tests/test_agent_tools_read.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ha_spark/agent/__init__.py ha_spark/agent/tools.py tests/test_agent_tools_read.py
git commit -m "feat(agent): tool core read functions (plan/state/forecast/predictions/health/context)"
```

---

### Task 4: Tool core — act tools

Add `add_context` and `run_plan`. `run_plan` reuses `run_once` (apply stays PROACTIVE_MODE-gated, so a `simulate`/`off` run performs no real `call_service`).

**Files:**
- Modify: `ha_spark/agent/tools.py`
- Test: `tests/test_agent_tools_act.py`

**Interfaces:**
- Produces:
  - `add_context(settings, kind: str, start_date: date, end_date: date, note: str = "") -> ContextResult`
  - `run_plan(settings) -> PlanResult`
- Consumes: `ContextStore.add`, `energy/scheduler.py:run_once`, `PlanResult`/`ContextResult` from Task 3.

- [ ] **Step 1: Write failing act-tool tests**

```python
from datetime import date
from pathlib import Path

import httpx
import respx

from ha_spark.agent import tools
from ha_spark.config import Settings


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", db_path=str(tmp_path / "t.db"), **kw
    )


async def test_add_context_persists(tmp_path: Path) -> None:
    result = await tools.add_context(
        _settings(tmp_path), "away", date(2026, 7, 1), date(2026, 7, 5), note="hol"
    )
    assert any(f["kind"] == "away" for f in result.facts)


@respx.mock
async def test_run_plan_simulate_makes_no_service_call(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    service = respx.post(url__regex=r"http://ha\.test/api/services/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    await tools.run_plan(_settings(tmp_path, proactive_mode="simulate"))
    assert not service.called
```

Run: `pytest tests/test_agent_tools_act.py -q`
Expected: FAIL with `AttributeError: module 'ha_spark.agent.tools' has no attribute 'add_context'`.

- [ ] **Step 2: Implement the act tools**

Append to `ha_spark/agent/tools.py` (add `from datetime import date` and the `run_once` import at the top):

```python
from datetime import date  # add to existing datetime import line

from ha_spark.energy.scheduler import run_once


async def add_context(
    settings: Settings, kind: str, start_date: date, end_date: date, note: str = ""
) -> ContextResult:
    async with ContextStore(settings.db_path) as store:
        await store.add(kind, start_date, end_date, note=note, source="agent")
    return await get_context(settings)


async def run_plan(settings: Settings) -> PlanResult:
    plan = await run_once(settings)
    entities = [
        {"entity_id": eid, "state": value, "attributes": attrs}
        for eid, value, attrs in plan_to_payload(plan, settings)
    ]
    return PlanResult(plan=entities, generated_at=datetime.now(UTC).isoformat())
```

> `ContextStore.add` raises `ValueError` on an unknown `kind` or reversed dates — the route layer (Task 7) maps that to HTTP 400.

- [ ] **Step 3: Run act-tool tests**

Run: `pytest tests/test_agent_tools_act.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ha_spark/agent/tools.py tests/test_agent_tools_act.py
git commit -m "feat(agent): tool core act functions (add_context, run_plan)"
```

---

### Task 5: New config options + add-on schema

Add `agent_surface`, `agent_exposure`, `agent_api_token`, `agent_expose_port` to `Settings`, `_OPTION_KEYS`, and the add-on `config.yaml` (options + schema). Keep the existing sync test green.

**Files:**
- Modify: `ha_spark/config.py` (Settings fields + `_OPTION_KEYS`)
- Modify: `ha_spark_addon/config.yaml` (`options` + `schema`)
- Test: `tests/test_config.py` (add coverage; the option-sync test already exists)

**Interfaces:**
- Produces on `Settings`: `agent_surface: Literal["off", "on"]`, `agent_exposure: Literal["read", "read_act", "read_write"]`, `agent_api_token: str`, `agent_expose_port: bool`.

- [ ] **Step 1: Write failing config tests**

```python
from ha_spark.config import _OPTION_KEYS, Settings


def test_agent_defaults() -> None:
    s = Settings(ha_url="http://ha.test", ha_token="x")  # type: ignore[call-arg]
    assert s.agent_surface == "off"
    assert s.agent_exposure == "read_act"
    assert s.agent_expose_port is False
    assert s.agent_api_token == ""


def test_agent_options_in_whitelist() -> None:
    for key in ("agent_surface", "agent_exposure", "agent_api_token", "agent_expose_port"):
        assert key in _OPTION_KEYS
```

Run: `pytest tests/test_config.py -q -k agent`
Expected: FAIL (attributes/keys missing).

- [ ] **Step 2: Add fields to `Settings` and `_OPTION_KEYS`**

In `ha_spark/config.py`, add to the `Settings` class (near the other operational fields):

```python
    agent_surface: Literal["off", "on"] = Field(default="off")
    agent_exposure: Literal["read", "read_act", "read_write"] = Field(default="read_act")
    agent_api_token: str = Field(default="")
    agent_expose_port: bool = Field(default=False)
```

Add the four key strings to the `_OPTION_KEYS` frozenset.

- [ ] **Step 3: Add to `ha_spark_addon/config.yaml`**

Under `options:`:

```yaml
  agent_surface: off
  agent_exposure: read_act
  agent_expose_port: false
```

Under `schema:`:

```yaml
  agent_surface: list(off|on)
  agent_exposure: list(read|read_act|read_write)
  agent_api_token: password?
  agent_expose_port: bool
```

> `agent_api_token` has no `options:` default (blank → auto-generated at runtime), matching how `octopus_api_key` is schema-only.

- [ ] **Step 4: Run config + sync tests**

Run: `pytest tests/test_config.py -q`
Expected: PASS (including the existing options/schema sync test).

- [ ] **Step 5: Commit**

```bash
git add ha_spark/config.py ha_spark_addon/config.yaml tests/test_config.py
git commit -m "feat(config): agent surface options (surface/exposure/token/expose_port)"
```

---

### Task 6: Bearer-token auth helper

Create `ha_spark/agent/auth.py`: resolve the token (configured value, else a `/data`-persisted generated one) and a verifier. A blank-configured token auto-generates once and is logged once.

**Files:**
- Create: `ha_spark/agent/auth.py`
- Test: `tests/test_agent_auth.py`

**Interfaces:**
- Produces:
  - `resolve_token(settings: Settings, token_path: Path) -> str` — returns `settings.agent_api_token` if set, else reads `token_path`, else generates (`secrets.token_urlsafe(32)`), writes it `0o600`, logs once, returns it.
  - `verify(header_value: str | None, token: str) -> bool` — constant-time check of a `Bearer <token>` header.
  - `TOKEN_PATH = Path("/data/agent_token")`

- [ ] **Step 1: Write failing auth tests**

```python
from pathlib import Path

from ha_spark.agent.auth import resolve_token, verify
from ha_spark.config import Settings


def _settings(**kw: object) -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="x", **kw)  # type: ignore[call-arg]


def test_configured_token_wins(tmp_path: Path) -> None:
    tok = resolve_token(_settings(agent_api_token="abc"), tmp_path / "agent_token")
    assert tok == "abc"


def test_generates_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "agent_token"
    first = resolve_token(_settings(), path)
    assert first and path.read_text().strip() == first
    assert resolve_token(_settings(), path) == first  # stable across calls


def test_verify() -> None:
    assert verify("Bearer abc", "abc") is True
    assert verify("Bearer wrong", "abc") is False
    assert verify(None, "abc") is False
    assert verify("abc", "abc") is False  # missing scheme
```

Run: `pytest tests/test_agent_auth.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 2: Implement `ha_spark/agent/auth.py`**

```python
"""Bearer-token auth for the agent surface's published port."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from ha_spark.config import Settings
from ha_spark.logging import get_logger

log = get_logger(__name__)

TOKEN_PATH = Path("/data/agent_token")


def resolve_token(settings: Settings, token_path: Path = TOKEN_PATH) -> str:
    if settings.agent_api_token:
        return settings.agent_api_token
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    log.info("Generated agent API token (printed once): %s", token)
    return token


def verify(header_value: str | None, token: str) -> bool:
    if not header_value or not header_value.startswith("Bearer "):
        return False
    presented = header_value[len("Bearer ") :]
    return hmac.compare_digest(presented, token)
```

- [ ] **Step 3: Run auth tests**

Run: `pytest tests/test_agent_auth.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ha_spark/agent/auth.py tests/test_agent_auth.py
git commit -m "feat(agent): bearer-token resolve/verify helper"
```

---

### Task 7: Register tool routes with exposure gating + port auth

Wire the tool core into the FastAPI app: read routes always; act routes only at `read_act`/`read_write`; `set_config` only at `read_write` (reusing `AppState.apply_options`). A `require_token` flag gates a router with bearer auth (used by the published port in Task 9); the ingress app mounts the same routes without the token requirement.

**Files:**
- Modify: `ha_spark/api/server.py` (`build_app` gains a `require_token: bool = False` param + token; add an agent router builder)
- Test: `tests/test_agent_routes.py`

**Interfaces:**
- Produces:
  - `build_app(state: AppState, *, require_token: bool = False, token: str = "") -> FastAPI` — when `require_token`, every request must carry `Authorization: Bearer <token>` (401 otherwise).
  - Routes: `GET /agent/plan|state|forecast|predictions|health|context`; `POST /agent/context` (act); `POST /agent/run` (act); `POST /agent/config` (write). Routes absent below their tier per `state.settings.agent_exposure`.

- [ ] **Step 1: Write failing route/gating tests**

```python
from datetime import time
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings


def _state(tmp_path: Path, exposure: str = "read_act") -> AppState:
    return AppState(  # type: ignore[call-arg]
        settings=Settings(
            ha_url="http://ha.test", ha_token="x",
            db_path=str(tmp_path / "t.db"), agent_exposure=exposure,
        ),
        options_path=tmp_path / "options.json",
    )


@respx.mock
def test_read_route_available(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    with TestClient(build_app(_state(tmp_path))) as client:
        assert client.get("/agent/plan").status_code == 200


def test_act_route_absent_in_read_mode(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read"))) as client:
        assert client.post("/agent/context", json={}).status_code == 404


def test_config_route_absent_below_read_write(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read_act"))) as client:
        assert client.post("/agent/config", json={"min_soc": 30}).status_code == 404


def test_config_route_present_in_read_write(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read_write"))) as client:
        resp = client.post("/agent/config", json={"min_soc": 30.0})
    assert resp.status_code == 200
    assert resp.json()["min_soc"] == 30.0


@respx.mock
def test_token_required_when_configured(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app) as client:
        assert client.get("/agent/plan").status_code == 401
        ok = client.get("/agent/plan", headers={"Authorization": "Bearer sekret"})
        assert ok.status_code == 200
```

Run: `pytest tests/test_agent_routes.py -q`
Expected: FAIL (routes/params missing).

- [ ] **Step 2: Add the agent router + token gate to `build_app`**

In `ha_spark/api/server.py`, import the tool core and auth, and extend `build_app`:

```python
from fastapi import Depends, FastAPI, Header, HTTPException, Request

from ha_spark.agent import auth, tools


def _agent_router(state: AppState) -> "APIRouter":
    from fastapi import APIRouter

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
```

Change the `build_app` signature and add the token dependency + mount the router:

```python
def build_app(state: AppState, *, require_token: bool = False, token: str = "") -> FastAPI:
    app = FastAPI(title="ha-spark", docs_url=None, redoc_url=None)
    setattr(app.state, STATE_ATTR, state)

    if require_token:
        async def _auth(authorization: str | None = Header(default=None)) -> None:
            if not auth.verify(authorization, token):
                raise HTTPException(status_code=401, detail="invalid or missing token")
        app.router.dependencies.append(Depends(_auth))

    # ... existing /api/* route definitions unchanged ...

    app.include_router(_agent_router(state))
    return app
```

> Keep the existing `/api/health`, `/api/plan`, `/api/config` definitions exactly as in Task 1 between `setattr(...)` and `include_router`.

- [ ] **Step 3: Run route tests**

Run: `pytest tests/test_agent_routes.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ha_spark/api/server.py tests/test_agent_routes.py
git commit -m "feat(agent): tool routes with exposure gating and optional token auth"
```

---

### Task 8: MCP adapter at `/mcp`

Expose the same tools over MCP by mounting a FastMCP server's Streamable-HTTP ASGI app at `/mcp`, registering tools per the current exposure level.

**Files:**
- Create: `ha_spark/agent/mcp_server.py`
- Modify: `ha_spark/api/server.py` (`build_app` mounts the MCP app)
- Test: `tests/test_agent_mcp.py`

**Interfaces:**
- Produces: `build_mcp(state: AppState) -> FastMCP` registering `get_plan`/`get_state`/`get_forecast`/`get_predictions`/`get_health`/`get_context` always, plus `add_context`/`run_plan` at `read_act`+, plus `set_config` at `read_write`.
- Consumes: `tools.*`, `AppState`.

- [ ] **Step 1: Write a failing MCP registration test**

(Test the registration surface directly — no network — by listing registered tool names per exposure.)

```python
from pathlib import Path

from ha_spark.agent.mcp_server import build_mcp
from ha_spark.api.server import AppState
from ha_spark.config import Settings


def _state(tmp_path: Path, exposure: str) -> AppState:
    return AppState(  # type: ignore[call-arg]
        settings=Settings(ha_url="http://ha.test", ha_token="x", agent_exposure=exposure),
        options_path=tmp_path / "options.json",
    )


async def test_read_mode_excludes_act_tools(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, "read"))
    names = {t.name for t in await mcp.list_tools()}
    assert "get_plan" in names
    assert "run_plan" not in names and "add_context" not in names


async def test_read_write_includes_set_config(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, "read_write"))
    names = {t.name for t in await mcp.list_tools()}
    assert {"run_plan", "set_config"} <= names
```

Run: `pytest tests/test_agent_mcp.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 2: Implement `ha_spark/agent/mcp_server.py`**

```python
"""FastMCP server exposing the agent tool core, gated by exposure level."""

from __future__ import annotations

from datetime import date

from mcp.server.fastmcp import FastMCP

from ha_spark.agent import tools
from ha_spark.api.server import AppState


def build_mcp(state: AppState) -> FastMCP:
    mcp = FastMCP("ha-spark")
    s = state.settings
    exposure = s.agent_exposure

    @mcp.tool()
    async def get_plan() -> dict[str, object]:
        """Current computed charge plan as HA sensor entities."""
        return (await tools.get_plan(state.settings)).model_dump()

    @mcp.tool()
    async def get_state() -> dict[str, object]:
        """Live energy inputs (SoC, solar, EV, rates)."""
        return (await tools.get_state(state.settings)).model_dump()

    @mcp.tool()
    async def get_forecast() -> dict[str, object]:
        """Tomorrow's load forecast and its source."""
        return (await tools.get_forecast(state.settings)).model_dump()

    @mcp.tool()
    async def get_predictions() -> dict[str, object]:
        """Proactive decisions/predictions for tomorrow."""
        return (await tools.get_predictions(state.settings)).model_dump()

    @mcp.tool()
    async def get_health() -> dict[str, object]:
        """Doctor checks (HA/Ollama/DB/history)."""
        return (await tools.get_health(state.settings)).model_dump()

    @mcp.tool()
    async def get_context() -> dict[str, object]:
        """Stored household context facts."""
        return (await tools.get_context(state.settings)).model_dump()

    if exposure in ("read_act", "read_write"):

        @mcp.tool()
        async def add_context(kind: str, start_date: str, end_date: str, note: str = "") -> dict[str, object]:
            """Add a household context fact (e.g. away/guests). Dates are ISO YYYY-MM-DD."""
            return (
                await tools.add_context(
                    state.settings, kind, date.fromisoformat(start_date),
                    date.fromisoformat(end_date), note=note,
                )
            ).model_dump()

        @mcp.tool()
        async def run_plan() -> dict[str, object]:
            """Recompute and apply the plan now (apply still PROACTIVE_MODE-gated)."""
            return (await tools.run_plan(state.settings)).model_dump()

    if exposure == "read_write":

        @mcp.tool()
        async def set_config(updates: dict[str, object]) -> dict[str, object]:
            """Update whitelisted ha-spark options (hot-reloaded)."""
            state.apply_options(updates)
            return state.current_options()

    return mcp
```

- [ ] **Step 3: Mount the MCP app in `build_app`**

In `ha_spark/api/server.py`, before `return app` in `build_app`:

```python
    from ha_spark.agent.mcp_server import build_mcp

    app.mount("/mcp", build_mcp(state).streamable_http_app())
```

> `FastMCP.streamable_http_app()` returns a Starlette ASGI app; FastAPI mounts ASGI sub-apps with `app.mount`. If the installed `mcp` version names this differently, check `python3 -c "from mcp.server.fastmcp import FastMCP; print([m for m in dir(FastMCP) if 'app' in m.lower()])"` and use the streamable-HTTP ASGI accessor it reports.

- [ ] **Step 4: Run MCP tests + full API suite**

Run: `pytest tests/test_agent_mcp.py tests/test_agent_routes.py tests/test_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/agent/mcp_server.py ha_spark/api/server.py tests/test_agent_mcp.py
git commit -m "feat(agent): MCP surface at /mcp gated by exposure level"
```

---

### Task 9: Optional published port in the daemon

When `agent_surface == on` and `agent_expose_port == true`, bind a second uvicorn site on the host-mapped port with `require_token=True`. Ingress site stays token-free.

**Files:**
- Modify: `ha_spark/energy/scheduler.py:run_forever`
- Test: `tests/test_agent_port.py`

**Interfaces:**
- Consumes: `make_server`, `serve_in_background`, `stop_server`, `build_app` (Task 2/7), `auth.resolve_token` (Task 6).
- Constant: published port `8098` (`AGENT_PORT`, added to `api/server.py`; mapped in `config.yaml`).

- [ ] **Step 1: Write the failing port test**

```python
import asyncio
from pathlib import Path

import httpx

from ha_spark.agent.auth import resolve_token
from ha_spark.api.server import AGENT_PORT, AppState, build_app, make_server, serve_in_background, stop_server
from ha_spark.config import Settings


async def test_published_port_requires_token(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="x", agent_api_token="sekret",
        db_path=str(tmp_path / "t.db"),
    )
    state = AppState(settings=settings, options_path=tmp_path / "options.json")
    token = resolve_token(settings, tmp_path / "agent_token")
    server = make_server(build_app(state, require_token=True, token=token), "127.0.0.1", AGENT_PORT)
    task = await serve_in_background(server)
    try:
        async with httpx.AsyncClient() as client:
            base = f"http://127.0.0.1:{AGENT_PORT}/agent/health"
            assert (await client.get(base)).status_code == 401
            ok = await client.get(base, headers={"Authorization": "Bearer sekret"})
            assert ok.status_code == 200
    finally:
        await stop_server(server, task)
```

Run: `pytest tests/test_agent_port.py -q`
Expected: FAIL with `ImportError: cannot import name 'AGENT_PORT'`.

- [ ] **Step 2: Add `AGENT_PORT` and bind the second site**

In `ha_spark/api/server.py` add `AGENT_PORT = 8098` next to `INGRESS_PORT`.

In `ha_spark/energy/scheduler.py:run_forever`, after the ingress server starts, add the optional port server (and import `resolve_token`):

```python
    from ha_spark.agent.auth import resolve_token
    from ha_spark.api.server import AGENT_PORT

    port_server = None
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
```

Extend the `finally` teardown to also stop the port server:

```python
    finally:
        if serve_task is not None:
            await stop_server(server, serve_task)
        if port_server is not None and port_task is not None:
            await stop_server(port_server, port_task)
```

> The ingress server already mounts the agent routes (Task 7) without a token; only this second site sets `require_token=True`.

- [ ] **Step 3: Run the port test**

Run: `pytest tests/test_agent_port.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ha_spark/api/server.py ha_spark/energy/scheduler.py tests/test_agent_port.py
git commit -m "feat(agent): optional token-protected published port"
```

---

### Task 10: Packaging — version, ports, changelog, docs

Wire the add-on packaging so the surface ships and builds.

**Files:**
- Modify: `ha_spark_addon/config.yaml` (`version`, `ports`, `ports_description`)
- Modify: `ha_spark_addon/CHANGELOG.md`
- Modify: `ha_spark_addon/DOCS.md`

- [ ] **Step 1: Bump version + map the optional port**

In `ha_spark_addon/config.yaml`: set `version: "0.13.0"`; add:

```yaml
ports:
  8098/tcp: null
ports_description:
  8098/tcp: ha-spark agent surface (MCP + OpenAPI) — token-protected; leave unmapped unless used
```

> `null` leaves the port unpublished by default; the user maps a host port only when opting in.

- [ ] **Step 2: Add the CHANGELOG entry**

Prepend to `ha_spark_addon/CHANGELOG.md`:

```markdown
## 0.13.0

- Agent surface: ha-spark now exposes its data and a few gated actions to an
  external model. The add-on HTTP API moved to FastAPI; in addition to the
  existing ingress API it serves OpenAPI tool routes under `/agent/*` (for
  open-webui / curl) and an MCP server at `/mcp` (for Claude). A new
  `agent_exposure` option (`read` | `read_act` | `read_write`, default
  `read_act`) controls how much is exposed; act/write still pass the existing
  PROACTIVE_MODE gate, and the LLM never reaches `call_service`. Enable with
  `agent_surface: on`; for external (non-ingress) clients set
  `agent_expose_port: true` and map port 8098 — requests then require the
  bearer token (`agent_api_token`, auto-generated and logged once if blank).
```

- [ ] **Step 3: Document connecting clients in DOCS.md**

Add an "Agent surface" section to `ha_spark_addon/DOCS.md` covering: the four new options; that ingress access needs no token while the published port does; the exact open-webui step (add `http://<host>:8098/openapi.json` as a tool server with the bearer token); the Claude MCP endpoint (`http://<host>:8098/mcp`); and the note that claude.ai-web additionally needs a public HTTPS reverse proxy / Nabu Casa.

- [ ] **Step 4: Verify the whole suite + gates**

Run: `ruff check . && mypy ha_spark && pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add ha_spark_addon/config.yaml ha_spark_addon/CHANGELOG.md ha_spark_addon/DOCS.md
git commit -m "chore(addon): ship agent surface in 0.13.0 (ports, changelog, docs)"
```

> Release (outside this plan, when shipping): bump confirmed on `master`, then `git tag v0.13.0 && git push origin master v0.13.0` so the add-on image can build from the tag.

---

## Self-Review

**Spec coverage:**
- Migrate aiohttp→FastAPI, port routes → Tasks 1–2. ✅
- Tool core (read/act) reusing existing logic → Tasks 3–4. ✅
- `set_config` via `apply_options` (no SQLite store) → Task 7 (`/agent/config`) + Task 8 (`set_config` MCP tool). ✅
- Exposure levels gating tool registration → Tasks 7–8. ✅
- Auth (token on port, ingress trusted) + optional published port → Tasks 6, 9. ✅
- MCP at `/mcp` + OpenAPI for open-webui → Tasks 8, 1 (FastAPI gives `/openapi.json`). ✅
- Config options + schema sync → Task 5. ✅
- Packaging (version 0.13.0, ports, changelog, docs) → Task 10. ✅
- `get_eval` explicitly deferred (spec YAGNI) — no task, intentional. ✅
- Testing matrix (ported routes, auth, gating, actuation gate, set_config, MCP/OpenAPI smoke) → covered across Tasks 1,3,4,7,8,9. ✅

**Placeholder scan:** No "TBD"/"handle errors"/"similar to". The two `>` notes that ask the implementer to check a dataclass-vs-pydantic call and the MCP ASGI accessor name are version-verification steps with the exact command to run, not placeholders.

**Type consistency:** `build_app(state, *, require_token=False, token="")` is defined in Task 1 (base) and extended in Task 7 with the same signature; `make_server`/`serve_in_background`/`stop_server` signatures defined in Task 2 are reused verbatim in Tasks 7/9. Tool result models (`PlanResult`/`StateResult`/`ForecastResult`/`PredictionsResult`/`HealthResult`/`ContextResult`) defined in Task 3 are consumed unchanged in Tasks 4, 7, 8. `AppState`/`apply_options`/`current_options` reused unchanged throughout.
