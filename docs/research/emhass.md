# EMHASS competitive analysis

Date: 2026-07-16
Subject: [davidusb-geek/emhass](https://github.com/davidusb-geek/emhass) ("EMHASS"), researched for ha-spark competitive positioning.

Repo stats at time of writing: 636 GitHub stars, 153 forks, 46 open issues,
most recent release `v0.17.9` on 2026-07-12
([repo page](https://github.com/davidusb-geek/emhass); [CHANGELOG](https://github.com/davidusb-geek/emhass/blob/master/CHANGELOG.md)).
EMHASS ships from a single core author, David HERNANDEZ TORRES (copyright
line in the [README](https://raw.githubusercontent.com/davidusb-geek/emhass/master/README.md),
2021–2026), with a release cadence of roughly 1–2 point releases a month
sustained since 2021. Unlike Predbat, EMHASS is a Python **module** with an LP
optimization core rather than a simulation/search engine — this is the
comparison ha-spark's own ROADMAP already draws when contrasting "LP solver"
vs "brute-force/simulation search" architectures.

## 1. Optimization model

**Solver stack: PuLP → CVXPY, defaulting to HiGHS.** EMHASS was originally
built on PuLP with CBC/GLPK backends; `v0.16.0` (January 2026) "completely
re-engineered the core optimization backend, moving from PuLP to CVXPY,"
introducing vectorized constraint generation and reporting "optimization
times are 4-5x faster, clocking in at approximately 0.1s per iteration" per
the [CHANGELOG](https://github.com/davidusb-geek/emhass/blob/master/CHANGELOG.md).
Current `optimization.py` ([source](https://raw.githubusercontent.com/davidusb-geek/emhass/master/src/emhass/optimization.py))
imports `cvxpy as cp` and defaults to the open-source **HiGHS** solver,
overridable via `optim_conf["lp_solver"]`, with configurable thread count.

**Horizon/timestep.** Docs default `optimization_time_step` to 30 minutes and
forecast horizon (`delta_forecast_daily`) to 1 day, both user-configurable
([config docs](https://emhass.readthedocs.io/en/latest/config.html)).

**Cost functions.** `_build_objective_function` implements three named modes
(source, as above): **"profit"** (maximize export income minus import cost,
with separate total-PV-sell vs grid-import-only variants), **"cost"**
(minimize import cost, same split), and **"self-consumption"** (two
variants: "bigm" — a big-M penalty on grid import — and "maxmin" — direct
maximization of a self-consumption variable `SC`).

**Deferrable loads.** Heavily parametrized, count set by
`number_of_deferrable_loads` (default 2, no documented upper bound). Each
load `k` gets a continuous power variable `p_deferrable[k]` plus binary
state variables (`p_def_bin1` on/off, `p_def_start` rising edge,
`p_def_bin2` sustained-run, `p_def_stop` falling edge for loads with a
minimum-off-time). Constraints cover time-window masks, total energy targets
(Big-M relaxed), minimum run/off timesteps with elapsed-time tracking
(issue #952), mid-operation power-pinning (issue #605), semi-continuous
mode, single-constant-block mode, sequence/list power profiles, startup
penalties, per-load cost overrides, and a flag to exclude non-electric loads
(gas/oil) from the grid balance. This is a materially richer deferrable-load
model than a simple on/off scheduler — closer to a small MILP sub-problem
per load.

**Battery model.** SoC dynamics are vectorized: stored energy is tracked via
cumulative sum of `(p_sto_pos/eff_dis + p_sto_neg*eff_chg) * timestep`,
bounded each step by `battery_minimum_state_of_charge` /
`battery_maximum_state_of_charge` (as % of `battery_nominal_energy_capacity`
in Wh), with separate charge/discharge efficiency factors and SoC-derated
power limits. A terminal constraint ties total energy change to
`(soc_init - soc_final) * capacity`. Newer additions include SoC "recovery"
parameters for single-shot recovery from out-of-band violations, an
intermediate SoC floor target at a specific timestep (issue #553), and
ramp-rate limits in "dynamic mode." A `set_use_battery_identification`
option lets EMHASS learn capacity and round-trip efficiency from historical
data rather than requiring the user to enter nameplate values.

**Thermal loads — yes, and unusually developed.** EMHASS models thermal
deferrable loads two ways: `thermal_config` for a passive building/zone
(start temp, outdoor-temp forecast array, hard min/max comfort band, soft
desired-temp with overshoot penalty, heating-rate/cooling-constant physics
params) and `thermal_battery` for an active heating system such as a hot
water tank or heat pump (thermal losses, heating demand from a draw-off
profile or building-physics calculation, heat-pump COP either configured or
derived via a Carnot-based `resolve_thermal_battery_cop()`, plus a
first-order thermal-inertia filter state carried across warm starts). This
is a genuine physics-based load model, not just a deferrable-load shim — see
the [Thermal Model docs page](https://emhass.readthedocs.io/en/latest/) and
[Discussion #340 "Use Case: Thermal Model"](https://github.com/davidusb-geek/emhass/discussions/340).
It is also a documented source of solver infeasibility (§5).

## 2. Forecasting

EMHASS names its forecast methods explicitly in `forecast.py`
([source](https://raw.githubusercontent.com/davidusb-geek/emhass/master/src/emhass/forecast.py))
and the [Forecasts docs page](https://emhass.readthedocs.io/en/latest/):

**Load forecast — `get_load_forecast()`**, four methods:
- **"typical"** (default): "basic statistics and a year long load power
  data grouped by the current day-of-the-week," 30-minute resolution,
  scaled via `maximum_power_from_grid`.
- **"naive"**: pure persistence — assumes the forecast for a future period
  equals observed values from a past period, offset controlled by
  `delta_forecast_daily` (default 24h).
- **"mlforecaster"**: the custom ML path (below).
- **"csv"/"list"**: direct externally-supplied data, no local computation.

**PV forecast — `get_weather_forecast()`**, three external-API paths plus
local computation:
- **"open-meteo"**: default weather API; irradiance converted to power
  locally via **PVLib**'s `ModelChain` (`get_power_from_weather()`).
- **"solcast"**: commercial subscription service with P50/P10/P90
  probabilistic estimates, multi-day horizons, conservative-bias blending.
- **"solar.forecast"** (forecast.solar): needs only installed capacity in
  kW as input.
- **"csv"/"list"**: direct externally-supplied data.
All three API methods support response caching to cut call volume.

**Price forecast**: peak/off-peak time-of-day bands with multiple
configurable windows, a constant-value method for PV sell price, or
CSV/runtime-list injection for external providers (e.g. Nordpool spot
prices — though see issue #309 for a report of that path failing).

**MLForecaster — the "learned" forecaster.** A scikit-learn-based
autoregressive regression framework: candidate regressors include KNN,
Random Forest, and Gradient Boosting; lag features and hyperparameters are
tuned via **Bayesian optimization through Optuna**; validation uses
time-series-aware cross-validation to avoid look-ahead leakage. EMHASS also
ships a calibration tool that walk-forward-validates all load-forecast
methods against 90 days of history so a user can pick empirically rather
than guess. Retrain cadence is user-triggered (a `model_fit` API action),
not automatic/scheduled by default — see issue reports of that call
[hanging under the REST retrieval path in v0.15.1](https://github.com/davidusb-geek/emhass/issues/648)
(§5).

Net: load and price forecasting are almost entirely local computation;
PV forecasting is where EMHASS leans hardest on external services
(Solcast/forecast.solar), with a locally-computed PVLib fallback via
Open-Meteo irradiance for users without a paid subscription.

## 3. Integrations & actuation

**EMHASS does not directly actuate hardware — it publishes.** The
`command_line.py` publish path (`publish_data()`,
[source](https://raw.githubusercontent.com/davidusb-geek/emhass/master/src/emhass/command_line.py))
posts optimization results and forecasts to Home Assistant as sensor
entities via a `RetrieveHass`/`post_data`/`post_scalar_sensor` mechanism —
e.g. `sensor.p_deferrable0`, `sensor.battery_identified_capacity`. No code
path calls an HA service (`call_service`) to flip a switch or set a
charger's mode directly. The docs' own framing: "Home Assistant provides a
platform for the automation of household devices based on the optimization
plan generated by EMHASS" — i.e. **the user must write their own HA
automations** that watch these sensors and issue the actual `switch.turn_on`
/ `number.set_value` calls. This is a materially different actuation model
from both Predbat (which writes to inverter entities/REST APIs itself) and
ha-spark (which has a gated driver layer with read-back verification,
`energy/chargers.py`). EMHASS's design pushes all actuation risk into
user-authored automations that EMHASS itself has no visibility into or
control over.

**EV charger support: none native.** The docs and config reference
([config.html](https://emhass.readthedocs.io/en/latest/config.html))
contain no EV-charger-specific entity type or config section; the CHANGELOG
does not document EV-charger features either. An EV charger can only be
modeled indirectly as a generic deferrable load (with the load's own energy
target/window constraints) — there is no OCPP/charger-protocol integration
comparable to Predbat's Gateway/EVC work.

**Multi-battery / multi-inverter: not supported as first-class.** Config
options (`battery_nominal_energy_capacity`, efficiencies, power limits, SoC
bounds) are singular, scalar parameters — the docs describe no mechanism for
declaring a second battery or inverter. `v0.10.0` (June 2024) added "support
for hybrid inverters and PV curtailment computation," which addresses AC-
vs DC-coupled single-inverter topologies, not multiple independent
batteries. Net: EMHASS is architecturally a single-battery optimizer.

## 4. Deployment

Four install paths, all documented from the
[README](https://raw.githubusercontent.com/davidusb-geek/emhass/master/README.md):
**PyPI** (`pip install`), **Conda**, standalone **Docker**, and a dedicated
**Home Assistant Add-on** via the separate
[emhass-add-on](https://github.com/davidusb-geek/emhass-add-on) repository
(add the repo to the HA add-on store, install, configure through the add-on's
web UI). The add-on and core-module versions are decoupled — the add-on repo
`config.yml` pins which EMHASS Docker tag it ships, so add-on users can lag
behind core releases.

Initial setup is config-heavy: users must supply Home Assistant sensor
entity IDs for load/PV/grid measurement, a long-lived access token,
timezone, cost-function choice, PV system specs (module/inverter model,
array orientation) if using PVLib-based local computation, and (for
Solcast/forecast.solar) API credentials entered under "Show unused optional
configuration options" in the add-on UI. EMHASS also pulls **2 days of
historical consumption data from the HA recorder database** on each run for
load-forecast fitting — a step users report becoming unreliable on large or
poorly-pruned recorder databases (§5). No MQTT is required for the base
add-on install; EMHASS communicates with HA over its REST/WebSocket API
using the long-lived token, and publishes back the same way.

## 5. Where it excels vs where users struggle

**Where EMHASS excels.** The optimization core is genuinely deep: a proper
LP/MILP formulation (not a heuristic search) with named, well-defined cost
functions, a deferrable-load model rich enough to express minimum-run-time,
startup penalties, and mid-cycle re-planning, and — unusually for this
category of tool — a physics-grounded thermal/heat-pump load model with its
own COP derivation. The `v0.16.0` PuLP→CVXPY rewrite (4-5x faster) shows the
maintainer is still actively improving the mathematical core, not just
bolting on integrations. The MLForecaster's use of Optuna-tuned scikit-learn
regressors with a walk-forward 90-day calibration tool is a more rigorous
"pick your forecast method empirically" workflow than most comparable
projects offer.

**Configuration complexity is the dominant, repeated complaint.**
[Issue thread: "Can't get EMHASS to start/work"](https://community.home-assistant.io/t/cant-get-emhass-to-start-work-help-wanted/681662)
(opened 2024-01-29 by `andreas-bulling`): optimization failed with
`Variable sensor.ac_loads was not found. This is typically because no data
could be retrieved from Home Assistant`, root-caused to a `KeyError` on
`sensor.ac_loads_positive` missing from the internal dataframe — i.e. a
sensor-entity-ID mismatch between what EMHASS expects and what the user
configured. A follow-up reply over a year later (2025-03-03, `grzywek`) hit
the *identical* unresolved error; the original poster's response: "No, I
gave up eventually and never tested/used this integration…" — over a year
with no fix or clear diagnostic path. A helpful forum regular's
[post #3561 in the main EMHASS thread](https://community.home-assistant.io/t/emhass-an-energy-management-for-home-assistant/338126/3561)
(2025-11-12) recommends newcomers start with **static tariff values** before
adding complexity — itself an implicit acknowledgment that the default
onboarding path is too much at once.

**Recorder-database dependency causes flaky first runs.** EMHASS retrieves
2 days of consumption history from HA's recorder database for load
forecasting; forum reports describe this becoming "troublesome" on large
recorder databases, with the practical fix being to restrict recording to a
minimal sensor set and wipe the recorder DB to start fresh — a workaround
that shouldn't be necessary for a first-run optimization call.

**Solver infeasibility has several known, documented triggers**, per
project history and issue search: deferrable loads with
`minimum_power_of_deferrable_loads > 0` combined with a mid-cycle power pin
below that minimum; passing a **float** instead of an int for
`operating_hours_of_each_deferrable_load` — no error raised, just a silent
infeasible result
([issue #561](https://github.com/davidusb-geek/emhass/issues/561)); thermal
model desired-temperature targets set for the first or second timestep when
the current temperature doesn't already meet them, described in project
notes as occurring "quite often"; and historically, battery SoC starting
below the configured minimum SoC in MPC mode (fixed by AC-coupled-form work
addressing issue #936). The pattern across these: **infeasibility fails
silently or with an unhelpful error** rather than surfacing the violated
constraint to the user.

**Data-retrieval reliability regressions.**
[Issue #648](https://github.com/davidusb-geek/emhass/issues/648) ("Home
Assistant REST API data retrieval 'hangs' in 0.15.1"): a 9-day
`model_fit` call over the REST path stalled indefinitely at "Retrieve hass
get data method initiated…" with no completion, while switching to the
(then-new, contributor-added) WebSocket retrieval path completed the same
call in under a second — i.e. a version regression in the default data path
that a config toggle works around, not a fix.

**Publish-path bugs compound when features are combined.**
[Issue #587](https://github.com/davidusb-geek/emhass/issues/587) reports
three interacting bugs in `/action/publish-data` on `v0.13.5`: battery
sensor data is silently dropped when a thermal deferrable load is also
configured; predicted temperature data isn't published at all (no sensor,
no attribute); and the publisher is hardcoded to 30-minute steps, raising a
`ValueError` when the optimization itself runs at 5-minute resolution,
forcing users to give up timestep granularity as a workaround. Reporter
describes all three as "100% reproducible"; the issue was open with no
visible maintainer response at time of research.

**Maintenance / bus factor.** Copyright and commit activity trace to a
single named author (David HERNANDEZ TORRES) across the project's five-year
history; the GitHub contributors graph did not fully resolve during this
research but the CHANGELOG's authorial voice and the README's single-name
copyright line are consistent with a primarily solo-maintained project with
occasional external PRs merged in (e.g. the WebSocket retrieval path in
issue #648 is credited to "another contributor"). Release cadence is
healthy (1-2 releases/month sustained), which suggests active but
resource-constrained maintenance — several of the issues above (#587, and
the year-plus-unresolved sensor-mismatch thread) show real gaps in response
latency on non-trivial reports even while shipping features. Compare to
Predbat's own self-diagnosed complexity/maintenance tension
(`docs/research/predbat.md` §4) — this is a smaller-team version of the
same "features keep shipping, longstanding rough edges don't get closed"
pattern, just without the multi-year public discussion thread Predbat has.

## 6. Beat / match / trail vs ha-spark

Read against ha-spark's own `ROADMAP.md`, `CONTEXT.md`, and module
docstrings (`energy/sources.py`, `energy/planner.py`, `energy/chargers.py`,
`energy/supply_guard.py`, `router.py`, `copilot.py`, `intent_parser.py`,
`config.py`).

**Where ha-spark already beats EMHASS:**

- **Actuation model.** EMHASS never actuates hardware itself — it publishes
  advisory sensors and leaves every real write to user-authored HA
  automations EMHASS has no visibility into. ha-spark's driver layer
  (`energy/chargers.py`) actuates directly under an explicit authority model
  (`control: observe|ha_spark|supplier`) with `PROACTIVE_MODE` gating,
  per-action isolated failure, and read-back verification. This is the
  inverse tradeoff of §3's "publish vs actuate" split: EMHASS's approach is
  safer in the narrow sense that a bug in EMHASS can't directly flip a
  switch, but it also means EMHASS offers no guarantee at all about
  correctness of the actuation that eventually happens — that's entirely
  outsourced to whatever automation the user wrote. ha-spark owns the whole
  path and gates it centrally, which is auditable in a way "trust the user's
  automation" is not.
- **Explains itself in plain language.** EMHASS has no natural-language
  layer; its interface is a config UI, a dashboard, and log lines. ha-spark's
  copilot (`router.py`, `copilot.py`) grounds a local Ollama LLM in the
  actual computed plan with a deterministic offline fallback
  (`intent_parser.py`), and the LLM is architecturally barred from reaching
  `call_service` — a distinction that has no EMHASS analogue to even
  compare against, since EMHASS has no LLM tier at all.
- **Onboarding friction.** EMHASS's dominant complaint cluster (§5) is
  sensor-entity-ID mismatches between what EMHASS's config expects and what
  the user's HA setup actually exposes, sometimes unresolved for over a
  year (the `ac_loads_positive` thread). ha-spark's raw `entity_id`
  addressing with no fuzzy name matching is the same "you must get the ID
  right" precondition, but the failure mode is different — the ROADMAP's
  standing decision to require exact entity_ids narrows this to a single,
  well-scoped configuration step rather than the sprawl of forecast method /
  cost function / PV system spec / recorder-DB retrieval config surface
  EMHASS's onboarding thread describes.

**Where they roughly match:**

- **Both delegate PV forecasting to external services with a local
  fallback.** EMHASS: Solcast/forecast.solar with PVLib+Open-Meteo as a
  free-tier local fallback. ha-spark: same general shape per
  `energy/sources.py` (external forecast in, local computation as
  fallback), though EMHASS's PVLib `ModelChain` integration is a more
  mature, purpose-built solar-physics fallback than what a from-scratch
  local PV model would typically achieve.
- **Deterministic core, non-LLM decision path.** EMHASS's LP solver and
  ha-spark's `energy/planner.py` (pure function, inputs+config→plan) are
  both fully deterministic, auditable decision engines with no ML/LLM
  in the actual plan computation — the two projects agree on this
  principle even though the actual algorithms differ substantially (LP
  optimization vs a direct energy-balance calculation, per the ROADMAP's
  ADR-0002 framing already used in the Predbat writeup).

**Where ha-spark clearly trails:**

- **Optimization model depth.** EMHASS's LP/MILP formulation — named cost
  functions (profit/cost/self-consumption with bigm/maxmin variants),
  minimum-run-time and startup-penalty deferrable-load constraints, and a
  genuine physics-based thermal/heat-pump load model with COP derivation —
  is materially more sophisticated than ha-spark's current direct
  energy-balance planner. This is the same "different philosophy" caveat as
  the Predbat writeup, but EMHASS's version of "different" is a real
  optimization engine, not just a different search strategy, so the gap in
  raw modeling power is larger here than the ha-spark/Predbat gap.
- **Forecasting rigor.** EMHASS's MLForecaster (Optuna-tuned scikit-learn
  regressors, walk-forward 90-day calibration across multiple candidate
  methods) is a more disciplined "prove which forecast method actually
  works for this house" workflow than anything currently in ha-spark.
- **Thermal/HVAC load modeling.** ha-spark has no equivalent to EMHASS's
  `thermal_config`/`thermal_battery` model; this is a capability EMHASS has
  that ha-spark simply doesn't attempt yet.
- **Deployment path breadth.** EMHASS ships pip, Conda, Docker, and HA
  add-on paths with a five-year-mature add-on repo. ha-spark currently ships
  the add-on path per its own packaging conventions (CLAUDE.md) without the
  same breadth of standalone install options.
- **Maturity/field exposure.** Five years of continuous shipping and a
  636-star, actively-discussed community forum thread give EMHASS far more
  real-world edge-case exposure than ha-spark has accumulated. The
  documented rough edges (§5) are real, but they're the rough edges of a
  tool that has been run against a much wider variety of households, HA
  configurations, and hardware than ha-spark has been tested against to
  date.

**One honest caveat on method:** EMHASS's optimization core is stronger than
ha-spark's specifically *because* it took on LP-modeling complexity (solver
dependency, infeasibility failure modes, a much larger config surface) that
ha-spark's simpler direct-calculation planner deliberately avoids. The
fair comparison is "ha-spark trades optimization sophistication for
auditability and a smaller failure surface," not "ha-spark's planner is
strictly behind." Whether that tradeoff is worth it depends on whether
ha-spark's target households need EMHASS-grade load/thermal modeling — the
ROADMAP's own "Competitive MVP" framing (CONTEXT.md) suggests the answer,
for Kyle's household specifically, is currently "no."
