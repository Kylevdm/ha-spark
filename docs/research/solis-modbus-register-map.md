# Research: the full Solis Modbus register map over a Waveshare RS485-to-TCP gateway

Issue: [#249](https://github.com/Kylevdm/ha-spark/issues/249), part of map [#247](https://github.com/Kylevdm/ha-spark/issues/247).
Date: 2026-10-08.

## Question

What does a full Solis hybrid Modbus surface look like for a native
integration? The ticket asks for six things:

- the input and holding registers that `Pho3niX90/solis_modbus` and
  `wills106/homeassistant-solax-modbus` expose;
- model and firmware variance;
- safe read cadence and batching;
- write semantics: timed slots, 43135 forced charge, the 43007 power switch,
  and export limits;
- how the gateway behaves with several TCP clients;
- which registers ha-spark needs today and which are only needed for parity.

## Sources and provenance

The same provenance ranking as #80 applies: **(A)** official Solis or Waveshare
documents, **(B)** integration source code, **(C)** community reports, **(L)**
this repo's own live-fire measurements.

- **(B) solis_modbus** at
  [`6911f53`](https://github.com/Pho3niX90/solis_modbus/tree/6911f53354b239ba27c4df01569896240022f766)
  (2026-10-07). It is the larger map. Its hybrid table says it is "based on
  RS485_MODBUS RTU Hybrid Inverter Protocol Ver3.2", and later comments cite
  Ver3.4. Files:
  [`sensor_data/hybrid_sensors.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/sensor_data/hybrid_sensors.py),
  [`switch_sensors.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/sensor_data/switch_sensors.py),
  [`select_sensors.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/sensor_data/select_sensors.py),
  [`time_sensors.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/sensor_data/time_sensors.py),
  [`client_manager.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/client_manager.py),
  [`modbus_controller.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/modbus_controller.py),
  [`data_retrieval.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/data_retrieval.py),
  [`const.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/const.py),
  [`__init__.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/__init__.py),
  [`helpers.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/helpers.py),
  [`data/solis_config.py`](https://github.com/Pho3niX90/solis_modbus/blob/6911f53354b239ba27c4df01569896240022f766/custom_components/solis_modbus/data/solis_config.py).
- **(B) solax-modbus** at
  [`ab73b9e`](https://github.com/wills106/homeassistant-solax-modbus/tree/ab73b9e1d154b1db9b98a29f33b49af87af4d191)
  (2026-10-07), in
  [`plugin_solis.py`](https://github.com/wills106/homeassistant-solax-modbus/blob/ab73b9e1d154b1db9b98a29f33b49af87af4d191/custom_components/solax_modbus/plugin_solis.py).
  This is the integration the household runs today.
- **(A) Waveshare**: the
  [RS485 TO ETH (B) wiki](https://www.waveshare.com/wiki/RS485_TO_ETH_(B)) and
  the [manual template](https://www.waveshare.com/wiki/Template:RS485_TO_ETH_(B)_Manual).
  The linked v1.33 PDF manual now returns 404. A v1 UART TO ETH (B) manual
  exists, but its text could not be extracted here.
- **(A) Solis protocol PDF**: still not freely reachable, as #80 and #109 found.
  Where this document cites the protocol (the 300 ms inter-frame floor and the
  Ver3.4 TOU and dispatch sections), it does so **through solis_modbus's
  comments that quote it**. Treat those as B quoting A.
- **(L) Prior work in this repo**, cited and not re-researched:
  - [`docs/solis-control-modbus-overlay.yaml`](../solis-control-modbus-overlay.yaml)
    (the overlay);
  - [`ha_spark/devices/inverters/solis.py`](../../ha_spark/devices/inverters/solis.py)
    and `solis_clock.py`;
  - [`ha_spark/presets.py`](../../ha_spark/presets.py);
  - [`docs/research/109-solis-timed-charge-register-flash-endurance.md`](109-solis-timed-charge-register-flash-endurance.md)
    (flash endurance);
  - [`docs/research/inverter-drivers.md`](inverter-drivers.md) §2 (Modbus is
    single-master);
  - [`docs/runbooks/RUN-83-log.md`](../runbooks/RUN-83-log.md) and
    [`solis-forced-charge-live-fire.md`](../runbooks/solis-forced-charge-live-fire.md);
  - issues [#80](https://github.com/Kylevdm/ha-spark/issues/80) (the 43110 bit
    table), [#85](https://github.com/Kylevdm/ha-spark/issues/85) (solax
    autorepeat), [#88](https://github.com/Kylevdm/ha-spark/issues/88) (gateway
    settings), [#100](https://github.com/Kylevdm/ha-spark/issues/100) (slot
    register dump) and [ADR-0003](../adr/0003-ha-spark-owns-the-solis-inverter.md).

## Answer in brief

1. **The map is large, but the needed slice is small.** solis_modbus defines
   about 420 register-backed entities across about 50 read groups. solax-modbus
   touches 126 distinct registers. ha-spark needs **about 45 registers**, and
   they can be read in **5–6 frames**. The 5–6 frames already include the
   battery-power register that #244 adds.
2. **Addressing is direct.** Both integrations pass the documented number as the
   PDU address. Registers 3xxxx are **input** registers (FC04) and 4xxxx are
   **holding** registers (FC03/FC06/FC16). solis_modbus decides on
   `start_register >= 40000`. The overlay already follows this convention.
3. **Cadence is bounded by the protocol's 300 ms inter-frame floor (B quoting
   A), and the floor applies across every client together.** solis_modbus
   enforces 310 ms spacing per link and reads each contiguous group in one
   frame. No client can enforce that spacing across *other* clients, and the
   gateway does not document doing so. The current overlay, which polls one
   frame per entity, is already close to the budget by itself (see "Read
   cadence"). A native integration should be **the only Modbus client**.
4. **Writes are fire-and-forget in both integrations**, with an optimistic
   cache and no read-back. ha-spark's read-back verification (ADR-0003, #164)
   has to live in the new integration. Neither integration supplies it.
5. **Firmware variance is real and readable at runtime.** The integration
   should read four identity registers at startup:
   - 33000: protocol version and model code (`0x31` = 1-phase LV AC-coupled);
   - 35000: inverter type definition;
   - 33289: TOU function version (`0xAA55` = V2);
   - 34502: Remote Dispatch capability (`0xAA55`).

   On **V2-TOU firmware, 43110 bit 1 is cleared within ~15 s**. The timed-slot
   surface ha-spark drives is then replaced by 43707 and 43711–43791. A native
   integration must check 33289 before trusting the V1 slot path.
6. **43135 has a conflict.** Both integrations say `1` = force charge and `2` =
   force discharge. RUN-83 concluded that `1` = force discharge. RUN-83's own
   step 5 wrote `1` through the solax "Force charge" option. This needs a bench
   test before any 43135 work. It does not affect the shipped timed-slot path.
7. **Waveshare multi-host works, but its arbitration is undocumented, and
   "storage" (caching) mode is the factory default.** The #88 instruction to
   turn storage mode off is confirmed as necessary and cannot be skipped.
   The overlay correctly uses `type: tcp`, the MBAP protocol in the gateway's
   "Modbus TCP<->RTU" mode. #88's `rtuovertcp` suggestion fits *transparent*
   mode, which has no multi-host arbitration.

## 1. Register map

The tables below follow solis_modbus's hybrid map (B), cross-checked against
solax-modbus (B) where both define a register. **Scale** is the multiplier from
raw to engineering units. **Need** has three values:

- **today**: ha-spark reads or writes it now, through the overlay or a preset
  entity;
- **#244**: added by the in-flight battery-energy SoC check (branch
  `ticket-222-car-power-evidence`, commit `c07cb95`);
- **parity**: only needed so users can uninstall solis_modbus or solax-modbus.

### 1a. Input registers (FC04)

| Range | Contents | Scale / notes | solis_modbus poll | Need |
|---|---|---|---|---|
| 33000–33019 | Model code (hi = protocol ver, lo = model), DSP ver, HMI ver, protocol ver, serial (16 words) | `decode_inverter_model` in `helpers.py` | once | **today (new)**: identity and variance gate |
| 35000 | Inverter type definition | | once | **today (new)**: variance gate |
| 33022–33027 | RTC yy, mm, dd, hh, mi, ss | read as one frame (#161) | slow | **today** (overlay clock) |
| 33029–33040 | PV energy totals, today, month, year | U32 pairs, 0.1 kWh for today | slow | parity (PV models) |
| 33041–33043 | Max inverter current, BMS battery temperature | 0.1 | fast | parity |
| 33049–33058 | PV1–4 V/I, total PV power (U32) | 0.1 | fast | parity (AC-coupled units have no PV input) |
| 33070–33096 | Alarm code, bus voltages, phase V/I, active, reactive and apparent power, temperature, grid frequency, **status (33095)**, lead-acid temperature | 0.1 / 0.01 | fast | parity; 33095 is useful for health |
| 33116–33123 | Fault bitfields (grid, backup, battery, inverter ×2), operating status, operating mode, grid standard | bitfields | fast | parity |
| 33126–33131 | Meter total energy, voltage, current, **meter active power** (S32) | 0.001 kWh / 0.1 V / 0.01 A / W | fast | parity; a candidate source for `grid_power_entity` |
| **33132** | **Storage control switching value** (read mirror of 43110) | bitfield, bit 5 = grid charge allowed | fast | **today** (overlay) |
| **33133** | **Battery voltage** | 0.1 V | fast | **today** (`battery_voltage_entity`) |
| 33134 / 33135 | Battery current / **current direction** (0 charge, 1 discharge) | 0.1 A | fast | **#244** (signs 33149) |
| 33136–33138 | LLC bus voltage, backup phase A V/I | 0.1 | fast | parity |
| **33139** | **Battery SoC** | % | fast | **today** (`soc_entity`) |
| 33140–33146 | SoH, BMS voltage, BMS current, BMS charge and discharge current limits, BMS fault words | 0.01 / 0.1 | fast | parity; the BMS limits are useful context for the 62.5 A ceiling |
| 33147 / 33148 | Household load power / backup load power | W | fast | parity; a candidate true-load source |
| **33149–33150** | **Battery power** (U32), signed by 33135 | W | fast | **#244** (`battery_power_entity`) |
| 33151–33157 | AC grid port power (S32), backup phases B and C, inverting/rectifying power | W / ×10 | fast | parity |
| 33161–33189 | Battery charge and discharge, grid import and export, consumption totals, today and yesterday | U32 kWh, 0.1 kWh daily | slow | parity; candidate `derive_*` statistic sources (household-configured) |
| 33200–33217 | Mirrors: backup enable, battery charge-enable and direction, max charge and discharge current, over-discharge SoC, force-charge SoC, AFCI, leakage, fast battery current | 0.1 | slow | parity (read-back mirrors of 43011/43018/43117/43118) |
| 33243–33250 | Parallel AC, **EPM setpoint power, EPM status, EPM backflow realtime**, meter placement | ×100 W for EPM | normal | parity (export-limit read-back) |
| 33251–33286 | Per-phase meter V/I/P/Q/S, power factor, frequency, meter import and export totals | | fast | parity |
| **33289** | **TOU function version** (`0xAA55` = V2) | | slow | **today (new)**: variance gate for the slot path |
| 33300–33337 | Second meter block | feature `DUAL_METER` | fast / slow | parity |
| 33512–33518 | Per-phase AC grid port power | ×10 W | fast | parity |
| 33530–33535 | Generator | feature `GENERATOR` | fast | parity |
| 33580–33596 | Household and backup load energy totals | | slow | parity |
| 34243, 34328–34393 | Parallel sync; smart-port V/I/P | features `PARALLEL` and `SMART_PORT` (S6 only) | | parity (S6 only) |
| 34445–34497 | AC-coupling generation and power | feature `AC_COUPLING` | | parity |
| 34502–34504 | **Remote Dispatch capability, function map, running status** | `0xAA55` = capable | once / slow | **today (new)**: variance gate; see 2e |

### 1b. Holding registers (FC03 read, FC06/FC16 write)

| Range | Contents | Scale / values | Write | Need |
|---|---|---|---|---|
| **43000–43005** | **RTC set** yy, mm, dd, hh, mi, ss | | FC16, 6 words (solax "Sync RTC"; `solis sync-clock`) | **today** (clock sync, #161) |
| **43007** | **Power switch**: `190` (0xBE) = on, `222` (0xDE) = off | both integrations agree | FC06 | **today** (`inverter_power_switch_entity`, ADR-0003 rule 2 reconcile) |
| 43009 | Battery model (LV: small ints; HV: `0xNN00`) | enum | FC06 | parity (installer setting; not exposed by default) |
| 43010–43028 | Max charge SoC, **over-discharge SoC (43011)**, max charge and discharge current, float and equalise voltage, **force-charge SoC (43018)**, rated capacity, over-discharge and force-charge voltage, **backup SoC (43024)**, force-charge power limit (×10 W), force-charge source | % / 0.1 A / 0.1 V | FC06 | parity; 43011 and 43018 are the floors behind RUN-83's charge-gate hypothesis |
| **43073** | **Export limit switch**, bit 4 = limit on (solax: `0` / `16`; X3 adds bit 6 balanced/unbalanced, giving `0/16/64/80`) | bitfield | FC06 read-modify-write | parity → becomes **needed** if ha-spark owns export limits (see 2d) |
| **43074** | **Backflow (export) power limit** | ×100 W (solax 0–9 900 W; solis_modbus 0–20 000) | FC06 | parity / export (2d) |
| 43070 / 43081 | Output-limit gate (`0xAA` on, `0x55` off) / output power clamp | ×10 W | FC06 | parity |
| **43110** | **Storage control switch** (write side of 33132); bit table in #80 | bitfield | FC06 read-modify-write | today, **read only** (ha-spark asserts bit 5 through 33132 and never writes it) |
| 43111 | Backup supply on/off | 0/1 | FC06 | parity |
| 43116–43118 | Charge/discharge current (via 43249 bit), battery max charge and discharge current | 0.1 A | FC06 | parity |
| 43128–43137 | **RC block**: RC AC grid power, **RC force discharge power (43129)**, battery charge and discharge limit power, RC grid adjustment (43132: 0/1/2), RC grid P and Q, **RC force charge/discharge (43135)**, **RC force charge power (43136)**, off-grid over-discharge SoC | ×10 W | FC06 | backlog (ADR-0003 demoted RC); parity |
| **43141 / 43142** | **Timed charge / discharge current** | 0.1 A DC (raw 600 = 60.0 A) | FC06, applies on write | **today** (`charge_current_entity` and overlay) |
| **43143–43150** | **Slot 1**: charge start h/m, end h/m; discharge start h/m, end h/m | | FC16 8-word block (solax button); solis_modbus writes 2-word h/m pairs | **today** (driven) |
| 43151–43152 | reserved | | | |
| **43153–43160 / 43163–43170** | **Slots 2 and 3** (same layout) | | FC16 | **today** (zero-guarded) |
| 43173–43190 | **Slots 4 and 5** (same layout; 43181–43182 reserved) | | FC16 | parity (solis_modbus only, absent from solax) |
| 43195 | Export calibration | ±1000 | FC06 | parity |
| 43249 | Special settings bitfield (MPPT parallel, protections, constant-voltage mode) | bitfield | FC06 read-modify-write | parity |
| **43282** | **RC timeout** | 1–30 min, default 5 | FC06 | backlog (RC watchdog, #85) |
| 43291 / 43292 | Flexible export limit (×100 W) / enable (`0xAA` on, `0x00` off) | South-Australia (SAPN) only; disabled by default | FC06 | parity |
| 43302, 43340, 43361–43369, 43483, 43487–43488, 43815 | Auxiliary flags, generator, MPPT scan, hybrid function bits (43483 bit 3 = allow export under self-use, inverted), peak-shaving baseline | bitfields | FC06 read-modify-write | parity |
| 43707 | **V2 TOU period switches**: bits 0–5 charge periods 1–6, bits 6–11 discharge periods 1–6 | bitfield | FC06 read-modify-write | **today if V2 firmware** (2a) |
| 43708–43749 / 43750–43791 | **V2 TOU charge / discharge slots 1–6**, 7 words each: cut-off SoC, current (0.1 A), cut-off voltage (0.1 V), start h/m, end h/m | | FC16 | **today if V2 firmware** (2a) |
| 44100–44112 (+44116.. schedule) | **Remote Dispatch** (Ver3.4): master, fail-safe minutes, limit switches, import and export caps (×100 W), control mode, power target (S32 ×10 W), function bits, SoC window, reserve SoC, PV limit | RAM-only | FC16, two atomic chunks | future (2e) |
| 44280 | Remote active power control; bits 4–7 = PV shutdown; **reverts after the RC timeout** | | FC06 + keep-alive | parity |

The overlay's slot-1 reads (11 registers at 5 s) and slot-2/3 reads (16
registers at 30 s) are a subset of the 43141–43170 block above. That block is
one contiguous 30-register read.

## 2. Write semantics

### 2a. Timed slots (the driven surface)

- **What ha-spark ships** (L, ADR-0003, #100): FC16 of the 8-word slot block
  at 43143, 43153 or 43163, and FC06 of 43141/43142. Writing the block **is**
  the commit. The solax "Update Charge/Discharge Times" button is a
  `WRITE_MULTI_MODBUS` of exactly those 8 words (`value_function_timingmode`,
  `plugin_solis.py`).
- **solis_modbus writes differently**: each time entity calls
  `async_write_holding_registers(register, [hour, minute])`, a 2-word FC16
  (`time.py`). So for solis_modbus's users, firmware accepts *partial* FC16
  writes into the slot block. RUN-83 recorded that individual `number` writes
  followed by the button press worked on this inverter. RUN-83 does not record
  whether a 2-word write on its own takes effect without the 8-word block.
  **Open**: a native integration should keep the proven 8-word block write.
- **Preconditions** (#80, solis_modbus): 43110 bit 1 enables V1 timed mode and
  bit 5 allows grid charge. solis_modbus has a source comment ("Doc literally
  says '0=Allow 1=Not allow' for bit 5, but every field-verified source … has
  the OPPOSITE"): the Solis document's polarity for bit 5 is wrong, and **set
  = allowed** is correct. That matches ha-spark's `_GRID_CHARGE_BIT` and the
  live `33132 = 35`.
- **V2 TOU firmware** (B: `const.py`, `select_sensors.py`, solis_modbus issue
  #475, bench-verified on an S6-EH1P): when `33289 == 0xAA55`, "43110 bit 1 …
  is acknowledged but cleared by the firmware within ~15 s, so it can never
  stick". TOU then runs from the 43707 period switches and the 43708–43791
  slots. Those slots carry their own cut-off SoC and current, and they cover
  six charge and six discharge periods. **This is the single biggest
  firmware-variance risk for a "new households" integration** (map #247). An
  S6 household on V2 firmware would see ha-spark's slot-1 writes land and read
  back correctly while the inverter ignores them. The household's 33289 value
  has not been recorded. Read it before building.
- **Flash wear**: still unconfirmed (#109). Nothing in either integration's
  source mentions it. #46's write-if-changed rule remains the mitigation.

### 2b. 43135 forced charge (RC)

- **Values in source (B)**: solis_modbus `RC_FORCE_MODE_REG = 43135  # 0 =
  none, 1 = force charge, 2 = force discharge`. solax `option_dict={0: "Off",
  1: "Force charge", 2: "Force discharge"}`. The two independent
  implementations agree.
- **Conflict with RUN-83 (L)**: RUN-83 concluded "`1` = force discharge" from
  one raw write that coincided with ~1.5 kW of export. In step 5 of the same
  run, the solax "Force charge" option was selected. By solax's source that
  writes `1`, and that write latched with no actuation. Two `1` writes produced
  different behaviour minutes apart. "`1` = discharge" is therefore not
  established. One reading consistent with everything observed: `1` *is* force
  charge, and it is gated (by mode or SoC, per RUN-83's own hypothesis). The
  17:57 export then had another cause, for example the RC power setpoints
  43128/43133 or the grid-adjustment register 43132 acting on their own.
  **Unresolved. Bench-test before any 43135 work.** ADR-0003's demotion of RC
  is unaffected.
- **Sequencing (B)**: solis_modbus writes 43135 first, then re-writes 43136,
  43129 and 43282 from cache (`companion_writes`). It cites solis_modbus issue
  #352: "Solis firmware requires 43135 to be enabled BEFORE the setpoints and
  RC Timeout (43282) are written, otherwise the values do not latch". RUN-83
  found ordering made no difference on this inverter (L). Treat this as
  variance.
- **Keep-alive**: solax resends every scan, about 15 s, indefinitely, and the
  resend does not survive a restart (#85). solis_modbus has no 43135 resend. It
  keeps alive only 44280, at an interval derived from 43282
  (`solis_binary_sensor.py`).

### 2c. 43007 power switch

FC06 of `190` (on) or `222` (off). Both integrations define the same values
(solax `select` "Power Switch"; solis_modbus switch with `on_value: 190,
off_value: 222`, its issue #476). ha-spark already drives this through the
solax select entity, reconciled every minute (ADR-0003 rule 2). A native
integration needs a read-back of 43007 itself, which solis_modbus polls in the
43007–43028 group at **normal** speed (15 s).

### 2d. Export limits

- **Main path**: 43073 bit 4 enables the limit and 43074 sets it in 100 W
  units. 43073 is a bitfield (X3 models also use bit 6 for balanced versus
  unbalanced output), so the write must be a **read-modify-write**. solis_modbus
  refuses a read-modify-write when it has no cached value and the live read
  fails, "to avoid clearing other bits" (`solis_binary_sensor.py`). The
  integration should copy that rule.
- **Read-back**: 33247 (EPM setpoint power) and 33249 (EPM backflow realtime),
  both ×100 W.
- **Other paths, not for general use**: 43291/43292 is SAPN-only flexible
  export, off by default. Writing `0x55` to 43292 leaves it stuck on
  (solis_modbus #499). 43070/43081 is an instant AC-output clamp. 43483 bit 3
  (inverted) is "allow export under self-use".
- ha-spark does not write any export limit today. Axle export runs through
  slot-1 discharge (ADR-0003). Export limits are therefore parity, unless the
  supervised-export map (#128) decides otherwise.

### 2e. Remote Dispatch (44100–44199), firmware Ver3.4

From solis_modbus `__init__.py` (B): the block is gated on `34502 == 0xAA55`.
The registers are "RAM-only … with an inverter-side failsafe (44101): if the
controller goes silent the inverter reverts on its own". It must be written as
two contiguous FC16 chunks, global 44100–44104 then realtime 44105–44112,
because "scattered single-register writes get silently dropped". This has the
two properties the timed-slot path lacks: no flash wear and a hardware dead-man
timer. It exists only on newer firmware, so it is a candidate future control
surface, not something to build on now.

### 2f. Write path behaviour in both integrations

- solis_modbus queues writes and executes them under the shared link lock with
  the inter-frame wait. It holds a queued write while the link is down ("so a
  write queued during a reconnect window isn't … silently drop[ped]"). It
  updates its cache optimistically and does **no read-back**.
- solax writes, then waits for the next poll.
- In both, a successful entity state means "the write was sent", not "the
  device holds the value". ha-spark's verification (bounded observe window,
  #164) must be built into the native integration's services.

## 3. Read cadence and batching

- **Protocol floor (B quoting A)**: `client_manager.py`: "Solis RS485_MODBUS
  protocol (Hybrid Inverter V3.1 …) section 3.2 … 'More than 300ms
  communications frame interval is required.' Applies to every request on the
  link, read or write." solis_modbus enforces 310 ms per link, shared across
  slaves.
- **solis_modbus defaults**:
  - fast 5 s (the config floor is 10 s, or 2 s in its "extreme" profile);
  - normal 15 s;
  - slow 30 s;
  - one frame per contiguous group;
  - pymodbus `timeout=5, retries=1`, because retry storms "flood the …
    datalogger, desync transaction IDs" (#395/#406).

  Its "full ~11-group pass can never be" faster than about 3.4 s. Groups that
  raise exception 2 (illegal address) are bisected and the bad register is
  dropped (`data_retrieval.py`). That is its runtime answer to model variance.
- **solax-modbus**: `block_size=40` registers per frame, default scan 15 s
  (#85).
- **Today's load (L plus arithmetic, an inference)**: core HA `modbus:` issues
  one request per sensor. The overlay polls 11 sensors at 5 s (2.2 req/s) and
  17 sensors at 30 s (0.57 req/s), about 2.8 req/s. That is one frame every
  ~360 ms, **already at the 300 ms floor before solax-modbus adds its own
  polls**. Nothing in the repo attributes a failure to this. The measured
  ~5.2 s write-to-read-back latency (#164) is consistent with it.
- **What a native integration needs per cycle** (≈45 registers), in these
  frames:

  | Frame | Registers | Cadence |
  |---|---|---|
  | input | 33132–33152 (21): mode, battery V/I/direction, SoC, BMS, loads, battery power, grid port power | ~5–10 s |
  | input | 33022–33027 (6): clock | ≤30 s; staleness gate is 60 s |
  | holding | 43141–43170 (30): currents and slots 1–3 | 30 s, plus on demand after a write |
  | holding | 43007 (1), or 43007–43028 (22) for the battery floors | 15 s |
  | holding | 43110 (1), optional because 33132 mirrors it | slow |
  | once | 33000–33019, 35000, 33289, 34502 | startup |

  That is about 0.5 req/s against the overlay's ~2.8 req/s. Parity-only groups
  can ride the slow tier.
- **Read-back after a write**: one targeted FC03 of the written range, after
  the inter-frame wait, replaces the overlay's 16 × 1 s observe loop.

## 4. The gateway with several TCP clients

What Waveshare documents (A, RS485 TO ETH (B) wiki and manual template):

- "Supports 30 TCP connections as a TCP server."
- Multi-host: "In the query mode of one question and one answer, the support
  network port allows multiple computers to access the same serial device at
  the same time."
- "A simple Modbus RTU to TCP conversion lacks a mechanism for multiple hosts."
- **Storage mode is the default**: "the default Modbus adopts the storage mode,
  which will automatically train the query commands". "The storage-type Modbus
  gateway can store this content, significantly improving the speed of Modbus
  TCP queries". "The storage-type function can be disabled."
- Protocol conversion is selected as "Modbus TCP<-->RTU", and the port becomes
  502.

What Waveshare does **not** document: how simultaneous requests are queued,
whether the gateway enforces any inter-frame gap on the RS485 side, its
response timeout, or how storage mode refreshes or invalidates its cache after
a write. Those gaps have three consequences:

- **Storage mode off is required** for read-back to mean anything (#88). This
  is now known to be the factory default, so the onboarding and health check
  for a new household should say so.
- **The MBAP framing is correct.** In "Modbus TCP<-->RTU" mode the gateway
  speaks Modbus TCP, so HA's `type: tcp` (the shipped overlay) and solis_modbus's
  `AsyncModbusTcpClient` are right. `rtuovertcp`, which #88 suggested, would only
  fit transparent mode, and transparent mode does not arbitrate.
- **Coexistence works but is unmanaged.** The overlay and solax-modbus have
  shared the gateway since #90 without recorded corruption (L). The 300 ms
  floor, however, is per *serial link*. Each client spaces only its own frames,
  so two clients can together send faster than the floor allows.
  `inverter-drivers.md` §2 already notes that Modbus is single-master. The
  integration should be the **sole client**, and the migration (map #247,
  "Moving … off solis_modbus") should be a short overlap, not a permanent dual
  poll.

## 5. Model and firmware variance, summarised

| Signal | Where | Use |
|---|---|---|
| Model code | 33000 (hi = protocol version, lo = model; `0x31` = 1P LV AC-coupled, `0x30` = 1P LV hybrid, `0x9x` = S6) | pick the register profile |
| Inverter type definition | 35000 | cross-check |
| Serial prefix | 33004–33019 | how solax picks a type; unrecognised prefixes get no entities |
| TOU version | 33289 (`0xAA55` = V2) | V1 slots (43143..) versus V2 slots (43707..) |
| Remote Dispatch | 34502 (`0xAA55`) | whether 44100.. exists |
| Illegal-address exceptions | per group | drop absent registers (solis_modbus bisection) |
| Feature flags | PV, BMS, generator, smart port (S6), AC coupling, parallel, dual meter, HV battery | which parity groups to poll |

The household's AC-coupled RAI-class unit appears in solis_modbus as
`RAI-3K-48ES-5G` (type HYBRID). solax detects it by serial prefix. #83 check 7
left the exact model label unconfirmed. Reading 33000/35000 natively settles
it.

## Open questions

1. **33289 on the household inverter.** Is it V1 or V2 TOU? This decides
   whether the shipped slot path is portable as-is. It is a read-only check.
2. **43135 value semantics**: the conflict in 2b. Bench test, own sitting.
3. **Partial FC16 into the slot block.** Does a 2-word h/m write take effect on
   this firmware, or only the 8-word block? This matters only if the
   integration wants per-field time entities.
4. **Waveshare arbitration internals.** The documentation is silent. Measure
   it empirically if a dual-client overlap is kept for longer than a cutover.
5. **Flash backing of 43141–43170** (#109): unchanged.
