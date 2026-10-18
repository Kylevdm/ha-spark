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

## Engineering highlights

- **A pure planner.** `compute_plan` takes inputs and config, returns a plan,
  and does no I/O. Every plan line comes from a reservation or a price, so a
  test can replay any decision and the copilot can explain it
  ([why not an LP solver](docs/adr/0002-auditable-over-optimal-planning.md)).
- **Gated actuation.** All hardware writes go through one decision path.
  ha-spark writes only when the owner has switched on real control and the
  SoC reading is valid, and it reads back every write to confirm it. A failed
  action doesn't stop the others. Simulate mode runs the same path and skips
  the writes.
- **Untrusted inputs.** ha-spark treats Home Assistant states, tariff and Axle
  payloads, and LLM replies as untrusted. It coerces or validates each one
  before use. A bad payload drops out of the plan and never crashes ha-spark
  or triggers a write. The LLM can't reach a hardware command.
- **One driver interface.** Solis and AlphaESS inverters implement the same
  driver interface, so the planner never handles vendor-specific charge
  mechanics. [`docs/adding-an-inverter.md`](docs/adding-an-inverter.md)
  describes it.
- **Quality gates.** ha-spark is Python 3.11+ on asyncio. Ruff, strict mypy,
  and over 1,000 tests must pass before every merge. The tests use respx for
  HTTP and fake servers for WebSocket. The add-on is a Docker image that Home
  Assistant builds from a pinned release tag.
- **Written decisions.** Each architecture decision has an
  [ADR](docs/adr/), and [`CONTEXT.md`](CONTEXT.md) defines the domain terms.

> Status: add-on v0.22.0 includes:
>
> - a deterministic planner that replans every half hour, holds battery energy
>   back with reservations, and can derive base load from component statistics
> - per-device control authority
> - fixed, dynamic, and Octopus Intelligent tariffs, plus Axle export events
>   as a separate provider that works with any of them
> - native Solis timed-slot control. A per-minute reconcile turns the power
>   switch off during Octopus dispatches and EV charging, and ha-spark checks
>   the inverter clock before any export.
> - SoC checks against integrated battery power. When readings keep failing,
>   ha-spark can program a fallback charge. Once readings have passed for
>   `soc_recovery_minutes` (default 10), it applies a fresh plan and returns to
>   normal planning when that plan reads back clean.
> - V2L. The planner flags when a house-battery refill from the car would cost
>   less than grid import (scheduling it waits on plugged-in sensing), and
>   ha-spark asks you to start V2L when an Axle export slot would otherwise go
>   unfunded. ha-spark never switches V2L itself.
> - simulate mode and savings backtests
> - onboarding, the NL copilot, and an optional API that external agents can
>   use to read data and trigger gated actions (`agent_surface`)
>
> Axle export completed its first paid export on real hardware on 2026-10-05
> ([#134](https://github.com/Kylevdm/ha-spark/issues/134)). It stays a
> supervised prototype until it passes the v1.0.0 release gate, so follow
> the [supervised export runbook](docs/runbooks/supervised-axle-export.md) and
> don't run it unattended.
>
> [`ha_spark_addon/CHANGELOG.md`](ha_spark_addon/CHANGELOG.md) lists what each
> release shipped, the GitHub
> [milestones](https://github.com/Kylevdm/ha-spark/milestones) track open work,
> and [`CONTEXT.md`](CONTEXT.md) defines the project's terms.

## Axle export events

ha-spark can take part in [Axle](https://axle.energy) flexibility events. It
exports battery energy to the grid during the paid slots. Axle is a separate
event provider, so it works with any supported tariff.

1. **Event intake.** ha-spark reads the event from the Axle API and falls back
   to a Home Assistant entity if the API read fails. It rejects an event with
   a missing or timezone-less timestamp, an end before its start, a
   future-dated update, or an unknown direction. The planner never schedules
   from a rejected event.
2. **Planning.** The planner picks complete half-hour slots inside the event
   and caps export at the configured DNO export limit. It uses reservations to
   charge and hold back enough energy to fund those slots, and keeps the
   household's own post-event reservation.
3. **Actuation.** The inverter driver checks the power switch, control
   authority, and SoC freshness, then writes the discharge window and reads it
   back. ha-spark checks the inverter clock first, because the inverter fires
   the window on its own clock.
4. **Cleanup.** After the event ends, ha-spark clears the window and reads it
   back to confirm, so the schedule can't repeat the next day.

The first paid event ran end to end on real hardware on 2026-10-05. Nobody
entered the event window by hand or intervened during the event
([evidence log](https://github.com/Kylevdm/ha-spark/issues/134)). The run
found two gaps. Export paused for 10 to 15 seconds at each mid-event replan,
and 0.20.0 fixed that ([#218](https://github.com/Kylevdm/ha-spark/issues/218)).
ha-spark sent no lifecycle notices because no notify service was configured,
and proving them on a real event is still open
([#219](https://github.com/Kylevdm/ha-spark/issues/219)). Until the feature
passes the v1.0.0 release gate, run it under the
[supervised export runbook](docs/runbooks/supervised-axle-export.md).

### When an export pays

The planner funds export slots by charging beforehand and holding energy
back. If that isn't enough, it may spend the post-event reservation, which is
the energy set aside to run the house from the end of the event to the next
cheap slot. It does that only when the event pays more than buying the energy
back costs
([#207](https://github.com/Kylevdm/ha-spark/issues/207)):

```text
fund the slot if  event rate > worst import price where the shortfall lands / round-trip efficiency
```

The rule includes no margin and no battery-wear cost. The planner never spends
the energy reserved for house load before or during the event, and never takes
the battery below `min_soc`.

As an illustration (these aren't measured figures), take an event that pays
£0.80/kWh and a battery with 90% round-trip efficiency. If the shortfall lands
in slots priced at £0.30/kWh, buying it back costs £0.33/kWh, so the planner
funds the slot. Its plan reason reads "Funded 1.20 kWh from the post-event
reserve: £0.80 event vs £0.33 buy-back." If those slots cost £0.75/kWh, buying
back costs £0.83/kWh and the planner skips the slot. The plan then reports
the SoC the event would need at its start to fund every slot
([#214](https://github.com/Kylevdm/ha-spark/issues/214)).

ha-spark doesn't yet record what each event earned. The savings backtest
covers grid import under your tariff, not Axle payments. Recording per-event
earnings is tracked in [#373](https://github.com/Kylevdm/ha-spark/issues/373).

### Axle roadmap

The supervised prototype
([#128](https://github.com/Kylevdm/ha-spark/issues/128)) closes after the next
real event proves two things:

- the lifecycle notices reach the owner's phone
  ([#219](https://github.com/Kylevdm/ha-spark/issues/219));
- the inverter can hold a timed-discharge current below its ceiling, with
  reliable read-back ([#136](https://github.com/Kylevdm/ha-spark/issues/136)).

In v1, the companion integration takes over the inverter export write,
read-back, clock gate, and power-switch handling. The add-on will send it
per-device intents, such as an export window, instead of register values
([#316](https://github.com/Kylevdm/ha-spark/issues/316),
[#323](https://github.com/Kylevdm/ha-spark/issues/323)). The add-on keeps the
Axle provider, event funding, the buy-back rule, and the V2L request. Every v1
install starts in Observe mode, so an owner has to opt in to Optimise before
ha-spark exports for an event.

Open work on the feature itself:

- per-inverter current limits in place of one hard-coded model limit
  ([#241](https://github.com/Kylevdm/ha-spark/issues/241));
- reading today's solar forecast, so ha-spark can accept same-day daytime
  events and check them against the DNO limit
  ([#201](https://github.com/Kylevdm/ha-spark/issues/201));
- counting a V2L refill from the car when funding an event
  ([#188](https://github.com/Kylevdm/ha-spark/issues/188));
- recording what each event earned
  ([#373](https://github.com/Kylevdm/ha-spark/issues/373)).

## Road to v1.0.0

v1.0.0 lets a new household set up and run ha-spark from the Home Assistant
UI ([#247](https://github.com/Kylevdm/ha-spark/issues/247)). It adds:

- an ingress web app for onboarding, statistics, a visual plan, chat, a manual
  override, and settings, giving every owner task a home in the UI
  ([CLI parity](https://github.com/Kylevdm/ha-spark/issues/364#issuecomment-6241994831));
- a companion HACS integration that talks to Solis and AlphaESS inverters
  directly, replaces the Solcast integration, and owns ha-spark's entities.
  The add-on keeps the
  planner and the UI, and the integration makes no planning decisions
  ([ADR 0005](docs/adr/0005-integration-authority-and-operating-modes.md));
- configuration that you edit in the UI, with the Supervisor options cut down
  to the keys needed to start the add-on;
- one site-wide operating mode (Observe, Protect or Optimise) in place of
  `proactive_mode` and per-device `control`. Every v1 install starts in
  Observe ([ADR 0008](docs/adr/0008-operating-mode-and-authority-lifecycle.md)).

The earlier v1 definition
([#94](https://github.com/Kylevdm/ha-spark/issues/94)) still applies where it
doesn't conflict. Of its features, derived base load, half-hourly replanning,
reservations, and supervised Axle export have shipped. V2L refill planning
has shipped in the planner but can't schedule a refill until plugged-in
sensing lands.
The morning plan digest and the readiness signal
([#53](https://github.com/Kylevdm/ha-spark/issues/53)) are still open. The
readiness signal will tell you when your simulate history supports switching
on real control. `1.0.0`
ships once every item in the
[release gate](docs/releasing.md#v100-release-gate) is confirmed on real
hardware.

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
configured power switch every minute. It turns the switch off during an
Octopus dispatch or EV charging hold and back on afterwards, and leaves the
inverter in a safe state when it gives up control. Disable any existing automations that write to the same
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

In v1, `backtest` and `forecast-eval` fold into a new `stats` command,
`plan --apply` is retired (`run` becomes a replan request), and
`solis sync-clock` becomes a device-generic `clock sync`
([#364](https://github.com/Kylevdm/ha-spark/issues/364#issuecomment-6241994831)).
