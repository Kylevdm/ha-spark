#!/usr/bin/env bash
# Drives the ha-spark CLI end-to-end with no live Home Assistant / Ollama.
#
# ha-spark is a CLI that talks to HA over REST/WS and to a remote Ollama, with a
# deterministic offline fallback. On a clean machine none of those exist, so we
# point HA_URL and OLLAMA_URL at a dead port (127.0.0.1:9, "discard"): every
# connection is *refused fast* (no 2-minute read timeouts), which exercises the
# real degrade-don't-crash paths the planner is built around.
#
# Usage:  ./smoke.sh            # run from anywhere; finds the repo root itself
# Exit 0 = every command behaved as expected.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO"

# Activate the venv if present (created by the Build step in SKILL.md).
[ -f .venv/bin/activate ] && . .venv/bin/activate

# Dead endpoints -> connection refused immediately -> offline paths.
export HA_URL=http://127.0.0.1:9
export HA_TOKEN=dummy
export OLLAMA_URL=http://127.0.0.1:9
export DB_PATH="$(mktemp -u /tmp/ha-spark-smoke.XXXX.db)"
trap 'rm -f "$DB_PATH"' EXIT

fail=0
# run "<label>" <expected-exit> -- <cmd...>
run() {
  local label="$1" want="$2"; shift 2; [ "$1" = "--" ] && shift
  echo "=== $label ==="
  timeout 30 "$@"; local got=$?
  if [ "$got" -ne "$want" ]; then
    echo "  !! FAIL: $label exited $got, expected $want"; fail=1
  else
    echo "  ok ($got)"
  fi
  echo
}

# plan: the core. Degrades to baseline forecast with HA down; must still print a
# plan and exit 0.
run "plan (offline, baseline forecast)" 0 -- python -m ha_spark plan
# ask: routes to Ollama, falls back to the offline parser when it's unreachable.
run "ask (offline parser fallback)" 0 -- python -m ha_spark ask "what is tonight's charge plan?"
# context: pure local SQLite, no network at all.
run "context add" 0 -- python -m ha_spark context add away --from 2026-07-01 --to 2026-07-14 --note smoke
run "context list" 0 -- python -m ha_spark context list
run "context remove" 0 -- python -m ha_spark context remove 1
# health: probes everything; with HA down it reports critical and exits 1.
run "health (HA down -> critical, exit 1)" 1 -- python -m ha_spark health

if [ "$fail" -eq 0 ]; then echo "ALL SMOKE CHECKS PASSED"; else echo "SMOKE FAILURES ABOVE"; fi
exit "$fail"
