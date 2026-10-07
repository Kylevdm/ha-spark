# ha-spark

Local-first battery charge planner for Home Assistant. Every half-hour (local
time) it re-forecasts household load and solar generation and resizes the
overnight cheap-rate charge. With `proactive_mode: on` it programs the inverter
and keeps the Solis power switch in step with dispatch holds. The default,
`simulate`, logs the writes it would make without changing hardware.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add `https://github.com/Kylevdm/ha-spark`.
2. Install the **ha-spark** add-on. Home Assistant builds the image on your
   machine, so the first install takes a few minutes.
3. Open the **Configuration** tab, set your options (see below), then start the
   add-on.

## Configuration

### Entity IDs (required for your installation)

The defaults match the author's hardware (Solis inverter, Solcast, Octopus
Intelligent, myenergi zappi). Point these at your own entities:

| Option | What it must be |
|---|---|
| `soc_entity` | Battery state of charge (%) |
| `soc_max_report_age_minutes` | How old Home Assistant's `last_reported` for `soc_entity` may be before ha-spark treats the SoC as stale and blocks real charge writes (default `10.0`). Some integrations never re-report an unchanged SoC, so an older SoC still counts as fresh if `battery_voltage_entity` reported within this age, which shows the integration is live. That exception lasts up to 12 hours. ha-spark does not check whether the battery is idle |
| `soc_failure_threshold` | How many consecutive failed SoC observations trigger the configured Solis fallback (default `3`). Before fallback entry, one good observation resets the count. Once fallback is requested, verified, or unconfirmed, a passing observation leaves it in place until recovery (`soc_recovery_minutes`) |
| `soc_recovery_minutes` | How many minutes of continuously passing SoC observations an active Solis fallback needs before ha-spark recovers (default `10`). Any failed observation restarts the count. The SoC may move, but its `last_reported` must never go backwards and must advance at least once; the battery-voltage liveness exception does not count. ha-spark then computes a fresh plan from the current SoC and the remaining cheap-rate window. The fallback stays in place until that plan is written and read back; a failed or mismatched read-back keeps the fallback and retries every minute. In `simulate`, `off` or `observe` mode nothing is written, so recovery completes once the fresh plan is computed |
| `solis_fallback_current_a` | Optional fallback-current ceiling in amps. When set, a sustained SoC failure programs only the configured `charge_window_start`–`charge_window_end` and this current, capped by `max_charge_current_a` and the Solis 62.5 A register limit. It is disabled when blank; there is no implicit current default. Only a successful Solis read-back is reported as verified |
| `battery_power_entity` | Optional battery power in W, sampled each minute to check SoC against energy flow. The Solis preset uses `sensor.solisac_battery_power`. Both power signs count. Jumps unexplained by energy flow by more than 2 percentage points fail; unchanged SoC fails after more than 2 points of energy flows. Two consecutive reads of a rejected changed value accept a possible BMS recalibration with a warning. Missing power skips that interval; without this option these checks are off. The baseline resets on restart |
| `battery_voltage_entity` | Battery voltage (V). ha-spark also uses it to tell whether the SoC's source is live, so pick the voltage sensor from the same integration as `soc_entity` |
| `solar_tomorrow_entity` | Solcast "forecast tomorrow" sensor (with `detailedForecast` attribute) |
| `octopus_rate_entity` | Octopus current electricity rate sensor |
| `dispatch_entity` | Octopus Intelligent dispatching binary sensor |
| `ev_plug_entity` / `ev_status_entity` | EV charger plug/status sensors; `ev_status_entity` also drives the charging hold |
| `consumption_energy_entity` | Household load energy statistic, excluding battery and EV charging |
| `grid_power_entity` | Optional whole-house supply power sensor (W); enables the supply guard |
| `charge_current_entity` | Optional inverter timed-charge current `number` entity for dashboards and telemetry. ha-spark controls the Solis natively (see below) |
| `inverter_power_switch_entity` | Inverter power switch `select` entity that the per-minute dispatch-hold check writes |
| `ha_template_charge_needed_entity` | Optional HA template sensor; the plan report shows its value next to ha-spark's for comparison |
| `inverter` | Which inverter ha-spark controls: `solis` (default) or `alphaess` |
| `solis_control_hub` | Name of the HA `modbus:` overlay hub that ha-spark uses to write the Solis timed-slot registers (default `solis_control`; see [`docs/solis-control-modbus-overlay.yaml`](../docs/solis-control-modbus-overlay.yaml)) |
| `solis_modbus_slave` | Modbus slave/unit id on that hub (default `1`) |
| `inverter_clock_tolerance_minutes` | How far the Solis inverter clock may drift from the household clock (`timezone`) before ha-spark refuses to arm an export window and `health` warns (default `5`). Fix drift with `python -m ha_spark solis sync-clock` |
| `inverter_clock_dst_sync` | Off by default. When on, ha-spark syncs the Solis inverter clock from the household clock after a daylight-saving change, as `sync-clock` does. It writes only when `proactive_mode` is `on` with control authority; in `simulate` it logs "would sync". It can sync at any point in the six hours after the change, so a restart or a switch to `on` in that time still syncs, and it retries each minute. It notifies through `notify_service` on success, or if the sync is still failing after 30 minutes. It never corrects ordinary drift |
| `alphaess_serial` | AlphaESS system serial (only needed when `inverter: alphaess`) |
| `person_entities` | Optional comma-separated `person`/`device_tracker` entity ids for occupancy signal recording |
| `heatpump_energy_entity` | Optional dedicated heat-pump energy sensor (kWh) for signal recording |
| `outdoor_weather_entity` | Weather entity with a `temperature` attribute (default `weather.home`) for signal recording |
| `v2l_power_entity` | Optional V2L discharge-power sensor (W); enables the V2L tally (see "V2L" below) |

### Solis control

ha-spark writes the Solis timed-slot charge registers itself, with the
`modbus.write_register` service on the `solis_control` overlay hub, and reads
them back through that hub's `sensor.solis_control_*` entities. Control does
not depend on the solax integration. An add-on can't add `modbus:` entries to
your `configuration.yaml`, so you add the overlay
(`docs/solis-control-modbus-overlay.yaml`) once by hand.

### Power-switch holds

On the Solis, ha-spark re-reads the dispatch state and checks
`inverter_power_switch_entity` every minute, separately from the half-hourly
plan. It turns the switch `Off` during an active dispatch hold and `On`
otherwise. When `ev_status_entity` is configured, Charging, Boosting and
Delivering also hold the switch `Off` outside the overnight charge window,
even without a matching dispatch. Five consecutive clear reads release that
hold; eco+ Diverting does not start one. Unreadable EV status is ignored for
the hold and appears as a warning in `ha-spark health`. With real control
enabled, the next minute's check corrects a restart, a failed write, or a
switch change made outside ha-spark. When ha-spark gives up control, including
during a clean shutdown, it writes a safe state: switch `On`, the configured
cheap charge window (default `23:30`-`05:30`), and an empty discharge window
(`00:00`-`00:00`) unless a verified export is still running. If ha-spark can't
trust the dispatch state, it won't open a new export window.

Before setting `proactive_mode: on`, disable any Home Assistant automation or
manual schedule that writes the same inverter or power switch, and keep them
disabled while ha-spark has control. The handover and rollback steps are in
[`docs/adr/0003-ha-spark-owns-the-solis-inverter.md`](../docs/adr/0003-ha-spark-owns-the-solis-inverter.md).

### Derived base load (ADR-0001, optional)

By default the load forecast uses one consumption sensor that you supply. On
most installs that sensor includes battery charging, so the forecast follows
the charge ha-spark itself scheduled the night before, and the sensor's history
has the same error. The derived path rebuilds base load by **energy balance**
from your HA long-term component statistics. It writes to the same external
statistic id (`ha_spark:house_load`) as the source-entity backfill below, and
the forecast keeps reading `consumption_energy_entity`, so you don't need to
repoint anything when you switch paths.

Per hour: `base = grid_import - grid_export + solar_generation + battery_discharge - battery_charge - ev_charge`.

| Option | What it must be |
|---|---|
| `derive_grid_import_entity` | **Required** if you set any of these. Long-term statistic id for grid import (kWh or compatible). Without it, the derived path refuses to run. |
| `derive_grid_export_entity` | Optional grid-export statistic id. If unset, ha-spark treats export as zero and adds a note to the run report. |
| `derive_solar_generation_entity` | Optional solar-production statistic id. |
| `derive_battery_charge_entity` | Optional battery-charge statistic id. |
| `derive_battery_discharge_entity` | Optional battery-discharge statistic id. |
| `derive_ev_charge_entity` | Optional EV-charge statistic id. |
| `derive_invert_*` | Sign-convention flag per component (`true` flips the canonical direction after unit conversion). ha-spark never guesses the sign, because a mis-signed export or battery-charge sensor would break the balance without any error. |

Run `ha-spark backfill-load --derive` once to write the full history (lookback
`BACKFILL_LOOKBACK_DAYS`, default 730). After every plan run the daemon also
re-derives the trailing 48 hours from the component statistics. It recomputes
and upserts every derivable hour in that window, so late or corrected component
rows replace the earlier values and the forecast never trains on stale base
load. The cumulative `sum` is anchored on the latest target row before the
window. A failed re-derivation is logged and never blocks planning.
`--from` selects the source-entity path and `--derive` selects this one. You
can't use both at once, and both write to the same external id.

Each hourly component must use a supported unit: `W`/`kW` mean power, or
`kWh`/`Wh` energy change. A component with any other unit is disabled, and the
run report gives the reason.

### Multiple inverters / device control (optional)

Single-inverter installs need no change here. ha-spark still reads the flat
`inverter` and entity-ID options above, converting them in memory without
rewriting `options.json`. `devices` is the structured alternative for installs
that want explicit per-device authority:

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

`control` decides who may write the device. A real write needs **both**
`control: ha_spark` **and** `proactive_mode: on`. With `observe`, ha-spark
reads and plans around the device but never writes it. `supplier` is reserved
for a device a third party controls. Both compute and log an `[OBSERVE]` action
line instead of writing, whatever `proactive_mode` is set to. Leave `control`
unset for `ha_spark`, which is the default and what the flat-key conversion
always produces.

### Planner

- `proactive_mode`: `off` (compute only), `simulate` (log the writes it
  *would* make; default), `on` (program the configured inverter and power
  switch where applicable). Run in `simulate` for a few nights and check the
  log before switching to `on`.
- `battery_capacity_kwh`, `battery_voltage_v`, `min_soc`, `target_soc_cap`,
  `max_charge_current_a`, `solis_fallback_current_a`: the battery and inverter
  model. The fallback current is optional and has no default.
- `charge_strategy`: `deficit` buys only the forecast shortfall; `fill`
  charges to `target_soc_cap` every night, which pays once the export rate is
  higher than the off-peak rate.
- `charge_buffer_pct`, `charge_efficiency`, `solar_haircut_k`,
  `solar_percentile`, `expected_load_kwh`: forecast and sizing settings. You can
  usually leave them at their defaults.
- `charge_window_start` / `charge_window_end`: your cheap-rate window.
- `plan_run_time`: no longer used, and kept so existing configs still load. The
  daemon recomputes the plan every half-hour slot.

### Supply guard (optional)

If an EV dispatch overlaps the battery's timed charge, total supply draw can
exceed what the main fuse should carry. Set `grid_power_entity` to a
whole-house supply power sensor (W). On every tick inside the charge window,
the daemon then lowers the timed-charge current to keep total draw under
`supply_max_current_a` (default 75 A), and raises it back toward the planned
current as headroom returns. `supply_voltage_v` (default 240) converts the
sensor's watts to amps. The guard's writes follow `proactive_mode` in the same
way as the plan's. Leave `grid_power_entity` empty to turn the guard off.

### ML load model (optional)

The add-on image includes scikit-learn. A standalone install needs the
`[habits]` extra; without scikit-learn, `load_model: ml|auto` falls back to the
slot-profile median.

The ML model is a gradient-boosted quantile model that forecasts tomorrow's
load from Open-Meteo temperatures (heating degree hours drive heat-pump
demand), day of week and season, recent load, recorded occupancy, and UK bank
holidays.

- `load_model`: `median` (profile only), `ml` (use the model whenever it can
  run), or `auto` (default), which uses ML only while `ha-spark forecast-eval`
  shows it beating the median over the trailing 14 days. ha-spark records both
  forecasts, so `auto` switches to ML when it wins and back to the median when
  it stops winning.
- `buffer_mode`: `fixed` keeps `charge_buffer_pct`; `quantile` replaces it
  with the model's own uncertainty, (P90 − P50)/P50, whenever the ML forecast
  drives the plan, so days with a narrow forecast range buy less margin.
- `latitude` / `longitude`: site coordinates for Open-Meteo. Leave them unset
  to use HA's configured location. ha-spark caches fetched past temperatures
  in the signal ledger, so the model can still run from recorded data when
  Open-Meteo is unreachable.

The model only supplies load numbers to the deterministic planner. On any
failure ha-spark falls back to the median forecast.

### Context facts (away / guests)

Context facts tell the planner about days that won't look like a normal week,
and it scales the load forecast for them:

```
ha-spark context add away   --from 2026-07-01 --to 2026-07-14 --note Italy
ha-spark context add guests --from 2026-12-24 --to 2026-12-27
ha-spark context add high_usage --from 2026-08-10 --to 2026-08-10 --factor 1.5
ha-spark context list
ha-spark context remove 3
```

`away` multiplies the forecast by `away_load_factor` (default 0.4), `guests`
by `guests_load_factor` (default 1.3), and `high_usage`/`low_usage` by the
`--factor` you give. Overlapping facts multiply. The plan report's forecast
line lists every active fact, so you can see each adjustment and remove it by
id. Facts only change the forecast. They never write to hardware.

You can also set them in plain language through `ha-spark ask`, and through
any chat interface connected to it:

```
ha-spark ask "I'm on holiday for the next two weeks"
  Noted. Away Sat 13 Jun to Fri 26 Jun. The planner will assume about 40% of
  normal load on those days. Undo with `ha-spark context remove 4`.
ha-spark ask "what do you know about my holidays?"   # lists stored facts
```

When Ollama is reachable, it extracts the dates as strict JSON, which ha-spark
validates before storing anything. Offline, a deterministic parser handles ISO
dates and phrases like "next week", "this weekend", and "for a fortnight".
Either way, ha-spark repeats the fact back with an undo command. The language
model only records facts you can review; it never controls hardware.

### Learned habits

ha-spark learns from occupancy and away history as it builds up:

- It predicts tomorrow's **occupancy** from the weekday/weekend pattern of
  recorded `occupancy_home_frac` and passes it to the ML model.
- It learns the **away load factor** by comparing your use on past `away` days
  with normal days of the same type, and applies it once there is enough
  history (the plan report marks it `(learned)`). Until then it uses the
  configured `away_load_factor`.

`ha-spark learn-factors` shows the current learned away factor, tomorrow's
predicted occupancy, and any advisory habit predictions. The daemon logs those
predictions each run, labelled with `proactive_mode`. They are advisory and
never write to hardware.

### V2L (vehicle-to-load, optional)

V2L is a manual physical adapter with no control API, so ha-spark only reads
it. When `v2l_power_entity` is set, each daemon tick reads the car's V2L
discharge power (W), adds it to the kWh delivered this session, values that
energy at the configured rates less conversion losses, publishes
`sensor.ha_spark_v2l_*`, and sends HA notifications. V2L doesn't change the
planner or the drivers.

| Option | What it must be |
|---|---|
| `v2l_power_entity` | V2L discharge-power sensor (W). Empty (the default) disables the feature. |
| `v2l_round_trip_efficiency` | Charge and discharge conversion losses combined into one efficiency (default `0.85`). ha-spark divides delivered kWh by this to get the energy needed to refill the car. |
| `v2l_peak_rate_gbp` | £/kWh import rate that V2L saves while it runs (default `0.30`). |
| `v2l_offpeak_rate_gbp` | £/kWh cheap rate used to refill the car later (default `0.07`). |
| `v2l_cutoff_time` | Local time the cheap window starts. The unplug notification fires at or after it (default `01:00`). |
| `v2l_notify_service` | Deprecated fallback HA `notify.<service>` target, scheduled for removal. `notify_service` takes precedence; this is used only when `notify_service` is blank. |
| `v2l_budget_kwh` | Optional V2L budget in kWh, used in place of car SoC (the car has no HA integration, so ha-spark can't read its SoC). `0` disables the plug-in warning. A top-up request never asks for more than this. |
| `v2l_rectifier_efficiency` | Rectifier efficiency from the car's AC output into the house battery (default `0.94`). |
| `v2l_charge_kw` | DC rate the rectifier charges the house battery at (default `2.15`, the measured rate at its 10 A AC input limit). Sets the "start V2L by" time. |
| `v2l_soc_tolerance_pct` | How many SoC points below the overnight plan's expected path, on an Axle event day, count as the plan having been wrong (default `5`). |

Published sensors: `sensor.ha_spark_v2l_power_w` (current discharge power),
`sensor.ha_spark_v2l_energy_kwh` (session total), and
`sensor.ha_spark_v2l_net_saving_gbp` (avoided peak import minus the cheap-rate
refill cost, which can be negative). Only part of the car's AC output offsets
import: it passes the rectifier into the house battery and then the battery's
discharge leg, taken as the square root of `charge_efficiency`. The session tally survives restarts and
resets at the start of a new day once the car is idle.

ha-spark sends up to three notifications per session, each at most once:

- an unplug reminder at the cutoff, because the cheap window is starting and
  powering the house from the car no longer saves money;
- a reminder to plug the car in to recharge when V2L stops;
- a warning when you are about to reach `v2l_budget_kwh`.

**Top-up request for an underfunded Axle event.** ha-spark asks you to start
V2L when a paid export slot would still be skipped for funding, even after
spending the post-event reserve. It asks only when the charge window can't fix
the gap, which means one of these:

- the overnight charge was capped by `max_charge_current_a` for the window;
- on the event day, live SoC is more than `v2l_soc_tolerance_pct` points below
  what the last overnight plan expected for that time.

It also asks only when V2L energy costs less than the event pays:
`v2l_offpeak_rate_gbp` divided by `v2l_round_trip_efficiency`, the rectifier
and the battery's discharge leg. The notification gives the shortfall, the car
energy to draw, and the time to start V2L by. It goes out whether or not the
car is plugged in. You start V2L yourself. The request repeats only when the
shortfall grows by 0.5 kWh or more.

`ha-spark v2l` prints the live tally. None of this writes to hardware. The
planner reads live SoC at plan time, so it already sees the effect of V2L.

### Forecast ledger

Every plan run records its forecast for tomorrow (model, total kWh, per-slot
breakdown), replacing the earlier record for that date and model.
`ha-spark forecast-eval [--days N]` compares those recorded forecasts with
actual consumption and reports MAE/MAPE per model. `load_model: auto` uses that
comparison to decide whether the ML model drives plans.

A signal sampler also runs every 30 minutes and records these household
signals: `occupancy_home_frac` (from `person_entities`), `heatpump_kwh` (from
`heatpump_energy_entity`), and `temp_out_c` (from `outdoor_weather_entity`).
Each is optional; leave its entity unset to skip it. If an entity can't be
read, ha-spark logs a warning and skips that signal only.

### Tariff

`rate_offpeak_gbp_kwh`, `rate_peak_gbp_kwh`, `rate_export_gbp_kwh`: the rates
for the cost projection printed with each plan and for `ha-spark backtest`.

`tariff_provider` selects how ha-spark prices plans. `fixed` (the default) uses
the rates above and the charge window. `dynamic` prices each half-hour slot at
its live price from an HA price sensor and treats the cheapest slots as "cheap"
for costing; the charge window itself doesn't change. Set
`dynamic_rates_entity` to an entity whose `rates` attribute is a list of
`{start, end, value_inc_vat}` (e.g. the BottlecapDave Octopus Energy
integration's `event....current_day_rates`). `dynamic_rates_entity_tomorrow`
is optional and covers tomorrow's slots the same way. If a read is missing or
bad, ha-spark falls back to the fixed rates. `ha-spark health` reports the live
provider status.

`octopus_intelligent` reads prices from the Octopus standard-unit-rates REST
API and planned dispatch windows from the Octopus Kraken GraphQL API, instead
of from HA sensors. Dispatch and cheap-window handling is otherwise the same as
`fixed`. It needs `octopus_api_key`, `octopus_account_number` (for the
dispatches query), and `octopus_product_code`/`octopus_tariff_code` (for the
rates endpoint, e.g. `INTELLI-VAR-22-10-14` / `E-1R-INTELLI-VAR-22-10-14-A`).
If authentication or an API call fails, ha-spark falls back to the fixed rates
and dispatches. `ha-spark health` reports the live provider status. ha-spark
never logs or echoes the API key.

`axle` adds the supervised Axle export-event source. Set `axle_api_key` to the
static token from Axle's Home Assistant account page. `axle_event_entity` can
name the Home Assistant mirror entity, which ha-spark reads if the direct
request fails. Set `axle_event_rate_gbp_kwh` to the paid export rate, because
Axle's Home Assistant event response doesn't include one. The provider accepts
only explicit, fresh export windows. Import events and malformed or stale
responses produce no export slots. The API key is a `password` option, and
ha-spark never writes it to logs or reports.

Set `notify_service` to the single Home Assistant `notify.<service>` target for
all notifications, including V2L and export lifecycle notices sent to the
person supervising. ha-spark sends one notice when it accepts an event, one
after a verified export start, one after verified cleanup, and one for a
terminal abort. It deduplicates notices by event and lifecycle step. A notice
is never proof that a hardware write succeeded. Leave both `notify_service` and
the deprecated `v2l_notify_service` blank to turn the notices off. The
supervised procedure is in `docs/runbooks/supervised-axle-export.md`.

On the Solis, an export window fires on the inverter's own clock. ha-spark
refuses to arm one unless `sensor.<solis_control_hub>_inverter_clock` (add it
from `docs/solis-control-modbus-overlay.yaml`) is under 60 s old and within
`inverter_clock_tolerance_minutes` of the household clock (`timezone`).
Otherwise it sends an "Axle export held: inverter clock" notice and retries on
each pass. `ha-spark health` reports the clock error as a warning above the
tolerance and a failure at 30 minutes or more. `ha-spark solis sync-clock` sets
the inverter clock from the household clock and reads it back. It writes only
when `proactive_mode` is `on`. ha-spark never runs it on its own, except after
a daylight-saving change when `inverter_clock_dst_sync` is on.

For paid export, the planner uses `battery_discharge_ceiling_kw` (default 3.2)
as a conservative battery output and `dno_export_limit_kw` as your
installation's grid-export limit. That defaults to 3.68 kW, the G98
fit-and-inform limit (16 A single-phase); raise it only to the limit in your
DNO's G99 approval. Both caps are separate from the Solis prototype's fixed
62.5 A timed-discharge command. The export path is supervised and has not been
validated on hardware for unattended use. Follow the
[`supervised Axle export runbook`](../docs/runbooks/supervised-axle-export.md)
for every live test.

### Octopus API (optional)

`octopus_api_key`, `octopus_mpan`, and `octopus_meter_serial` enable
`ha-spark pull-consumption`, which downloads grid-import history for cost
backtesting only. The load forecast does **not** use it. The same
`octopus_api_key` also serves the `octopus_intelligent` tariff provider above.

### Ollama (optional)

`ollama_url` / `ollama_model` point at a remote Ollama instance (e.g. over
Tailscale) for the natural-language features. The planner works without it,
and `health` reports a missing Ollama as a warning.

When Ollama is reachable, `ha-spark ask` gives it the plan ha-spark computed,
so its answers explain that plan:

```
ha-spark ask "why is it charging at 42 A tonight?"
ha-spark ask "what does tonight cost vs no battery?"
```

The model sees the same plan the `plan` command prints and only answers home
energy questions. It explains and reports, and never controls hardware. If
Ollama is down, the deterministic offline parser answers the energy questions
it recognises.

### HTTP API (for companion integrations)

The daemon serves an HTTP API for the companion integration through add-on
ingress, on port 8099. Home Assistant authenticates every request, and the
port is not mapped to the host network. The add-on answers only connections
from Home Assistant's ingress proxy (`172.30.32.2`); requests from any other
address, including other add-ons on the Supervisor network, get `403`.
Forwarding headers such as `X-Forwarded-For` are ignored. In standalone/dev
mode (`ha_url` + `ha_token`) it listens on `http://127.0.0.1:8099` only.
Endpoints:

- `GET /api/health`: liveness, and when the latest plan was computed
- `GET /api/plan`: the latest plan, as the same sensor payload the daemon
  publishes to Home Assistant
- `GET /api/config`: the current options, with secrets masked
- `POST /api/config`: merge the posted options, save them to
  `/data/options.json`, and reload the daemon's settings without a restart

### Agent surface

The agent surface is off by default. Set `agent_surface: on` to let an
external model (e.g. Claude, or any OpenAPI-compatible tool client) read
ha-spark's data and, optionally, trigger a few gated actions.

- `agent_surface` (`off` | `on`): the master switch, off by default.
- `agent_exposure` (`read` | `read_act` | `read_write`, default `read_act`):
  how much is exposed. `read` is data only (states, plan, forecast,
  predictions, health). `read_act` adds `add_context` and `run_plan`.
  `read_write` adds `set_config`.
- `agent_api_token`: bearer token for the published port. If you leave it
  blank, the add-on generates one on first start and prints it **once** to the
  add-on log. It's a `password` field, so the UI never shows it again.
- `agent_expose_port` (`bool`, default `false`): publish the agent surface on
  the host network for clients that can't reach add-on ingress.

The agent surface is always served through HA's ingress proxy, which already
authenticates the user, so requests there need no token. For external clients
(Claude Desktop, open-webui on your LAN or Tailnet) that can't use ingress, set
`agent_expose_port: true` and map host port **8098** (the `ports:` entry in
this add-on's configuration). Requests on that published port need the bearer
token.

- **open-webui**: add a tool server pointing at
  `http://<host>:8098/openapi.json`, with header `Authorization: Bearer
  <token>`.
- **Claude (Desktop, or via your own reverse proxy)**: point an MCP
  (Streamable HTTP) connector at `http://<host>:8098/mcp`, with the same
  bearer token. The server 307-redirects `/mcp` to `/mcp/`, and MCP clients
  follow the redirect.
- **claude.ai (web)** also needs a public HTTPS endpoint in front of the
  published port, such as a reverse proxy or Nabu Casa, because claude.ai
  can't reach a bare LAN or Tailnet address. You set that up yourself; the
  add-on doesn't.

Act and write tools still go through the `proactive_mode` gate, and the model
never calls `call_service` directly. The deterministic planner still makes
every hardware decision.

## Onboarding

1. **Check the Log tab** after the first start. The add-on runs
   `ha-spark health` and prints one line per check: HA REST, HA WebSocket,
   Ollama, SQLite, load history, supply guard, tariff provider, household
   clock, inverter clock (Solis only), and entity config.
2. **Map your entities.** From a shell in the add-on container (e.g. the SSH
   add-on with `docker exec -it addon_<slug> sh`, or the add-on's own
   terminal), run `ha-spark onboard`. It scans your HA entities and proposes
   one for each config field, with the reason it matched and whether it agrees
   with the current setting:
   - `ha-spark onboard --preset solis` fills anything it can't match from the
     reference Solis/Solcast/Octopus/zappi setup.
   - `ha-spark onboard --write` also prints an options fragment ready to paste.
   - `ha-spark onboard --json` prints the proposals as JSON for other tools.

   The proposals are advisory. Review them and set the options in the
   **Configuration** tab yourself; the wizard never rewrites your config.
3. The load forecast needs hourly household-load history:
   - `ha-spark backfill-load --list` lists statistics you can import. Then
     `ha-spark backfill-load --from <entity_id>` imports one as
     `ha_spark:house_load` history. `ha-spark onboard` tells you when there is
     enough history.
   - Or set the `derive_*_entity` options (grid import is required) and run
     `ha-spark backfill-load --derive` once. See "Derived base load" above;
     each plan run then keeps the last 48 hours up to date.
4. `ha-spark plan` prints tonight's plan without applying it.
5. Leave the add-on running; it recomputes the plan every half-hour. When the
   simulated decisions look right, disable conflicting automations, confirm
   the Solis overlay and entity reads are healthy, then set
   `proactive_mode: on`. Follow the handover procedure linked above.

## Data

The SQLite store lives at `/data/ha_spark.db` and survives restarts and
updates.

Dispatch power evidence: `ev_power_entity` reads direct car draw (W or kW).
`dispatch_grid_power_entity` supplies an independent grid-import reading;
set `dispatch_grid_power_invert: true` for the Solis import-negative meter.
These options do not enable the supply guard. During an active dispatch,
car draw ≥1.4 kW or grid import ≥3 kW above forecast house load confirms it.
After ten minutes, an otherwise uncorroborated dispatch is released only when
both readable power signals are below those thresholds. Missing or invalid
power evidence retains it; live dispatch, adjusted rate, charging status and
a connected plug retain their existing precedence. Planning and the minute reconcile use the daily-average house forecast
(the latter from the last plan). The Solis preset supplies both sensors and inversion.
