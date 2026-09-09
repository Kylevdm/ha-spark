# ha-spark roadmap

*The home energy autopilot that explains itself — and sets up in 15 minutes.*

> **Phases and status live in the GitHub tracker, not this file:**
> [milestones](https://github.com/Kylevdm/ha-spark/milestones) (one per
> phase/add-on version) and their [issues](https://github.com/Kylevdm/ha-spark/issues).
> This file keeps only the durable content: positioning, design rules,
> comparison, core bets, and non-goals. Vocabulary: `CONTEXT.md`.
> Design decisions: [`docs/adr/`](docs/adr/).

## Positioning

ha-spark plans your home's energy day for you: it forecasts tomorrow's solar and
household load, works out how much overnight charge your battery actually needs
at the cheap rate, and actuates the inverter itself — no template sensors, no
hand-written automations, no YAML.

Two design rules define the project:

1. **A deterministic planner decides; an LLM only explains.** Battery setpoints
   come from an auditable energy-balance model, never from a language model.
   The natural-language layer sits *on top* — "what's the plan for tonight?",
   "why are you charging to 80%?", "what did you save this week?" — and runs
   against a local Ollama instance with a deterministic fallback, so there is
   no cloud dependency and no hallucinated control.
2. **Trust is earned, not assumed.** ha-spark starts in observe/simulate mode,
   logging exactly what it *would* have done alongside what your current setup
   did, with a cost backtest to quantify the difference. You flip it to real
   control when the numbers convince you.

Everything runs locally as a Home Assistant add-on. No cloud service, no
subscription, no data leaving your network.

## How it compares

[EMHASS](https://github.com/davidusb-geek/emhass) and
[Predbat](https://github.com/springfall2008/batpred) are excellent, mature
projects and the right choice for many households today. ha-spark makes a
different bet: that most people with a battery and solar want an autopilot they
can install, understand, and trust in an evening — not an optimization
framework to configure.

| | EMHASS | Predbat | ha-spark |
|---|---|---|---|
| Optimizes a plan | ✅ LP solver | ✅ | ✅ energy-balance planner ([why not LP](docs/adr/0002-auditable-over-optimal-planning.md)) |
| Actuates hardware itself | ❌ user wires automations | ✅ | ✅ with guard rails (SoC validity, read-back, failure isolation) |
| Setup effort | YAML + sensor templates + REST commands | YAML; docs assume HA/file-editing fluency | add-on options UI; onboarding wizard planned |
| Explains decisions in plain language | ❌ | ❌ | ✅ planned (local LLM over the deterministic plan) |
| Try-before-trust mode | ❌ | partial (read-only mode) | ✅ simulate mode + savings backtest |
| Cloud dependence | none | none (paid cloud version exists) | none, by design |

## Core bets

### 1. Fifteen-minute onboarding

The most common complaint about home energy management in the HA community is
setup pain. ha-spark's target experience:

1. Add the repository URL in the add-on store and install.
2. `onboard` detects your inverter, solar forecast, tariff, and EV entities
   from the Home Assistant registry and proposes the configuration.
3. The health check confirms connectivity end to end.
4. The same evening, simulate mode shows you the first overnight plan.

First simulated plan within 15 minutes of install, zero YAML.

### 2. An energy copilot that explains itself

A read-only natural-language surface over the planner and live state: ask what
the plan is, why it chose what it chose, and what it saved you. Later, NL
requests adjust *planner configuration* ("keep the battery above 30% this
weekend") — they never bypass the planner to actuate hardware directly.

## Where the plan lives

- **Shipped:** everything through add-on v0.9.0 (deterministic planner, Solis
  actuation with guard rails, simulate mode + backtest, onboarding wizard, NL
  copilot, forecast ledger + weather-aware ML, context store, occupancy
  habits), then Phase 7 (device-driver core, 1.0.0) —
  `ha_spark_addon/CHANGELOG.md` is the shipped record.
- **In flight and next:** the [milestones](https://github.com/Kylevdm/ha-spark/milestones),
  currently Phase 8 (multi-supplier tariffs) then the competitive-MVP
  sub-phases 10.1–10.4 (epic [#43](https://github.com/Kylevdm/ha-spark/issues/43)).
  Phase 9 (EV charger drivers) is formally deferred
  ([#61](https://github.com/Kylevdm/ha-spark/issues/61)).
- **Someday:** the post-1.0 bucket
  ([#62](https://github.com/Kylevdm/ha-spark/issues/62)).

Cross-cutting throughout: the deterministic planner decides; **security is a
top priority** (see `CLAUDE.md`); every controllable device carries a
`control: observe | ha_spark | supplier` authority, and real writes need
`control == ha_spark` **and** `PROACTIVE_MODE == on`.

## Non-goals

- **No cloud service.** Local-first is a feature, not a phase.
- **No LLM-decided setpoints.** The language model explains and reports; it
  does not control hardware.
- **No fuzzy entity-name matching.** All control paths use exact `entity_id`s.

For installation and configuration, see
[`ha_spark_addon/DOCS.md`](ha_spark_addon/DOCS.md).
