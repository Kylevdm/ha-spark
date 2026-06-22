---
name: run-ha-spark
description: Build, run, and smoke-test the ha-spark CLI — the local-first Home Assistant energy planner. Use to run, start, launch, build, install, or test ha-spark, compute a charge plan, or check it works without a live Home Assistant or Ollama.
---

# Run ha-spark

`ha-spark` is a **CLI** (`python -m ha_spark <command>`), not a GUI or server. It
reads live Home Assistant state over REST/WebSocket, plans the overnight battery
charge deterministically, and talks to a remote Ollama for natural language — with
a deterministic **offline fallback** when HA/Ollama are unreachable.

On a clean machine you have neither HA nor Ollama. The driver
`.claude/skills/run-ha-spark/smoke.sh` exercises the real degrade-don't-crash
paths by pointing `HA_URL`/`OLLAMA_URL` at a dead port — so `plan`, `ask`,
`context`, and `health` all run offline. **That is the agent path; run it first.**

All paths below are relative to the repo root (the unit).

## Prerequisites

Python 3.11+ (tested on 3.12) and `pip`. No OS packages or GPU needed — it's pure
Python. No HA or Ollama instance required for the smoke path.

## Build

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Run (agent path) — the driver

```bash
.claude/skills/run-ha-spark/smoke.sh
```

Runs six CLI commands against dead HA/Ollama endpoints and checks each exit code.
Prints `ALL SMOKE CHECKS PASSED` (exit 0) when every command degraded correctly.
Takes ~3s. It activates `.venv` itself and uses a throwaway temp DB; safe to run
from any cwd. What it covers:

- `plan` — the core planner; falls back to the baseline load forecast, still
  prints a plan, exits 0.
- `ask "…"` — routes to Ollama, falls back to the offline parser, exits 0.
- `context add/list/remove` — pure local SQLite, exit 0.
- `health` — probes HA/Ollama/SQLite; with HA down reports critical, **exits 1**
  (expected — the driver asserts exit 1 here).

To drive a single command yourself with the same offline setup:

```bash
. .venv/bin/activate
HA_URL=http://127.0.0.1:9 HA_TOKEN=dummy OLLAMA_URL=http://127.0.0.1:9 \
  DB_PATH=/tmp/ha-spark.db python -m ha_spark plan
```

## Run (real / human path)

Point it at a real HA instance via `.env` (see `.env.example`): set `HA_URL` +
`HA_TOKEN` for standalone/dev mode, optionally `OLLAMA_URL` + `OLLAMA_MODEL`.
Then `python -m ha_spark states`, `plan`, `ask "…"`, or `run --once`. `python -m
ha_spark --help` lists every command. Writes to the inverter only happen when
`PROACTIVE_MODE=on`.

## Test

```bash
. .venv/bin/activate
ruff check . && mypy ha_spark && pytest -q
```

## Gotchas

- **Config fails fast with no creds.** `load_settings()` raises `ConfigError`
  unless add-on (`SUPERVISOR_TOKEN`) or dev (`HA_URL`+`HA_TOKEN`) credentials are
  present. The smoke path sets dummy `HA_URL`/`HA_TOKEN` to get past this — the
  values just have to exist, they don't have to reach anything.
- **Use a dead *port*, not a dead host.** `127.0.0.1:9` (discard) refuses
  connections instantly. A routable-but-silent host instead makes Ollama's chat
  call hang for the full `ollama_timeout` (120s) before falling back. Point
  `OLLAMA_URL` at `127.0.0.1:9` too, or `ask` takes ~2 min.
- **This container has a real Ollama on the Tailnet** (`100.x.y.z:11434`). If you
  leave `OLLAMA_URL` at its default, the health probe will actually succeed and
  `ask` will hit a live model — fine, but not deterministic. The driver overrides
  it for repeatability.
- **`health` exiting 1 is correct** when HA is down, not a failure. It's
  green/critical/degraded → exit 0/1/2.
- **DB path is `DB_PATH`** (the pydantic field name), there is no `--db` flag.
  Defaults to `data/ha_spark.db` under cwd.
- **`pytest` must run inside `.venv`.** A system `pytest` on `$PATH` will collect
  nothing but import errors (`No module named 'httpx'`, `Unknown config option:
  asyncio_mode`).

## Troubleshooting

- `ModuleNotFoundError: No module named 'httpx'` / `Unknown config option:
  asyncio_mode` → you're using a `pytest`/`python` outside the venv. `.
  .venv/bin/activate` first.
- `ask` hangs ~2 minutes then answers → `OLLAMA_URL` points somewhere that
  accepts the TCP connection but never replies. Set it to `http://127.0.0.1:9`.
- `ConfigError` on startup → no credentials; set `HA_URL` + `HA_TOKEN` (any
  values for the offline smoke).
