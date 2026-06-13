# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`ha-spark` is a local-first, natural-language agent for Home Assistant. It talks
to HA over REST + WebSocket, runs inference against a single remote Ollama
instance with a deterministic offline fallback, stays sensor-aware before acting,
will learn household habits over time, and ships as a Home Assistant add-on. Plain
Python 3.11+ / asyncio — no agent framework.

Remote: `https://github.com/Kylevdm/ha-spark.git`. The repo and the Python import
package are both `ha-spark` / `ha_spark`.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ".[dev,habits]" for Phase 5 ML deps

# Run (standalone/dev: set HA_URL + HA_TOKEN in .env first; see .env.example)
python -m ha_spark states                  # list entity states (REST)
python -m ha_spark states --domain light   # filter by domain
python -m ha_spark states --watch          # live stream over WebSocket

# Quality gates — all three must be green before merge
ruff check .
mypy ha_spark
pytest -q
pytest tests/test_rest.py::test_get_states_parses_entities   # single test
```

## Runtime modes (config)

`ha_spark/config.py` (`Settings`, pydantic-settings) supports two modes from one
code path; `load_settings()` overlays `/data/options.json`, then validates:

- **Add-on mode (default).** HA Core is reached through the Supervisor proxy —
  `ha_rest_url` → `http://supervisor/core/api`, `ha_websocket_url` →
  `ws://supervisor/core/websocket` — authenticated with `SUPERVISOR_TOKEN`.
  User-exposed options come from `/data/options.json`. `load_settings()` raises
  `ConfigError` (fail fast) if neither add-on nor dev credentials are present.
- **Standalone/dev escape hatch.** Set **both** `HA_URL` + `HA_TOKEN`
  (`is_standalone` becomes true) to talk to an HA instance directly; the REST/WS
  URLs are then derived from `HA_URL` (http→ws, https→wss).
- `auth_token` resolves to the dev `HA_TOKEN` if set, else `SUPERVISOR_TOKEN`.
  Consumers (e.g. `cli.py`) use `auth_token`, `ha_rest_url`, `ha_websocket_url` —
  never the raw fields — so they are mode-agnostic.

## Architecture

The Home Assistant connectivity layer lives under `ha_spark/ha/`:

- **`config.py`** — `Settings` + `load_settings()` (see Runtime modes above).
- **`ha/rest.py`** — `HomeAssistantRest`, a thin async wrapper over the HA REST
  API (`get_states`, `get_state`, `get_services`, `call_service`). Owns one
  shared `httpx.AsyncClient`; use as an async context manager.
- **`ha/websocket.py`** — `HomeAssistantWebSocket`: auth handshake, subscribes to
  `state_changed`, dispatches to registered async listeners, reconnects with
  exponential backoff (2/4/8/16s). Listener and parse failures are isolated so a
  bad payload or one listener never kills the loop.
- **`ha/state_cache.py`** — `StateCache`: the "sensor-aware" substrate. Seeded
  from a full REST dump (`seed`), then kept live by registering
  `on_state_changed` as a WebSocket listener. Read this before acting.
- **`ha/models.py`** — pydantic models: `EntityState` (with `domain` /
  `friendly_name` helpers), `StateChangedEvent`, `ServiceCall`.
- **`cli.py`** — argparse CLI; the `states` command wires REST seed + optional WS
  watch together. `__main__.py` makes `python -m ha_spark` run it.

Data flow: REST seeds the `StateCache`; the WebSocket stream feeds incremental
`state_changed` updates into the same cache; the agent (future phases) reads the
cache, decides via the Ollama router, and acts through `call_service`.

## Design direction (`usernotes.md` is authoritative)

`usernotes.md` records the user's design decisions; honor it over older notes:

- **Single remote Ollama tier**, reached over Tailscale (often a Tailnet IP like
  `http://100.x.y.z:11434`), plus a **deterministic offline intent parser**
  fallback. There is no second local model tier. Shipped in Phase 2
  (`ha_spark/router.py`, `ha_spark/intent_parser.py`, `ha-spark ask`): a fast
  `/api/tags` health probe; on failure/timeout, hand straight to the offline
  parser. Config is the single `OLLAMA_URL` + `OLLAMA_MODEL`.
- **Raw `entity_id` addressing.** All tools/HA operations use raw `entity_id`
  strings; no fuzzy name resolution. Schemas may return `friendly_name` as data
  but must not depend on name-based lookups.
- **Proactivity has a hard observe-only mode** (not yet implemented). A single
  `PROACTIVE_MODE = "off" | "simulate" | "on"` flag (default off/simulate early).
  The habit learner exposes `predict_actions(context) -> [(action, confidence,
  reason)]` independent of execution; the orchestrator always requests/logs
  predictions, then executes real service calls or logs them as "simulated" based
  on the flag — same decision path, side effects suppressed. (Config field
  deferred until the orchestrator phase has a consumer.)
- **`health`/doctor CLI command** (`python -m ha_spark health`, implemented in
  `ha_spark/health.py`): probes HA REST, the HA WS auth handshake, Ollama
  `/api/tags`, SQLite writability, and load-history readiness; human-readable,
  exit 0/1/2 (green/critical/degraded).

## Conventions

- **Async-first:** all I/O is `async`; share one `httpx.AsyncClient` per client.
- **Strict typing:** mypy runs `strict = true` (`disallow_untyped_defs`); keep it
  clean. pydantic mypy plugin is enabled.
- **Tests required per module:** mock HTTP with `respx`; for WS/memory use a fake
  server / temp SQLite. pytest runs in `asyncio_mode = "auto"` (no
  `@pytest.mark.asyncio` needed). ruff lints `E,F,I,UP,B,ASYNC,W`, line length 100.
- **Phase-per-branch/PR:** one phase per branch, each ending runnable and
  verifiable. Done: Phase 0-1 (scaffolding + HA connectivity), the energy
  planner MVP (add-on v0.1.0), Phase 2 (Ollama router + offline parser,
  `ha-spark ask`), Phase 3 (EV supply guard, add-on 0.2.0), Phase 6A
  (forecast ledger + signal sampler, 0.3.0), and Phase 6B (weather-aware ML
  quantile model, 0.4.0). Next is Phase 6C (context store); see ROADMAP.md.
- **No-push fallback:** if a session lacks git credentials, produce a `git bundle`
  + `git format-patch` and push from a credentialed clone. (`*.bundle` / `*.patch`
  are gitignored.)
