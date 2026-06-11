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
| `charge_current_entity` | Inverter timed-charge current `number` entity (the only control written) |
| `inverter_power_switch_entity` | Inverter power switch `select` entity |
| `ha_template_charge_needed_entity` | Optional HA template sensor for comparison logging |

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

### Tariff

`rate_offpeak_gbp_kwh`, `rate_peak_gbp_kwh`, `rate_export_gbp_kwh` — used for
the cost projection printed with each plan and by `ha-spark backtest`.

### Octopus API (optional)

`octopus_api_key`, `octopus_mpan`, `octopus_meter_serial` enable
`ha-spark pull-consumption` (grid-import history for cost backtesting only —
it is **not** used as the load forecast).

### Ollama (optional)

`ollama_url` / `ollama_model` point at a remote Ollama instance (e.g. over
Tailscale) for the natural-language agent features. The planner runs fine
without it; health reports it as a warning.

## Onboarding

1. **Check the Log tab** after the first start: the add-on runs
   `ha-spark health` and prints a line per dependency (HA REST, HA WebSocket,
   Ollama, SQLite, load history).
2. The load forecast needs hourly household-load history. From a shell in the
   add-on container (e.g. the SSH add-on with
   `docker exec -it addon_<slug> sh`, or the add-on's own terminal):
   - `ha-spark onboard` — readiness check.
   - `ha-spark backfill-load --list` — list statistics usable as a backfill
     source, then `ha-spark backfill-load --from <entity_id>` to import one
     as `ha_spark:house_load` history.
3. `ha-spark plan` — print tonight's plan without applying it.
4. Leave the add-on running; it executes the plan daily at `plan_run_time`.
   When the simulated decisions look right, set `proactive_mode: on`.

## Data

The SQLite store lives at `/data/ha_spark.db` and survives restarts and
updates.
