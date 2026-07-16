# CLAUDE.md

Guidance for Claude Code in this repository.

## Clauses
1. Ask, don't assume. If something is unclear, ask before writing a single line.
2. Simplest solution first. No abstractions or flexibility that weren't requested.
3. Don't touch unrelated code, even if you think it could be improved — flag it instead.
4. Flag uncertainty explicitly before proceeding.
5. If you see a clearly better approach, say so (tradeoff in 2–4 bullets) before implementing.

## Project

`ha-spark`: local-first energy autopilot for Home Assistant (REST + WebSocket),
with a single remote Ollama tier + deterministic offline fallback for NL. Ships
as an HA add-on. Plain Python 3.11+ / asyncio — no agent framework. Repo and
import package: `ha-spark` / `ha_spark` (`github.com/Kylevdm/ha-spark`).

**Sources of truth:** `ROADMAP.md` (direction/status) → active plan/tickets →
`ha_spark_addon/CHANGELOG.md`. `CONTEXT.md` is the domain glossary — use its
vocabulary. Module docstrings document the architecture; read them rather than
expecting it restated here. Core data flow: `energy/sources.py:gather_inputs`
(REST) → `energy/planner.py:compute_plan` (**pure**: inputs + config → plan) →
driver/charger apply (gated). The NL layer (`router.py`, `copilot.py`,
`intent_parser.py`) sits on top and only explains.

## Security (top priority)

ha-spark holds HA credentials and actuates hardware — review every change for
security and prefer the safe default. Hard rules:

- **Never log or echo secrets** (`SUPERVISOR_TOKEN`, `HA_TOKEN`, supplier API
  keys) anywhere: logs, errors, plan/CLI output, tool responses. Mark secret
  add-on options `password`. Don't commit `.env`.
- **Validate everything from outside the process** — HA states/config, LLM
  replies, third-party payloads are untrusted. Coerce with the tolerant helpers
  (`_to_float`/`_opt_float`), validate structured input with pydantic before it
  is persisted or acted on. Bad payloads degrade, never crash or actuate.
- **The LLM never controls hardware.** It explains, or proposes facts that are
  validated before storage; it must not reach `call_service`. The deterministic
  planner is the sole decider.
- **Actuation invariants:** real writes only when `PROACTIVE_MODE == on` (and
  `control == ha_spark` under the driver layer), never on an invalid SoC, always
  read-back verified, failures isolated per action. Don't weaken these
  (`energy/chargers.py`, `energy/supply_guard.py`).
- **Parameterised SQL only**; no `eval`/`exec`/`shell=True`/shell interpolation
  of external data.
- **Network:** outbound only to configured endpoints; any inbound surface must
  be token-authenticated, ingress-bound, and gated like the CLI.
- **Dependencies:** pinned, minimal, each addition justified. The add-on
  installs from a pinned `vX.Y.Z` tag.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"        # ".[dev,habits]" for the ML load model

python -m ha_spark states      # dev mode needs HA_URL + HA_TOKEN in .env
python -m ha_spark health      # end-to-end doctor, exit 0/1/2

# Quality gates — all three green before merge
ruff check .
mypy ha_spark
pytest -q
```

## Config modes

`config.py` `Settings` / `load_settings()` (pydantic-settings, overlays
`/data/options.json`): **add-on mode** (default) reaches HA via the Supervisor
proxy with `SUPERVISOR_TOKEN`; **standalone/dev** needs both `HA_URL` +
`HA_TOKEN`. Consumers use the derived `auth_token` / `ha_rest_url` /
`ha_websocket_url`, never the raw fields.

Standing decisions (details in ROADMAP.md): single remote `OLLAMA_URL` tier
with deterministic offline fallback; raw `entity_id` addressing, no fuzzy name
matching; `proactive_mode = off|simulate|on` (default `simulate`) gates every
write — same decision path, side effects suppressed.

## Conventions

- **Async-first**; one shared `httpx.AsyncClient` per client. **mypy strict**;
  pydantic plugin on. **Tests per module**: `respx` for HTTP, fake server/temp
  SQLite for WS/memory; pytest `asyncio_mode = "auto"`; ruff `E,F,I,UP,B,ASYNC,W`,
  line length 100.
- **Phase-per-branch/PR**, each ending runnable. Status lives in ROADMAP.md +
  CHANGELOG.md, not here.
- **Add-on packaging per phase:** bump `ha_spark_addon/config.yaml` `version`,
  add CHANGELOG entry, update DOCS.md + schema. Keep `config.yaml`
  `options`/`schema` and `config.py` `_OPTION_KEYS` in sync (test-enforced).
- **Release procedure (two things must agree):** the Supervisor build runs
  `pip install "...@v${BUILD_VERSION}"` where `BUILD_VERSION` = the `config.yaml`
  `version` **on `master`** (the store reads the default branch), and the
  matching `vX.Y.Z` tag must be pushed or the build fails. Sequence: commit
  bump → tag → push branch + tag → merge to `master`.
- **Add-on base image:** Supervisor 2026.04.0+ ignores `build.yaml`/`BUILD_FROM`
  — the base is set via `FROM` in `ha_spark_addon/Dockerfile`. It's a glibc
  `python:slim` base (musllinux has no sklearn wheels); no s6/bashio, so
  `run.sh` is plain shell and `config.yaml` sets `init: true`.
- **No-push fallback:** without git credentials, produce a `git bundle` +
  `format-patch` (both gitignored) and push from a credentialed clone.
