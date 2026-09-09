# ML survey: learned inputs for home energy planning

Date: 2026-07-16
Subject: wayfinder ticket #70 — what ML approaches (open-source and academic)
exist for load forecasting, solar/PV forecasting, learned battery efficiency/
degradation, and household behaviour prediction, that could feed
`energy/planner.py:compute_plan` as **inputs**, without moving any decision
authority off the deterministic planner. Written to sit alongside
`docs/research/emhass.md` and `docs/research/predbat.md`.

**Standing constraint this survey is scoped to:** ha-spark's planner is a pure
function (inputs + config → plan); ML/LLM may only improve the inputs
(forecasts, learned efficiencies, behaviour signals) fed into that function.
Nothing here proposes an ML component that decides — see §5 for why the
decision-side alternatives (RL, learned policies, end-to-end optimizers) are
deliberately out of scope for ha-spark, documented rather than adopted.

## 1. Load forecasting (household electrical load)

**Baseline: persistence / historical averaging.** Both EMHASS's "naive"
method (assume future period equals a past period, offset by
`delta_forecast_daily`) and "typical" method (day-of-week grouped statistics
over a year of history) and Predbat's default `days_previous` lookback
(`docs/research/predbat.md` §1, §4) are the boring baseline everything else
is measured against. Zero training cost, no library dependency beyond what
already talks to HA's recorder, and per EMHASS's own calibration tooling
these baselines are often competitive for a single household — see §6.

**skforecast (scikit-learn-compatible recursive/direct multi-step
forecasting).** A Python library purpose-built for turning any
scikit-learn-API regressor (including XGBoost, LightGBM) into a multi-step
time-series forecaster by auto-generating lag-feature matrices
([skforecast docs](https://skforecast.org/0.14.0/introduction-forecasting/introduction-forecasting.html);
[GitHub](https://github.com/skforecast/skforecast)). This is exactly the
mechanism EMHASS's `MLForecaster` uses under the hood: EMHASS's forecast
module documents "mlforecaster that uses regression models considering
auto-regression lags as features" with candidate regressors KNN, Random
Forest, and Gradient Boosting, lag/hyperparameter tuning via Bayesian
optimization through Optuna, and time-series-aware cross-validation to avoid
look-ahead leakage (`docs/research/emhass.md` §2). EMHASS additionally ships
a walk-forward 90-day calibration tool that backtests all its load-forecast
methods against real history so a user can pick empirically rather than
guess — the single most disciplined "prove it before you trust it" workflow
found in this survey.
**Data requirement:** weeks to a year of load history (EMHASS's "typical"
method wants a full year for day-of-week grouping; MLForecaster's walk-forward
calibration uses 90 days). **Compute:** trivial — scikit-learn regressors on
a few thousand lag-feature rows fit in seconds on any modest CPU; Optuna
hyperparameter search adds minutes, is triggered by a user-initiated
`model_fit` action, not scheduled by default. **Deps:** `scikit-learn`,
optionally `lightgbm`/`xgboost`, `optuna` — all pure-Python-wheel-installable
on glibc `python:slim`, no GPU. **Shipped in the HA ecosystem today:**
yes — EMHASS's MLForecaster is exactly this, in production for years.

**Gradient boosting (LightGBM/XGBoost) directly, without the skforecast
wrapper.** Academic literature strongly favours LightGBM for household-level
short-term load forecasting: a comparative study on a London-households
dataset (LightGBM vs KNN, SVM, linear regression, decision trees, random
forest, XGBoost, CatBoost, extremely randomized trees) found LightGBM the
most effective, MAPE 0.18%, R² 0.86
([International Journal of Ambient Energy, 2025](https://www.tandfonline.com/doi/full/10.1080/01430750.2025.2577864)).
LightGBM's leaf-wise tree growth trains faster and often more accurately than
XGBoost's level-wise growth on tabular time-series features; both have
built-in L1/L2 regularization to control overfitting on a single house's
noisy history
([ScienceDirect ensemble comparison](https://www.sciencedirect.com/science/article/abs/pii/S0957417423031883)).
Ensembling LightGBM+XGBoost+CatBoost (or with LSTM/TCN components) recovers a
further few percent accuracy in several papers but adds real engineering
complexity and additional dependencies (deep-learning frameworks) for a
return that is unlikely to matter at single-household scale — flagged here
as a diminishing-returns pattern, not recommended for ha-spark specifically.
**Data requirement:** the REFIT-dataset comparative study
([arXiv:2512.00856](https://arxiv.org/pdf/2512.00856)) that benchmarks SARIMA
through Transformers on individual UK households is a useful reference point
for realistic single-house forecast error floors — worth reading before
assuming any technique will do dramatically better than a well-tuned
baseline on one house's noisy, habit-driven load curve.
**Compute:** LightGBM on tabular lag features is CPU-fast (sub-second
inference, seconds to fit) — no GPU needed, the standard case this survey
was scoped to. **Library maturity:** LightGBM and XGBoost are both extremely
mature, pinnable, glibc-wheel-installable Python packages.

**Quantile regression / probabilistic forecasts.** Rather than a single
point forecast, LightGBM and XGBoost both support a `quantile` objective
(pinball loss) directly, producing calibrated prediction intervals (e.g.
P10/P50/P90) at negligible extra compute cost — same features, same training
loop, different objective, per
[scikit-learn's own quantile GBR example](https://scikit-learn.org/stable/auto_examples/ensemble/plot_gradient_boosting_quantile.html)
and the wider literature on GBM pinball-loss quantile forecasting. This is
directly relevant to a planner that wants a conservative (P90) load estimate
to size battery reserve against, rather than trusting a point forecast that
is wrong half the time by construction. **Recommended as a cheap upgrade
path** once a point-forecast GBM baseline exists — same dependency, same
compute footprint, materially more useful output for a safety-margined
planner.

**NeuralProphet/Prophet.** A neural (PyTorch-based) reimplementation of
Facebook's Prophet, aimed at interpretable decomposition (trend + seasonality
+ events) rather than raw accuracy
([neuralprophet.com](https://neuralprophet.com/);
[arXiv:2111.15397](https://arxiv.org/pdf/2111.15397)). Pulls in PyTorch as a
dependency — a materially heavier, harder-to-pin dependency footprint than
scikit-learn/LightGBM for a benefit (interpretable decomposition) that
matters less once the planner and copilot already explain their own
reasoning in plain language. **Not recommended** for ha-spark: the
dependency cost isn't justified when the copilot layer already provides the
"why" a Prophet-style additive decomposition would otherwise buy.

**Custom small neural net (Predbat's LoadML).** Predbat's own from-scratch
alternative to `days_previous`: a small multilayer perceptron implemented in
pure NumPy (no PyTorch/TensorFlow dependency), ~889k parameters, layers
`[512, 256, 64]` with ReLU, He init, AdamW with weight decay 0.01, Huber loss
for spike robustness, predicting the next 5-minute consumption step from
1,446 input features (historical load/PV/temperature/import-export-rate lags
plus 6 cyclical time encodings)
([springfall2008/batpred docs/load-ml.md](https://github.com/springfall2008/batpred/blob/main/docs/load-ml.md)).
Trains on a "live fetch" of up to 28 days from HA plus a persisted on-disk
90-day rolling database (`predbat_ml_history.npz`) to work around HA
recorder retention limits; retrains via curriculum learning (4 progressive
passes, 100 epochs) on first run, then fine-tunes every 2 hours (30 epochs,
early stopping) with an EMA-tracked normalization drift to adapt to seasonal/
tariff changes without full retraining. Notable design choice: pure NumPy
avoids the PyTorch/TensorFlow dependency entirely while still getting a
from-scratch trainable net — a genuinely CPU-friendly path if a project
wants neural-net flexibility without the framework weight, though it also
means reimplementing autodiff/optimizer plumbing that PyTorch would give for
free. **Shipped in the HA ecosystem today:** yes, in production in Predbat.

## 2. Solar / PV forecasting

**Baseline: pvlib + physics-based `ModelChain`.** `pvlib-python` is the
standard open-source library for converting weather/irradiance inputs into
DC/AC PV power output using named physics models (solar position →
plane-of-array irradiance via a transposition model → cell/module temperature
→ DC power via the Sandia Array Performance Model or single-diode model → AC
power via the Sandia Inverter Model), packaged behind the high-level
`ModelChain` interface
([pvlib-python ModelChain docs](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.modelchain.ModelChain.html);
[pvlib python paper, OSTI](https://www.osti.gov/pages/servlets/purl/1993714)).
This is not "ML" — it's the physics baseline every ML approach in this
section should be measured against and, ideally, correct rather than
replace. EMHASS uses exactly this as its local-computation fallback
(`get_power_from_weather()` on Open-Meteo irradiance,
`docs/research/emhass.md` §2) when a user has no Solcast/forecast.solar
subscription. **Compute:** trivial, pure NumPy-scale arithmetic, no training.
**Deps:** `pvlib` is mature, pip-installable, glibc-friendly.

**Commercial forecast APIs (Solcast, forecast.solar) as the accuracy
ceiling.** Both EMHASS and Predbat treat Solcast as the primary, most
accurate PV forecast source (probabilistic P10/P50/P90 estimates,
multi-day horizons) with forecast.solar as a capacity-only free-tier
alternative (`docs/research/emhass.md` §2, `docs/research/predbat.md` §1).
These are not local ML — they're vendor-side proprietary forecasts consumed
as external data — but they set the bar: any locally-trained ML correction
is being asked to close the gap between a free physics/weather-only estimate
and a commercial forecast that likely already blends its own ML/NWP-ensemble
techniques upstream, a gap it may not be able to close without equivalent
data (satellite imagery, NWP model access) that a local install won't have.

**Open-Meteo as the free, no-key weather/irradiance input.** Open-Meteo
provides free (non-commercial, up to 10,000 calls/day, no API key) hourly/
daily weather forecasts including global tilted irradiance for arbitrary
panel orientation and a dedicated Satellite Radiation API
([open-meteo.com](https://open-meteo.com/);
[open-meteo/open-meteo GitHub](https://github.com/open-meteo/open-meteo)).
This is the input EMHASS's PVLib fallback consumes and the backup Predbat
added in `v8.40.4` alongside Solcast/forecast.solar
(`docs/research/predbat.md` §1). **Recommended as the default free-tier
weather input** regardless of which downstream PV model is used — no
dependency risk, no credential to manage, already proven in two competing
projects.

**MOS-style learned bias correction on top of the physics baseline.** Rather
than training an ML model to forecast PV output from scratch, the mature
literature pattern is Model Output Statistics (MOS): keep the NWP-driven
physics forecast (pvlib/`ModelChain`) as the base signal and train a small,
cheap model (historically Kalman filters for pure bias/mean-error
correction, or a shallow regressor/ANN when correcting both bias and
random error) to correct its systematic error against a house's own PV
meter history
([Energies 2026, MOS+ML for PV generation](https://doi.org/10.3390/en19020486);
[Energies 2024, WRF-SOLAR bias correction](https://doi.org/10.3390/en17010088)).
One study reports MOS reducing solar-radiation RMSE by ~48.6% relative to
raw NWP output. This is the single most attractive PV technique for ha-spark
per the "boring, small, dependency-light" framing this survey was asked to
rank against: it needs only the physics forecast (already free) plus the
house's own generation meter history (already collected), a tiny regression
model (even a per-hour linear bias correction is a legitimate first cut),
and no additional weather-data vendor. **Data requirement:** weeks to a
season of paired (forecast, actual) generation to fit a stable seasonal bias
correction — much less than a from-scratch PV forecaster would need, because
the physics model already carries the bulk of the signal.
**Not yet shipped** by name in EMHASS or Predbat's public docs as reviewed
here (both delegate accuracy work to commercial APIs rather than local bias
correction) — a plausible ha-spark differentiator if pursued.

**Physical + ML hybrid models generally.** Broader literature confirms
physics+ML hybridization outperforms either alone for PV power forecasting
([ScienceDirect, "Benefits of physical and machine learning hybridization for
photovoltaic power forecasting"](https://www.sciencedirect.com/science/article/pii/S1364032122006566)) —
this is the academic generalization of the MOS-correction pattern above, and
supports treating "physics baseline + learned correction" as the
recommended architecture rather than either a pure-physics or pure-ML
PV forecaster.

## 3. Learned battery efficiency / degradation

**EMHASS's battery self-identification — the concrete HA-ecosystem
anchor.** EMHASS ships an opt-in, default-off feature
(`set_use_battery_identification`) that learns two constants the optimizer
otherwise takes on trust from user-entered nameplate values: usable capacity
(in the same reported-SoC units the optimizer already uses) and a *lumped*
round-trip efficiency, from a single AC-side power meter plus the reported
SoC time series
([EMHASS config docs](https://emhass.readthedocs.io/en/stable/config.html)).
It cannot split efficiency into separate charge/discharge components, so it
assigns both `sqrt(round_trip_efficiency)`. Critically: it is "data-hungry —
needs weeks of signed power and SoC with enough deep cycles, and if the data
is too shallow or the fit fails a sanity check it publishes nothing and keeps
your configured values" — i.e. it fails closed, never silently degrading
behind a bad fit. And in its first shipped version it is **advisory only**:
it publishes a suggested value as an HA sensor and never overwrites the
optimizer's live config — a human or a separate automation must act on it.
This is close to the exact shape ha-spark's own "ML learns inputs only"
constraint would want: a learned correction that a human/gate approves before
it changes what the planner trusts, not a silent overwrite.
**Recommended as the direct template** for any ha-spark equivalent —
same failure-closed data-hungriness check, same advisory-not-authoritative
posture.

**Underlying technique: coulomb counting as the ground truth signal, with
ML correcting its known failure mode.** Coulomb counting (integrating
current over time to estimate charge/discharge) is the traditional
method for SoC, but "error accumulation from current measurement and
capacity deviation degrades its fidelity over operating life" — this is
precisely the degradation signal an efficiency/capacity learner is trying
to correct for
([PMC, SOC estimation lithium-ion pouch cell ML comparison](https://pmc.ncbi.nlm.nih.gov/articles/PMC12103532/)).
Academic SoC-estimation comparisons (coulomb counting, extended Kalman
filter, and ML methods including random forest) generally find random
forest and hybrid coulomb-counting+ML methods (e.g. "movIRVM-Coulomb":
moving mean + incremental relevance vector machine + coulomb counting) more
robust than coulomb counting alone under real-world driving/usage load
profiles
([ScienceDirect, hybrid ML coulomb counting SOC](https://www.sciencedirect.com/science/article/abs/pii/S2352152X23004784)).
Almost all of this literature targets SoC (charge level) rather than
round-trip efficiency or capacity fade directly — the EMHASS approach above
is the more directly applicable prior art for what ha-spark actually needs.

**State-of-health (capacity fade / degradation) — heavier, EV-battery-scale
tooling, likely overkill for a single-household stationary battery.**
**BatteryML** (open-source ML-on-battery-degradation platform,
[arXiv:2310.14714](https://arxiv.org/pdf/2310.14714)), **BEEP** (Battery
Evaluation and Early Prediction, a Python framework for battery-cycling data
management and SoH modeling,
[ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2352711020300492)),
and **SOHbenchmark** (a 100-battery, 5-deep-learning-model benchmark repo,
[GitHub](https://github.com/wang-fujin/SOHbenchmark)) are all built around
lab-cycling datasets with dense per-cycle charge/discharge curves — the kind
of data a battery-cell manufacturer or research lab collects, not what a
home telemetry stream (coarse SoC + AC power samples every few minutes)
provides. Deep-learning SoH approaches in this space commonly use partial
constant-current charging-curve segments as input
([MDPI, deep learning SoH from partial CC curves](https://www.mdpi.com/2313-0105/10/6/206)) —
a signal a stationary home battery's inverter telemetry is unlikely to
expose at the needed resolution. **Not recommended** for ha-spark: the data
requirements and tooling weight (PyTorch-based deep learning, lab-grade
per-cycle datasets) are mismatched to what a home energy system can actually
observe; a simple periodic re-fit of capacity/efficiency in the EMHASS style
is the right altitude.

## 4. Household behaviour prediction (occupancy, EV plug-in, appliance use)

**Home Assistant's own Bayesian binary sensor — the boring baseline, already
in core.** HA ships a built-in `bayesian` integration: a virtual binary
sensor that combines other sensors' states via Bayes' rule (prior +
per-observation likelihoods → posterior probability, thresholded) to infer
higher-level states like occupancy, cooking, or showering
([home-assistant.io/integrations/bayesian](https://www.home-assistant.io/integrations/bayesian/)).
Zero new dependencies (it's core HA), zero training step in the ML sense —
the "model" is a handful of user-specified conditional probabilities. This
is the cheapest possible occupancy signal and should be the first thing
checked before building anything more elaborate, since it's already running
in most non-trivial HA installs.

**Area Occupancy Detection (community integration) — a step up, still
Bayesian, but multi-sensor and self-tuning.** A third-party HA custom
component that fuses motion/occupancy sensors, media-player state,
door/window sensors, appliance power draw, and environmental sensors
(temperature, humidity, CO2, illuminance, sound, air quality) via Bayesian
probability, and — notably — "automatically learns how sensor states relate
to actual occupancy using motion sensors as ground truth"
([Hankanman/Area-Occupancy-Detection](https://github.com/Hankanman/Area-Occupancy-Detection);
[project docs](https://hankanman.github.io/Area-Occupancy-Detection/)).
This is the closest existing HA-ecosystem prior art for "learned household
behaviour signal feeding a planner as an input" outside of EMHASS/Predbat
themselves. **Compute/deps:** runs inside HA's own Python process, no heavy
ML dependency implied by its Bayesian-fusion design.

**EV plug-in / charging-session prediction — survival analysis and logistic
regression on time-of-week features, per the academic literature.** Two
directly relevant threads: (1) logistic regression predicting EV
charging-station occupancy from neighbor-station occupancy and time features
achieved 88.4% average / 92.2% max accuracy across 57 stations
([arXiv:2204.13702](https://arxiv.org/pdf/2204.13702)); (2) survival-analysis
framings — Weibull-baseline-hazard time-to-event models — used to estimate
plug-in/departure time distributions, with fitted hazard intensities serving
as plug-in probability estimates
(EV-charging-occupancy multistep forecasting literature,
[arXiv:2106.04986](https://arxiv.org/pdf/2106.04986)). Both are
classical-statistics techniques (logistic regression, Weibull survival
models) — cheap to fit, interpretable, and a natural match for "time-of-week
+ recent-history" feature sets a home telemetry stream can actually produce
(plug-in timestamp history, day-of-week, recent-days'-pattern), as opposed to
public charging-network-scale data. A newer strand
([arXiv:2512.07723](https://arxiv.org/pdf/2512.07723)) uses a Transformer to
model real-time-to-departure for delayed-full-charging strategies — heavier
machinery, relevant only if ha-spark needed sub-hour departure-time
precision that a simpler survival model can't give; not justified as a
first step.

**Markov models for sequential/appliance-usage prediction.** Markov-chain and
"Markov for Discrmination" approaches appear in the sequential-appliance/
acquisition literature
([ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0167923607000334))
as a lightweight way to model state-transition patterns (e.g. appliance
on/off sequences) without a full ML training pipeline — a first-order Markov
chain over time-of-day-binned states is essentially a lookup table with
smoothing, cheaper than any of the above and a reasonable first cut for
"what's this house's routine on a given day-of-week/time-of-day."

## 5. Decision-side techniques — deliberately not adopted, documented for why

ha-spark's `energy/planner.py` is a pure function (inputs + config → plan);
the standing project decision (CLAUDE.md, ROADMAP.md ADR-0002 per
`docs/research/predbat.md` §5) is that this stays the sole decider. The
following are real, active research/engineering directions that other
projects and academic work pursue for the *decision* itself, surveyed here
only to document why ha-spark doesn't:

- **Reinforcement learning for battery/HEMS scheduling.** An active
  research area — deep RL, safe-RL under Constrained MDPs, and fuzzy
  Q-learning have all been applied to residential battery/storage scheduling
  ([MDPI, RL for HEMS+V2H](https://www.mdpi.com/2076-3417/13/9/5539);
  [ScienceDirect, RL battery storage optimization](https://www.sciencedirect.com/science/article/abs/pii/S0957582024012643);
  [ScienceDirect, safe RL multi-energy management](https://www.sciencedirect.com/science/article/abs/pii/S0378779622003443)).
  The literature itself flags the problem ha-spark is avoiding: "deploying
  RL in real-world energy and home automation systems presents challenges
  including computational complexity, model interpretability, real-time
  decision constraints, and safety considerations" — and the field's own
  response is bolting *explainability* back on after the fact (differentiable
  decision trees as RL policy networks, hybrid RL+rule-based systems) rather
  than getting it for free the way a hand-written deterministic function
  does
  ([arXiv:2403.11947, Explainable RL via differentiable decision
  trees](https://arxiv.org/html/2403.11947)). A training loop also implies a
  simulator/environment model of the house that must itself be trusted, a
  wholly different validation burden than unit-testing a pure function.
- **End-to-end learned optimizers / policy networks generally.** Same
  argument as above in the general case: any model that maps
  (state → action) directly is a black box relative to a human auditing
  "why did it charge at 2pm" — precisely the property ha-spark's copilot
  (`router.py`/`copilot.py`) is built to provide by grounding explanations
  in an actual computed plan, not a policy's internal activations.
- **Why this matters operationally, not just philosophically:** a
  mis-specified reward function or an under-covered training distribution
  in an RL policy fails silently and unpredictably (wrong action, no
  visible reason) — the same failure mode this survey's PV/load ML
  recommendations (§1–§3) are careful to fail *closed* on (EMHASS's
  battery-ID sanity check refusing to publish on shallow data is the
  concrete counter-example of the right posture). A deterministic planner
  fed by ML-improved inputs preserves that "wrong input just makes the plan
  worse in an inspectable way" property; a learned policy does not.

Net: RL/learned-policy techniques are real and worth tracking, but they
target the part of the pipeline (`compute_plan`'s decision) that ha-spark
has explicitly and correctly fenced off from ML, per project standing
decisions — nothing here should be read as a gap.

## 6. Ranked by value-for-effort on local (CPU-only, glibc python:slim) hardware

| Rank | Technique | Category | Effort | Local-CPU cost | Already in HA ecosystem? |
|---|---|---|---|---|---|
| 1 | Physics baseline first: pvlib `ModelChain` + Open-Meteo (no API key) | PV forecast | Low — mature libs, no training | Negligible | Yes (EMHASS) |
| 2 | Persistence/historical-average baseline (day-of-week grouping) | Load forecast | Trivial — no ML dependency at all | Negligible | Yes (EMHASS "typical"/"naive", Predbat `days_previous`) |
| 3 | MOS-style learned bias correction on physics PV forecast | PV forecast | Low-medium — small regressor on (forecast, actual) pairs | Trivial to fit | Not seen shipped in HA ecosystem — plausible differentiator |
| 4 | skforecast + LightGBM/XGBoost recursive load forecaster | Load forecast | Medium — feature engineering + lag matrix, but library does the work | Seconds to fit, sub-second inference | Yes (EMHASS MLForecaster) |
| 5 | Quantile-objective GBM for P10/P50/P90 load forecast | Load forecast (probabilistic) | Low incremental — same pipeline as #4, different objective | Same as #4 | Not explicitly named in EMHASS/Predbat docs reviewed — cheap upgrade |
| 6 | EMHASS-style battery capacity/round-trip-efficiency identification (advisory, fails closed on shallow data) | Battery learned efficiency | Medium — needs weeks of signed power+SoC and a sanity-check gate | Trivial (simple regression/curve fit) | Yes (EMHASS, advisory-only) |
| 7 | HA core Bayesian sensor / Area Occupancy Detection community integration | Occupancy | Low — mostly config, some already-learned fusion | Negligible | Yes (HA core + community) |
| 8 | Logistic regression / Weibull survival analysis on time-of-week features for EV plug-in prediction | Behaviour prediction | Medium — needs plug-in history, classical stats fit | Trivial | Not seen shipped for home EV charging specifically — plausible build |
| 9 | Markov chain over time-of-day-binned appliance/occupancy states | Behaviour prediction | Low — closer to a smoothed lookup table than an ML model | Negligible | Not seen shipped in HA ecosystem — cheap first cut |
| 10 | Predbat-style pure-NumPy small MLP for load forecasting | Load forecast | High — reimplements training/optimizer plumbing from scratch | Low but real (minutes per fine-tune cycle) | Yes (Predbat LoadML) |
| 11 | NeuralProphet | Load/PV forecast | Medium-high — PyTorch dependency for marginal interpretability gain | Moderate (PyTorch on CPU) | Not seen shipped in HA ecosystem |
| 12 | Lab-grade SoH/degradation ML (BatteryML, BEEP, SOHbenchmark, deep learning on charge curves) | Battery degradation | High — deep learning stack, lab-cycling-scale data ha-spark can't collect | High, and largely moot without the data | No — mismatched to home telemetry |
| 13 | Reinforcement learning / learned policy for scheduling decisions | Decision-side (out of scope) | Very high — training loop + simulator + safety wrapper research is itself active | High | No — and deliberately not pursued (§5) |

**Top-line recommendation for ha-spark specifically:** start at rows 1–2
(already-necessary baselines, zero new risk), add row 6 (EMHASS's own
battery-identification pattern is a template ha-spark can copy almost
directly, including its fail-closed/advisory posture) and row 4 (skforecast +
LightGBM is the highest-leverage genuinely-"learned" addition — mature
library, tiny compute footprint, proven in EMHASS for years) before touching
anything heavier. Row 3 (MOS-style PV bias correction) is the most
interesting *novel* opportunity found in this survey — cheap, physics-first,
and not yet shipped by either reference competitor.

## Sources

- [skforecast docs](https://skforecast.org/0.14.0/introduction-forecasting/introduction-forecasting.html), [skforecast GitHub](https://github.com/skforecast/skforecast)
- [EMHASS config docs](https://emhass.readthedocs.io/en/stable/config.html), `docs/research/emhass.md` (this repo)
- [International Journal of Ambient Energy 2025 — LightGBM household load forecasting](https://www.tandfonline.com/doi/full/10.1080/01430750.2025.2577864)
- [ScienceDirect — CatBoost/XGBoost hybrid short-term load forecasting](https://www.sciencedirect.com/science/article/abs/pii/S0957417423031883)
- [arXiv:2512.00856 — SARIMA to Transformers on REFIT single-household dataset](https://arxiv.org/pdf/2512.00856)
- [scikit-learn — quantile gradient boosting regression example](https://scikit-learn.org/stable/auto_examples/ensemble/plot_gradient_boosting_quantile.html)
- [NeuralProphet](https://neuralprophet.com/), [arXiv:2111.15397](https://arxiv.org/pdf/2111.15397)
- [springfall2008/batpred docs/load-ml.md](https://github.com/springfall2008/batpred/blob/main/docs/load-ml.md), `docs/research/predbat.md` (this repo)
- [pvlib-python ModelChain docs](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.modelchain.ModelChain.html), [pvlib python paper (OSTI)](https://www.osti.gov/pages/servlets/purl/1993714)
- [Open-Meteo](https://open-meteo.com/), [open-meteo/open-meteo GitHub](https://github.com/open-meteo/open-meteo)
- [Energies 2026 — MOS + ML for PV generation forecasting](https://doi.org/10.3390/en19020486)
- [Energies 2024 — WRF-SOLAR bias correction](https://doi.org/10.3390/en17010088)
- [ScienceDirect — physical + ML hybridization for PV forecasting](https://www.sciencedirect.com/science/article/pii/S1364032122006566)
- [PMC — SOC estimation lithium-ion pouch cell, ML vs coulomb counting/EKF](https://pmc.ncbi.nlm.nih.gov/articles/PMC12103532/)
- [ScienceDirect — hybrid ML coulomb-counting SOC estimation](https://www.sciencedirect.com/science/article/abs/pii/S2352152X23004784)
- [arXiv:2310.14714 — BatteryML](https://arxiv.org/pdf/2310.14714), [BEEP (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2352711020300492), [SOHbenchmark GitHub](https://github.com/wang-fujin/SOHbenchmark)
- [MDPI — deep learning SoH from partial constant-current charging curves](https://www.mdpi.com/2313-0105/10/6/206)
- [Home Assistant — Bayesian integration](https://www.home-assistant.io/integrations/bayesian/)
- [Hankanman/Area-Occupancy-Detection](https://github.com/Hankanman/Area-Occupancy-Detection), [project docs](https://hankanman.github.io/Area-Occupancy-Detection/)
- [arXiv:2204.13702 — logistic regression EV charging-station occupancy](https://arxiv.org/pdf/2204.13702)
- [arXiv:2106.04986 — multistep EV charging station occupancy forecasting](https://arxiv.org/pdf/2106.04986)
- [arXiv:2512.07723 — Transformer real-time-to-departure for delayed-full-charging](https://arxiv.org/pdf/2512.07723)
- [ScienceDirect — Markov/survival analysis for sequential appliance-acquisition prediction](https://www.sciencedirect.com/science/article/abs/pii/S0167923607000334)
- [MDPI — RL for HEMS + V2H integration](https://www.mdpi.com/2076-3417/13/9/5539)
- [ScienceDirect — RL algorithms for residential battery storage optimization](https://www.sciencedirect.com/science/article/abs/pii/S0957582024012643)
- [ScienceDirect — safe RL, CMDP, multi-energy management for smart home](https://www.sciencedirect.com/science/article/abs/pii/S0378779622003443)
- [arXiv:2403.11947 — explainable RL via differentiable decision trees for HEMS](https://arxiv.org/html/2403.11947)
