# ha-spark

Local-first battery charge planner for Home Assistant. Once a day (at
`plan_run_time`, default 22:00 local) it forecasts tomorrow's household load
and solar yield, sizes the overnight cheap-rate charge, and — when
`proactive_mode` is `on` — sets the inverter's timed charge current.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add `https://github.com/Kylevdm/ha-spark`.
2. Install the **ha-spark** add-on. The image is built locally on your
   machine; the first install takes a few minutes.
3. Open the **Configuration** tab and set your options (see below), then
   start the add-on.

## Configuration

### Entity IDs (required for your installation)

The defaults match the author's hardware (Solis inverter, Solcast, Octopus
Intelligent, myenergi zappi). Point these at your own entities:

| Option | What it must be |
|---|---|
| `soc_entity` | Battery state of charge (%) |
| `battery_voltage_entity` | Battery voltage (V) |
| `solar_tomorrow_entity` | Solcast "forecast tomorrow" sensor (with `detailedForecast` attribute) |
| `octopus_rate_entity` | Octopus current electricity rate sensor |
| `dispatch_entity` | Octopus Intelligent dispatching binary sensor |
| `ev_plug_entity` / `ev_status_entity` | EV charger plug/status sensors |
| `consumption_energy_entity` | True household load energy statistic (excluding battery/EV charging) |
| `grid_power_entity` | Optional whole-house supply power sensor (W); enables the supply guard |
| `charge_current_entity` | Inverter timed-charge current `number` entity (the only control written) |
| `inverter_power_switch_entity` | Inverter power switch `select` entity |
| `ha_template_charge_needed_entity` | Optional HA template sensor for comparison logging |
| `inverter` | Which inverter ha-spark controls: `solis` (default) or `alphaess` |
| `charge_window_start_entity` / `charge_window_end_entity` | Optional HA entities ha-spark writes the timed-charge window to (blank = leave the inverter's window as-is) |
| `alphaess_serial` | AlphaESS system serial (only needed when `inverter: alphaess`) |
| `person_entities` | Optional comma-separated `person`/`device_tracker` entity ids for occupancy signal recording |
| `heatpump_energy_entity` | Optional dedicated heat-pump energy sensor (kWh) for signal recording |
| `outdoor_weather_entity` | Weather entity with a `temperature` attribute (default `weather.home`) for signal recording |

### Multiple inverters / device control (optional)

Single-inverter installs need no change here — the flat `inverter` +
entity-ID options above are still read directly (in memory, `options.json` is
never rewritten). `devices` is the structured alternative for installs that
want explicit per-device authority:

```yaml
devices:
  - id: main_inverter
    type: inverter
    driver: solis          # solis | alphaess
    control: ha_spark       # observe | ha_spark | supplier
    entities:
      charge_current: number.solisac_timed_charge_current
      window_start: time.solisac_charge_start
      window_end: time.solisac_charge_end
      power_switch: select.solisac_power_switch
```

`control` is the authority gate: a real write requires **both**
`control: ha_spark` **and** `proactive_mode: on`. `observe` (ha-spark reads and
plans around the device but never writes it) and `supplier` (reserved — a
third party is expected to control it) both compute and log a `[OBSERVE]`
action line instead of writing, regardless of `proactive_mode`. Leave
`control` unset for `ha_spark` (the default, and what the flat-key migration
always produces).

### Planner

- `proactive_mode` — `off` (compute only), `simulate` (log the writes it
  *would* make; default), `on` (really set the charge current). Run in
  `simulate` for a few nights and check the log before switching to `on`.
- `battery_capacity_kwh`, `battery_voltage_v`, `min_soc`, `target_soc_cap`,
  `max_charge_current_a` — battery/inverter model.
- `charge_strategy` — `deficit` buys only the forecast shortfall; `fill`
  charges to `target_soc_cap` every night (wins once export rate exceeds the
  off-peak rate).
- `charge_buffer_pct`, `charge_efficiency`, `solar_haircut_k`,
  `solar_percentile`, `expected_load_kwh` — forecast/sizing knobs; the
  defaults are sensible.
- `charge_window_start` / `charge_window_end` — your cheap-rate window.
- `plan_run_time` — local HH:MM at which the daily plan runs.

### Supply guard (optional)

When the battery is timed-charging and an EV dispatch lands in the same
window, total supply draw can climb past what the main fuse should carry. Set
`grid_power_entity` to a whole-house grid/supply power sensor (W) and the
daemon will, on every tick inside the charge window, throttle the
timed-charge current so total draw stays under `supply_max_current_a`
(default 75 A), restoring it toward the planned current as headroom returns.
`supply_voltage_v` (default 240) converts the sensor's watts to amps. Writes
respect `proactive_mode` exactly like the nightly plan. Leave
`grid_power_entity` empty to disable the guard entirely.

### ML load model (optional)

> Not bundled in the add-on image: scikit-learn has no musllinux wheel, so it is
> left out to keep the build compiler-free. `load_model: ml|auto` therefore falls
> back to the slot-profile median until scikit-learn is installed in the
> environment (e.g. standalone with the `[habits]` extra).

When scikit-learn is available, a weather-aware gradient-boosted quantile model
can forecast tomorrow's load instead of the slot-profile median, using
Open-Meteo temperatures (heating degree hours →
heat-pump demand), day-of-week/season, recent-load lags, recorded occupancy,
and UK bank holidays.

- `load_model` — `median` (profile only), `ml` (always prefer the model when
  it can run), or `auto` (default): use ML only once `ha-spark forecast-eval`
  shows it beating the median over the trailing 14 days. Both forecasts are
  shadow-recorded nightly, so `auto` switches by itself once the model earns
  it — and switches back if it stops winning.
- `buffer_mode` — `fixed` keeps `charge_buffer_pct`; `quantile` replaces it
  with the model's own uncertainty, (P90 − P50)/P50, whenever the ML forecast
  drives the plan (confident days buy less margin).
- `latitude` / `longitude` — site coordinates for Open-Meteo; leave unset to
  use HA's own configured location. Fetched past temperatures are cached into
  the signal ledger, so the model still runs from recorded data when
  Open-Meteo is unreachable.

The deterministic planner is unchanged — the model only supplies the load
numbers fed into it, and falls back to the median chain on any failure.

### Context facts (away / guests)

Tell the planner about days that won't look like a normal week, and it scales
the load forecast accordingly:

```
ha-spark context add away   --from 2026-07-01 --to 2026-07-14 --note Italy
ha-spark context add guests --from 2026-12-24 --to 2026-12-27
ha-spark context add high_usage --from 2026-08-10 --to 2026-08-10 --factor 1.5
ha-spark context list
ha-spark context remove 3
```

`away` multiplies the forecast by `away_load_factor` (default 0.4), `guests`
by `guests_load_factor` (default 1.3), and `high_usage`/`low_usage` by the
`--factor` you give. Overlapping facts multiply. Every active fact is printed
in the plan report's forecast line, so each adjustment is visible and can be
removed by id. Facts are data only — they never actuate hardware.

You can also set them in plain language through `ha-spark ask` (and so any
chat surface wired to it):

```
ha-spark ask "I'm on holiday for the next two weeks"
  -> Noted — away Sat 13 Jun – Fri 26 Jun. The planner will assume ~40% of
     normal load on those days. Undo with `ha-spark context remove 4`.
ha-spark ask "what do you know about my holidays?"   # lists stored facts
```

When the Ollama tier is reachable it extracts the dates (returning strict
JSON, validated before anything is stored); offline, a deterministic parser
handles ISO dates and phrases like "next week", "this weekend", and "for a
fortnight". Either way the fact is echoed back with an undo command, and the
language model never controls hardware — it only records reviewable facts.

### Learned habits

As occupancy and away history accumulate, ha-spark learns from it:

- Tomorrow's **occupancy** is predicted from the weekday/weekend pattern of
  recorded `occupancy_home_frac` and fed to the ML model.
- The **away load factor** is learned from how much less you actually used on
  past `away` days versus normal days of the same type, and applied
  automatically once there's enough history (the plan report marks it
  `(learned)`); until then the configured `away_load_factor` is used.

`ha-spark learn-factors` shows the current learned away factor, tomorrow's
predicted occupancy, and any advisory habit predictions. The daemon logs those
predictions each run, labelled with `proactive_mode` — they are advisory only
and never actuate hardware.

### Forecast ledger

Every nightly run records the forecast it used (model, total kWh, per-slot
breakdown) for the date it predicted. `ha-spark forecast-eval [--days N]`
joins those recorded forecasts against actual consumption and reports
MAE/MAPE per model — the baseline a future ML model must beat before it can
drive plans (`load_model: auto`).

A signal sampler also runs every 30 minutes, recording household signals used
by later phases: `occupancy_home_frac` (from `person_entities`),
`heatpump_kwh` (from `heatpump_energy_entity`), and `temp_out_c` (from
`outdoor_weather_entity`). All three are optional — leave them unset to skip
that signal; an unreadable entity logs a warning and is skipped without
affecting the others.

### Tariff

`rate_offpeak_gbp_kwh`, `rate_peak_gbp_kwh`, `rate_export_gbp_kwh` — used for
the cost projection printed with each plan and by `ha-spark backtest`.

`tariff_provider` selects how plans are costed: `fixed` (default) uses the
rates above plus the charge window — this is the provider every existing
install is already on, so upgrading needs no config changes; `dynamic` costs each half-hour slot at its
live price from an HA price sensor, choosing the cheapest slots as "cheap" for
costing (the charge window itself is unchanged). Set `dynamic_rates_entity` to
an entity whose `rates` attribute is a list of `{start, end, value_inc_vat}`
(e.g. the BottlecapDave Octopus Energy integration's
`event....current_day_rates`); `dynamic_rates_entity_tomorrow` is optional and
covers tomorrow's slots the same way. A missing/bad read falls back to the
fixed rates — `ha-spark health` reports the live provider status.

`octopus_intelligent` is a first-class Octopus Intelligent tariff: prices come
from the Octopus standard-unit-rates REST API and planned dispatch windows
come straight from the Octopus API (Kraken GraphQL) instead of an HA sensor —
dispatch/cheap-window handling is otherwise identical to `fixed`. Requires
`octopus_api_key`, `octopus_account_number` (for the dispatches query), and
`octopus_product_code`/`octopus_tariff_code` (for the rates endpoint, e.g.
`INTELLI-VAR-22-10-14` / `E-1R-INTELLI-VAR-22-10-14-A`). An auth or API
failure falls back to the fixed rates/dispatches — `ha-spark health` reports
the live provider status; the API key is never logged or echoed.

### Octopus API (optional)

`octopus_api_key`, `octopus_mpan`, `octopus_meter_serial` enable
`ha-spark pull-consumption` (grid-import history for cost backtesting only —
it is **not** used as the load forecast). The same `octopus_api_key` also
drives the `octopus_intelligent` tariff provider above.

### Ollama (optional)

`ollama_url` / `ollama_model` point at a remote Ollama instance (e.g. over
Tailscale) for the natural-language agent features. The planner runs fine
without it; health reports it as a warning.

When Ollama is reachable, `ha-spark ask` grounds it in the live computed plan,
so chat explains the actual decision rather than guessing:

```
ha-spark ask "why is it charging at 42 A tonight?"
ha-spark ask "what does tonight cost vs no battery?"
```

The model is given the same plan the `plan` command prints and is scoped to
home energy — it explains and reports only, and never controls hardware. If
Ollama is down, the deterministic offline parser answers the energy queries it
recognises instead.

### HTTP API (for companion integrations)

The daemon serves a read-only REST API through add-on ingress (authenticated
via Home Assistant, not exposed to the host network). Reach it at
`http://localhost:8099` from within the add-on's network, or through the
companion integration proxy once wired up. Endpoints:

- `GET /api/plan` — current computed charge plan (same data as `ha-spark plan`)
- Config hot-reload: edit `/data/options.json` and the daemon detects the change
  and reloads within seconds — no restart needed.

### Agent surface

Off by default. Set `agent_surface: on` to let an external model (e.g. Claude,
or any OpenAPI-compatible tool client) read ha-spark's data and, optionally,
trigger a few gated actions.

- `agent_surface` (`off` | `on`) — master switch, off by default.
- `agent_exposure` (`read` | `read_act` | `read_write`, default `read_act`) —
  how much is exposed. `read` is data-only (states, plan, forecast,
  predictions, health). `read_act` additionally exposes `add_context` and
  `run_plan`. `read_write` additionally exposes `set_config`.
- `agent_api_token` — bearer token for the published port. Leave blank and the
  add-on generates one on first start and prints it **once** to the add-on
  log; it's a `password` field, so it's never shown back in the UI.
- `agent_expose_port` (`bool`, default `false`) — publish the agent surface on
  the host network for clients that can't reach add-on ingress.

The agent surface is always served over HA's authenticated ingress proxy —
no token needed there, since ingress already authenticates the user. For
external clients (Claude Desktop, open-webui on your LAN/Tailnet) that can't
go through ingress, set `agent_expose_port: true` and map host port **8098**
(the `ports:` entry in this add-on's configuration). Requests on that
published port require the bearer token.

- **open-webui**: add a new tool server pointing at
  `http://<host>:8098/openapi.json`, with header `Authorization: Bearer
  <token>`.
- **Claude (Desktop, or via your own reverse proxy)**: point an MCP
  (Streamable HTTP) connector at `http://<host>:8098/mcp`, with the same
  bearer token. The server 307-redirects `/mcp` → `/mcp/`, which MCP clients
  follow automatically.
- **claude.ai (web)** additionally needs a public HTTPS endpoint in front of
  the published port — a reverse proxy or Nabu Casa — since claude.ai cannot
  reach a bare LAN/Tailnet address. That's a deployment step you manage
  yourself, not something the add-on sets up.

Act and write tools still pass the existing `proactive_mode` gate, and the
model never reaches `call_service` directly — the deterministic planner
remains the sole decider. Nothing here changes that.

## Onboarding

1. **Check the Log tab** after the first start: the add-on runs
   `ha-spark health` and prints a line per dependency (HA REST, HA WebSocket,
   Ollama, SQLite, load history).
2. **Map your entities.** From a shell in the add-on container (e.g. the SSH
   add-on with `docker exec -it addon_<slug> sh`, or the add-on's own
   terminal), run `ha-spark onboard`. It scans your HA entities and proposes
   which one maps to each config field, with the reason it matched and whether
   it agrees with the current setting:
   - `ha-spark onboard --preset solis` fills anything it can't match from the
     reference Solis/Solcast/Octopus/zappi setup.
   - `ha-spark onboard --write` also prints a ready-to-paste options fragment.
   - `ha-spark onboard --json` emits the proposals for tooling.

   Proposals are advisory — review them and set the options in the
   **Configuration** tab yourself; the wizard never rewrites your config.
3. The load forecast needs hourly household-load history:
   - `ha-spark backfill-load --list` — list statistics usable as a backfill
     source, then `ha-spark backfill-load --from <entity_id>` to import one
     as `ha_spark:house_load` history. `ha-spark onboard` reports when the
     history is sufficient.
4. `ha-spark plan` — print tonight's plan without applying it.
5. Leave the add-on running; it executes the plan daily at `plan_run_time`.
   When the simulated decisions look right, set `proactive_mode: on`.

## Data

The SQLite store lives at `/data/ha_spark.db` and survives restarts and
updates.
