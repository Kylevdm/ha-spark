# ha-spark

Local-first battery charge planner for Home Assistant. Every half-hour (local
time) it re-forecasts household load and solar generation and resizes the
overnight cheap-rate charge. The site operating mode (Observe, Protect or
Optimise) determines which automatic hardware writes are authorised. Every
fresh install starts in Observe: it previews plans without changing hardware.
Settings, presets and agent requests cannot change the operating mode.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add `https://github.com/Kylevdm/ha-spark`.
2. Install the **ha-spark** add-on. Home Assistant builds the image on your
   machine, so the first install takes a few minutes.
3. Start the add-on in Observe, then configure ha-spark's own store through
   `ha-spark config` (see below). The add-on **Configuration** tab holds only
   `log_level`; it is not an editor for runtime settings.

## Operating-mode page

Open **ha-spark → Open Web UI** in Home Assistant. This ingress-only page is
available only to Home Assistant administrators; it is not served on the agent
port or the standalone/dev listener. Standalone mode changes are CLI-only.

The page shows the site mode (including **authority unknown**), epoch,
transition and failing actions, holds and their causes, fallback and its causes,
last change and requester kind, and every unmet entry precondition for each
mode. Refresh to see current progress; an accepted request is not proof that
hardware has verified it.

Protect and Optimise require confirmation. Before enabling Optimise, disable
any pre-existing automations or manual schedules that write the same devices.
Entering Optimise also requires no SoC fallback or recovery in progress; wait
for SoC recovery before requesting the change.
Axle export remains supervised: ha-spark will arm configured export windows,
but a person must be present for preparation and verified cleanup (see the
[supervised-export runbook](../docs/runbooks/supervised-axle-export.md)).
A change uses the epoch displayed on the page. A conflict reloads status and
requires a new click; it never retries the change automatically. With authority
unknown, only **Confirm Observe** is offered; that confirmation writes no
hardware. A queued request may still finish, so refresh before trying again.

CLI equivalents are `ha-spark mode show`, `ha-spark mode set protect`,
`ha-spark mode set optimise`, `ha-spark mode set observe`, and
`ha-spark mode confirm-observe`. Only the running daemon changes authority.

## Updating to 0.25.0 (cutover)

1. **Switch the operating mode to Observe before updating**, using the mode
   page or `ha-spark mode set observe`. Wait for the handover to complete.
2. **Copy your options YAML and keep it:** open the add-on **Configuration**
   tab, choose **Edit in YAML**, and save the contents somewhere safe before
   the update. This legacy copy may contain secrets; do not share it.
3. **Update.** On the first start, ha-spark seeds its store once from
   `options.json`, or from the Supervisor's stored copy of your options. On
   current Supervisors the expected source after an update is `supervisor`,
   because the Supervisor strips retired keys from `options.json` but keeps
   them in its stored copy. The log line
   `Configuration committed ... actor=seed source=options|supervisor|defaults`
   tells you which source was used. Options never saved in the form are absent
   from the Supervisor's copy; ha-spark's code defaults match the 0.24.1 defaults.

   If no saved options are found while the operating mode is Protect or
   Optimise, ha-spark enters configuration repair with:
   “No saved options found to seed from while the operating mode is not Observe; import a saved configuration”.
   Run `ha-spark config import <file>` on the saved copy. Invalid seed values
   also require repair, not an unchecked start. In Observe the same case seeds
   defaults instead (`source=defaults`), so import the saved copy then.
4. **Check `ha-spark config show`**, confirm the mappings and limits, then
   return to your previous mode through the mode page or `ha-spark mode set`.
   Importing settings never changes the operating mode.

After updating, the Supervisor logs `Option '<key>' does not exist in the schema`
for each retired key every 15 minutes. This is expected and harmless: those
stored legacy options are no longer runtime settings. Keep your saved YAML
until you have checked the new store.

## Configuration

### ha-spark's own store

All runtime fields described below are **settings in ha-spark's own store**,
not Supervisor add-on options. Only `log_level` remains in the Supervisor form.
The one settings service validates ingress, CLI, seed and import changes.
Environment variables and `.env` are not runtime sources: they supply only
bootstrap HA credentials, the standalone data path and log level.

The data directory (`/data` in the add-on, beside the database in standalone/dev)
contains:

- `ha_spark_config.json`: the versioned **settings document**, with
  `schema_version`, a positive `revision`, and `settings` holding explicit owner
  inputs only. Defaults and derived device definitions are not copied into it.
- `ha_spark_secrets.json`: the separate **secrets file**, mode `0600`, containing
  the Octopus key, Axle key and agent token inputs at the same revision.

Every commit increments the revision and commits the pair together; matching
`*.prev` files allow recovery from an interrupted pair replacement. The
initialisation marker prevents a lost store from being silently seeded again.
A rejected save leaves the files and running snapshot unchanged. Unknown keys
are refused, and concurrent writers must supply the revision they last read;
conflicts require a fresh read, not a blind retry. A save never changes authority,
the operating mode or hardware. The data directory cannot be moved by a save.

Safe planning, tariff and fenced entity-ID changes apply live and request a
replan under the normal operating-mode gates. Device/driver addressing (including
`inverter`, explicit `devices`, `solis_control_hub`, `solis_modbus_slave` and
`alphaess_serial`), listeners, the agent token and startup-bound clients require
an add-on restart. The read view includes each field's `owner`, `secret` and
`reload` metadata. Integration-owned fields remain here for now, tagged
`integration`; they have not yet moved to a companion integration.

A change requiring restart is reported as `pending_restart`, including the
fields concerned. The whole candidate waits: no live part of the same save
runs ahead, and later saves remain pending while restart-field differences
remain. Restart loads and checks the committed candidate as a whole.

### Owner CLI

Run these inside the add-on container, or in your standalone environment:

```sh
ha-spark config show
ha-spark config export > settings.json
ha-spark config set target_soc_cap=85 notify_service=mobile_app_phone
ha-spark config set-secret octopus_api_key
ha-spark config set-secret axle_api_key --clear
ha-spark config import settings.json
```

- `show` returns effective values, revision, input keys, field metadata and
  live/pending-restart status. Each secret is only `{"secret": true, "set": true|false}`.
- `export` returns only non-secret inputs, not defaults or secret values.
- `set KEY=VALUE…` merges non-secret changes. Values are parsed as JSON when
  possible, otherwise as strings. It reads the revision before saving; a race
  reports a conflict. Use `set-secret`, not `set`, for secret fields.
- `set-secret KEY` reads a no-echo prompt on a terminal, or one line from stdin.
  **Never put a secret value on argv** (including shell arguments); use the
  prompt or secure stdin. `--clear` explicitly unsets the named secret.
- `import FILE` accepts size-limited JSON or safe YAML: either an export or a
  saved legacy Supervisor options file. It **replaces non-secret inputs**, not
  merges them; omitted fields return to defaults. Omitted secrets and legacy
  `***` masks preserve existing secrets; clearing is explicit. A legacy file
  can carry old secrets into the separate secrets file, but new CLI secret
  entry uses `set-secret`, never command-line values. Legacy `log_level` is
  ignored, retired `proactive_mode` and device `control` are dropped with a
  warning, and `db_path` is accepted only if it matches the fixed database path.
  Move a non-default database to that fixed path first; import cannot move it.

All CLI writes go through the owner-only daemon socket, not direct file edits,
and refuse when no daemon is running. Without a daemon, `show` and `export`
can read the files without changing them. In repair only `show` and `import`
are available; import reads and supplies the current repair token automatically.
Exports are secret-free backups, not backups of credentials.

### Configuration repair

Missing or unreadable files after initialisation, a mismatched pair with no
matching previous pair, invalid values, an unsupported schema version, a failed
seed or a failed pending-restart validation trigger **configuration repair**.
A document from a newer schema is not overwritten by import.

This bootstrap-only state serves diagnostics and repair through ingress and
the daemon socket. It builds no device drivers, runs no scheduler tick, computes
no plan, performs no mode transition, and makes **no hardware writes at all**,
including shutdown safe-state writes, hold maintenance, clock sync or fallback.
The published agent surface is unavailable. The authority store is read-only:
recorded holds and transitions remain recorded, not enforced or advanced.

**Protection and optimisation are suspended.** A switch already Off stays Off;
ha-spark does not release it. The EV is not protected from drawing the house
battery, any resident export window is unsupervised, and no fallback charging
schedule is applied. Take manual control of the equipment as needed until
configuration is repaired; repair itself supplies no safety net.

`ha-spark health` exits non-zero with a failing Configuration check;
`/api/health` reports failure. `ha-spark mode show` and the mode page show stored
authority, holds and transitions marked `suspended: configuration_repair`, not
active protection. Settings-dependent CLI commands refuse rather than using an
untrusted mapping. Socket work and mode changes, including Confirm Observe,
are refused during repair.

Repair ends only when a valid import commits with the current repair token:
use `ha-spark config import <file>`, or the ingress import endpoint below.
Ordinary saves are refused. Import validates the complete candidate and normal
save guards still apply; it cannot bypass fallback validation in Optimise.
Normal startup then begins in the same process, including startup hold
reconciliation and monitor restoration before the first plan. An existing
transition resumes only on that normal startup path.

### Entity IDs (required for your installation)

The defaults match the author's hardware (Solis inverter, Solcast, Octopus
Intelligent, myenergi zappi). Point these at your own entities:

| Setting | What it must be |
|---|---|
| `soc_entity` | Battery state of charge (%) |
| `soc_max_report_age_minutes` | How old Home Assistant's `last_reported` for `soc_entity` may be before ha-spark treats the SoC as stale and blocks real charge writes (default `10.0`). Some integrations never re-report an unchanged SoC, so an older SoC still counts as fresh if `battery_voltage_entity` reported within this age, which shows the integration is live. That exception lasts up to 12 hours. ha-spark does not check whether the battery is idle |
| `soc_failure_threshold` | How many consecutive failed SoC observations trigger the configured Solis fallback (default `3`). Before fallback entry, one good observation resets the count. Once fallback is requested, verified, or unconfirmed, a passing observation leaves it in place until recovery (`soc_recovery_minutes`) |
| `soc_recovery_minutes` | How many minutes of continuously passing SoC observations an active Solis fallback needs before ha-spark recovers (default `10`). Any failed observation restarts the count. The SoC may move, but its `last_reported` must never go backwards and must advance at least once; the battery-voltage liveness exception does not count. ha-spark then computes a fresh plan from the current SoC and the remaining cheap-rate window. The fallback stays in place until that plan is written and read back; a failed or mismatched read-back keeps the fallback and retries every minute. In Observe nothing is written, so recovery completes once the fresh plan is computed |
| `solis_fallback_current_a` | Optional fallback-current ceiling in amps. When set, a sustained SoC failure programs only the configured `charge_window_start`–`charge_window_end` and this current, capped by `max_charge_current_a` and the Solis 62.5 A register limit. It is disabled when blank; there is no implicit current default. Only a successful Solis read-back is reported as verified |
| `battery_power_entity` | Optional battery power in W, sampled each minute to check SoC against energy flow. The Solis preset uses `sensor.solisac_battery_power`. Both power signs count. Jumps unexplained by energy flow by more than 2 percentage points fail; unchanged SoC fails after more than 2 points of energy flows. Two consecutive reads of a rejected changed value accept a possible BMS recalibration with a warning. Missing power skips that interval; without this setting these checks are off. The baseline resets on restart |
| `battery_voltage_entity` | Battery voltage (V). ha-spark also uses it to tell whether the SoC's source is live, so pick the voltage sensor from the same integration as `soc_entity` |
| `solar_tomorrow_entity` | Solcast "forecast tomorrow" sensor (with `detailedForecast` attribute) |
| `solar_today_entity` | Optional Solcast "forecast today" sensor; recommended for accurate charge sizing. Honours `solar_percentile` and `detailedForecast`, like the tomorrow sensor. If missing or invalid, today's solar counts as zero: this oversizes the charge, never undersizes it. |
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
| `solis_model_confirmed` | Boolean, default `false`. Owner confirmation that the inverter is the validated reference (33000 = `0x0026` AND 35000 = `0x2031`) when identity sensors cannot be read. Readable mismatches always override confirmation; V2 TOU (`33289 = 0xAA55`) is not validated. This setting grants no write authority |
| `solis_modbus_slave` | Modbus slave/unit id on that hub (default `1`) |
| `inverter_clock_tolerance_minutes` | How far the Solis inverter clock may drift from the household clock (`timezone`) before ha-spark refuses to arm an export window and `health` warns (default `5`). Fix drift with `python -m ha_spark solis sync-clock` |
| `inverter_clock_dst_sync` | Off by default. When on, ha-spark syncs the Solis inverter clock from the household clock after a daylight-saving change, as `sync-clock` does. It writes only when the site operating mode authorises clock changes; in Observe it logs "would sync". It can sync at any point in the six hours after the change, so a restart or an authorised mode change in that time still syncs, and it retries each minute. It notifies through `notify_service` on success, or if the sync is still failing after 30 minutes. It never corrects ordinary drift |
| `alphaess_serial` | AlphaESS system serial (only needed when `inverter: alphaess`) |
| `person_entities` | Optional comma-separated `person`/`device_tracker` entity ids for occupancy signal recording |
| `heatpump_energy_entity` | Optional dedicated heat-pump energy sensor (kWh) for signal recording |
| `outdoor_weather_entity` | Weather entity with a `temperature` attribute (default `weather.home`) for signal recording |
| `v2l_power_entity` | Optional V2L discharge-power sensor (W); enables the V2L tally (see "V2L" below) |

### Read-only inverter diagnostic

Run `python -m ha_spark inverter show` to read each configured inverter's model
identity, validation reason, declared hold contract, and resident slot-1 charge
window, charge current (43141) and configured power-switch state. Identity words
are shown as raw integers, keyed by register address; `null` means unreadable.
Resident readings show `unknown` when unreadable, never a configured substitute.
This command uses only HA REST GETs: no register writes, select/switch service
calls, clock writes, refresh service calls, or authority-store writes. It works
in Observe as well as Protect/Optimise and does not change any existing write gate.

Add the three slow-polling input sensors from the overlay document to enable
identity reads (`model_word`, `inverter_type_definition`, `tou_version`). Only
the two-word reference identity is validated, not Solis in general. If identity
cannot be read, leave `solis_model_confirmed: false` unless you have independently
confirmed that reference identity. AlphaESS declares no validated hold surface
(#196). The contract declaration is for future protection preconditions; this
diagnostic does not itself establish fresh protection evidence.

### Solis control

ha-spark writes the Solis timed-slot charge registers itself, with the
`modbus.write_register` service on the `solis_control` overlay hub, and reads
them back through that hub's `sensor.solis_control_*` entities. Control does
not depend on the solax integration. An add-on can't add `modbus:` entries to
your `configuration.yaml`, so you add the overlay
(`docs/solis-control-modbus-overlay.yaml`) once by hand.

Within one ha-spark process, actions on the same inverter bus are serialised
from prerequisite reads through the final read-back. This includes plan and
fallback programming, supply-guard resizing, power-switch holds and clock
sync. The daemon's authority store is the sole write gate, applying the site
operating mode to each action. This coordination does not cover
other processes or external Home Assistant automations.

### Power-switch holds

On the Solis, ha-spark re-reads the dispatch state and checks
`inverter_power_switch_entity` every minute, separately from the half-hourly
plan. It turns the switch `Off` during an active dispatch hold and back `On`
only to release a hold it recorded itself. When `ev_status_entity` is configured, a fresh
Charging, Boosting or Delivering report also holds the switch `Off`, even
without a matching dispatch. Inside the overnight charge window a new EV hold
doesn't start when fresh `battery_power_entity` evidence shows the battery
charging or idle and the inverter's resident slot-1 charge window reads back
fresh. The window's overlay sensors report only on change, so an unchanged
window counts as fresh while the hub's `sensor.<hub>_inverter_clock` reports
within `battery_power_max_age_seconds` (for up to 12 hours since the window
last reported). A window field older than that maximum with no fresh clock
report, or older than 12 hours, means the hold applies. Five advancing fresh not-charging reports release an EV hold, and
the overnight window never releases one; eco+ Diverting does not start one.
Stale or unreadable EV status neither starts nor releases a hold and appears
as a warning in `ha-spark health`. In Protect or Optimise, the next minute's
check corrects a restart, a failed write, or a switch change made outside
ha-spark. When ha-spark gives up control, including
during a clean shutdown, it writes a safe state: switch `On`, the configured
cheap charge window (default `23:30`-`05:30`), and an empty discharge window
(`00:00`-`00:00`) unless a verified export is still running. The switch is
written `On` only when it reads back `On` or `Off`; otherwise that step is
refused and logged. A clean shutdown writes only in Optimise. While an EV hold
is recorded or a known dispatch end is still ahead, it restores the charge
window, leaves the switch `Off`, and sends a notice; the next start reconciles
the hold first. If ha-spark can't trust the dispatch state, it won't open a new
export window.

Before enabling Optimise, disable any Home Assistant automation or
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

| Setting | What it must be |
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

### Multiple inverters (optional)

Single-inverter installs need no change here. ha-spark still reads the flat
`inverter` and entity-ID settings above, deriving devices in memory without
persisting that derived list in the settings document. `devices` is the structured alternative for installs
that need explicit device definitions. The site operating mode covers them all:

```yaml
devices:
  - id: main_inverter
    type: inverter
    driver: solis          # solis | alphaess
    entities:
      charge_current: number.solisac_timed_charge_current
      window_start: time.solisac_charge_start
      window_end: time.solisac_charge_end
      power_switch: select.solisac_power_switch
```

### Planner

- `battery_capacity_kwh`, `battery_voltage_v`, `min_soc`, `target_soc_cap`,
  `max_charge_current_a`, `solis_fallback_current_a`: the battery and inverter
  model. The fallback current is optional and has no default.
- `charge_strategy`: `deficit` buys only the forecast shortfall; `fill`
  charges to `target_soc_cap` every night, which pays once the export rate is
  higher than the off-peak rate.
- `charge_buffer_pct`, `charge_efficiency`, `solar_haircut_k`,
  `solar_percentile`, `expected_load_kwh`: forecast and sizing settings. You can
  usually leave them at their defaults.
- `solar_haircut_k` (default `1.0`) multiplies the forecast reported by the
  Solcast entity. Any dampening or hard limit configured in the Solcast
  integration has already been applied, so the corrections stack. Use one
  correction: leave Solcast dampening and hard limit off and tune
  `solar_haircut_k`, or keep them on and leave `solar_haircut_k` at `1.0`.
- `charge_window_start` / `charge_window_end`: your cheap-rate window.
- `plan_run_time`: no longer used, and kept so existing configs still load. The
  daemon recomputes the plan every half-hour slot.

### Daemon-only hardware writes

`plan --apply`, `run --once`, `solis sync-clock`, the agent's `run_plan`
(including MCP) and `POST /agent/run` submit fixed operations to the running
ha-spark daemon. Replans use the daemon's live SoC, fallback and hold state;
a one-shot request cannot overwrite an active fallback charging schedule.
The daemon's committed site operating mode and safety checks still apply.

The CLI connects only to `ha_spark.sock` in `/data` (standalone/dev: beside the
configured database). The Unix socket is mode `0600`, not a TCP endpoint or an
ingress/agent port. An exclusive `ha_spark.lock` prevents a second daemon;
only its holder removes a stale socket, and shutdown removes the socket.
In-process agent requests use the same daemon queue directly. Requests are
processed on the loop's next tick, normally within 60 seconds.

Without a reachable daemon, plan/run commands and standalone agent apps still
compute and return the plan, but refuse hardware writes with a clear reason.
CLI plan/run exit successfully after printing that refusal; `sync-clock`
returns 1 because no sync was verified (2 for a non-Solis install).

### Supply guard (optional)

If an EV dispatch overlaps the battery's timed charge, total supply draw can
exceed what the main fuse should carry. Set `grid_power_entity` to a
whole-house supply power sensor (W). On every tick inside the charge window,
the daemon then lowers the timed-charge current to keep total draw under
`supply_max_current_a` (default 75 A), and raises it back toward the planned
current as headroom returns. `supply_voltage_v` (default 240) converts the
sensor's watts to amps. The guard's writes follow the site operating mode and
safety checks, as the plan's do. Leave `grid_power_entity` empty to turn the guard off.

### SoC integrity and fallback

ha-spark checks `soc_entity` once a minute (see the `soc_*` settings above). It
publishes the result as `sensor.ha_spark_soc_integrity`, whose state is the
operating state and whose attributes give the failure count, the integrity
reason and evidence, the fallback window and current ceiling, and recovery
progress. What you see in that sensor, the log and the action lines:

- **Blocked** (`pending_failure`): from the first failed check, real SoC-based
  programming and charge-rate increases are refused with a `[BLOCKED]` line
  quoting the reason. The program already on the inverter is left as it is,
  and the supply guard can still reduce current.
- **Fallback threshold** (`fallback_threshold`): `soc_failure_threshold`
  consecutive failures without a configured `solis_fallback_current_a`, or
  without a Solis inverter. Nothing new is written. AlphaESS has no fallback
  and refuses new programming while the SoC fails.
- **Verified fallback** (`fallback_verified`, `fallback_confirmed: true`): the
  Solis fallback charging schedule (`charge_window_start`–`charge_window_end`
  at the fallback current ceiling) read back as written, logged as
  `[FALLBACK]`. The supply guard may lower it and restores no higher than the
  ceiling.
- **Failed or unconfirmed fallback** (`fallback_requested` or
  `fallback_failed`, `fallback_confirmed: false`): the write failed or did
  not read back, logged as `[FAILED]`. Increases stay blocked, reductions are
  still allowed, and ha-spark retries each minute. In Observe nothing is
  written and the state stays `fallback_requested`.
- **Recovery** (`recovery_since`, `recovery_ready`): after
  `soc_recovery_minutes` of passing checks, ha-spark applies a fresh plan. The
  fallback remains the reported state until that plan reads back clean, logged
  as `[RECOVERED]`. If it doesn't, the fallback is kept but marked unconfirmed,
  and ha-spark retries each minute.

A computed plan is not a confirmed program. While the SoC is untrusted, the
SoC-dependent plan sensors show `unavailable`, and only a matching read-back
counts as confirmed. The failure count and fallback status survive a restart;
recovery progress does not, so the wait starts over.

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
predictions each run, labelled with `operating_mode`. They are advisory and
never write to hardware.

### V2L (vehicle-to-load, optional)

The pure slot planner can schedule V2L refills when overnight charge and solar
leave a named reservation short, and the car's fill price divided by
`v2l_round_trip_efficiency` is cheaper than grid import for that shortfall.
Refill kWh and rates describe energy delivered into the house battery. Refills
finish before the load they fund, respect battery headroom, run at the preferred
rate (possibly for part of a slot), and share `v2l_budget_kwh` across the plan.
A zero budget disables planning. The explicit `car_plugged_in` planner input
defaults to false; an economic, feasible refill then sets
`v2l_wanted_but_unavailable` instead of scheduling energy. HA plugged-in sensing
and rectifier actuation are separate work; these actions are plan data only.

V2L is a manual physical adapter with no control API, so ha-spark only reads
it. When `v2l_power_entity` is set, each daemon tick reads the car's V2L
discharge power (W), adds it to the kWh delivered this session, values that
energy at the configured rates less conversion losses, publishes
`sensor.ha_spark_v2l_*`, and sends HA notifications.

| Setting | What it must be |
|---|---|
| `v2l_power_entity` | V2L discharge-power sensor (W). Empty (the default) disables the feature. |
| `v2l_round_trip_efficiency` | Charge and discharge conversion losses combined into one efficiency (default `0.85`). ha-spark divides delivered kWh by this to get the energy needed to refill the car. |
| `v2l_peak_rate_gbp` | £/kWh import rate that V2L saves while it runs (default `0.30`). |
| `v2l_offpeak_rate_gbp` | £/kWh cheap rate used to refill the car later (default `0.07`). |
| `v2l_cutoff_time` | Local time the cheap window starts. The unplug notification fires at or after it (default `01:00`). |
| `v2l_notify_service` | Deprecated fallback Home Assistant notify service name without `notify.` (for example, `mobile_app_<device>`), scheduled for removal. `notify_service` takes precedence; this is used only when `notify_service` is blank. |
| `v2l_budget_kwh` | Optional V2L budget in kWh, used in place of car SoC (the car has no HA integration, so ha-spark can't read its SoC). `0` disables refill planning and the plug-in warning. Planned refills and top-up requests never exceed this. |
| `v2l_max_rate_kw` | Maximum planned house-battery refill rate (default `3.0` kW). |
| `v2l_preferred_rate_kw` | Preferred planned refill rate (default `1.5` kW); must be positive and no higher than the maximum. |
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

### Readiness recording

The daemon captures half-hour slot intent and the import, export and event rates
from the plan revision in force on the boundary tick. The ledger includes the
operating mode, forecast model and config generation, battery parameters, checked
start energy, charge-window fraction and current, export, hold intent and V2L
availability. Untrusted hold evidence is recorded as unknown; an untrusted SoC
leaves the energy estimate missing, never zero.

Records are write-once in the existing SQLite database. Mid-slot replans do not
replace them. The first daemon tick in a slot can capture it within the five-minute
boundary tolerance (`BOUNDARY_TOLERANCE`), allowing for tick drift; a missed boundary,
absent plan or missing horizon slot stays a gap,
including after a restart. Guard samples are recorded only inside the charging
window in Observe, with invalid reads retained for coverage. Recording uses reads
only, never changes authority, and failures do not interrupt control-loop work.

`ha-spark readiness slots [--days N]` lists recorded starts and gaps by household-local
day (default: seven days including today, through the current slot). The diagnostic
is read-only and does not require a running daemon or HA connection. Readiness
recording is evidence collection, not permission to change the operating mode.

After each household-local day closes, the daemon joins that day's actual metered
evidence once and stores an immutable day snapshot in the same database: the
measured grid import and export, solar generation, battery charge and discharge
per half-hour (EV charging and V2L only when configured), the gross house load,
the day's start and end battery energy, and the next cheap slot's import rate.
Required components must be configured; an unconfigured EV or V2L counts as a true
zero, while a configured component with no measurement for a slot is recorded as
a substituted zero and excludes the day — never a real zero. An hour measured
only hourly is spread evenly across its two half-hours and flagged. Days with
slot gaps, non-Observe slots or missing boundary evidence are stored excluded
with the reason. Joins are retried while HA's five-minute statistics still exist
(about ten days); a day that could not be joined in time is stored excluded with
that reason. Recording failures are isolated and never interrupt control-loop
work or write hardware.

`ha-spark readiness coverage [--days N]` prints each stored day's provenance
counts (measured, hourly-spread, substituted, absent) and every exclusion
reason, read-only from the database; it never joins a day or creates one.

### Readiness evaluation

`ha-spark readiness show` prints the current verdict, measurements, qualifying
household-local days and excluded days with reasons. It reads existing evidence
only: no HA connection, database creation, notice or authority change.

The daemon evaluates once after the most recent closed day's snapshot is saved.
It uses the **latest qualifying days**, not a fixed calendar span. A day must
have a qualifying measured snapshot, a replayable intent history and a driving
forecast issued before the local day started. Cost replay carries its own battery
energy from the day's actual start and values the ending-energy difference at
the next cheap import rate. Any zero-filled derived-load slot excludes the day.
Forecast MAE compares the driving model with its paired median on identical days,
against measured base load (gross house load less V2L). A model change, any
median-driven day, missing paired median or zero baseline MAE is insufficient
evidence. Missing days are never treated as zero cost or zero breaches.

| Setting | Default | Validation / meaning |
| --- | --- | --- |
| `readiness_window_days` | `21` | Positive integer; latest qualifying household-local days required |
| `readiness_mae_improvement_pct` | `10` | 0–100%; minimum MAE improvement over the median |
| `readiness_max_cost_delta` | `0.0` | Finite GBP; maximum total replay minus incumbent cost |
| `readiness_min_guard_coverage_pct` | `90` | 0–100%; valid guard samples / expected in-window minute ticks |

Passing requires cost delta at or below the threshold, zero would-have-breached
guard events, sufficient guard coverage and the forecast improvement margin.
Measured failures take precedence over insufficient evidence. The first pass
persists a notice flag before sending a notice with the numbers to the configured
notify target (or an HA persistent notification). Further passes do not resend;
only a failing evaluation re-arms it, not insufficient evidence. Delivery or
recording failures are isolated. Readiness is a recommendation: it never changes
the operating mode, authority epoch, holds or hardware behaviour.

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
`fixed`. It needs `octopus_api_key` and `octopus_account_number`. It discovers
the current import tariff and product codes from the account API on each rate
fetch, so a tariff switch is picked up without a restart. Existing
`octopus_product_code`/`octopus_tariff_code` settings are optional manual
overrides; set both to skip discovery. If the account has multiple import meter
points, set `octopus_mpan` in the store to choose one. If that setting
does not match, or discovery fails, rates fall back to the fixed rate/window
and `ha-spark health` reports a warning. `octopus_meter_serial` is only needed
for consumption reads. ha-spark never logs or echoes the API key, account
number, or MPAN.

`export_event_provider` selects an independent export-event source: `none`
(the default) disables event reads, and `axle` adds supervised Axle export
events on top of any `tariff_provider` (`fixed`, `dynamic`, or
`octopus_intelligent`). Import prices and dispatch holds still come from the
selected tariff; a dispatch hold takes priority over an overlapping export
window. `ha-spark health` reports the tariff and export-event providers
separately. Set `axle_api_key` to the
static token from Axle's Home Assistant account page. `axle_event_entity` can
name the Home Assistant mirror entity, which ha-spark reads if the direct
request fails. Set `axle_event_rate_gbp_kwh` to the paid export rate, because
Axle's Home Assistant event response doesn't include one. The provider accepts
only explicit, fresh export windows. Import events and malformed or stale
responses produce no export slots. Enter the API key with
`ha-spark config set-secret axle_api_key`; it lives in the secrets file and
ha-spark never writes it to logs or reports.

**Importing an older `tariff_provider: axle` configuration:** the old `axle`
tariff value has been removed. Change it to `fixed` (or `octopus_intelligent`,
with its required credentials) and add `export_event_provider: axle` in the
saved file before importing it. Other Axle field names are unchanged.

Set `notify_service` to the bare Home Assistant notify service name, such as
`mobile_app_<device>` (without `notify.`), for all notifications, including V2L and
export lifecycle notices sent to the person supervising. ha-spark sends one
notice when it accepts an event, one
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
when the site operating mode authorises clock changes. ha-spark never runs it on its own, except after
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
- `GET /api/config`: the settings service's read view, with revision, field
  metadata, input keys and pending-restart status; secrets are set/unset only.
  In repair it returns reasons and a repair token instead.
- `POST /api/config`: merge `{"expected_revision": 1, "changes": {...}}` through
  the settings service. Returns the new revision, changed field names and
  `live` or `pending_restart`; 400 for sanitised validation reasons, 409 for
  a revision conflict, or 503 in repair.
- `POST /api/config/import`: replace inputs with
  `{"expected": 1, "document": {...}}`, or use the current repair token as
  `expected` in repair. This is the only configuration write accepted in repair.

Both POST endpoints require the ingress proxy's valid `X-Remote-User-Id`,
`X-HA-Spark-Request: config` and `Content-Type: application/json`. Failed request
checks return 403 or 415. All writes use the same validation and save guards;
no settings page is added in this phase.

The config endpoints exist only on ingress. They aren't served on the
published agent port, whatever the token, or on the standalone/dev listener.

### Agent surface

The agent surface is off by default. Set `agent_surface: on` to let an
external model (e.g. Claude, or any OpenAPI-compatible tool client) read
ha-spark's data and, optionally, trigger a few gated actions.

- `agent_surface` (`off` | `on`): the master switch, off by default.
- `agent_exposure` (`read` | `read_act` | `read_write`, default `read_act`):
  how much is exposed. `read` is data only (states, plan, forecast,
  predictions, health). `read_act` adds `add_context` and `run_plan`.
  `read_write` adds `set_config`, which in this version refuses every change
  with `unavailable`: agents can't change configuration until owner-approved
  config proposals exist in phase 14.
- `agent_api_token`: bearer token for the published port. If you leave it
  blank, the add-on generates one on first start and saves it to
  `/data/agent_token` in the add-on container (read it from the add-on shell
  with `cat /data/agent_token`). The token never appears in the add-on log.
  Set your own token with `ha-spark config set-secret agent_api_token`;
  config reads show only set/unset status, never the token.
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

Hardware actions still go through the daemon's site operating mode gate, and the model
never calls `call_service` directly. The deterministic planner still makes
every hardware decision.

## Onboarding

1. **Check the Log tab** after the first start. The add-on runs
   `ha-spark health` and prints one line per check: HA REST, HA WebSocket,
   Ollama, SQLite, load history, supply guard, tariff provider, household
   clock, inverter clock (Solis only), notify service (when configured), and entity config.
2. **Map your entities.** From a shell in the add-on container (e.g. the SSH
   add-on with `docker exec -it addon_<slug> sh`, or the add-on's own
   terminal), run `ha-spark onboard`. It scans your HA entities and proposes
   one for each config field, with the reason it matched and whether it agrees
   with the current setting:
   - `ha-spark onboard --preset solis` fills anything it can't match from the
     reference Solis/Solcast/Octopus/zappi setup.
   - `ha-spark onboard --write` also prints an import-ready YAML settings
     fragment and `ha-spark config import onboard.yaml`. Save only the fenced
     YAML as that file. Import replaces non-secret inputs: merge with an export
     first if you need to retain other settings. The fragment contains no secrets.
   - `ha-spark onboard --json` prints the proposals as JSON for other tools.

   The proposals are advisory. Review them and save with `ha-spark config set`
   or the printed import command; discovery itself never rewrites your config.
3. The load forecast needs hourly household-load history:
   - `ha-spark backfill-load --list` lists statistics you can import. Then
     `ha-spark backfill-load --from <entity_id>` imports one as
     `ha_spark:house_load` history. `ha-spark onboard` tells you when there is
     enough history.
   - Or set the `derive_*_entity` settings (grid import is required) and run
     `ha-spark backfill-load --derive` once. See "Derived base load" above;
     each plan run then keeps the last 48 hours up to date.
4. If `notify_service` is configured, run `ha-spark notify-test` from the add-on
   shell. Confirm that the test notice arrived on the intended device. A
   successful command only confirms Home Assistant accepted the send.
5. `ha-spark plan` prints tonight's plan without applying it.
6. Leave the add-on running; it recomputes the plan every half-hour. When the
   Observe plan looks right, disable conflicting automations, confirm
   the Solis overlay and entity reads are healthy before enabling Optimise.
   Follow the handover procedure linked above.

## Hold evidence freshness and durable ownership

Only fresh EV/dispatch evidence can start a new hold. A confirmed EV hold
survives stale evidence and restarts, and clears after five strictly advancing
fresh not-charging reports. Stale or backwards reports reset that clearance
sequence; equal report times do not advance it. A dispatch hold ends at its
last trusted end; stale updates cannot extend it. Unreadable schedules remain
untrusted, not trusted-empty; dispatch evidence filtering still happens before pricing.

Each configured source has its own maximum age in seconds:

`hold_degraded_minutes` (default **60**, range **5–1440**) controls when a
retained owned hold without fresh evidence raises a degraded notification. It
notifies once per stale episode, including across restarts; fresh evidence
re-arms it. EV hold clearance progress and the last trusted dispatch end are
persisted, and settings reloads do not reset them. A saved request is not a
read-back confirmation. In Optimise, a settings save is refused before persistence
if it would invalidate the fallback charging schedule (including its current,
window or limits). Remapping a held switch retains the original hold and raises
a degraded notice; move to Observe before the replacement mapping can act. A
release is never redirected to the replacement entity.

Owner-Off switches without an owned hold are never
released by hold reconciliation. A release writes the switch On only once the
schedule it hands back to reads back: the resident slot-1 schedule in Protect;
in Optimise this plan applied and verified, or the verified fallback schedule
while fallback is active. Until then the hold stays and a refused line is logged.
`ha-spark health` warns while a release is refused, naming why (switch unreadable
or schedule not verified). After `hold_degraded_minutes` one notification is sent
per episode, including across restarts; a verified release re-arms it.

| Setting | Source | Default |
| --- | --- | --- |
| `ev_status_max_age_seconds` | `ev_status_entity` charging status | 300 |
| `ev_plug_max_age_seconds` | `ev_plug_entity` plug status | 300 |
| `ev_power_max_age_seconds` | `ev_power_entity` car power | 300 |
| `live_dispatch_max_age_seconds` | `dispatch_entity` live state | 300 |
| `adjusted_rate_max_age_seconds` | `octopus_rate_entity` adjusted-rate flag | 300 |
| `dispatch_grid_power_max_age_seconds` | `dispatch_grid_power_entity` grid power | 300 |
| `battery_power_max_age_seconds` | `battery_power_entity` direction evidence | 300 |
| `octopus_dispatch_max_age_seconds` | Octopus API dispatch windows | 2700 |

Settings require `0 < max_age_seconds <= 86400`.
An explicitly unset/`None` limit is **unknown**, with
`no validated max-age; set <option>`; no unmeasured source should acquire a
guessed default.

**How defaults were measured:** read-only sampling of HA `/api/states` every
10 seconds for 30 minutes on the reference household, 2026-10-19
23:00–23:30 UTC. All sampled entities advanced HA's top-level `last_reported`
only when their value changed. EV charger status, plug and charging power
(one charger integration), and supplier live-dispatch/current-rate entities
had zero reports (their previous reports were hours old): change-only sources.
Charger-integration grid power (same device as EV status) had 28 reports,
median gap 61 s, maximum 122 s. Inverter battery power had 53 reports,
median gap 15 s, maximum 165 s. The 300 s power limit is approximately twice
the largest observed gap, rounded. Change-only sources use the same 300 s
limit for sibling liveness. Octopus API windows use ha-spark's own successful
fetch time in `gather_inputs`, on each half-hourly replan; 2700 s (45 min)
is one replan interval plus margin, not a measured HA reporting cadence.

**Configure siblings for change-only sources:** `ev_liveness_entity` covers EV
status, plug and EV power; `dispatch_liveness_entity` covers live dispatch and
adjusted rate. Both default empty. Choose an entity from the **same integration
and device** that reports regularly (for example the reference charger's grid
power). Without a sibling, unchanged values such as “Paused” or dispatch “off”
go stale 300 seconds after each change. Set the sibling rather than assuming
repeated reads establish liveness.

An own report is fresh when present, timezone-aware, non-future and within its
source's limit. Otherwise a recent non-future sibling report within that same
limit proves liveness, provided the own report is known, non-future and no more
than `MAX_UNCHANGED` (12 hours) old. Fresh own reports take precedence. When
liveness comes from the sibling, the **effective report time is the sibling's
report time**; otherwise it is the own report time. Known old reports are stale,
future own reports are future, and missing/unparseable/timezone-less own times
are unknown. Only HA top-level `last_reported` counts: never polling time,
`last_updated`, `last_changed` or attribute timestamps. Re-reading a cached
state preserves its report time and only increases its age.

**Battery direction:** `battery_power_entity` must report an explicit `W` unit.
Set `battery_power_discharge_sign: positive|negative`; there is deliberately no
default. With `positive`, positive W means discharging and negative W charging;
`negative` reverses that mapping. Absolute power below 50 W is idle. Missing
sign, non-finite/malformed power or any non-W/missing unit means unknown
direction. On the reference install the inverter read **−2851 W** while the
house imported about **4.0 kW** against about **1.0 kW** consumption: negative
was charging, so this household sets **`positive`**. Do not infer the sign
from another manufacturer's convention. Direction and time validity are
reported separately; a known direction does not imply fresh evidence.

`python -m ha_spark health` includes an `Evidence <source>` row for each
configured source: fresh/stale/future/unknown, effective report time, age,
maximum age and whether liveness came from the sibling, plus battery direction.
The evidence section is read-only: no hardware or authority-store writes.
For the supplier row it makes its own read-only fetch and reports that fetch's
validity, not the age of the daemon's last plan. The daemon's planner inputs
retain their original `gather_inputs` fetch time.

## Data

The SQLite store lives at the fixed `/data/ha_spark.db` in the add-on and
survives restarts and updates. It holds history, not settings. Standalone/dev
uses bootstrap `DB_PATH` to fix the database and data directory at startup.
`db_path` is not a runtime setting: neither a save nor an import can move the
data directory or database. Settings and secrets live beside it.

Dispatch power evidence: `ev_power_entity` reads direct car draw (W or kW).
`dispatch_grid_power_entity` supplies an independent grid-import reading;
set `dispatch_grid_power_invert: true` for the Solis import-negative meter.
These settings do not enable the supply guard. During an active dispatch,
car draw ≥1.4 kW or grid import ≥3 kW above forecast house load confirms it.
After ten minutes, an otherwise uncorroborated dispatch is released only when
both readable power signals are below those thresholds. Missing or invalid
power evidence retains it; live dispatch, adjusted rate, charging status and
a connected plug retain their existing precedence. Planning and the minute reconcile use the daily-average house forecast
(the latter from the last plan). The Solis preset supplies both sensors and inversion.
