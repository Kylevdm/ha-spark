# Changelog

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
