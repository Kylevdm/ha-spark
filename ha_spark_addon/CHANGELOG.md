# Changelog

## 1.0.0-rc2

- Multi-supplier tariffs: the planner and `ha-spark backtest` now cost every
  plan against a normalised per-slot price schedule instead of a fixed
  two-rate window. Three providers, selected with `tariff_provider`: `fixed`
  (default, reproduces the previous behaviour exactly — existing configs need
  no changes), `dynamic` (half-hourly prices from an HA price sensor via
  `dynamic_rates_entity`), and `octopus_intelligent` (Octopus API prices +
  planned dispatch windows via `octopus_api_key`/`octopus_account_number`/
  `octopus_product_code`/`octopus_tariff_code`). A misconfigured provider is
  rejected at startup with a message naming the bad field; a live price
  read that fails at runtime falls back to `fixed` rather than blocking a
  plan. See DOCS.md "Tariff" for the full option reference.
## 1.0.0-rc1

Release candidate for 1.0.0 — pre-release for testing before promotion.

- Device-driver core (Phase 7): inverter adapters now live in a `devices/`
  driver package behind a registry, each advertising a `Capability` set, and
  every controllable device carries a `control` authority
  (`observe | ha_spark | supplier`). A real write now requires BOTH
  `control: ha_spark` and `proactive_mode: on` — `observe`/`supplier` compute
  and log (`[OBSERVE]`) but never actuate. Config gains a structured `devices:`
  list; existing flat setups are migrated in memory automatically (no config
  change needed, options.json is never rewritten). The agent surface's
  `get_state` now reports each device's control authority. No behaviour change
  for the existing Solis install.
- (Back-note) The multi-inverter charge contract (`ChargeIntent`, the
  inverter selector, the AlphaESS adapter) landed in an earlier build without a
  changelog entry; it is the foundation this phase formalises.

## 0.13.0

- Agent surface: ha-spark now exposes its data and a few gated actions to an
  external model. The add-on HTTP API moved to FastAPI; in addition to the
  existing ingress API it serves OpenAPI tool routes under `/agent/*` (for
  open-webui / curl) and an MCP server at `/mcp` (for Claude). A new
  `agent_exposure` option (`read` | `read_act` | `read_write`, default
  `read_act`) controls how much is exposed; act/write still pass the existing
  PROACTIVE_MODE gate, and the LLM never reaches `call_service`. Enable with
  `agent_surface: on`; for external (non-ingress) clients set
  `agent_expose_port: true` and map port 8098 — requests then require the
  bearer token (`agent_api_token`, auto-generated and logged once if blank).

## 0.12.0

- Daemon HTTP API serving: the daemon now exposes a read-only REST API through
  add-on ingress (no open port, authenticated via HA's ingress proxy) for the
  future companion integration. Hot config reload when `/data/options.json`
  changes — the daemon detects the change and reloads without restart. API
  endpoint: `GET /api/plan` (returns the current computed plan).

## 0.11.0

- Add the proactive orchestrator: a single decision/audit path that, after each
  daily run, computes the habit API's predictions for tomorrow, honors the
  `proactive_mode` flag (`off`/`simulate`/`on`), logs each as a `Proactive
  decision`, and publishes them to Home Assistant as a new
  `sensor.ha_spark_predictions` (count + per-prediction action/confidence/
  reason/outcome). Predictions stay advisory for now — nothing is actuated yet;
  this builds the seam future proactivity hangs off. The away-period prediction
  now uses the learned away load factor when enough history exists (best-effort,
  so a history-fetch hiccup never suppresses predictions).

## 0.10.0

- ha-spark now publishes its computed charge plan back to Home Assistant as
  `sensor.ha_spark_*` entities (charge needed, target/current SoC, overnight
  current, forecast load/solar, deficit, planned/baseline cost, plan status,
  last run) after every run, so the numbers are visible in HA instead of only
  in logs/SQLite. The last-published values are cached to `/data` and
  re-pushed on daemon startup so a restarted add-on doesn't show `unknown`
  until the next scheduled run.
- The add-on's entity config options (`soc_entity`, `charge_current_entity`,
  etc.) now ship blank instead of defaulting to the original author's Solis
  setup; use `ha-spark onboard` (entity auto-discovery) or
  `ha-spark onboard --preset solis` to fill them in. `ha-spark health` now
  flags any required entity left unset.

## 0.9.1

- Fix add-on build failure on the Python 3.13 / Alpine (musllinux) base image:
  drop the explicit `scikit-learn`/`numpy` install. scikit-learn ships no
  musllinux wheel, so pip was source-compiling it and failing for lack of a C
  compiler. The optional ML load model (`load_model: ml|auto`) is import-guarded;
  the forecast degrades to the slot-profile median. No CLI/option changes.

## 0.9.0

- Onboarding wizard (Phase 4): `ha-spark onboard` now scans Home Assistant's
  entities and proposes which one maps to each ha-spark config field (battery
  SoC, battery voltage, Solcast forecast, Octopus rate/dispatch, EV sensors,
  household consumption, grid power, charge-current control, inverter switch,
  heat-pump energy, weather), ranked by device class, unit, attributes, and
  name. Each proposal shows the configured value, the best match with why it
  matched, and whether they agree.
- `--json` emits the proposals for tooling; `--write` prints a ready-to-paste
  options fragment; `--preset solis` fills fields discovery can't match from
  the reference Solis/Solcast/Octopus/zappi setup. Proposals are advisory —
  you review and set the options yourself; the wizard never rewrites config.
- `onboard` still reports load-history readiness and keeps its exit code.

## 0.8.0

- NL copilot (Phase 5): `ha-spark ask` now grounds the Ollama tier in the live
  computed plan. Before answering, it feeds the model the same plan the `plan`
  command prints (SoC, solar, load forecast + source, deficit, charge current,
  projected cost/saving, active context), so chat explains the actual decision
  — "why 42 A", "what does tonight cost" — instead of guessing. Scoped to the
  home-energy domain; the model explains and reports only, never claims to have
  changed a setting (the deterministic planner still decides and acts).
- Grounding is best-effort and the probe runs first: if Ollama is down the
  offline parser answers as before, and if the plan can't be computed the model
  is told so rather than inventing figures.

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
