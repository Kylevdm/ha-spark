# CLAUDE.md

Guidance for Claude Code in this repository.

## Clauses
1. Ask, don't assume. If something is unclear, ask before writing a single line.
2. Simplest solution first. No abstractions or flexibility that weren't requested.
3. Don't touch unrelated code, even if you think it could be improved; flag it
   instead.
4. Flag uncertainty explicitly before proceeding.
5. If you see a clearly better approach, say so (tradeoff in 2–4 bullets) before implementing.

## Project

`ha-spark`: local-first energy autopilot for Home Assistant (REST + WebSocket),
with one owner-chosen chat provider (remote Ollama, or opt-in OpenAI-compatible)
+ deterministic offline fallback for NL. Ships
as an HA add-on. Plain Python 3.11+ / asyncio, no agent framework. Repo and
import package: `ha-spark` / `ha_spark` (`github.com/Kylevdm/ha-spark`).

**Sources of truth:** GitHub milestones/issues on `Kylevdm/ha-spark`
(direction/status) → active plan/tickets → `ha_spark_addon/CHANGELOG.md`
(shipped record). `README.md` carries the positioning, design rules,
comparison, and non-goals. `CONTEXT.md` is the domain glossary; use its
vocabulary. Module docstrings document the architecture; read them rather than
expecting it restated here. Core data flow: `energy/sources.py:gather_inputs`
(REST) → `energy/planner.py:compute_plan` (**pure**: inputs + config → plan) →
driver apply (gated). The NL layer (`router.py`, `copilot.py`,
`intent_parser.py`) sits on top and only explains.

## Security (top priority)

ha-spark holds HA credentials and actuates hardware, so review every change for
security and prefer the safe default. Hard rules:

- **Never log or echo secrets** (`SUPERVISOR_TOKEN`, `HA_TOKEN`, supplier API
  keys) anywhere: logs, errors, plan/CLI output, tool responses. Mark secret
  add-on options `password`. Don't commit `.env`.
- **Validate everything from outside the process**: HA states/config, LLM
  replies, third-party payloads are untrusted. Coerce with the tolerant helpers
  (`_to_float`/`_opt_float`), validate structured input with pydantic before it
  is persisted or acted on. Bad payloads degrade, never crash or actuate.
- **The LLM never controls hardware.** It explains, or proposes facts that are
  validated before storage; it must not reach `call_service`. The deterministic
  planner is the sole decider.
- **Actuation invariants:** the site **operating mode** (Observe/Protect/
  Optimise, ADR-0008) is the only write gate, and each site has exactly one
  **authority owner** (the add-on or the integration) that holds it. The
  authority owner is derived from the topology's
  inverter transports (any `legacy` inverter, or none, means the add-on; all
  `native` means the integration) and is confirmed only by the durable
  ownership handshake; while it is pending or unknown nothing writes. The mode
  lives in the authority owner's durable authority store, never in Settings,
  and only an owner's transition request changes it.
  - *Add-on-owned sites:* the add-on's store is the authority and only the
    daemon actuates: real writes go through `WriteGate.mode_for` and the
    write-time `WriteGate.recheck` (`devices/execution.py`).
  - *Integration-owned sites:* the add-on's store carries the retirement
    marker, `WriteGate` refuses every direct write, and the daemon's supply
    guard, hold reconcile, SoC monitor, clock sync and V2L do nothing. The
    add-on writes no hardware itself: a plan reaches devices only as typed
    device intents sent as `apply` commands stamped with the plan epoch, and
    owner requests travel the same command channel. The integration's authority store (`authority_store.py`) is the
    gate, and every charge-affecting write (plan intents, owner entity edits,
    fallback, hold restoration, supply-guard cuts and restores) goes through
    the one charge-write path, `NativeChargeBoundary` (`native_write.py`):
    guard lock then bus lock, write-time recheck of authority, ownership and
    limits, the guard clamp, exact read-back. Power-switch and clock writes use
    their own verified helpers under the same authority checks. A `read_only`
    device (failed identity, caching on, gateway not owned) and a pending
    switch write nothing.
  - *Native export is supervised only:* it runs only in Optimise with a live
    owner `supervise_export` for that event, within the device export bound,
    and never in fallback, Protect, manual control or Observe. Every clear is
    zero-confirmed. The live tripwire stops every export on a connection after
    two consecutive fresh grid readings more than 200 W over the site export
    limit; a missing or stale reading only warns and raises a repair, never
    cuts. Its stop latch is saved before any cleanup write (the record goes
    `stop_pending`), lasts to that export window's end across restarts and
    Re-probes, and refuses that event as `tripped` even under a new
    supervision.

  Under either authority owner, Observe, unknown authority and a failed
  authority save write nothing, with one exception: on an integration-owned
  site an explicit owner clock sync (the `sync_clock` command or the
  clock-drift repair's fix flow) may write in Observe or a manual session, but
  never with unknown authority, a stale epoch or a transition in progress
  (`native_clock.py`); Protect writes only its holds (and enabled
  clock sync), plus, on an integration-owned site, supply-guard cuts and
  restores and the fallback schedule written as the owner's starting schedule
  when leaving Optimise; Optimise runs the plan. An invalid SoC blocks every SoC-derived write;
  always read back; isolate failures per action. Writes that don't depend on
  SoC keep their own safeguards: holds (discharge prevention), authorised
  clock sync, and the Solis fallback charging schedule in Optimise. On the
  add-on it starts after `soc_failure_threshold` failed SoC checks and
  programs the configured cheap-rate window at an explicitly set
  `solis_fallback_current_a`; on a native site it starts on `soc_integrity`
  or `addon_liveness` and uses the owner's per-inverter currents from the
  integration options. Both fallbacks go through the same gates and read-back,
  are charge-only and have no default current, and AlphaESS never gets
  either. A hold
  starts only on fresh evidence and ends on fresh clear evidence, a
  dispatch's last trusted end, or an explicit owner handover; `On` is written
  only to a readable switch. Don't weaken these (`ha_spark/devices/inverters/`,
  `energy/supply_guard.py`, `energy/soc_monitor.py`, `authority.py`,
  `transitions.py`, and in the integration `authority_store.py`,
  `ownership.py`, `native_write.py`, `native_holds.py`,
  `native_fallback_control.py`, `guard_loop.py`, `native_export.py`,
  `native_tripwire.py`).
- **Parameterised SQL only**; no `eval`/`exec`/`shell=True`/shell interpolation
  of external data.
- **Network:** outbound only to configured endpoints; any inbound network
  surface must be token-authenticated, ingress-bound, and gated like the CLI.
  Local IPC (the daemon's Unix socket) needs owner-only (0600) permissions in
  the data dir plus a kernel peer-uid check instead of a token, and must not
  grant write permission beyond the daemon's own gates.
- **Dependencies:** pinned, minimal, each addition justified. The add-on
  installs from a pinned `vX.Y.Z` tag.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"        # ".[dev,habits]" for the ML load model

python -m ha_spark states      # dev mode needs HA_URL + HA_TOKEN in .env
python -m ha_spark health      # end-to-end doctor, exit 0/1/2

# Quality gates: all three green before merge
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

Standing decisions: one owner-chosen chat provider (remote `OLLAMA_URL`, or an
opt-in OpenAI-compatible `/chat/completions` endpoint, ADR-0010) with
deterministic offline fallback; raw `entity_id` addressing, no fuzzy name
matching; every install starts in Observe, which runs the same decision path
as Optimise with side effects suppressed (`[SIMULATE]`).

## Conventions

- **Async-first**; one shared `httpx.AsyncClient` per client. **mypy strict**;
  pydantic plugin on. **Tests per module**: `respx` for HTTP, fake server/temp
  SQLite for WS/memory; pytest `asyncio_mode = "auto"`; ruff `E,F,I,UP,B,ASYNC,W`,
  line length 100.
- **Phase-per-branch/PR**, each ending runnable. Status lives in the GitHub
  milestones/issues + CHANGELOG.md, not here.
- **Add-on packaging per phase:** bump `ha_spark_addon/config.yaml` `version`,
  add a changelog fragment, update DOCS.md + schema. Keep `config.yaml`
  `options`/`schema` and `config.py` `_OPTION_KEYS` in sync (test-enforced).
- **Release procedure (two things must agree):** the Supervisor build runs
  `pip install "...@v${BUILD_VERSION}"` where `BUILD_VERSION` = the `config.yaml`
  `version` **on `master`** (the store reads the default branch), and the
  matching `vX.Y.Z` tag must be pushed or the build fails. Sequence: commit
  bump → tag → push branch + tag → merge to `master`.
- **Changelog fragments:** a PR never edits `CHANGELOG.md`, so parallel PRs
  can't conflict there. A user-visible change adds
  `ha_spark_addon/changelog.d/<issue>.md` holding its final bullet(s), written
  as they will appear. The release's bump commit folds every fragment, in issue
  order, into a new `## X.Y.Z` section at the top of `CHANGELOG.md` and deletes
  them (`docs/releasing.md`).
- **Add-on base image:** Supervisor 2026.04.0+ ignores `build.yaml`/`BUILD_FROM`;
  the base is set via `FROM` in `ha_spark_addon/Dockerfile`. It's a glibc
  `python:slim` base (musllinux has no sklearn wheels); no s6/bashio, so
  `run.sh` is plain shell and `config.yaml` sets `init: true`.
- **No-push fallback:** without git credentials, produce a `git bundle` +
  `format-patch` (both gitignored) and push from a credentialed clone.

## Agent skills

### Issue tracker

GitHub Issues on `Kylevdm/ha-spark`, via the `gh` CLI. External PRs **are** a
triage surface: `/triage` runs them through the same labels and states as
issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, unremapped: `needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`. See
`docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` (the domain glossary) + `docs/adr/` at the repo
root. See `docs/agents/domain.md`.
