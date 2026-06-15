# ha-spark roadmap

*The home energy autopilot that explains itself — and sets up in 15 minutes.*

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
| Optimizes a plan | ✅ LP solver | ✅ | ✅ energy-balance planner |
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

## Phases

| Phase | What ships |
|---|---|
| ✅ MVP (done) | Deterministic planner (solar + load forecast → overnight charge current), Solis actuation with guard rails, dispatch-slot handling, simulate mode, cost backtest, scheduled daemon, HA add-on packaging |
| ✅ 2 — LLM router (done) | Two-tier router behind `ha-spark ask`: remote Ollama chat (`/api/tags` probe gates `/api/chat`) with a deterministic offline parser answering energy queries (plan, SoC, solar, strategy, mode, window) from the planner pipeline |
| ✅ 3 — EV integration (done) | Live supply guard (throttle battery charging when whole-house draw nears the main-fuse limit, e.g. during an EV dispatch) plus EV dispatch energy in the plan report |
| ✅ 6A — Forecast ledger (done) | Forecast-vs-actual accuracy ledger (`ha-spark forecast-eval`) and a 30-min signal sampler (occupancy, heat-pump energy, outdoor temp) so training data accumulates |
| ✅ 6B — Weather-aware ML model (done) | Gradient-boosted quantile slot model (Open-Meteo temps, HDD, day-type, lags, occupancy); `load_model: auto` gated by the ledger; quantile buffer mode |
| ✅ 6C — Context store (done) | Date-ranged facts (away/guests) via `ha-spark context`; deterministic load scaling, visible in the plan report |
| ✅ 6D — LLM context extraction (done) | "I'm on holiday for two weeks" in `ha-spark ask` → structured fact in the context store (Ollama JSON extraction + offline fallback); facts only, never setpoints |
| ✅ 6E — Occupancy habits (done) | Predict occupancy from recorded patterns; learn the away-load factor (auto-applied); seed of the `predict_actions` habit API (advisory, gated by `PROACTIVE_MODE`) |
| ✅ 4 — Onboarding wizard (done) | Entity auto-discovery (domain / device class / unit / attribute / name matching) and `ha-spark onboard` proposal with `--json`/`--write`/`--preset`; Solis reference preset |
| ✅ 5 — NL copilot v1 (done) | Plan/state Q&A grounded in live planner output: `ha-spark ask` feeds the computed plan and state into the Ollama tier so answers explain the actual decision, scoped to the energy domain; context set/queried in chat via 6C/6D |
All of the above shipped through add-on **v0.9.0** (tagged `v0.9.0`).

## v1.0 — Modular ecosystem + agent surface

Open ha-spark beyond the one Solis/Solcast/Octopus/zappi setup, and make it
something a future Home Assistant agent ("Jarvis") can orchestrate. Detailed
plan: see the active plan file. Cross-cutting throughout: the deterministic
planner still decides; **security is a top priority** (see `CLAUDE.md`); every
controllable device carries a `control: observe | ha_spark | supplier`
authority, and real writes need `control == ha_spark` **and**
`PROACTIVE_MODE == on`.

| Phase | What ships | Add-on |
|---|---|---|
| 7 — Device-driver core | `devices/` driver layer (inverter drivers + registry), `Capability`/`ControlAuthority`, structured per-device config + migration shim off the flat 0.9.0 config; planner actuation routed through drivers + the authority gate. Zero behaviour change for the current setup. | 1.0.0 |
| 8 — Multi-supplier tariffs | `TariffProvider` ecosystem yielding a normalised per-slot import/export price schedule + controlled windows (`fixed`, `time_of_use`, `dynamic`/half-hourly price sensors, `export`, `octopus_intelligent`); planner costed against the schedule, not a fixed window. | 1.1.0 |
| 9 — EV drivers + supplier authority | EV charger drivers; EV defaults to `supplier` (observe & plan around), with an optional `ha_spark` control path; reads V2L availability. | 1.2.0 |
| 10 — Multi-source charging + notifications | R48 rectifier drivers (grid-wired for charging outside the night slot; V2L-fed from the car); planner chooses among charge sources by cost/availability; a `NOTIFY` action ("plug in the car") via HA `notify`. | 1.3.0 |
| 11 — Heat pump (observe + model) | First-class heat-pump device fed explicitly into the load model; control deferred. | 1.4.0 |
| 12 — Driver-aware onboarding | `onboard` proposes driver + provider + entity map + capability coverage; per-driver/supplier presets; multi-device. | 1.5.0 |
| 13 — MCP agent surface ("Jarvis") | An authenticated, ingress-bound MCP server exposing read tools (plan/state/eval/predictions/health) and gated act tools (context, run-plan, notify) for an external HA agent. | 1.6.0 |

## Later (v3, post-1.0)

Heat-pump *active* coordination + hot-water tank, multi-inverter sites,
Solcast bias correction, EV-dispatch propensity prediction, more vendor
presets/drivers as the ecosystem grows.

## Backlog

- **Re-bundle the ML load model in the add-on image.** Dropped in 0.9.1 because
  scikit-learn ships no musllinux wheel and was source-compiling (no compiler in
  the base image → build failure). `load_model: ml|auto` currently degrades to
  the slot-profile median on the add-on. Re-add `scikit-learn`/`numpy` to
  `ha_spark_addon/Dockerfile` once a musllinux wheel is published, or compile it
  in (build-base + openblas — heavier image, slow on Pi hosts).

## Non-goals

- **No cloud service.** Local-first is a feature, not a phase.
- **No LLM-decided setpoints.** The language model explains and reports; it
  does not control hardware.
- **No fuzzy entity-name matching.** All control paths use exact `entity_id`s.

For installation and configuration, see
[`ha_spark_addon/DOCS.md`](ha_spark_addon/DOCS.md).
