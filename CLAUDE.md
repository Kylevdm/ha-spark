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
    guard, hold reconcile, SoC monitor and clock sync do nothing (the V2L
    tick still reads, tallies and notifies; it writes no hardware). The
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
    owner supervision for that event (sent by the owner or by the owner's
    durable export opt-in, ADR-0013), within the device export bound,
    and never in fallback, Protect, manual control or Observe. Every clear is
    zero-confirmed. The live tripwire stops every export on a connection after
    two consecutive fresh grid readings more than 200 W over the site export
    limit; a missing or stale reading only warns and raises a repair, never
    cuts. Its stop latch is saved before any cleanup write (the record goes
    `stop_pending`), lasts to that export window's end across restarts and
    Re-probes, and refuses that event as `tripped` even under a new
    supervision.
  - *Native daytime window (phase 17):* a daytime purchase reaches a native
    Solis inverter only as a dated charge intent, armed in the charge half of
    Slot 2 through `NativeChargeBoundary` (Slot 1 and Slot 2's discharge half
    untouched), only in Optimise with a current epoch, trusted SoC, a trusted
    inverter clock and no hold on that inverter (`hold_active`; the hold
    handover below is the one exception). Protect,
    fallback, manual control and Observe never arm one. Its durable record is
    saved before the write and armed only after exact read-back. It is
    cleared at its end, on a `none` request and when the site leaves Optimise
    (the `cancel_export` step); `untrusted` keeps only a verified resident
    window. Every clear is zero-confirmed before the record is retired; a
    failed clear keeps the record, is retried by the native tick and raises a
    repair. Add-on-owned, AlphaESS and legacy Solis sites never get one.
  - *Hold handover (ADR-0015):* under the owner's integration opt-in
    (`dispatch_handover`, off by default, sent to the add-on from api_version
    6) the planner admits a started dispatch purchase on a coarse-hold
    inverter, and its dated window (with a target) may arm under an owned,
    confirmed dispatch hold: Slot 2 is armed with the switch still Off and read
    back exactly, the handover is recorded in memory, and only then is On
    written under a fresh recheck. The hold leaves the switch On only while the
    handover is live (`native_handover.live`: counted v6 heartbeat, Optimise
    with no manual session, fallback or transition, a live trusted dispatch,
    verified positive current, the inverter clock within 60 s and inside the
    window more than two minutes from its end, trusted SoC more than 2 % below
    target, the armed record and verified face uncontradicted by a newer poll);
    otherwise it writes Off as usual. Any Slot 2 write, and a zero charge
    current, on a handed-over inverter writes Off first under the hold's own
    refusal (so never toward Observe); while that Off fails and the hold stands,
    the Slot 2 write fails and the window stays. A verified or observed Off ends
    the handover and that window is never handed over again;
    an On that may have landed keeps the Off obligation. Nothing is persisted: a
    restart forgets the handover and the hold writes Off on its next tick.

  Under either authority owner, Observe, unknown authority and a failed
  authority save write nothing, with one exception: on an integration-owned
  site an explicit owner clock sync (the `sync_clock` command or the
  clock-drift repair's fix flow) may write in Observe or a manual session, but
  never with unknown authority, a stale epoch or a transition in progress
  (`native_clock.py`); Protect writes only its holds (and enabled
  clock sync), plus, on an integration-owned site, supply-guard cuts and
  restores and the fallback schedule written as the owner's starting schedule
  when leaving Optimise; Optimise runs the plan. An invalid SoC blocks every SoC-derived write (except, in a manual session
  started with protection off and `override_soc_trust`, the owner's own charge
  edits and guard restores up to the owner's edited current on that inverter,
  ADR-0014; owner discharge edits are not SoC-gated);
  always read back; isolate failures per action. A config or accepted-limits
  change between planning and the write refuses every write for that tick,
  holds and clock sync included; the next tick re-plans. Writes that don't
  depend on SoC keep their own safeguards: holds (discharge prevention), authorised
  clock sync, and the Solis fallback charging schedule in Optimise. On the
  add-on it starts after `soc_failure_threshold` failed SoC checks and
  programs the configured cheap-rate window at an explicitly set
  `solis_fallback_current_a`; on a native site it starts on `soc_integrity`
  or `addon_liveness` and uses the owner's per-inverter currents from the
  integration options. Both fallbacks go through the same gates and read-back,
  are charge-only and have no default current, and AlphaESS never gets
  either. A hold
  starts only on fresh evidence and ends on fresh clear evidence, a
  dispatch's last trusted end, or an explicit owner handover (on a native
  site an add-on Kraken window counts as dispatch evidence only with the
  integration's own fresh positive car evidence, can only add or extend a
  dispatch hold, and is remembered in memory only, ADR-0016); `On` is written
  only to a readable switch. Don't weaken these (`ha_spark/devices/inverters/`,
  `energy/supply_guard.py`, `energy/soc_monitor.py`, `authority.py`,
  `transitions.py`, and in the integration `authority_store.py`,
  `ownership.py`, `native_write.py`, `native_holds.py`,
  `native_fallback_control.py`, `guard_loop.py`, `native_export.py`,
  `native_export_write.py`, `native_export_supervision.py`, `native_tripwire.py`,
  `native_dated_write.py`, `native_handover.py`, `native_slots.py`).
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
  add a changelog fragment, update DOCS.md. A new setting is a `Settings`
  field annotated with `_metadata(owner, reload, secret=...)` (as
  `happy_hour_enabled` is); `config.yaml` holds only `log_level`, and
  `_OPTION_KEYS` is the frozen 0.24 legacy seed list: don't add to either.
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
