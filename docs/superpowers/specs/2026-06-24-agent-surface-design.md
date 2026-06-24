# ha-spark agent surface (MCP + OpenAPI tool server) — design

> Status: design approved, pre-implementation.
> Date: 2026-06-24.
> Pulls forward ROADMAP Phase 13 ("MCP agent surface — Jarvis", v1.6.0).

## Context

ha-spark's NL features (`ask`/copilot) require a local Ollama tier, with a
deterministic offline parser as the floor — so a user without a GPU gets only the
offline path. There is no programmatic surface an *external* model can drive.

What already exists (do not rebuild):

- **`ha_spark/api/server.py` (shipped v0.12.0)** — an **aiohttp** web app bound to
  add-on **ingress** (`ingress_port: 8099`), serving `GET /api/health`,
  `GET /api/plan`, and **`GET`/`POST /api/config`**. `POST /api/config`
  (`AppState.apply_options`, server.py:62) already merges into
  `/data/options.json` and **hot-reloads `Settings` with no restart** — this *is*
  the config-write path; the originally-specced SQLite override store is dropped.
- The daemon (`energy/scheduler.py:run_forever`) hosts that app via an aiohttp
  `AppRunner`, sharing one `AppState` so `POST /api/config` is picked up on the
  next loop tick.
- ha-spark already **publishes its plan to HA** as `sensor.ha_spark_*`
  (v0.10.0) and `sensor.ha_spark_predictions` (v0.11.0) via
  `energy/publish.py:plan_to_payload`.

So the device-visibility gap is already addressed; this feature is purely the
**external agent surface** layered onto the existing API.

This feature opens ha-spark as a **tool server**: it exposes its computed data
(plan, state, forecast, eval, predictions, health, context) and a small set of
gated actions to an external LLM host. Two motivations:

1. **Interact with ha-spark's data from the model you already use** — Claude
   (Desktop / claude.ai) or open-webui — instead of only the CLI.
2. **Bring your own model (the product driver).** A user with no GPU for Ollama
   points their cloud model at ha-spark and gets the advanced/agentic features,
   with the *external* model doing the reasoning Ollama would have done locally.
   The offline parser stays as the no-model floor; Ollama stays for local-first;
   the tool server adds a third lane (external brain, ha-spark as tool provider).

Intended outcome: an authenticated, opt-in surface, served from the add-on,
speaking **both** MCP (for Claude) and OpenAPI (for open-webui / curl / scripts),
with read / act / write access controlled by a single config level and the
existing actuation guard rails preserved underneath.

### Direction & scope decisions (from brainstorming)

- **Tool server first**, architected so an internal cloud-inference tier (ha-spark
  calling Claude/OpenAI as an Ollama alternative) can be added later **without
  rework** — both consume the same tool core.
- **Both protocols, one process** — not two servers, not an end-user proxy.
- **Reachable via ingress always + an optional published port.**
- **Exposure level is a config knob** (`read | read_act | read_write`), default
  `read_act` (the expected 99% setting).

## Architecture

**Migrate the existing aiohttp `api/server.py` onto FastAPI** (decision: free
OpenAPI for open-webui + clean MCP mounting), porting the current `/api/plan` +
`/api/config` behaviour verbatim, then layer the agent tools on top. One
protocol-agnostic **tool core** is wrapped by the FastAPI routes (OpenAPI, for
open-webui / curl) and a FastMCP sub-app at `/mcp` (for Claude). Served from a
single **uvicorn** ASGI process inside the add-on.

```
            ┌─────────────── ha_spark/api (FastAPI) ─────────────┐
 Claude  ──▶ /mcp  (FastMCP, `mcp` SDK)  ─┐
 open-webui ▶ /openapi.json + REST routes ┼─▶ agent/tools.py (tool core) ─▶ existing
 curl/scripts ▶ REST routes ──────────────┘    │  planner / sources / forecast /
                                                │  orchestrator / health / context /
                                                │  publish.plan_to_payload
   reached via: HA ingress (always, no token)  +  optional published port (token)
```

### Components

- **`agent/tools.py`** — the tool core: typed `async` functions returning pydantic
  models, the single source of truth (the future cloud-inference tier calls these
  same functions). No transport/HTTP code. Reuses `gather_inputs`+`compute_plan`,
  `orchestrate`, `run_health`, `ContextStore`, `plan_to_payload`, `run_once`.
- **`api/server.py`** — **migrated to FastAPI.** Keeps `AppState` (incl.
  `apply_options` / hot reload) and the ported routes `GET /api/health`,
  `GET /api/plan`, `GET`/`POST /api/config`; adds the agent tool routes; mounts
  the FastMCP sub-app at `/mcp`; installs the auth dependency and exposure-level
  registration.
- **`agent/auth.py`** — bearer-token load / generate / verify (FastAPI dependency).
- **`energy/scheduler.py:run_forever`** — rewired: replace the aiohttp `AppRunner`
  /`TCPSite` (`start_server`/`stop_server`) with a `uvicorn.Server` run as a
  concurrent task in the same event loop, still sharing `AppState`. Binds the
  ingress port always; also binds the published port when `agent_expose_port`.

### Dependencies added

`fastapi`, `uvicorn` (replacing the `aiohttp` web-server usage in `api/server.py`;
`aiohttp` stays a dependency — still used elsewhere), and `mcp`. All reputable,
pinned per the dependency rule. No `fastapi-mcp` and no `mcpo` proxy for the end
user. FastMCP ships inside the official `mcp` SDK and mounts as an ASGI sub-app on
the FastAPI app.

## The tool core

Each tool reuses existing logic; near-zero new domain code. Functions live in
`agent/tools.py`; the adapters register them.

| Tool | Tier | Reuses |
|---|---|---|
| `get_plan` | read | `energy/sources.py:gather_inputs` → `energy/planner.py:compute_plan` (the `cli.py:_cmd_plan` path, no apply); serialized via `energy/publish.py:plan_to_payload` (reuses the existing `/api/plan` shape) |
| `get_state` (curated energy snapshot) | read | the `PlannerInputs` returned by `gather_inputs` (SoC, solar, EV, rates) — not a raw HA dump |
| `get_forecast` | read | derived from the same `gather_inputs`+`compute_plan` call — `plan.load_kwh`, `inputs.load_slots`, `load_source` (no separate `predict_home_load` call) |
| `get_predictions` | read | `energy/orchestrator.py:orchestrate(settings) -> list[Decision]` |
| `get_health` | read | `health.py:run_health(settings) -> list[CheckResult]` |
| `get_context` | read | `energy/context.py:ContextStore.list_all` |
| `add_context` | **act** | `ContextStore.add` (validated like `context_intent.ExtractedContext`) |
| `run_plan` | **act** | `energy/scheduler.py:run_once` (apply still PROACTIVE_MODE-gated) |
| `set_config` | **write** | the existing `AppState.apply_options` path (below) |

`get_eval` is **out of v1** (YAGNI): it needs forecast+actual data assembly the
CLI does ad hoc; defer until asked. `get_forecast` covers the common need.

Tool results serialize the existing dataclasses/models — e.g. `get_plan` returns
the `ChargePlan` fields (`energy/models.py`): `soc_now`, `target_soc`,
`required_kwh`, `overnight_current_a`, charge window, `planned_cost` /
`baseline_cost`, `model`, `strategy`, `actions`, etc. Secrets are never included.

## Exposure levels & gating

One config knob `agent_exposure: read | read_act | read_write` (default
`read_act`), mirroring `PROACTIVE_MODE`:

- It controls **which tools are registered at all.** In `read` mode the act/write
  tools do not exist on the surface (not merely refused); `read_act` adds the act
  tools; `read_write` additionally adds `set_config`.
- Act/write tools that are exposed still pass through the **existing** guards
  underneath. `run_plan` actuates only under `PROACTIVE_MODE == on` (and, per the
  v1.0 driver work, `control == ha_spark`); the model never reaches `call_service`
  directly. **The LLM proposes; the deterministic planner still decides** — the
  CLAUDE.md "LLM never controls hardware" invariant is preserved.

### `set_config` persistence (the write tier)

`set_config` **reuses the existing `AppState.apply_options` path** (server.py:62):
merge the update into `/data/options.json`, hot-reload `Settings`, return the new
options. It already whitelists to `_OPTION_KEYS` and validates (a bad value raises
→ mapped to a tool/HTTP 400). No new storage and no restart. The originally-specced
SQLite `runtime_overrides` table is **dropped** — `apply_options` is the canonical
write path and is already shipped and tested. `set_config` is a thin wrapper that
calls it and is registered only at `read_write` exposure.

## Auth & reachability

- **Bearer token required on the published-port path; the ingress path stays
  trusted** (HA's ingress proxy already authenticates the user, and the shipped
  companion integration calls `/api/*` through it with no token — keep that
  working). The token gate is applied per-request based on which site received it.
  Option `agent_api_token` (schema type `password`); if blank, auto-generate on
  first start, persist to `/data`, and print **once** to the add-on log.
- **Ingress always on** (`config.yaml: ingress: true`, `ingress_port`) for an
  HA-side agent / future "Jarvis".
- **Optional published port**, `agent_expose_port: false` by default — opt-in for
  Claude Desktop / open-webui over LAN/Tailnet (same trust model as the existing
  Ollama-over-Tailscale setup). The app binds inside the container regardless;
  the host port is published only when enabled. claude.ai-web additionally needs a
  public HTTPS reverse proxy / Nabu Casa — **documented, not coded.**

### New config options (config.yaml + config.py `_OPTION_KEYS`, kept in sync)

| Option | Type | Default | Meaning |
|---|---|---|---|
| `agent_surface` | `list(off\|on)` | `off` | Master enable (opt-in feature) |
| `agent_exposure` | `list(read\|read_act\|read_write)` | `read_act` | Tool tier exposed |
| `agent_api_token` | `password?` | (blank → auto-gen) | Bearer token |
| `agent_expose_port` | `bool` | `false` | Publish the host port |

## Error handling & security

- Every tool argument validated with pydantic before use — LLM replies and any
  client payloads are untrusted (CLAUDE.md). A bad payload returns a structured
  tool error and **never** crashes the process or actuates hardware.
- Missing/invalid token → `401`. Modest request-size caps; no per-tool rate
  limiting in v1 (YAGNI).
- Secrets (`agent_api_token`, `octopus_api_key`, `SUPERVISOR_TOKEN`, `HA_TOKEN`)
  never appear in any tool output, error message, log line, or the OpenAPI schema.
- Outbound calls remain only to the already-configured endpoints; the surface is
  inbound-only and authenticated.

## Testing

- **Tool-core unit tests** — respx-mocked HA, fake/temp-SQLite store; each tool
  returns its expected model.
- **Ported routes still pass** — the existing `tests/test_api.py` behaviour for
  `/api/health`, `/api/plan`, `/api/config` holds after the FastAPI migration
  (rewritten to FastAPI's `TestClient`).
- **Auth** — request on the published-port path without token → `401`; the ingress
  path without token still works.
- **Exposure gating** — `read` mode → act/write tools absent; `read_act` →
  `set_config` absent.
- **Actuation gate** — `run_plan` under `PROACTIVE_MODE == simulate` performs no
  real `call_service` (assert no write).
- **`set_config`** — calls `apply_options`; the value lands in `options.json` and
  `current_options()` reflects it; non-whitelisted key ignored, invalid value → 400.
- **MCP smoke** — list tools, call `get_plan`.
- **OpenAPI smoke** — FastAPI `TestClient` `GET /api/plan` returns the plan model.
- Quality gates as always: `ruff check .`, `mypy ha_spark`, `pytest -q` all green.

## Out of scope (YAGNI)

- `notify` tool — waits for ROADMAP Phase 10.
- The internal cloud-inference tier — architected-for, not built here.
- claude.ai OAuth / public-HTTPS automation — documented as a user deployment step.
- HA-device / dashboard entity publishing — a separate brainstorm/spec.
- Per-tool rate limiting beyond basic size caps.

## Packaging (per repo conventions)

Next version is **0.13.0** (latest shipped tag is `v0.12.0`). Bump
`ha_spark_addon/config.yaml` `version`, add a `CHANGELOG.md` entry, update
`DOCS.md` (new options + how to connect Claude / open-webui), keep the options
schema + `config.py:_OPTION_KEYS` in sync (enforced by an existing test), add the
optional `ports:` mapping (default unmapped) for the published port, and create a
matching annotated `v0.13.0` tag + GitHub release so the add-on image can build.

## Verification (end-to-end)

1. `agent_surface: on`, `agent_exposure: read_act`, start the add-on; confirm the
   token is printed once to the log and no secret leaks.
2. `curl -H "Authorization: Bearer <token>" http://<host>:<port>/plan` returns the
   current `ChargePlan`; the same call without the header returns `401`.
3. Point open-webui at `/openapi.json`; confirm the read tools appear and
   `get_plan` returns data.
4. Connect Claude (Desktop or via reverse proxy) to `/mcp`; confirm tool discovery
   and a `get_plan` call.
5. Call `add_context` ("away next week"); confirm it lands in `ContextStore` and
   shifts the next plan's load factor.
6. With `PROACTIVE_MODE: simulate`, call `run_plan`; confirm it computes and logs
   but performs no real `call_service`.
7. Set `agent_exposure: read`; confirm act/write tools disappear from both
   `/openapi.json` and the MCP tool list.
