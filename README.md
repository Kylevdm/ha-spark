# ha-spark

Local-first home automation agent for Home Assistant.

Talks to Home Assistant (HAOS/Supervised) over its REST + WebSocket API and to
Ollama for inference (a small model on the HA device, a larger model on a LAN
box, with routing and offline fallback). Designed to be sensor-aware before
acting, to learn household habits over time, and to be controlled in natural
language — text first, voice (via HA Assist) later. Packaged as a Home Assistant
add-on.

> Status: energy-planner MVP. See the planning notes / implementation plan for the
> architecture and phased build-out.

See [`ROADMAP.md`](ROADMAP.md) for where the project is going and how it
differs from EMHASS / Predbat.

## Install as a Home Assistant add-on

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/Kylevdm/ha-spark`.
2. Install **ha-spark** (built locally; first install takes a few minutes),
   configure your entity IDs and tariff on the Configuration tab, and start it.

See [`ha_spark_addon/DOCS.md`](ha_spark_addon/DOCS.md) for the full option
reference and onboarding flow (health check → load-history backfill → plan →
enable real control).

## Development

Requires Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"           # add ".[dev,habits]" for Phase 5 ML deps

cp .env.example .env              # then set HA_URL and HA_TOKEN

# Quality gates
ruff check . && mypy ha_spark && pytest -q
```

### Try it (Phase 1: Home Assistant connectivity)

```bash
python -m ha_spark states                 # list all entity states (via REST)
python -m ha_spark states --domain light  # filter by domain
python -m ha_spark states --watch         # stream live changes over WebSocket
```

Configuration is read from environment variables / `.env` (see `.env.example`).
