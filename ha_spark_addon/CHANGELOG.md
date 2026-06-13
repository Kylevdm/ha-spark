# Changelog

## 0.7.0

- Occupancy habits + learned factors (Phase 6E):
  - Tomorrow's occupancy is predicted from the day-type pattern of recorded
    `occupancy_home_frac` samples and fed to the ML model as a real feature
    for the target day (previously the history mean).
  - The `away` load factor is learned from past away periods (actual load on
    those days vs same-day-type normal days) and applied automatically once
    enough away history exists, falling back to `away_load_factor` otherwise.
    The plan report marks a learned factor with `(learned)`.
  - `ha-spark learn-factors` reports the learned away factor, tomorrow's
    predicted occupancy, and the advisory habit predictions.
  - The daemon logs `predict_actions` advisories each run (gated by, and
    labelled with, `PROACTIVE_MODE`) — the seam later proactivity builds on.
    Nothing is executed yet; the model still only ever records reviewable
    facts and the deterministic planner still decides.

## 0.6.0

- Natural-language context (Phase 6D): `ha-spark ask "I'm on holiday for the
  next two weeks"` now records a context fact and replies with its planner
  effect and an undo command. When the Ollama tier is reachable it extracts
  the dates as strict JSON (validated before anything is stored); offline, a
  deterministic parser handles ISO dates and phrases like "next week", "this
  weekend", and "for a fortnight".
- `ha-spark ask "what do you know about my holidays?"` lists stored facts,
  answered directly from the context store.
- The router runs this extraction/query pass before plain chat. The language
  model only ever records reviewable facts — it never actuates hardware, and
  every recorded fact is echoed back and removable via `ha-spark context`.

## 0.5.0

- Context store (Phase 6C): record date-ranged household facts the planner
  consumes as a deterministic load multiplier. `ha-spark context add away
  --from 2026-07-01 --to 2026-07-14` lightens the overnight charge for a
  holiday; `guests` heightens it; `high_usage`/`low_usage --factor X` apply a
  custom multiplier. `context list` / `context remove <id>` round-trip.
- Active facts scale tomorrow's load forecast (both the median and ML
  candidates by the same factor, so accuracy scoring and the quantile buffer
  are unaffected) and are named in the plan report's forecast line.
- `away_load_factor` (default 0.4) and `guests_load_factor` (default 1.3)
  options set the multipliers; Phase 6E will learn them from history.

## 0.4.0

- Weather-aware ML load model (Phase 6B, optional): gradient-boosted quantile
  regression (P50/P90) over the hourly load history plus Open-Meteo
  temperatures, occupancy signals, and UK bank holidays. Needs the `[habits]`
  Python extra; without it everything falls back to the median profile.
- `load_model` option: `median` (previous behaviour), `ml` (always prefer the
  model), or `auto` (default — use ML only once `forecast-eval` shows it
  beating the median over the trailing 14 days; both forecasts are
  shadow-recorded nightly so the comparison accumulates automatically).
- `buffer_mode: quantile` replaces the fixed `charge_buffer_pct` with the
  model's own uncertainty, (P90 − P50)/P50, whenever the ML forecast drives
  the plan.
- `latitude`/`longitude` options for Open-Meteo (default: read from HA's own
  configured location). Fetched past temperatures are cached into the signal
  ledger so the model still runs from recorded data when Open-Meteo is down.

## 0.3.0

- Forecast ledger (Phase 6A): the daemon now records each night's load
  forecast (model, total kWh, per-slot breakdown) alongside the date it
  predicted, so accuracy can be scored later.
- `ha-spark forecast-eval [--days N]` joins recorded forecasts against actual
  consumption and reports MAE/MAPE per model — the baseline a future ML model
  (Phase 6B) must beat before it can drive plans.
- New signal sampler records `occupancy_home_frac` (from `person_entities`),
  `heatpump_kwh` (from `heatpump_energy_entity`), and `temp_out_c` (from
  `outdoor_weather_entity`) every 30 minutes, building training data for later
  phases. All three are optional and degrade silently if unconfigured or
  unreadable.

## 0.2.0

- Live supply guard (Phase 3, EV-aware): when `grid_power_entity` is set, the
  daemon throttles the battery's timed-charge current whenever whole-house AC
  draw exceeds `supply_max_current_a` (default 75 A) — e.g. an EV dispatch
  landing mid-window — and restores it as headroom returns. Gated by
  `proactive_mode` like all writes; disabled until the sensor is configured.
- Plan report now shows the EV energy Octopus plans to deliver across the
  upcoming dispatches.
- `ha-spark health` gains a supply-guard sensor check.

## 0.1.0

- Initial add-on release: daily charge-plan daemon (`ha-spark run`) with
  startup health report, full options schema (planner knobs, tariff, entity
  IDs, Octopus API, Ollama), persistent SQLite store under `/data`.
