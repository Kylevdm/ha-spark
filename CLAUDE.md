# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Clauses
1. Ask, don't assume. If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements.
2. Simplest solution first. Alaways implement the simplest thing that could work. Do not add abstractions or flexibility that weren't explicitly requested.
3. Don't touch unrelated code. If a file or function is not directly part of the current task, do not modify it, even if you think it could be improved. Code based reviews will allow you to flag this.
4. Flag uncertainty explicitly. If you are not confident about an approach or technical detail, say so before proceeding. Confidence without certainty causes more damage than admitting the gap.
5. If you see a clearly better approach, say so before implementing. Explain the tradeoff in 2-4 bullets. If the current request is still reasonable, proceed unless the alternative avoids serious risk or wasted work.



## Project

`ha-spark` is a local-first, natural-language agent for Home Assistant. It talks
to HA over REST + WebSocket, runs inference against a single remote Ollama
instance with a deterministic offline fallback, stays sensor-aware before acting,
will learn household habits over time, and ships as a Home Assistant add-on. Plain
Python 3.11+ / asyncio — no agent framework.

Remote: `https://github.com/Kylevdm/ha-spark.git`. The repo and the Python import
package are both `ha-spark` / `ha_spark`.

## Security (top priority)

ha-spark holds Home Assistant credentials and can actuate hardware, so security
is a first-class requirement, not an afterthought — review every change for it
and prefer the safe default. Hard rules:

- **Never log or echo secrets.** `auth_token`/`SUPERVISOR_TOKEN`, `HA_TOKEN`,
  `octopus_api_key`, and any future API keys must never appear in logs, error
  messages, plan output, CLI output, or MCP/tool responses. Mark secret options
  `password` in the add-on schema (as `octopus_api_key` is). Don't commit `.env`.
- **Validate everything from outside the process** — HA sensor states, HA
  config, LLM replies, and third-party API payloads are all untrusted. Coerce
  with the tolerant helpers (`_to_float`/`_opt_float`) and validate structured
  input with pydantic (as `context_intent.ExtractedContext` does) *before* it is
  persisted or acted on. A bad payload must degrade, never crash or actuate.
- **The LLM never controls hardware.** The model only explains, or proposes
  reviewable facts that are validated before storage; it must not reach
  `call_service`. Keep the planner the sole decider (ROADMAP non-goal).
- **Actuation invariants:** real writes only when `PROACTIVE_MODE == on`
  (and, per the v1.0 driver work, `control == ha_spark`), never on an invalid
  SoC, always with read-back verification, failures isolated per action. Don't
  weaken these guard rails (`energy/chargers.py`, `energy/supply_guard.py`).
- **Parameterised SQL only** (aiosqlite `?` placeholders); never string-format a
  query. No `eval`/`exec`/`shell=True`; no shell interpolation of external data.
- **Network surfaces are authenticated and least-privilege.** Outbound calls go
  only to configured endpoints (HA, the single Ollama URL, Open-Meteo, the
  Octopus API). Any inbound surface (the planned MCP server) must require a
  token, bind to add-on ingress rather than an open port, and expose read vs.
  act tools under the same authority/PROACTIVE_MODE gating as the CLI.
- **Dependencies:** keep them pinned and minimal; justify and review each
  addition (supply-chain). The add-on installs ha-spark from a pinned `vX.Y.Z`
  tag, not a moving branch.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ".[dev,habits]" for the Phase 6 ML model
                                   # (the add-on image installs scikit-learn/numpy
                                   #  explicitly — river has no musllinux wheels)

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
- **`cli.py`** — argparse CLI; every command lives here. `__main__.py` makes
  `python -m ha_spark` run it.

The **energy planner** is the core, under `ha_spark/energy/`:

- **`sources.py`** (`gather_inputs`) reads live state over **REST** (SoC, solar,
  dispatches, EV, site location) — it does *not* use the `StateCache` (only the
  `states` CLI command does). **`forecast.py`** (`predict_home_load`) owns the
  load forecast with a fallback chain: weather-aware ML (`ml.py` + `weather.py`,
  optional) → slot-profile median (`profile.py`) → daily median → baseline,
  scaled by active context (`context.py`).
- **`planner.py`** (`compute_plan`) is **pure** — inputs + config → a
  `ChargePlan` of `ChargeAction`s. **`chargers.py`** (`Charger` Protocol,
  `SolisCharger`) applies them, gated by `PROACTIVE_MODE` with read-back.
  **`scheduler.py`** is the daily daemon (`run_once`/`run_forever`) and runs the
  live **`supply_guard.py`**.
- **`ledger.py`**/`eval.py` record forecasts vs. actuals (`forecast-eval`);
  **`habits.py`** learns occupancy/away factors and exposes `predict_actions`.
- The NL layer is top-level: **`router.py`** (Ollama probe→chat, else offline),
  **`intent_parser.py`** (offline energy + context parsing), **`context_intent.py`**
  (LLM context extraction), **`copilot.py`** (grounds chat in the live plan),
  **`onboarding_discover.py`**/`presets.py` (entity discovery), **`health.py`**.

Data flow: `gather_inputs` (REST) → `compute_plan` (pure) → `Charger.apply`
(gated). Separately, the `states` CLI seeds `StateCache` from REST and keeps it
live over the WebSocket stream. `ha-spark ask` routes through the Ollama tier
(grounded in the computed plan) or the deterministic offline parser.

## Design direction

**The GitHub tracker is the source of truth for phases and status** — one
milestone per phase/add-on version, issues under them (`gh` CLI; see
`docs/agents/issue-tracker.md`). ROADMAP.md keeps only the durable positioning,
design rules, and non-goals; shipped work is recorded in
`ha_spark_addon/CHANGELOG.md`; design decisions in `docs/adr/`. `usernotes.md`
records older design rationale — honor it only where the above are silent. The
next stage (see the milestones) moves to a modular `devices/` **driver layer**,
a multi-supplier **`TariffProvider`** (normalised per-slot price schedule, not
Octopus-shaped), per-device **control authority** (`observe|ha_spark|supplier`),
multi-source charging (R48 rectifiers + V2L), and an **MCP** agent surface — do
not reintroduce hardcoded Solis/Octopus/zappi assumptions into the planner.

Standing decisions:

- **Single remote Ollama tier**, reached over Tailscale (often a Tailnet IP like
  `http://100.x.y.z:11434`), plus a **deterministic offline intent parser**
  fallback. There is no second local model tier. Shipped in Phase 2
  (`ha_spark/router.py`, `ha_spark/intent_parser.py`, `ha-spark ask`): a fast
  `/api/tags` health probe; on failure/timeout, hand straight to the offline
  parser. Config is the single `OLLAMA_URL` + `OLLAMA_MODEL`.
- **Raw `entity_id` addressing.** All tools/HA operations use raw `entity_id`
  strings; no fuzzy name resolution. Schemas may return `friendly_name` as data
  but must not depend on name-based lookups.
- **Proactivity has a hard observe-only mode** (implemented). The
  `proactive_mode = "off" | "simulate" | "on"` setting (`config.py`, default
  `simulate`) gates every write in `chargers.py`/`supply_guard.py`: `off` =
  compute only, `simulate` = log intended writes, `on` = real `call_service`.
  The habit learner exposes `predict_actions(context) -> [(action, confidence,
  reason)]` (`energy/habits.py`) independent of execution; `scheduler.run_once`
  always requests/logs predictions — same decision path, side effects suppressed.
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
  verifiable. **The GitHub milestones/issues and `ha_spark_addon/CHANGELOG.md`
  are the source of truth for status** — don't duplicate the phase list here
  or in ROADMAP.md.
- **Add-on packaging per phase:** a phase that adds options/behaviour bumps
  `ha_spark_addon/config.yaml` `version`, adds a `CHANGELOG.md` entry, and
  updates `DOCS.md` + the options schema. Keep `config.yaml` `options`/`schema`
  and the `config.py` `_OPTION_KEYS` set in sync (a test enforces this).
- **Releasing:** the add-on Dockerfile installs ha-spark from
  `git+…@v${BUILD_VERSION}`, so **every shipped `config.yaml` version needs a
  matching annotated `vX.Y.Z` git tag and GitHub release** — otherwise the
  add-on cannot build. Tag + release as part of shipping a version.
- **No-push fallback:** if a session lacks git credentials, produce a `git bundle`
  + `git format-patch` and push from a credentialed clone. (`*.bundle` / `*.patch`
  are gitignored.)
- **Add-on release procedure (two things must agree):** the Supervisor build
  clones the package via `pip install "...@v${BUILD_VERSION}"`, where
  `BUILD_VERSION` = `ha_spark_addon/config.yaml` `version`. So a release needs
  BOTH (1) the `version` bumped in `config.yaml` **on `master`** — the add-on
  store advertises the version from the default branch, so a tag alone won't
  surface an update — and (2) a matching `vX.Y.Z` git **tag pushed** to GitHub,
  or the build fails with `pathspec 'vX.Y.Z' did not match`. Sequence: commit the
  bump → tag `vX.Y.Z` → push branch + tag → merge to `master`.
- **Add-on base image (Supervisor 2026.04.0+):** `build.yaml` is no longer read
  and `BUILD_FROM` is no longer injected — set the base directly with `FROM` in
  `ha_spark_addon/Dockerfile`. The ML model (`[habits]` extra: scikit-learn/numpy)
  has no musllinux wheel, so the add-on builds on a **glibc** base
  (`python:3.13-slim-bookworm`) to install prebuilt wheels instead of
  source-compiling. That base has no s6/bashio, so `run.sh` is plain shell and
  `config.yaml` sets `init: true` (Supervisor runs tini as PID 1). The ML
  weather path gets lat/lon from `config.yaml` or falls back to HA's home
  location (`sources.py` via `rest.get_config()`).
