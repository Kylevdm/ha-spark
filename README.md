# ha-spark

Local-first energy autopilot for Home Assistant.

ha-spark forecasts tomorrow's solar and household load, works out how much
overnight charge the battery actually needs at the cheap rate, and sets the
inverter's timed charge itself. A deterministic, auditable planner decides; a
natural-language layer only explains. It talks to Home Assistant
(HAOS/Supervised) over the REST + WebSocket API and to a single remote Ollama
instance for the natural-language features, with a deterministic offline
fallback when Ollama is unreachable. Packaged as a Home Assistant add-on.

> Status: shipped through add-on v0.15.0. Deterministic planner, device-driver
> core with per-device control authority, multi-supplier tariffs (fixed,
> dynamic, Octopus Intelligent), native Solis timed-slot actuation with guard
> rails, simulate mode + savings backtest, onboarding wizard, NL copilot, and
> an optional agent surface. [`ROADMAP.md`](ROADMAP.md) has the direction and
> how ha-spark differs from EMHASS / Predbat;
> [`ha_spark_addon/CHANGELOG.md`](ha_spark_addon/CHANGELOG.md) is the shipped
> record.

## Install as a Home Assistant add-on

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add `https://github.com/Kylevdm/ha-spark`.
2. Install **ha-spark** (built locally; the first install takes a few
   minutes), configure your entity IDs and tariff on the Configuration tab,
   and start it.

See [`ha_spark_addon/DOCS.md`](ha_spark_addon/DOCS.md) for the full option
reference and the onboarding flow: health check, load-history backfill, first
plan, then enabling real control.

Charging is driven through a per-inverter driver; set the `inverter` option to
`solis` (default) or `alphaess` to match your hardware. See
[`docs/adding-an-inverter.md`](docs/adding-an-inverter.md) for the driver
contract and how to add support for another inverter.

## Development

Requires Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"           # add ",habits" for the ML load model

cp .env.example .env              # then set HA_URL and HA_TOKEN

# Quality gates: all three green before merge
ruff check . && mypy ha_spark && pytest -q
```

### Try it

```bash
python -m ha_spark health                # end-to-end doctor (exit 0/1/2)
python -m ha_spark states                # list entity states (via REST)
python -m ha_spark states --domain light # filter by domain
python -m ha_spark states --watch        # stream live changes over WebSocket
python -m ha_spark plan                  # print tonight's plan without applying it
python -m ha_spark ask "why is it charging to 80% tonight?"
```

Other commands: `onboard` (propose entity mappings), `backfill-load`
(import or derive load history), `backtest`, `forecast-eval`, `context`,
`learn-factors`, `v2l`, `run` (the daemon). Configuration is read from
environment variables / `.env` (see `.env.example`); the add-on reads
`/data/options.json` instead.
