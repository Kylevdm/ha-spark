# #786: `ha_spark:house_load` jumps and the true daily base load

Research for [#786](https://github.com/Kylevdm/ha-spark/issues/786) (map #785).
Investigated on 2026-11-26 against `origin/master` at `3f47900` (0.43.1), plus
read-only queries of the live Home Assistant recorder (`recorder/statistics_during_period`,
`history/history_during_period`, `recorder/list_statistic_ids`), the entity
registry, and the add-on log (`ha apps logs 37491418_ha_spark_addon`, which covers
23–26 Nov only). Nothing was written to HA.

## TL;DR

- **The dates in the question are one day early.** The jumps sit in the UTC day
  buckets that start on **19 Nov** (+55,366) and **25 Nov** (+28,051).
  `get_statistics_range` returns `start = 1795046400` (2026-11-19T00:00Z) and
  `1795564800` (2026-11-25T00:00Z) for those buckets. At hourly resolution there
  are three spikes: **19 Nov 04:00Z +27,661**, **19 Nov 09:00Z +27,667**, and
  **25 Nov 08:00Z +27,985**.
- **Root cause: the ha-spark integration's own native Solis sensors published `0.0`
  for about 30 s after a Modbus reconnect.** Home Assistant read each 0.0 as a
  `total_increasing` meter reset, so when the next poll returned the real value
  (for example 27,794 kWh), it counted the whole lifetime reading as one hour's
  consumption. This happened to all four component totals at the same moment.
  The rolling derived rebuild in the daemon then turned those component
  `change` values into base load faithfully. The code that produced each spike
  is listed under Q1.
- **The fog continuity defect did not cause this.** No hourly row since 11 Nov
  has a negative change. A different continuity break exists on 11 Nov (−1,944
  kWh) and is described under New defects.
- **True base load** (ADR-0001 balance, glitch-free state differences):
  - About **10–18 kWh/day from 7 to 18 Nov**, when the house was **unoccupied**
    (`input_boolean.home_occupied` was off from 10 Nov 13:52Z to 18 Nov 20:19Z).
  - About **33–43 kWh/day from 19 to 25 Nov**, with the house occupied.
  - About **18–36 kWh/day in late October**, from older and less reliable
    sources.
- **The pre-18-Nov ~10 kWh/day was essentially real, not an under-count.** It
  came from an empty house. The statistic misses only about 1–4 kWh/day of
  Growatt PV and adds 0–1 kWh/day of quantisation noise. From 19 Nov the
  statistic **over-counts** because it includes EV charging (`ev_charge` is not
  configured): 80 kWh against a true 39 kWh on 22 Nov.

## Q1: Which write path produced each jump?

### The spikes, hourly (UTC)

From `ha_spark:house_load` hourly statistics:

| Hour (UTC) | state (kWh) | sum after |
|---|---|---|
| 19 Nov 03:00 | 2 | 96 |
| **19 Nov 04:00** | **27,661** | 27,757 |
| 19 Nov 08:00 | 1 | 27,762 |
| **19 Nov 09:00** | **27,667** | 55,429 |
| 25 Nov 07:00 | 2 | 55,746 |
| **25 Nov 08:00** | **27,985** | 83,731 |

Spikes larger than 500 kWh/h in the same hours, across every kWh statistic in
HA since 20 Oct:

| Component statistic | 19 Nov 04 | 19 Nov 09 | 25 Nov 08 |
|---|---|---|---|
| `sensor.solis_inverter_grid_import_total_energy` | +27,794 | +27,794 | +28,068 |
| `sensor.solis_inverter_grid_export_total_energy` | +757 | +757 | +759 |
| `sensor.solis_inverter_battery_discharge_total_energy` | +12,820 | +12,826 | +12,954 |
| `sensor.solis_inverter_battery_charge_total_energy` | +12,196 | +12,196 | +12,278 |
| `sensor.solis_inverter_consumption_total_energy` | +2,322 | +2,322 | +2,336 |

Applying the ADR-0001 formula to those hours reproduces the target rows exactly:

- 27,794 − 757 + 12,820 − 12,196 = **27,661**
- 27,794 − 757 + 12,826 − 12,196 = **27,667**
- 28,068 − 759 + 12,954 − 12,278 = **27,985**

Each glitched component's whole lifetime meter reading was counted as one hour's
flow.

### Why the components spiked: the native Solis sensors briefly read 0.0

Entity history from `history/history_during_period`, minimal response:

```
sensor.solis_inverter_grid_import_total_energy
  2026-11-19 04:24:29Z unavailable
  2026-11-19 04:24:39Z 0.0          <- read as a meter reset
  2026-11-19 04:25:10Z 27791.0      <- HA adds +27,791 to the sum
  ...
  2026-11-19 09:27:50Z unavailable
  2026-11-19 09:28:00Z 0.0
  2026-11-19 09:28:31Z 27794.0
  2026-11-25 08:03:33Z unavailable
  2026-11-25 08:03:43Z 0.0
  2026-11-25 08:04:13Z 28068.0
```

`battery_charge_total_energy` and `consumption_total_energy` show the same
`unavailable → 0.0 → real` pattern at the same timestamps.

- **These sensors belong to ha-spark itself.** The entity registry gives
  `platform: ha_spark`, `unique_id …_1_input_33169`, created 2026-11-11 09:09Z.
- **The registers are defined in `custom_components/ha_spark/solis_frames.py`.**
  - The `input` frame at 33161 (29 words) is declared at `solis_frames.py:31`.
  - Registers 33161, 33165, 33169, 33173 and 33177 are added as u32
    "… total energy" points with `unit="kWh"` at `solis_frames.py:86-89`.
  - Every kWh point except "yesterday" and "last …" gets `state_class`
    `total_increasing` (`solis_frames.py:67-70`).
- **The poller accepts any frame whose words are valid u16s.**
  - `custom_components/ha_spark/native.py:273-276` stores the frame, and
    `valid_words` (`solis_transport.py:135-136`) only range-checks the words.
  - An all-zero frame is therefore accepted, and `decode`
    (`solis_frames.py:128-176`) returns `0.0` for each total.
- **The only "first frames after a reconnect" guard covers SoC alone.**
  - `native.py:282-287` marks the first `SOC_SETTLE_FRAMES = 3` frames of
    `input_33132` as settling.
  - `soc_known` (`solis_frames.py:183-184`) hides only address 33139.
  - This guard is the #765 fix (`e6d1471`). It shows that the inverter is
    already known to return default or zero words just after a new connection.
    The lifetime energy totals in frame `input_33161` are not protected.
  - The `0.0` lasted one frame interval, 30 s (`Frame.interval` default,
    `solis_frames.py:21`), and the following poll was correct. That fits a
    first-frame-after-reconnect artefact.
- **HA then treats the drop as a reset.** For `total_increasing`, a drop to 0
  is a new meter cycle, and the next reading is added in full. The component
  statistics' own `sum` columns are therefore corrupted too.
  `sensor.solis_inverter_grid_import_total_energy` has sum 84,018 against a
  meter reading of 28,068.

### Which ha-spark path wrote the target rows

The **rolling derived rebuild in the daemon** wrote them, not `backfill-load --from`
and not the historical `--derive`:

- **The daemon calls it every plan run.** `ha_spark/energy/scheduler.py:636`
  calls `_run_derived_rerive` (`scheduler.py:646-681`), which calls
  `rerive_trailing_window` (`ha_spark/energy/derived_base_load.py:527-633`).
- **That function rebuilds the trailing 48 h and upserts it.**
  - It fetches each component's hourly `change` from `window_start`
    (`derived_base_load.py:573`, then `_fetch_component` at `:358-382` and
    `build_component_series` at `:315-355`, which reads `change` for kWh units
    at `:338-339`).
  - It applies the balance (`:586-593`, `base_load_balance` at `:117-129`).
  - It upserts with `start_sum` anchored on the latest row before the window
    (`:597-608`).
- **`build_component_series` and `derive_base_load` have no plausibility
  bound.** A +28,068 kWh hour passes straight through (`:347-354` and
  `:184-195`).
- **The add-on log shows the daemon doing this every 30 min.** Lines read
  "Derived base-load rerive: 47 rows upserted (… 48 h window …)".
  - The 25 Nov 08:00 row is still inside that window and is being re-upserted
    with 27,985 every cycle until about 27 Nov 08:00Z.
  - The 19 Nov rows fell out of the window on about 21 Nov. The log, which
    starts 23 Nov, therefore cannot show their writes. The rolling rebuild is
    still the only writer consistent with the timing.
- **A historical `backfill-load --derive` would write the same values.**
  `backfill_derived_load` (`derived_base_load.py:461-524`) reads the same
  component `change`. Re-running it cannot repair the spikes; it reproduces
  them.
- **`backfill-load --from` (`ha_spark/energy/onboarding.py:79-115`) is ruled
  out.** It clamps negatives and copies a single source sensor. None of the
  candidate single sensors has these spikes, and the target values match the
  multi-component formula to the kWh.

### Is it the fog "retained gap rows" defect?

No.

- **That defect is a different failure.** Per `docs/fog.md:66-72`, it can only
  make a regenerated sum fall *below* a retained middle row, which shows up as
  a negative hourly change.
- **There is no negative hourly change after 11 Nov 09:00Z.** The one gap,
  20 Nov 14:00–22:00Z, has no target rows at all, because the grid-import
  component had no statistics for those hours. The 9 h of energy lands in the
  23:00 row (+18 kWh), so no retained row is involved.
- **The fog entry's line references are stale.** It cites
  `derived_base_load.py:487,583`. On current master the import sites are `:502`
  (historical) and `:598` (rolling).

## Q2: True daily base load (ADR-0001 energy balance)

Base = grid_import − grid_export + solar + battery_discharge − battery_charge − ev_charge.

Sources:

- **From 12 Nov:**
  - Grid import/export and battery charge/discharge come from the ha-spark
    native Solis totals, as **day-over-day state differences**. These are
    immune to the reset glitch.
  - Solar is `sensor.growatt_total_energy_production`, the only PV on site:
    the Solis is AC-coupled and `sensor.solis_inverter_pv_total_energy` is
    always 0.
  - EV is `sensor.myenergi_zappi_22300254_energy_used_today`.
- **Up to 10 Nov (marked \*):** the solax_modbus `sensor.solisac_meter_grid_*_total`
  and `sensor.solisac_battery_*_today` statistics. These are less reliable
  because the daily-reset battery counters sometimes disagree with their
  states.
- **11 Nov** has no complete source, because the native sensors were created
  that morning.
- **Octopus import** (smart meter) is a cross-check on grid import. It agrees
  within about 1.5 kWh/day from 12 Nov.
- **V2L** energy enters the battery on the DC side (CONTEXT.md "V2L charger").
  It therefore appears as battery_discharge > battery_charge (+8–14 kWh/day
  from 19 Nov) and is correctly counted as house consumption.

| Day (UTC) | Grid in | Grid out | PV (Growatt) | Batt dis | Batt chg | EV (Zappi) | **Base (ADR-0001)** | Base incl. EV | Octopus import | `house_load` change |
|---|---|---|---|---|---|---|---|---|---|---|
| Mon 26 Oct* | 20.4 | 0.0 | 0.7 | 12.7 | 12.7 | 0.0 | **21.1** | 21.1 | 21.0 | – |
| Tue 27 Oct* | 22.9 | 0.0 | 3.2 | 12.6 | 13.8 | 0.0 | **24.9** | 24.9 | 23.6 | – |
| Wed 28 Oct* | 21.7 | 0.0 | 2.3 | 16.0 | 9.4 | 4.1 | **26.5** | 30.6 | 22.6 | – |
| Thu 29 Oct* | 26.9 | 0.0 | 2.5 | 15.0 | 15.8 | 3.4 | **25.2** | 28.6 | 27.7 | – |
| Fri 30 Oct* | 46.0 | 0.0 | 2.9 | 11.6 | 16.2 | 26.3 | **18.0** | 44.3 | 47.1 | – |
| Sat 31 Oct* | 17.3 | 0.2 | 1.5 | 15.0 | 11.8 | 0.0 | **21.7** | 21.7 | 17.8 | – |
| Sun 01 Nov* | 34.9 | 0.0 | 1.4 | 21.4 | 21.6 | 0.0 | **36.1** | 36.1 | 35.9 | – |
| Mon 02 Nov* | 34.0 | 0.0 | 2.0 | 21.5 | 21.6 | 0.0 | **35.9** | 35.9 | 34.8 | – |
| Tue 03 Nov* | 32.6 | 0.0 | 2.4 | 15.4 | 22.1 | 0.0 | **28.3** | 28.3 | 33.6 | – |
| Wed 04 Nov* | 21.6 | 0.0 | 0.9 | 22.1 | 10.9 | 0.0 | **33.7** | 33.7 | 22.3 | – |
| Thu 05 Nov* | 31.2 | 0.1 | 0.3 | 24.6 | 18.7 | 3.1 | **34.2** | 37.3 | 32.2 | – |
| Fri 06 Nov* | 45.8 | 0.0 | 1.3 | 6.7 | 18.6 | 16.4 | **18.8** | 35.2 | 47.1 | – |
| Sat 07 Nov* | 13.0 | 0.0 | 1.0 | 7.5 | 9.9 | 0.0 | **11.6** | 11.6 | 13.5 | – |
| Sun 08 Nov* | 14.3 | 0.0 | 1.1 | 7.2 | 7.7 | 0.0 | **14.9** | 14.9 | 14.7 | – |
| Mon 09 Nov* | 10.3 | 0.0 | 0.8 | 7.8 | 7.6 | 0.0 | **11.3** | 11.3 | 10.9 | – |
| Tue 10 Nov* | 9.0 | 0.0 | 0.4 | 3.6 | 6.6 | 0.0 | **6.4** | 6.4 | 11.3 | – |
| Wed 11 Nov | – | – | 1.1 | – | – | 0.0 | **–** | – | 11.4 | −1943.9 |
| Thu 12 Nov | 10.0 | 0.0 | 3.9 | 6.0 | 9.0 | 0.0 | **10.9** | 10.9 | 10.4 | 10.0 |
| Fri 13 Nov | 8.0 | 0.0 | 3.5 | 7.0 | 7.0 | 0.0 | **11.5** | 11.5 | 8.1 | 10.0 |
| Sat 14 Nov | 7.0 | 0.0 | 1.9 | 7.0 | 5.0 | 0.0 | **10.9** | 10.9 | 8.4 | 9.0 |
| Sun 15 Nov | 16.0 | 0.0 | 0.9 | 7.0 | 6.0 | 0.0 | **17.9** | 17.9 | 15.6 | 17.0 |
| Mon 16 Nov | 10.0 | 0.0 | 2.3 | 7.0 | 9.0 | 0.0 | **10.3** | 10.3 | 11.5 | 9.0 |
| Tue 17 Nov | 8.0 | 0.0 | 1.5 | 7.0 | 5.0 | 0.0 | **11.5** | 11.5 | 8.0 | 11.0 |
| Wed 18 Nov | 11.0 | 0.0 | 2.0 | 12.0 | 7.0 | 0.0 | **18.0** | 18.0 | 11.8 | 16.0 |
| Thu 19 Nov | 26.0 | 0.0 | 1.1 | 25.0 | 11.0 | 3.5 | **37.6** | 41.1 | 25.9 | 55,366 |
| Fri 20 Nov | 47.0 | 0.0 | 0.7 | 22.0 | 14.0 | 12.8 | **42.9** | 55.7 | 48.5 | 55.0 |
| Sat 21 Nov | 32.0 | 2.0 | 3.0 | 23.0 | 18.0 | 0.0 | **38.0** | 38.0 | 33.8 | 35.0 |
| Sun 22 Nov | 74.0 | 0.0 | 0.6 | 18.0 | 12.0 | 41.2 | **39.4** | 80.6 | 75.5 | 80.0 |
| Mon 23 Nov | 41.0 | 0.0 | 1.0 | 22.0 | 14.0 | 9.2 | **40.8** | 50.0 | 42.1 | 49.0 |
| Tue 24 Nov | 20.0 | 0.0 | 1.0 | 20.0 | 8.0 | 0.0 | **33.0** | 33.0 | 20.0 | 32.0 |
| Wed 25 Nov | 59.0 | 0.0 | 0.6 | 26.0 | 18.0 | 24.3 | **43.3** | 67.6 | 60.7 | 28,051 |

### Reading the table

- **Was 12–18 Nov an under-count?** Only slightly: about 1–4 kWh/day of
  Growatt PV is missing, partly offset by negative-hour clamping. The low level
  was real: `input_boolean.home_occupied` was `off` from 10 Nov 13:52Z to
  18 Nov 20:19Z. About 0.45 kW for an empty house matches the CONTEXT.md
  away-level note of about 300 W, plus heating.
- **Occupied base load is about 33–43 kWh/day from 19 to 25 Nov.** The late
  October and early November occupied days were about 18–36 kWh/day, from the
  older and less reliable sources.
- **What the statistic actually holds is `gi − ge + bd − bc`.** It matches the
  "Base incl. EV" column minus PV to within rounding. Two configuration gaps
  follow (inferred, because the settings store in the add-on's `/data` is not
  readable from here and the add-on options show every `derive_*` blank, so
  the values come from the ha-spark config store):
  - **`ev_charge` is not configured.** The add-on log says "component
    'ev_charge' not configured — omitted, treated as zero". Zappi charging is
    therefore counted as base load, which breaks ADR-0001: 22 Nov reads 80
    against a true 39.
  - **`solar_generation` points at a zero-valued PV statistic, probably
    `sensor.solis_inverter_pv_total_energy`, not the Growatt.** For example,
    13 Nov 10:00Z gives `house_load` = 1 = bd 1 + gi 0, with Growatt +0.3 kWh
    ignored. Every target row is a whole number, which a 0.1-kWh Growatt term
    would prevent.
- **The hourly shape is quantised.** The native Solis u32 totals have 1-kWh
  resolution (`solis_frames.py:86-89`, scale 1), so derived hours are 0/1/2 kWh
  steps. Integer per-component steps also trigger the negative clamp in
  `derive_base_load` (`derived_base_load.py:192-194`), which biases the sum
  upward. Daily totals stay roughly right (±1–2 kWh), but the hourly profile
  that the slot forecaster learns from is mostly noise.

## New defects found

1. **The native Solis energy totals publish 0.0 on the first frame after a
   reconnect.** HA treats it as a meter reset and books the lifetime reading as
   one hour.
   - Code: `custom_components/ha_spark/native.py:273-289` and
     `solis_frames.py:67-70,86-89,183-184`.
   - Effect: it corrupts the component statistics, `ha_spark:house_load`, and
     anything else that reads hourly `change`. The energy ledger backfill,
     `ha_spark/energy/ledger_backfill.py`, uses the same
     `derive_specs_from_settings`; it is likely affected but was not verified.
   - Fix direction: extend the #765 settling to frame `input_33161` (and
     `input_33029`), or never publish a `total_increasing` value below the last
     published one on the same connection.
2. **`build_component_series` and `derive_base_load` have no plausibility
   bound** (`derived_base_load.py:347-354, 184-195`). A component hour of
   +28,068 kWh is accepted, although the site supply limit is 80 A ×
   240 V ≈ 19 kWh/h (add-on options `supply_max_current_a`,
   `supply_voltage_v`). This is defence in depth for item 1 and for any other
   integration's reset glitch.
3. **The cumulative sum restarted at 0 on 11 Nov 09:00Z (−1,943.9 kWh in one
   day).**
   - The target already held a May–Sep 2026 source-entity history ending
     3 Sep at sum 1,950.874. The derived series began at sum 0.
   - Either derived path does this:
     - The rolling rebuild anchors only on rows within 7 days before its
       window (`derived_base_load.py:563-571`), so an older final row is
       ignored and `start_sum` falls back to 0 (`:597`).
     - The historical `--derive` always starts at `start_sum=0`
       (`derived_base_load.py:502`) while leaving older rows from the other
       path in place.
   - The logs start on 23 Nov, so they cannot show which path wrote the first
     row.
   - This belongs with the fog "cumulative continuity" entry, but it is a
     distinct trigger: retained rows *before* the derived coverage, not
     retained gap rows.
4. **Configuration (owner action, not code):** set `derive_ev_charge_entity` to
   the Zappi energy statistic, and set `derive_solar_generation_entity` to
   `sensor.growatt_total_energy_production`.

Repairing the stored history (the three spike rows, the component sums, and the
11 Nov break) needs a recorder write such as `recorder/adjust_sum_statistics`
or a corrected re-import. That was out of scope here because the work was
read-only. Note that a `backfill-load --derive` run today would reproduce the
spikes, because the corruption sits in the component statistics.

## Confidence

- **High** that the native-sensor 0.0 glitch is the root cause and that the
  rolling rebuild was the writer. The state history timestamps and the exact
  arithmetic match three times out of three.
- **Medium** on which entities are configured for `solar_generation` and
  `ev_charge`. This is inferred from the log and from the arithmetic, not read
  from the settings store.
- **Medium** on the October daily figures, which come from the older solax
  daily-reset battery counters.
