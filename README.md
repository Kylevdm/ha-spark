# ha-spark

Local-first energy autopilot for Home Assistant.

ha-spark plans your home's energy use. It forecasts solar generation and
household load, works out how much charge the battery needs from the cheap
overnight rate, and programs the inverter itself. You don't write template
sensors, automations, or YAML. A deterministic planner makes every decision,
and a natural-language layer answers questions about the plan without changing
it.

ha-spark ships as a Home Assistant add-on and talks to Home Assistant (HAOS or
Supervised) over the REST and WebSocket APIs. The natural-language features use
one remote Ollama instance and fall back to a deterministic parser when Ollama
is unreachable. There is no ha-spark cloud service or subscription. Apart from
Home Assistant, ha-spark contacts only your Ollama server, Open-Meteo (weather
for the ML load model), and the Octopus and Axle APIs if you configure them.

> Status: add-on v0.19.3 includes:
>
> - a deterministic planner that replans every half-hour and holds battery
>   energy back with slot reservations, with optional derived base load
> - per-device control authority
> - fixed, dynamic, Octopus Intelligent, and Axle event tariffs
> - native Solis timed-slot control, with a per-minute power-switch reconcile
>   and an inverter-clock check before any export
> - simulate mode and savings backtests
> - onboarding, the NL copilot, V2L monitoring, and an optional agent surface
>
> Axle export delivery is a supervised prototype. It has not yet completed a
> paid export on real hardware
> ([#134](https://github.com/Kylevdm/ha-spark/issues/134)), so don't run it
> unattended.
>
> [`ha_spark_addon/CHANGELOG.md`](ha_spark_addon/CHANGELOG.md) lists what each
> release shipped, the GitHub
> [milestones](https://github.com/Kylevdm/ha-spark/milestones) track open work,
> and [`CONTEXT.md`](CONTEXT.md) defines the project's terms.

## Destination

**v1.0.0 is the competitive MVP, validated on real hardware.** It runs a
zero-export house with a battery, solar panels, a V2L car, and Axle flexibility
events without supervision. That needs:

- base load derived from component statistics, instead of a consumption sensor
  that includes battery charging;
- the plan recomputed every half-hourly tariff slot;
- battery energy held back by named reservations, each computed backwards from
  an obligation;
- Axle export events delivered automatically (the main feature);
- the car refilling the house battery over V2L when that costs less than grid
  import;
- a morning plan digest on your phone, and a readiness signal that tells you
  when the simulate history supports switching on real control. You still make
  the switch.

Derived base load, half-hourly replanning, and reservations have shipped. Axle
export is a supervised prototype, and V2L refill, the phone digest, and the
readiness signal are still open. Each step has a
[milestone](https://github.com/Kylevdm/ha-spark/milestones). `1.0.0` follows
once the [release gate](docs/releasing.md#v100-release-gate) passes on the real
Solis.

## Design rules

1. **A deterministic planner decides; an LLM only explains.** Battery
   setpoints come from an energy-balance model in which every plan line traces
   to a reservation or a price
   ([why not an LP solver](docs/adr/0002-auditable-over-optimal-planning.md)).
   A language model never sets them. The natural-language layer answers
   questions such as "what's the plan for tonight?", "why are you charging to
   80%?", and "what did you save this week?". It runs against your own Ollama
   instance (on your LAN or over Tailscale) and falls back to a deterministic
   parser, so it needs no cloud service and cannot issue a hardware command.
2. **You switch on real control when you're ready.** ha-spark starts in
   `simulate` mode, which makes the same decisions and logs each write it
   would make without touching hardware. `ha-spark backtest` prices your
   recorded grid import under your tariff, and the plan report can show your
   existing charge-needed template sensor next to ha-spark's figure. Set
   `proactive_mode: on` when the logs convince you.

## How it compares

[EMHASS](https://github.com/davidusb-geek/emhass) and
[Predbat](https://github.com/springfall2008/batpred) are mature projects and the
right choice for many households. ha-spark is for people with a battery and
solar panels who want to install an autopilot in an evening and read why it
made each decision, without configuring an optimization framework.

| | EMHASS | Predbat | ha-spark |
|---|---|---|---|
| Optimizes a plan | ✅ LP solver | ✅ | ✅ energy-balance planner ([why not LP](docs/adr/0002-auditable-over-optimal-planning.md)) |
| Actuates hardware itself | ❌ user wires automations | ✅ | ✅ with guard rails (SoC validity, read-back, failure isolation) |
| Setup effort | YAML + sensor templates + REST commands | YAML; docs assume HA/file-editing fluency | add-on options UI + onboarding wizard |
| Explains decisions in plain language | ❌ | ❌ | ✅ local LLM over the deterministic plan |
| Try-before-trust mode | ❌ | partial (read-only mode) | ✅ simulate mode + savings backtest |
| Cloud dependence | none | none (paid cloud version exists) | none, by design |

## Non-goals

- **No cloud service.** ha-spark runs on your Home Assistant host, and no
  hosted version is planned.
- **No LLM-decided setpoints.** The language model explains and reports. It
  does not control hardware.
- **No fuzzy entity-name matching.** All control paths use exact `entity_id`s.

## Install as a Home Assistant add-on

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add `https://github.com/Kylevdm/ha-spark`.
2. Install **ha-spark**. Home Assistant builds the image locally, so the first
   install takes a few minutes.
3. Set your entity IDs and tariff on the **Configuration** tab, then start the
   add-on.

The aim is a first simulated plan within 15 minutes of installing.
`ha-spark onboard` proposes entity mappings from your HA registry,
`ha-spark health` checks each connection, and simulate mode shows the first
overnight plan the same evening.

[`ha_spark_addon/DOCS.md`](ha_spark_addon/DOCS.md) has the full option
reference and the onboarding steps: health check, load-history backfill, first
plan, then real control.

A per-inverter driver controls battery charging. Set the `inverter` option to
`solis` (the default) or `alphaess`. On Solis, ha-spark also checks the
configured power switch every minute: it turns the switch off during a dispatch
hold and back on afterwards, and leaves the inverter in a safe state when it
gives up control. Disable any existing automations that write to the same
inverter before you set `proactive_mode: on`.
[`docs/adding-an-inverter.md`](docs/adding-an-inverter.md) describes the driver
contract and how to add another inverter, and
[`docs/adr/0003-ha-spark-owns-the-solis-inverter.md`](docs/adr/0003-ha-spark-owns-the-solis-inverter.md)
has the Solis handover procedure.

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

Other commands: `onboard` (propose entity mappings), `generate-dashboard`,
`backfill-load` (import or derive load history), `import-csv` and
`pull-consumption` (load grid-import history for backtests), `backtest`,
`forecast-eval`, `context`, `learn-factors`, `v2l`, `solis sync-clock`, and
`run` (the daemon). ha-spark reads its configuration from environment variables
or `.env` (see `.env.example`). The add-on reads `/data/options.json` instead.
