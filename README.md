# ha-spark

Local-first energy autopilot for Home Assistant.

ha-spark plans your home's energy day for you: it forecasts tomorrow's solar
and household load, works out how much overnight charge your battery actually
needs at the cheap rate, and actuates the inverter itself. No template
sensors, no hand-written automations, no YAML. A deterministic, auditable
planner decides; a natural-language layer only explains. It talks to Home
Assistant (HAOS/Supervised) over the REST + WebSocket API and to a single
remote Ollama instance for the natural-language features, with a
deterministic offline fallback when Ollama is unreachable. Packaged as a
Home Assistant add-on: no cloud service, no subscription, no data leaving
your network.

> Status: shipped through add-on v0.15.0. Deterministic planner, device-driver
> core with per-device control authority, multi-supplier tariffs (fixed,
> dynamic, Octopus Intelligent), native Solis timed-slot actuation with guard
> rails, simulate mode + savings backtest, onboarding wizard, NL copilot, and
> an optional agent surface.
> [`ha_spark_addon/CHANGELOG.md`](ha_spark_addon/CHANGELOG.md) is the shipped
> record; the GitHub
> [milestones](https://github.com/Kylevdm/ha-spark/milestones) are the live
> tracker; [`CONTEXT.md`](CONTEXT.md) is the domain glossary.

## Design rules

1. **A deterministic planner decides; an LLM only explains.** Battery
   setpoints come from an auditable energy-balance model
   ([why not an LP solver](docs/adr/0002-auditable-over-optimal-planning.md)),
   never from a language model. The natural-language layer sits on top:
   "what's the plan for tonight?", "why are you charging to 80%?", "what did
   you save this week?". It runs against your own Ollama instance (LAN or
   Tailscale) with a deterministic fallback, so there is no cloud dependency
   and no hallucinated control.
2. **Trust is earned, not assumed.** ha-spark starts in observe/simulate
   mode, logging exactly what it *would* have done alongside what your
   current setup did, with a cost backtest to quantify the difference. You
   flip it to real control when the numbers convince you.

## How it compares

[EMHASS](https://github.com/davidusb-geek/emhass) and
[Predbat](https://github.com/springfall2008/batpred) are excellent, mature
projects and the right choice for many households today. ha-spark makes a
different bet: that most people with a battery and solar want an autopilot
they can install, understand, and trust in an evening, not an optimization
framework to configure.

| | EMHASS | Predbat | ha-spark |
|---|---|---|---|
| Optimizes a plan | ✅ LP solver | ✅ | ✅ energy-balance planner ([why not LP](docs/adr/0002-auditable-over-optimal-planning.md)) |
| Actuates hardware itself | ❌ user wires automations | ✅ | ✅ with guard rails (SoC validity, read-back, failure isolation) |
| Setup effort | YAML + sensor templates + REST commands | YAML; docs assume HA/file-editing fluency | add-on options UI + onboarding wizard |
| Explains decisions in plain language | ❌ | ❌ | ✅ local LLM over the deterministic plan |
| Try-before-trust mode | ❌ | partial (read-only mode) | ✅ simulate mode + savings backtest |
| Cloud dependence | none | none (paid cloud version exists) | none, by design |

## Non-goals

- **No cloud service.** Local-first is a feature, not a phase.
- **No LLM-decided setpoints.** The language model explains and reports; it
  does not control hardware.
- **No fuzzy entity-name matching.** All control paths use exact `entity_id`s.

## Install as a Home Assistant add-on

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add `https://github.com/Kylevdm/ha-spark`.
2. Install **ha-spark** (built locally; the first install takes a few
   minutes), configure your entity IDs and tariff on the Configuration tab,
   and start it.

The target experience is a first simulated plan within 15 minutes of install:
`ha-spark onboard` proposes the entity map from your HA registry, the health
check confirms connectivity end to end, and simulate mode shows the first
overnight plan the same evening.

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
