# Research: full solis_modbus parity — the entity gap and what parity means

Issue: [#312](https://github.com/Kylevdm/ha-spark/issues/312), part of map [#247](https://github.com/Kylevdm/ha-spark/issues/247).
Date: 2026-10-14.

This note decides nothing. It measures the gap between what ha-spark's
integration needs (the "step 1" slice from #249) and everything
`Pho3niX90/solis_modbus` creates. It ranks that gap by how often households
use each part, and lists the choices that define "parity".

## Sources and provenance

The ranking from #80 and #249 applies: **(A)** official Solis, Waveshare or
Home Assistant source and docs, **(B)** integration source code, **(C)**
third-party consumers of the entities, **(L)** this repo's own work.

- **(B) solis_modbus** at
  [`98ef3cd`](https://github.com/Pho3niX90/solis_modbus/tree/98ef3cd1b4d7c24eeb75b7e356df2e87ef545282)
  (2026-10-12, manifest version 4.4.0). Paths below are relative to
  `custom_components/solis_modbus/` at that commit. Entity counts come from
  parsing `sensor_data/*.py` as data, without running it.
- **(A) Home Assistant Core** at
  [`cc20697`](https://github.com/home-assistant/core/tree/cc206975bb1ca16a0fbe2e37077f30aca2ea84d6)
  (`dev`, 2026-10-14):
  - `homeassistant/components/energy/data.py`, the Energy dashboard's source
    schema;
  - `homeassistant/components/recorder/entity_registry.py` and
    `recorder/table_managers/statistics_meta.py`, which cover statistics
    continuity across an entity rename.
- **(C) Consumers**, used as a proxy for "commonly used":
  - [sunsynk-power-flow-card `docs/examples/solis.rst`](https://github.com/slipx06/sunsynk-power-flow-card/blob/43d2a8039400d7f739cb17ad9d612755b3013b3e/docs/examples/solis.rst),
    whose Example 2 is wired to solis_modbus entities. It is a popular
    dashboard card.
  - Predbat's Solis templates,
    [`templates/ginlong_solis.yaml`](https://github.com/springfall2008/batpred/blob/9bbe3f75b8275781dd7418237444fb9ab6154b18/templates/ginlong_solis.yaml),
    and its
    [inverter setup doc](https://github.com/springfall2008/batpred/blob/9bbe3f75b8275781dd7418237444fb9ab6154b18/docs/inverter-setup.md#solis-inverters-fb00-or-later).
    Predbat is the most widely used battery-automation consumer. Its Solis
    templates target **solax-modbus**, but they read the same registers.
  - solis_modbus's own **Essential** poll profile. This is the maintainer's
    curated read-only subset "to reduce datalogger load" (`const.py:78-81`).
- **(L)** [`solis-modbus-register-map.md`](solis-modbus-register-map.md) (#249)
  and [`integration-entity-contract.md`](integration-entity-contract.md)
  (#308), plus the map #247 decisions they cite.

**What this note cannot measure.** No public usage telemetry exists for
individual solis_modbus entities. The "how common" tiers below are an
**inference** from the three consumer proxies and the Energy dashboard schema.
They are not counts of real installs.

## Answer in brief

1. **The gap is bigger than "~420 entities".** solis_modbus's hybrid map
   defines **393 register-backed sensors** in 50 read groups. Of these, 72 are
   also `number` entities. It also defines **15 derived sensors**, **71
   switches**, **4 selects**, **44 `time` entities** and **9 services**. That
   is about **530 distinct entities** plus the services, before feature
   gating. #249's "~420" counted register rows. ha-spark's step 1 covers 9 of
   the 50 read groups.
2. **Most of the commonly used gap costs very little to close.** Ranked by
   use, the gap splits into four tiers:
   - **A: Energy dashboard sources** (4 frames, 37 sensors);
   - **B: common dashboard and automation entities** (3 frames, 26 sensors,
     plus the storage-mode controls);
   - **C: occasional or power-user entities** (19 frames, 188 sensors);
   - **D: hardware-specific or installer entities** (15 frames, 60 sensors).

   A and B together add **7 frames**, about **0.5 requests/s** at
   solis_modbus's default cadence. ha-spark's own step 1 is about 0.3–0.5
   requests/s. **A and B are a superset of solis_modbus's own Essential
   profile.**
3. **The one gap a household will see straight away is the Energy dashboard.**
   Step 1 reads no lifetime energy counters. If step 1 ships without tier A,
   the grid, solar and battery statistics stop for the whole gap. That leaves a
   permanent hole in long-term statistics. Card and automation gaps can be
   recovered later. Statistics gaps cannot.
4. **"Parity" should mean the read surface plus a short, deliberate write
   list, not every write.** solis_modbus offers about 200 write points:
   - 72 numbers, 71 switches, 4 selects and 44 times;
   - 3 raw-register services (`solis_write_holding_register`,
     `solis_write_time`, `solis_read_register`).

   Under ADR-0008, each write entity must be mode-gated, limit-checked, read
   back and logged. The device export bound is **read-only by decision**
   (#283). Default Assist exposure would cover a `switch` copy of all 71
   switches (#308 D7). The raw-register services cannot be parity: they
   bypass every one of those gates.
5. **The 43135 bench test is needed only if RC force is in parity.** None of
   the consumer proxies uses RC force. Predbat drives timed slots, and so does
   ha-spark's manual override (#255). RC force falls in tier C, so leaving it
   out of v1 parity removes the bench test from v1.
6. **Firmware coverage.** Parity for battery households means the **hybrid
   map**. That covers the S6-EH1P/EH2P/EH3P/EO1P, S5-EH1P/EO1P, RHI-*, RAI and
   S6-EA1P families. It also means V1 and V2 TOU (33289), the protocol Ver3.4
   blocks (Remote Dispatch, the 33200 mirrors, EPM state), and per-install
   feature sets. The grid/string map (S5-GR*, S6-GR1P, S5-GC, 4G; 68 sensors)
   is a separate question: those inverters have no battery.

## 1. What solis_modbus creates

### 1a. Entity inventory (hybrid map)

| Platform | Count | Source | Notes |
|---|---|---|---|
| `sensor`, register-backed | 393 (409 rows including `reserve`) | `sensor_data/hybrid_sensors.py`; created for every non-reserve row (`sensor.py:29-32`) | 50 read groups. 14 rows are `hidden` raw bitfields behind switches. |
| `number` | 72 | rows with `editable` (`number.py:24-27`) | The same registers also appear as sensors. |
| `sensor`, derived | 15 | `hybrid_sensors_derived` (`hybrid_sensors.py:4593`) | Per-string PV power, battery charge, discharge and net power, net grid power, net grid energy today, status string, power factor, last Modbus success, last clock adjustment, and similar. |
| `switch` | 71 (hybrid) | `sensor_data/switch_sensors.py` | 43110 bits (12), V2 TOU period bits (12), 43302 auxiliary flags (9), 43249 protections (8), 43483 hybrid functions (8), generator (14), and others; see §3. |
| `select` | 4 | `sensor_data/select_sensors.py` | Storage Mode (43110), RC Grid Adjustment (43132), RC Force (43135), Battery Model (43009). |
| `time` | 20 V1 + 24 V2 | `sensor_data/time_sensors.py:7-58` | V1 covers slots 1–5. V2 covers six charge and six discharge slots, and needs feature V2 (`time_sensors.py:34`). |
| services | 9 | `services.yaml` | Raw write, write-time and read; RC force charge, discharge and stop; dispatch, dispatch stop and dispatch schedule. |

Feature gating trims some of these at setup (`__init__.py:681-686`). The
config flow's defaults are `has_v2=True`, `has_generator=True` and
`has_epm=True` (`config_flow.py:90-95`; `data/solis_config.py:99-111`). So a
default install creates the V2 TOU entities and the generator entities even
on hardware that has neither.

### 1b. solis_modbus's own subsets

- **Essential profile** (`helpers.py:284-288`): groups flagged `essential`.
  These are identity (33000, 35000), clock (33022), PV energy (33029), PV live
  (33049), AC and status (33070), the battery and load frame (33132) and the
  energy totals (33161) (`hybrid_sensors.py:22, 82, 96, 154, 267, 365, 653,
  895`). Under any reduced profile, **no numbers, switches, selects or times
  are created** (`helpers.py:243-250`; `number.py:19-22`). The maintainer
  treats this set as the useful read-only core.
- **Extreme profile**: identity, PV live and meter/CT only, with optional
  battery and Smart Port frames. It exists for fast export-control loops
  (`hybrid_sensors.py:604, 652, 1248`; README "What's new in 4.2.0").

## 2. Classifying the gap

### 2a. What counts as "commonly used"

| Evidence | What it uses (solis_modbus registers) |
|---|---|
| **Energy dashboard** (A). `energy/data.py:109-180`: a grid source needs `stat_energy_from` and `stat_energy_to` (kWh) plus an optional `stat_rate` (power). A solar source needs `stat_energy_from` and an optional `stat_rate`. A battery source needs `stat_energy_from` and `stat_energy_to`, plus an optional `stat_rate` (or a two-sensor `power_config`, `:61-79`) and `stat_soc`. | Grid import and export lifetime 33169 and 33173 (or meter 33283 and 33285). PV lifetime 33029 and PV power 33057. Battery charge and discharge lifetime 33161 and 33165. Battery net power (derived from 33149 and 33135), grid power (33130, 33263 or 33151) and SoC 33139. Household consumption lifetime 33177. |
| **sunsynk-power-flow-card, solis_modbus example** (C), `solis.rst:66-143` | Daily battery charge and discharge, consumption, grid import and export, and PV (the 33161 and 33029 frames). Phase A voltage and current, grid frequency and temperature (33070 frame). Backup load power, AC grid port power, battery voltage, current, power, SoC and current direction (33132 frame). Meter total active power (33251 frame). DC voltage, current and power for strings 1–2 (33049 frame plus derived). Current status (33095 plus derived). |
| **Predbat Solis templates** (C), `ginlong_solis.yaml`; inverter-setup "FB00 or later" | Daily load, import, export and PV. RTC, battery power, PV power, house load, backup load and SoC. Backup/reserve SoC (43024) and minimum SoC (43011). Timed-charge current and slot times (43141–43150, or V2 slots on FB00 firmware). Storage-control mode (43110). Meter active power for grid power. |
| **solis_modbus Essential profile** (B), §1b | The 33000, 35000, 33022, 33029, 33049, 33070, 33132 and 33161 groups. |

### 2b. The tiers

Frame cost uses solis_modbus's grouping (one read frame per group) and its
default cadence: fast 10 s, normal 15 s, slow 30 s
(`config_flow.py:80-82`; #249 recorded fast 5 s at an older commit). The
protocol floor is one frame per 300 ms, about 3.3 frames/s, shared by every
client (#249 §3).

| Tier | Read groups (register start) | Frames | Sensors (writable) | Req/s | Who uses it |
|---|---|---|---|---|---|
| **Step 1** (ha-spark's own, #249) | 33000, 35000, 34502 (once); 33022; 33132–33157; 33289; 43007–43028; 43110–43111; 43141–43170 | 9 | 82 (14) | 0.30 | ha-spark. In the same frames: household load, backup load, battery current and direction, SoH, BMS limits, AC grid port power, battery floors 43010–43028. |
| **A: Energy dashboard** | 33161–33189 (slow); 33029–33040 (slow, PV); 33049–33058 (fast, PV); 33126–33131 (fast, meter) | 4 | 37 (0) | 0.27 | Every household that uses the Energy dashboard, plus the daily totals that cards and Predbat read |
| **B: common dashboard and automation** | 33070–33096 (AC, temperature, status); 33116–33123 (fault and operating bits); 43117–43118 (battery max charge and discharge current) | 3 | 26 (2) | 0.23 | Power-flow cards, status and alarm automations, Predbat |
| **C: occasional or power user** | Export limit 43073–43074; RC block 43128–43137 and 43282; slots 4–5 (43173–43190); V2 TOU 43707–43791 (2 groups); peak shaving 43487–43488; flexible export 43291–43292; EPM and parallel state 33243–33250; per-phase meter 33251–33286; per-phase grid port 33512–33518; load energy totals 33580–33596; mirrors 33200–33217; Remote Dispatch 34504 and 44100–44112; output clamp 43070 and 43081; PV shutdown 44280; export calibration 43195 | 19 | 188 (51) | 0.80 | Export-limit automations, RC and dispatch scripting, three-phase detail, owners of V2 firmware. **V2 firmware moves 43707–43791 into step 1** (#249 §2a). |
| **D: hardware-specific or installer** | BMS max inverter current 33041; generator (33530, 43361–43369, 43340, 43815); dual meter (33300, 33316); parallel (34243); Smart Port, S6 only (34328, 34391); AC coupling (34445, 34496); special settings and protections 43249; auxiliary flags 43302; hybrid functions 43483 | 15 | 60 (5) | 0.83 (0.33 for a typical feature set) | Installs with that hardware; installer commissioning |

The non-sensor platforms tier as follows:

| Entities | Tier |
|---|---|
| `time` slots 1–3 (12) and the power switch (43007) | Step 1. These are ha-spark-owned or protection-owned controls (#308 C10). |
| Storage Mode select and the 12 storage-control (43110) switches | **B**. Predbat writes 43110 and cards show it. ha-spark reads 33132 and never writes 43110 (#249 §1b). |
| Derived PV, battery and grid power; net grid energy; status string | A or B. They depend only on registers from step 1, A or B. |
| `time` slots 4–5 (8), V2 TOU times (24) and period switches (12) | C, or step 1 on V2 firmware |
| Export-limit switch, output-limit gate, backup supply, PV shutdown, RC Force select and 2 RC switches, RC Grid Adjustment | C |
| Generator (14), 43302 (9), 43249 protections (8), most 43483 bits, flexible export (SAPN only), Battery Model select | D |
| `solis_force_battery_*`, `solis_dispatch*` services | C |
| `solis_write_holding_register`, `solis_write_time`, `solis_read_register` | Excluded (§3c) |

**Full parity for a typical single-phase LV hybrid** comes to about 41 read
groups and about 1.9 requests/s at the default cadence. That fits under the
protocol floor with room to spare, but only if the integration is the **sole**
client (#249 §4).

### 2c. What the household loses in the step-1 → step-2 gap

This assumes step 1 exposes only the #249 slice and solis_modbus is
uninstalled.

| Lost | Effect | Recoverable later? |
|---|---|---|
| **A: lifetime and daily energy counters** | Energy dashboard grid, solar and battery flows stop; Predbat and card daily totals go blank | **No** for long-term statistics during the gap. Live entities come back in step 2. |
| PV live (string V/I/P), meter power | Solar power and CT readings on cards. Grid power for the Energy dashboard's `stat_rate` (unless 33151 from step 1 stands in, which is the AC grid port, not the grid connection) | Yes |
| **B: inverter status, temperature, phase V/I/frequency, fault bits** | Status and alarm cards; fault-notification automations | Yes, but alerting is lost during the gap |
| Storage Mode select and 43110 switches; battery floors as `number`s (43011, 43024, 43018); max currents | Automations that change mode or reserve SoC (Predbat-style). Manual control from dashboards | Yes. In Optimise, many of these would conflict with ha-spark's authority anyway (#308 C10). |
| C and D | Niche controls and detail | Yes |

Two cheap mitigations follow directly. Both are options, not decisions:

- **Expose everything in step 1's own frames.** Step 1 already reads
  33132–33157 and 43007–43028. Exposing those registers as entities costs no
  extra frames. It covers household load, backup load, battery current and
  direction, SoH, BMS limits, AC grid port power and the battery floors.
- **Add tier A (4 frames) to step 1**, and B (3 frames) if budget allows. That
  closes the Energy dashboard gap and covers the whole Essential profile,
  for about 0.5 more requests/s.

A third route keeps solis_modbus running on its **Essential** profile through
the gap. It is read-only: no write entities are created (`helpers.py:243-250`).
It polls 8 groups, about 0.4 requests/s. This breaks the sole-client rule
(#249 §4) for the length of the gap, and the gateway's multi-host arbitration
is undocumented. It also needs the gateway's caching mode off (#259, #282).

### 2d. Keeping Energy dashboard history across the swap

For sensors, the recorder's long-term statistics are keyed by `entity_id`. When
an entity is renamed, the recorder moves its statistics to the new id
(`recorder/entity_registry.py:28-35`). It refuses that move if the target id
already has statistics, and logs "already in use"
(`statistics_meta.py:385-392`).

The practical consequence, which is an inference: after removing the old
entity, the new integration's entity takes over the old `entity_id` and
continues the old series. Its own short pre-rename statistics are not merged.
Because both integrations read the **same lifetime registers** (for example
33169 and 33173), the `total_increasing` value carries on without a step. This
only helps if tier A exists by the time solis_modbus is removed. solis_modbus's
README gives owners the same manual procedure: rename the new entity to the old
id under Developer Tools → Statistics (README "Restoring Sensor History"). It
also recommends utility meters over its own today/yesterday registers, because
of clock skew at midnight (README "Total Sensors").

## 3. What "parity" should mean

### 3a. Every entity vs the set owners use

Every entity means about 530 entities and about 200 write points, many of them
gated on hardware most installs lack. solis_modbus itself creates generator
and V2 entities by default on hardware without them (§1a). Probing by
illegal-address bisection (`data_retrieval.py`, #249 §3) and reading identity
registers (#249 §5) would let the integration create only what the hardware
answers. That is a quality improvement over solis_modbus, not a parity
requirement.

A defensible parity line is:

- **Reads**: step 1 + A + B + C for the detected hardware. D only when its
  feature is detected.
- **Writes**: a named list, decided per control (§3b).

### 3b. Which write-capable entities count

The settled constraints that bear on this:

- **Mode-gated writes** (`0008:61`; #308 C10). Every device write entity must
  be refused per mode, validated against limits, read back and logged. Each
  added write entity carries that cost. ha-spark-owned controls are refused in
  Optimise, and protection-owned controls in Protect.
- **Device export bound is read-only** (#283 d2; #308 C17). solis_modbus's
  "Grid feed in power limit switch" (43073 bit 4) and "Backflow Power" number
  (43074) **cannot** be writable in parity without reopening #283. Owners who
  set the export limit from HA today lose that path by decision.
- **Assist default exposure** (#308 §3; Core `exposed_entities.py:32-44,
  304-316`). A `switch` is exposed to Assist by default unless it has an
  `entity_category` or is hidden. A direct copy of solis_modbus's 71 switches
  would put "Off-Grid Mode", "Inverter Power (On/Off)", the 43249 protection
  bits and others into Assist. Parity write entities need `entity_category:
  config`, or a non-switch platform. This feeds #308 D7.
- **Installer and protection settings.** Battery Model, float and equalise
  voltage, rated capacity, 43249 protections and grid-code entries are
  commissioning settings. A wrong write can damage hardware or break grid
  compliance. Making them read-only diagnostics in parity is a reasonable
  option. It is a security-first default, flagged here rather than decided.

On that basis, a candidate parity write list (to be decided) is:

- Storage Mode and its 43110 bits, using solis_modbus's read-modify-write and
  conflict rules (`select_sensors.py:3-9, 66-83`);
- battery floors 43011, 43018 and 43024, and max SoC 43010;
- max charge and discharge current 43117 and 43118;
- backup supply 43111;
- V1 slots 4–5 and the V2 TOU slots (V2 slots are needed in step 1 on V2
  firmware);
- peak shaving 43487 and 43488.

Excluded or deferred:

- export limit (read-only by decision);
- RC force and RC grid power (§3d);
- Remote Dispatch and PV shutdown (future control surface, #249 §2e);
- the 43070/43081 output clamp;
- installer and protection settings;
- flexible export (SAPN only; solis_modbus #499 notes it can stick).

### 3c. Services

The raw services `solis_write_holding_register`, `solis_write_time` and
`solis_read_register` (`services.yaml:1-107`) let any HA caller write any
holding register. That bypasses the mode gate, limits, read-back and audit.
They conflict with CLAUDE.md's actuation invariants and with ADR-0008, so
they **cannot** be parity. Owners who script raw writes lose that ability by
design. The RC-force services (`services.yaml:108-200`; `__init__.py:103-108,
359-384`) and the dispatch services (`services.yaml:201-…`) depend on §3d and
on Remote Dispatch.

### 3d. The 43135 bench test

- solis_modbus exposes RC force through a select, two switches and three
  services. All of them write 43135 first, then 43136/43129 and 43282 (#352
  ordering, `__init__.py:103-108, 359-384`).
- None of the consumer proxies uses it. Predbat drives timed slots and
  storage mode. The power-flow card is read-only. ha-spark's manual override
  is a planner instruction executed through slots (#255). ADR-0003 demoted
  RC.
- In Optimise, an RC force from another caller would fight the plan. It would
  need to be an ha-spark-owned control, refused in Optimise (C10), so it would
  be usable only in Observe, Protect or a manual session.

**Conclusion:** parity needs the 43135 test only if v1 parity includes RC
force. As tier C with no consumer evidence, it can be deferred past v1. The
test then stays in fog until someone asks for RC force. If it is included,
the test also has to settle the ordering variance (#249 §2b).

## 4. Firmware and hardware coverage

| Dimension | What solis_modbus supports | What parity implies |
|---|---|---|
| Model families | **HYBRID**: S6-EH1P, S6-EH2P, S6-EH3P, S6-EO1P, S6-EH3P10K-H-ZP (Zonneplan), S5-EH1P, S5-EO1P, RHI-*, RHI-1P/3P, RAI-3K-48ES-5G, generic WAVESHARE. **ENERGY** (also uses the hybrid map): S6-EA1P, RAI-*. **GRID/STRING** (separate 3xxx map, 68 sensors): S6-GR1P, S5-GR1P/GR3P, S5-GC, 3P(3-20)K-4G, 1P(2.5-6)K-4G (`data/solis_config.py:114-178`; `__init__.py:628-634`). README "Tested" lists S6-EH3P, S6-EH1P, S5-EH1, RAI-3K-48ES-5G, RHI-3K-48ES-5G and others. | The hybrid map for battery households. Whether the integration replaces solis_modbus on **grid-tie string inverters** (for example an AC-coupled site's PV inverter) is open: map #247 says "replaces Solis (all of it)". |
| Model selection | Chosen by the user in the config flow, not detected (`config_flow.py:88`) | Detect from 33000/35000 (#249 §5). A wrong pick is a known solis_modbus failure mode. |
| Feature flags | User toggles: PV, battery, HV battery, generator, V2, AC coupling, parallel, dual meter, EPM (`config_flow.py:90-95`) | Probe where possible: 33289 for V2, 34502 for dispatch, illegal-address exceptions for absent blocks. Ask only for what can't be probed (generator, dual meter). |
| Protocol versions | Base map "RS485_MODBUS RTU Hybrid Inverter Protocol Ver3.2" (`hybrid_sensors.py:17`). Ver3.4 additions: 33186–33189, 33200–33217, 33246–33250, 33289, 34502–34504, 44100+ (README 4.2.0 notes) | Ver3.4 blocks must tolerate exception 2 on older firmware |
| TOU generation | V1 (43110 bit 1 + 43143..) or V2 (`33289 == 0xAA55` → 43707–43791) (`const.py:41-46`) | **Step 1 on V2 firmware needs V2.** The reference site is V1 (#259). |
| Transports | TCP (S2-WL-ST datalogger, Waveshare, EW11), serial RS485, ESPHome serial proxy (README "Configuration"; `config_flow.py:74`) | Map #247's design assumes a TCP gateway. Serial and USB households would be a parity gap unless the transport is in scope. Some datalogger firmware has stopped serving port 502 (README "Troubleshooting"). |

## 5. Relevance to the two-step cutover

- **Lowest-regret step 1** (an option): the #249 slice, plus every register in
  the frames it already reads, plus tier A. That is about 13 frames and well
  under 1 request/s. It keeps the Energy dashboard series continuous. With
  tier B added it covers solis_modbus's whole Essential profile, so most
  dashboards and Predbat-style read automations keep working.
- **What the household then waits for in step 2:** the tier B and C writes
  (storage mode, battery floors, slots 4–5, peak shaving), three-phase and
  meter detail, and the tier D hardware groups. RC force, export-limit writes
  and the raw services are not "waiting". They are out by decision or by
  design.
- **The reference household runs solax-modbus, not solis_modbus** (#249,
  "Sources"). Its actual entity gap is against solax's Solis plugin: 126
  registers, different entity ids. The tiers above are by register, so they
  carry over, but entity-id continuity (§2d) must be planned against the
  solax ids.

## Open questions for the decision ticket

1. Does step 1 include tier A (and B), or does the gap run with
   solis_modbus/solax on a read-only profile?
2. Does v1 parity include RC force? If not, the 43135 bench test leaves v1.
3. The parity write list (§3b), and whether installer and protection settings
   are read-only.
4. Platform for parity writes (`switch` with `entity_category: config`, or
   `select`), which ties to #308 D7.
5. Is the grid/string (no-battery) map in scope, given "replaces Solis (all
   of it)"?
6. Is serial or USB transport in scope?
7. Entity-id continuity: should the integration suggest object ids that match
   solis_modbus (or solax) so Energy dashboard and automation references carry
   over, or ship a rename note?
