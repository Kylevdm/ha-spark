# Live-fire run log — #83, 2026-09-07

Recorder: `docs/runbooks/live_fire_recorder.py --interval 2 --csv docs/runbooks/run-83.csv`
started 16:22:11 UTC (17:22 BST). **All timestamps in this log are BST; the CSV is UTC (−1 h).**

All writes made by hand by Kyle in the HA UI. Agent made no writes.

## Step 0 — baseline, 17:22:46 BST (16:22:46 UTC)

| what | entity | reading | at-rest expected | ok |
|---|---|---|---|---|
| RC enable read-back | `switch.solis_control_rc_force_charge` | `off` | `off` | ✅ |
| RC power read-back | `sensor.solis_control_rc_force_charge_power` | `0 W` | `0` | ✅ |
| RC timeout read-back | `sensor.solis_control_rc_timeout` | `0 min` | `0` | ✅ |
| work mode bitfield | `sensor.solis_control_work_mode_bitfield` | `35` | `35` | ✅ |
| battery power | `sensor.solisac_battery_power` | `+964 W` (discharging) | signed | ✅ |
| battery current | `sensor.solisac_battery_current` | `18.3 A` | unsigned | ✅ |
| battery voltage | `sensor.solisac_battery_voltage` | `52.7 V` | — | — |
| battery SoC | `sensor.solisac_battery_soc` | `80 %` | — | — |
| grid | `sensor.solisac_meter_active_power` | `+71 W` (exporting) | negative = import | ⚠️ ~0, see below |
| bus health | `sensor.solisac_communication_health` | `Healthy` | `Healthy` | ✅ |
| incumbent writer | `select.solisac_power_switch` | `On` | `On` | ✅ |
| SoC gate threshold | `number.solisac_battery_minimum_soc` | `20 %` | record only | ✅ |
| weaker gate candidate | `number.solisac_force_charge_soc` | `10 %` | record only | ✅ |
| site rate ceiling | `number.solisac_battery_charge_current` | `62.5 A` DC | — | see below |
| fallback abort rung | `select.solisac_inverter_battery_control_override` | `unknown` | — | ⚠️ see below |
| Axle window | `sensor.axle_vpp_axle_event_window_state` | `upcoming` | not `in_progress` | ✅ |
| Axle minutes to start | `sensor.axle_vpp_axle_event_minutes_to_start` | `97` → **18:59 BST** | — | ✅ |
| inverter AC output | `sensor.solisac_active_power` | `906 W` | — | — |
| unavailable `solisac_*` / `solis_control_*` | — | **0** | 0 | ✅ |

### Sign convention re-verification

`18.3 A × 52.7 V = 964.4 W` vs `battery_power = 964 W` — **exact match**, magnitude confirmed.
Direction confirmed as **positive = discharging**: SoC is falling with no charging source present.
The runbook's polarity for `battery_power` therefore holds; a successful force drives it negative.

**`meter_active_power` polarity is NOT re-verified at this baseline** — grid is `+71 W`, i.e. ~0,
which is too near zero to prove sign. It is consistent with the documented convention
(battery 964 W → 906 W AC out → 71 W export) but reads as a small *export*, not an import.
Treat the import polarity as inherited from #100, not re-confirmed today.

### Preconditions

| # | precondition | status at 17:22 BST |
|---|---|---|
| 1 | outside 23:30–05:30 | ✅ |
| 2 | SoC ≤ ~95% | ✅ 80% |
| 3 | low solar (preferred) | ⚠️ PV effectively nil already — battery is carrying the whole 906 W house load, grid ~0. Better than expected. |
| 4 | no Octopus dispatch slot | ⚠️ `intelligent_dispatching` = `off` ✅, but `intelligent_smart_charge` = `on` (target 08:00, cap hours 0) and both daytime dispatch automations are armed. Recorder guards `pwr_sw`. |
| 5 | SoC trustworthy | ✅ 80%, consistent with a steady ~2 %/hr fall |
| 6 | overlay healthy | ✅ 43135=off, 43136=0 W, 43282=0, 33132=35 |
| 7 | person present | ✅ Kyle at the inverter |
| 8 | no Axle event | ✅ `upcoming`, 97 min out |

## Step 1/2 — enable RC, attempts 1 and 2 (setpoints at rest, 43136=0)

Enable-first ordering per #87. Both attempts: switch toggled on by hand in the HA UI,
no other write. Recorder poll interval 2 s; all times BST.

| attempt | write seen | reverted to `off` | held | 43136 | 43282 | mode | batt W | charge current? |
|---|---|---|---|---|---|---|---|---|
| 1 | 17:25:54 | 17:25:58 | ~4 s | 0 | 0 | 35 | +1220 → +1146 (discharging) | **none** |
| 2 | 17:27:45 | 17:28:01 | ~16 s | 0 | 0 | 35 | +1251 → +1230 (discharging) | **none** |

No abort conditions at any point: mode `35`, health `Healthy`, `power_switch` `On`, Axle `upcoming`.

### Reading

- The `on` state is HA's **optimistic write echo**, not a register read. The revert is a genuine
  read of 43135 returning `0`. Deduction: if `verify:` were not configured the switch would latch
  optimistically `on` forever, since nothing else writes it — it reverted, so the overlay is
  really reading the register.
- **The differing hold times (4 s vs 16 s) are phase, not behaviour.** What varies is how long
  until the overlay's next verify read lands, not how long the register held. On *both* attempts
  43135 read `0` at the first genuine read after the write.
- **Step 2 has an answer, and it is negative: there is no settling delay to measure.** The
  read-back never confirms `on`. The `verify: delay` 20 s guess in the overlay cannot be replaced
  with a measured value from this data, because the transition being timed never happens.
- Decision table: **row 3 — reads `0`, no current → the write itself was not accepted.** This is
  *not* the SoC-gate result; the gate remains untested, as no held enable was ever obtained.
- Confound not yet excluded: both attempts enabled with **43136 = 0 W**. "Force charge at 0 W" is
  a plausible thing for firmware to refuse and self-clear, which would make #87's enable-first
  rule backwards for this register. Tested next by reversing the order.

## Step 3/4 — setpoints written first, RC still off (17:31 BST)

Written by hand via Developer Tools → Actions, `modbus.write_register`, hub `solis_control`, slave 1.

| register | written | read back | at | verdict |
|---|---|---|---|---|
| 43136 | `150` | **`1500 W`** | 17:31:16 | **scale = 10 W/unit, as assumed** |
| 43282 | `5` | **`5 min`** | 17:31:47 | latched |

**Both latched with RC `off`, and held.** This settles [#90](https://github.com/Kylevdm/ha-spark/issues/90)'s
open question: 43136 reads `0` at rest because it is **unset**, not because RC setpoints exist only
while the enable is on. Setpoints persist independently of 43135.

Corollary, and the more useful one: 43136 and 43282 accept writes through the *same* modbus path
that reports 43135 as `0`. Hub name, slave ID, bus and write path are all proven good. The problem
was never a flaky bus — it is specific to 43135.

## Step 1/2 — attempt 3: enable RC *with* setpoints in place

| attempt | write seen | reverted | held | 43136 | 43282 | batt W | charge? |
|---|---|---|---|---|---|---|---|
| 3 | 17:32:46 | 17:32:50 | ~4 s | 1500 | 5 | +1109 (discharging) | **none** |

Setpoints survived the attempt. **Ordering is not the root cause** — #87's enable-first rule is
disproved as the explanation for the write-rejection history. Three switch attempts, three reverts,
zero actuation, with and without setpoints present.

## Step 5 — the decisive result: solax select latches 43135; actuation refused

`select.solisac_inverter_battery_control_override` → **Force charge** (options: `Off`,
`Force charge`, `Force discharge`).

| time (BST) | 43135 | 43136 | 43282 | mode | batt W | batt A | SoC | grid W |
|---|---|---|---|---|---|---|---|---|
| 17:35:58 | `off` | 1500 | 5 | 35 | +779 | 14.8 | 79 | 0 |
| **17:36:07** | **`on`** | 1500 | 5 | 35 | +779 | 14.8 | 79 | 0 |
| 17:37:43 | `on` | 1500 | 5 | 35 | +774 | 14.7 | 79 | +48 |
| 17:39:29 | `on` | 1500 | 5 | 35 | +801 | 15.2 | 79 | −87 |

**43135 held `on` for 3 min 22 s under genuine read-back with zero charge current.**

Critically, the `on` here is **not** an optimistic echo: the switch entity was never touched, so
the overlay read `1` from the register. This is the first read-back-proven successful write to
43135 in the map's history.

### Findings

1. **The solax `select` write path works on 43135; the overlay `switch` write path does not.**
   Same register, same bus, minutes apart — 3/3 failures via the switch, 1/1 success via the select.
   This is a control-surface difference, and it is directly actionable for
   [#82](https://github.com/Kylevdm/ha-spark/issues/82).
2. **Decision table, middle row: write accepted, actuation refused.** The SoC gate is real and
   lives in firmware, not in the write path. At 79% SoC the inverter holds the force-charge flag
   set and declines to act on it.
3. The two-month "writes are not reliably accepted" history is now better explained as **two
   separate things**: a write-surface difference (switch vs select) and a firmware actuation gate.

### Not established

- **Where the gate's threshold actually is.** `battery_minimum_soc` (20) remains the hypothesis;
  `force_charge_soc` (10) the weaker candidate. Neither was written — deliberately, per the handoff.
- **Why the overlay switch fails.** Value, function code, or write-single vs write-multiple all
  remain open. Worth diffing the two paths' actual modbus frames.
- Whether the gate is charge-specific — the Axle export event at 19:00 BST bears on this.

## Step 6 — watchdog: NOT ANSWERED (recorded as a gap, not a result)

RC enabled via the solax select 17:36:07; 43282 = 5 min, so expiry was due 17:41:07.
43135 still read `on` at 17:44:28 — **8m21s after enable, 3m21s past the watchdog** — and was
still set when we intervened at 17:46.

**This does not disprove the hardware watchdog.** The enable was made through solax, which is
documented to resend (#85); a resend loop would refresh the timer continuously and it would never
be reached. The clean test needs a latched enable through a write-once surface. Deferred.

## Step 7 — revert, and the control-surface tally

| # | time | surface | write | 43135 after | held? |
|---|---|---|---|---|---|
| 1 | 17:25:54 | overlay switch | on | `0` | ✗ ~4 s |
| 2 | 17:27:45 | overlay switch | on | `0` | ✗ ~16 s |
| 3 | 17:32:46 | overlay switch | on (setpoints armed) | `0` | ✗ ~4 s |
| — | 17:36:07 | **solax select** | Force charge | **`1`** | ✅ held 11 min |
| 4 | 17:46:06 | overlay switch | off | `1` (unchanged) | ✗ ~7 s |
| — | 17:47:59 | **solax select** | Off | **`0`** | ✅ stable |
| 5 | 17:53:41 | overlay switch | on (select parked `Off`) | `0` | ✗ ~6 s |
| — | 17:56:4x | **`modbus.write_register`** | 43135 = 1 | **`1`** | ✅ latched + actuated |
| — | 18:01:06 | **`modbus.write_register`** | 43135 = 0 | **`0`** | ✅ stable |

**`switch.solis_control_rc_force_charge` is 0/5 in both directions. `modbus.write_register` and the
solax `select` are 4/4 on the same register, same hub, same slave, minutes apart.**

Attempt 5 was run with the select parked at `Off`, which rules out inter-integration contention:
the switch fails on its own terms. The 7 s bounce at attempt 4 was the switch's `off` write failing
(optimistic echo, then a verify read of the still-set `1`) — **not** a competing writer. An earlier
contention hypothesis raised during the run is withdrawn.

→ **This is a bug in the overlay's switch definition** (wrong write target — coil vs holding
register — or a wrong `command_on`), not inverter behaviour. Its *read* path is healthy: the switch
is the entity that correctly reported the true register value at every step.

## Unintended actuation — forced export, 17:57 (recorded in full)

`modbus.write_register` 43135 = **1** was written to test the write path. It latched **and actuated**:

```
17:56:58  43135 on   batt +2201 W   grid    +5 W   ← house load, grid at zero
17:57:28  43135 on   batt +2191 W   grid   +14 W
17:57:43  43135 on   batt +2065 W   grid +1493 W   ← forced export at the setpoint
17:57:59  43135 on   batt  +479 W   grid     0 W   ← ceased, flag STILL SET
```

**43135 is a command code, not a boolean: `1` = force discharge.** The overlay renders any non-zero
as `on`, so everything that displayed `on` tonight was not necessarily the same command. The value
`1` was inherited uncritically from the overlay switch because the entity is *named* force_charge —
an unsafe assumption about an unverified hardware control register.

Magnitude is one 2 s sample at **+1493 W against a commanded 1500 W**. Too exact to be house load,
but it is a single sample — do not cite it as a sustained discharge.

### Findings from it

1. **The RC mechanism actuates at 78% SoC in the discharge direction**, honouring 43136 as the rate.
   Combined with the charge refusal at 79%, **the gate is charge-specific** — direct evidence for
   the emergency-recharge reading of `battery_minimum_soc`, previously only inferred.
2. **Actuation self-terminates independently of the enable flag.** It ran ≲60 s and stopped with
   43135 still `1` for a further three minutes. That is *not* the 5-minute watchdog. Possibly a
   keep-alive requirement — which would explain why solax resends at all. The flag is sticky; the
   actuation is not. Better safety posture than the runbook's step 6 hazard assumed, and it changes
   what a dead-man's-switch has to defend against.

## Cleanup — 18:02:59, verified at rest

43135 `off` · 43136 `0` · 43282 `0` · mode `35` · health `Healthy` · `power_switch` `On`.
Inverter unarmed, nothing left behind. Final SoC 78% (baseline 80%).

## Summary against the run's questions

| question | answer |
|---|---|
| Does a write to 43135 take, with read-back proof? | **Yes** — via `modbus.write_register` and the solax select. **Not** via the overlay switch (0/5). |
| Real settling delay? | **Unmeasurable from the switch** (never confirms). Via working surfaces, read-back appears within one poll (≤15 s). |
| Is the hardware watchdog real (43282)? | **Not answered** — resend loop confounds it. But actuation self-terminated ≲60 s regardless. |
| Does companion-write ordering matter? | **No.** Setpoints-first behaves identically. #87's rule is disproved as the root cause. |
| Is 43136 honoured; what scale? | **Yes. 10 W/unit, confirmed** (150 → 1500 W; observed export 1493 W). |
| Does 43136 persist after RC goes off? | **Yes** — it latches independently of 43135. Settles #90. |
| Do forced-charge writes stick reliably? | Surface-dependent, not random: 4/4 on the working surfaces, 0/5 on the switch. |
| **Is there an SoC gate?** | **Yes, and it is charge-specific.** Charge refused at 79% with the flag held 3m22s; discharge actuated at 78% within ~60 s. |

## Follow-ups

1. **Fix the overlay switch** — wrong write target or `command_on`. Blocks nothing (write via
   `modbus.write_register`), but the entity is actively misleading.
2. **Find the force-*charge* command code.** Not guessed tonight, deliberately. The solax select's
   "Force charge" wrote a non-zero that did not actuate — capture what value it actually writes.
3. **Model 43135 as an enumeration** in any driver (#84), never a boolean. A boolean abstraction
   over this register can command a discharge while believing it commanded a charge.
4. **Re-test the watchdog** through a write-once surface now that `modbus.write_register` is known to work.
5. **Probe the gate threshold** (`battery_minimum_soc` = 20) — still deferred, still risky, its own sitting.
## Axle export event — observation setup (19:00–20:00 BST, `import_export: export`)

Read-only throughout; no writes. Recorders: `live_fire_recorder.py` → `run-83.csv` (narrow set,
unbroken from 17:22 BST) and `axle_observer.py` → `axle-83.csv` (wide set: timed charge/discharge
slot registers, discharge current limits, and the `inverter_control_<SERIAL-REDACTED>_*` entities).

Inverter at rest before the event: 43135 `off`, 43136 `0`, 43282 `0`, mode `35`, `Healthy`,
`power_switch` `On`, SoC 78%. Nothing from the live-fire persists into the window.

### ⚠️ `inverter_control_<SERIAL-REDACTED>_*` is the Solis **cloud** integration — ~5 min delay

Owner-confirmed (2026-09-07). Consequences for reading `axle-83.csv`:

- **Never derive latency or ordering from the `cc_*` columns.** Their timestamps lag reality by
  roughly five minutes, so they cannot be sequenced against the local modbus columns.
- They remain valid as a **binary signal only**: did this surface change at all during the window.
- The entities also carry the inverter **serial** (redacted here — this repo is public and Solis
  serials identify an inverter to SolisCloud; the value is in the HA entity ids) — partially
  answering check 7,
  though the model designation is still unconfirmed (integration reports `model: None`).

### Predicted outcomes and what each would mean

| observation | reading |
|---|---|
| `switch.solis_control_rc_force_charge` / 43135 moves | Axle drives the **same RC path** we tested. Its written value would reveal the force-**charge** command code we declined to guess. |
| timed-discharge slot registers change | Axle works through the **slot path**; the RC register is not the production control surface. |
| only `select.solisac_power_switch` moves | Cruder surface than either. |
| **battery/grid actuate with no local register change at all** | Axle commands via **Solis Cloud**, not local modbus — the actuation has no locally-visible control surface. Would mean a third-party can actuate this inverter through a path ha-spark cannot see, observe, or arbitrate against. Directly material to [#82](https://github.com/Kylevdm/ha-spark/issues/82) and to the driver's contention model in [#84](https://github.com/Kylevdm/ha-spark/issues/84). |

The last row is a live possibility precisely *because* the cloud integration exists on this site.
### 18:24–18:25 BST — the timed-slot write recipe (owner-performed)

**Attribution corrected.** These writes were made **by Kyle, by hand**, not by Axle. An earlier
entry in this log attributed them to Axle and drew conclusions about third-party behaviour from
them; that reading is withdrawn in full. Kyle performed them as a demonstration of the sequence
ha-spark should itself perform in future, driven off the Axle API (a separate work package).

| time (BST) | entity | change |
|---|---|---|
| 18:24:39 | `number.solisac_timed_discharge_end_hours` | `0` → **`20`** |
| 18:24:48 | `number.solisac_timed_discharge_start_hours` | `0` → **`19`** |
| 18:25:02 | `button.solisac_update_charge_discharge_times` | **PRESSED** |

`start_minutes`/`end_minutes` already `0`; `timed_discharge_current` left at its resting `90.0 A`
DC (~4.7 kW at 52.7 V). Slot **1** only (`_2`/`_3` untouched since 2026-07-30 / 2026-03-03).
Throughout: `switch.solis_control_rc_force_charge` `off`, work mode `35`, `power_switch` `On`.

#### What this establishes

1. **The intended ha-spark write recipe, owner-validated:** write the slot registers individually,
   then press `button.solisac_update_charge_discharge_times` to commit. Register order within the
   slot did not matter; the commit is the operative step. This is the target sequence for the
   driver ([#84](https://github.com/Kylevdm/ha-spark/issues/84)) and for the Axle-API work package.
2. **There is a commit step, and no RC work in this map has ever used one.** Nothing in the RC
   path's history pressed an apply button. If 43135 has an equivalent, that is a complete
   alternative explanation for why RC writes read back as accepted yet inert — and it is testable
   **without guessing command codes**. Strongest open lead from this run.
3. The path is plain local modbus and therefore visible and arbitrable by ha-spark.

#### What is NOT established

- **Which surface Axle uses is still open.** Nothing observed tonight came from Axle.
- Plausible, and consistent with the owner performing this by hand: **Axle may write nothing to
  the inverter at all** — publishing events via API for the site to implement. If so, Axle is an
  *instruction source*, not a control surface, and #82's contention model needs no defence against
  it. Not yet confirmed.

#### ⚠️ Consequence for the 19:00–20:00 observation

The slot is now programmed to discharge 19:00–20:00 **regardless of Axle**. Any discharge in that
window is therefore **not attributable to Axle** — the manual slot and any Axle action are
confounded, and the original observation goal (identify Axle's control surface) cannot be met
tonight.

What the window *can* still deliver, and it is worth having: a clean measured characterisation of
the **timed-slot surface itself** — actuation latency against the programmed 19:00 boundary,
achieved rate against the commanded 90 A, behaviour at 77% SoC, and whether it terminates cleanly
at 20:00. That is a direct measurement of the surface ha-spark intends to drive, which the RC path
never gave us.

#### Correction to an earlier reading in this run

`sensor.solisac_communication_health` showed **`Degraded` from 18:17:58 while data continued to
flow** (values resumed 18:18:14 and never stopped). The sensor is sticky/lagging and `Degraded`
does **not** imply loss of data. The runbook lists health ≠ `Healthy` as an abort trigger — as
written it would abort runs that are fine. Recommend the abort condition be *mass entity
unavailability*, with health as corroboration only.
## RC path: no commit button — but a mode gate is the better hypothesis

**Q: does the RC path have a commit equivalent to `button.solisac_update_charge_discharge_times`?**
**A: no.** Swept every `button.*` entity on the instance. The only Solis buttons are
`solisac_update_charge_discharge_times` (`_1/_2/_3`, one per timed slot) and `solisac_sync_rtc`.
The apply step is **specific to the timed-slot surface**, not a general property of this inverter,
so it does not explain the RC path's behaviour. Lead closed.

The `solis_control` overlay exposes exactly four entities — `switch.…rc_force_charge`,
`sensor.…rc_force_charge_power`, `sensor.…rc_timeout`, `sensor.…work_mode_bitfield`. No commit
surface among them.

### `select.solisac_energy_storage_control_switch` = `Self-Use`

Same register as the work-mode bitfield (`35` = 32 + 2 + 1; bit semantics **unverified**, do not
assume). Its option list names grid-charging permission explicitly:

```
Self-Use - No Grid Charging          Self-Use - No Timed Charge/Discharge
Timed Charge/Discharge - No Grid Charging    Self-Use                   ← current
Backup/Reserve - No Grid Charging    Off-Grid Mode / Battery Awaken (+Timed)
Feed-in priority - No Grid Charging  Backup/Reserve (- No Timed C/D)
Feed-in priority - No Timed Charge/Discharge / Feed-in priority
```

**Hypothesis: grid charging is gated by operating MODE, not (only) by SoC.** It accounts for every
observation of this run, and is compatible with — not a rival to — the SoC gate:

| observation | explanation |
|---|---|
| forced **discharge** actuated at 78% | discharge is normal Self-Use behaviour, never mode-gated |
| forced **charge** refused at 79%, flag held 3m22s | grid charge mode-gated; SoC irrelevant |
| owner: force charge only ever worked below ~20% | emergency grid recharge permitted below `battery_minimum_soc` **regardless of mode** |

Together: grid charge is mode-gated in normal operation, with an SoC-triggered override beneath the
reserve floor. If correct, the map spent two months forcing a charge through a path the inverter
was configured to forbid.

**Status of the run's headline finding:** "firmware SoC gate" is downgraded from *result* to *one of
two candidate mechanisms*, with **mode the more likely primary**. Evidence here is circumstantial
(an option list, not a measurement).

### Testing it — deferred, own sitting

A mode change alters inverter behaviour **at all times**, not just during a test, and the runbook
currently treats any work-mode change as an abort condition. Requirements before attempting:

- Record the known-good return value: `Self-Use`, bitfield **`35`**.
- Confirm which mode permits grid charging *and* preserves timed charge/discharge (the 23:30–05:30
  timed charge slot must keep working).
- Person present; revert immediately; cheap-rate window.
- Note `select.solisac_energy_storage_control_switch` writes are themselves in the known-flaky set —
  a failed revert is a live possibility. Verify the bitfield reads `35` again before leaving.
## 19:00 BST — timed-slot actuation, measured

Slot programmed by hand at 18:25 (19:00–20:00, `timed_discharge_current` 90.0 A). **No writes
during this window.** Note the event is confounded for Axle-attribution purposes (see above); what
follows characterises the **timed-slot surface**, which is the useful part.

| time (BST) | batt W | batt A | SoC | grid W | 43135 | mode |
|---|---|---|---|---|---|---|
| 18:59:30 | +674 | 12.8 | 75 | −13 | off | 35 |
| **18:59:45** | **+2296** | **44.0** | 75 | **+485** | off | 35 | ← ramp begins |
| 19:00:00 | +3187 | 61.3 | 75 | +2182 | off | 35 | ← full rate |
| 19:00:09 | +3187 | 61.3 | 75 | +2182 | off | 35 | ← Axle sensor → `in_progress` |

### Findings

1. **62.5 A DC is the inverter's hardware ceiling** (owner-confirmed), not a configurable clamp.
   The slot *accepts* 90.0 A but the hardware delivers 61.3 A. `number.solisac_battery_discharge_current`
   **reports** that ceiling; it is not a rate knob.

   **Driver consequences ([#84](https://github.com/Kylevdm/ha-spark/issues/84)):**
   - Max deliverable ≈ **62.5 A DC ≈ 3.3 kW** at ~52.7 V. Any commanded rate above it is accepted
     silently and not delivered — plans must clamp to the ceiling, or expected energy will be
     over-forecast (here, 90 A commanded vs 61.3 A delivered ≈ 30% optimistic).
   - DC amps throughout — convert by battery voltage before comparing to grid or fuse limits.
   - Consistent with the overnight charge taper table (57–59 A observed), i.e. the same ceiling
     applies in the charge direction.

   **Open:** whether `timed_discharge_current` controls rate *below* the ceiling is **untested** —
   the only value tried (90 A) was above it, so the observed 61.3 A proves clamping, not control.
   A slot set to e.g. 30 A would settle it, and the driver needs that answer before it can throttle.
2. **The inverter acts on its own RTC, ~15 s ahead of HA's clock.** Ramp began 18:59:45, fully
   established by 19:00:00. `button.solisac_sync_rtc` last pressed 2026-07-09 — two months of drift.
   Slot boundaries are not exactly when ha-spark believes; consider a periodic RTC sync.
3. **Ramp idle → full in <15 s.**
4. **The RC path is untouched throughout** — 43135 `off`, mode `35`, `power_switch` `On`. The
   timed-slot surface is fully independent of the RC registers.
5. Arithmetic consistent: 61.3 A × ~52 V ≈ 3.2 kW battery − ~1.0 kW house load = 2182 W exported.
6. Axle's `event_window_state` flipped to `in_progress` at 19:00:09, **after** actuation — it is
   reporting, not commanding.

### ⚠️ Scope: the rate ceiling is a **Solis-only** constraint

Owner-scoped 2026-09-07: "For the Solis inverter only that is the constraint."

- 62.5 A DC ≈ **3.2 kW**, flat across SoC (measured 3.12–3.20 kW, 40%→99%).
- A full 26.88 kWh charge needs **~8.4 h**; the 23:30–05:30 window is **6 h ≈ 18.8 kWh ≈ 70%
  of pack**. **This battery cannot be refilled from empty in one cheap-rate window.** From 20%
  it reaches ~90%; from 30% it just makes 100%.
- Do **not** generalise either figure. Max rate is a **per-inverter measured capability**;
  the window arithmetic is specific to this pack size and this tariff. Big face's ~10 kWh
  AlphaESS shares neither (#72).
- The 2% voltage rise across the SoC range does **not** make high SoC charge faster — LFP's flat
  plateau cancels it. Any "keep SoC high for more kW" reasoning is unsupported on this pack.

### 20:00 BST — clean termination, and the window measured

| time (BST) | batt W | batt A | SoC | grid W |
|---|---|---|---|---|
| 19:59:30 | +3191 | 61.5 | 64 | +2037 |
| **19:59:45** | **+1406** | **26.9** | 64 | **+1391** | ← ramp-down begins |
| 20:00:00 | +922 | 17.6 | 64 | −1 | ← house load only |
| 20:00:09 | — | — | 64 | — | Axle sensor → `unknown`/`off` |

**Rate held rock steady for the full hour: mean 61.3 A / 3183 W over 236 samples,
min 61.1 A, max 61.5 A (±0.2 A).** No drift as SoC fell 75% → 64%, confirming the flat
LFP plateau from the history analysis. No overshoot, no hunting.

#### Findings

1. **Both edges fire ~15 s early** — ramp-up began 18:59:45, ramp-down 19:59:45. The symmetry
   rules out a ramp artefact and confirms **inverter RTC skew ≈ 15 s ahead of HA's clock**
   (`button.solisac_sync_rtc` last pressed 2026-07-09). The driver should sync RTC periodically
   rather than assume slot boundaries align with HA time.
2. **Termination is clean and boundary-exact** — no overrun, no residual export, straight back to
   serving house load. The timed-slot surface self-terminates reliably, unlike the RC path where
   the enable flag stayed set after actuation ceased.
3. **The RC path was untouched for the entire hour** — 43135 `off`, mode `35`, `power_switch` `On`.
4. Energy: ~3.18 kWh discharged for 11 SoC points. Implied usable capacity ~28.9 kWh against a
   26.88 kWh nominal — but SoC resolution is 1% (±0.27 kWh here), so this is **consistent with
   nominal, not a refinement of it**. Do not treat as a capacity measurement.
5. Axle's own sensors only ever *followed* the actuation (flipped at 19:00:09 and 20:00:09, nine
   seconds after each edge). Consistent with Axle as an instruction source, not a control surface —
   still not proof, since the slot was owner-programmed.

**Verdict on the timed-slot surface:** precise, steady, self-terminating, boundary-accurate to the
inverter's own clock, and fully observable over local modbus. On this evidence it is a far better
control surface for ha-spark than the RC path.

## Overnight, 23:30–05:30 BST — timed **charge**, the mode/SoC gate settled (#83 observation)

Housekeeping first: the 19:00–20:00 BST timed-discharge slot from above was event-specific
(2026-09-07 Axle event), so at 19:34:04 UTC `timed_discharge_start_hours`/`_end_hours` were zeroed
by hand and `button.solisac_update_charge_discharge_times` pressed to commit. Confirmed at rest —
it will not export again at 19:00 daily.

Runbook: `docs/runbooks/OVERNIGHT-83-observation.md`. Read-only throughout — no writes, by anyone,
during the window. Recorders: `live_fire_recorder.py` → `run-83-overnight.csv` (2,595 rows) and
`axle_observer.py` → `axle-83-overnight.csv` (2,596 rows), both stopped cleanly afterwards.

**The question this settled:** the live-fire found a forced **charge** refused via the RC register
(43135) at 79% SoC, against a forced **discharge** that actuated fine at the same SoC — leaving a
mode gate (`Self-Use`, whose option list names grid-charging permission explicitly) and an SoC gate
(`battery_minimum_soc` = 20, an emergency-recharge floor) indistinguishable. The inverter's own
**pre-existing** 23:30–05:30 timed-charge slot (60 A, no PV available at night) discriminates them
for free: any charge in that window is necessarily from the grid.

### 23:30 BST — charge start

| time (UTC) | batt W | batt A | SoC | grid W | 43135 | mode |
|---|---|---|---|---|---|---|
| 22:29:30 | +1082 | 20.7 | 53 | +11 | off | 35 |
| **22:29:44** | **−3075** | **57.7** | 53 | **−3893** | off | 35 | ← charge begins |
| 22:29:59 | −3139 | 58.8 | 53 | −4061 | off | 35 | ← full rate |

**Charging started at 53% SoC** — well above the 20% `battery_minimum_soc`/`force_charge_soc`
floor, on the plain `Self-Use` mode (not one of the `- No Grid Charging` or `- No Timed
Charge/Discharge` variants). `meter_active_power` swung to **≈ −4,000 W**, confirming the polarity
predicted from the earlier export reading (+2182 W during the 19:00 discharge slot): **negative =
import**.

### Findings

1. **Grid charging is not blocked by mode or by SoC in the timed-slot path.** This is the
   runbook's predicted "most decisive result available without writing anything," and it held:
   mode stayed `Self-Use` / bitfield `35`, 43135 stayed `off`, for the entire ~7-hour window — no
   writer ever touched either. **The refusal found in the live-fire is specific to the RC path**,
   not a property of mode or SoC. Both gate hypotheses from the earlier section are demoted to
   RC-path quirks; the timed slot is confirmed as the control surface of interest for
   [#82](https://github.com/Kylevdm/ha-spark/issues/82)/[#84](https://github.com/Kylevdm/ha-spark/issues/84).
2. **A third independent clock-offset measurement, same result.** Charge began 22:29:44 UTC — 1 s
   off the 22:29:45 predicted from the ~15 s RTC lead measured on both edges of the 19:00 discharge
   slot. Three edges now agree (19:59:45, 18:59:45, 22:29:44): the inverter's RTC runs ~15 s ahead
   of HA's clock on every boundary tested so far.
3. **Achieved current sits a little under commanded, not clamped to the ceiling.** Clean stats over
   the full charge (22:29:44–03:03:46 UTC, n=1079, excludes ramp and post-full-SoC samples):
   current **mean 58.0 A, range 52.8–58.8 A** against a commanded 60.0 A and a 62.5 A hardware
   ceiling. Unlike the 90 A discharge command that clamped hard to 61.3 A, the slot current field
   *does* control rate below the ceiling — but doesn't deliver the full commanded value either
   (~97%, consistently, not noise: the range never touches 60 A across 1,079 samples). Battery
   power held **−3155 W mean** (−3195 to −2888 W); grid import **−4051 W mean** (−6584 to −3133 W,
   the one −6584 W sample a single-poll house-load transient unrelated to the charge, self-resolved
   next poll).
4. **SoC reached 100%, and the taper is a hard cliff, not a ramp** — matching the live-fire's
   discharge taper table. Last full-rate sample: 03:03:46 UTC, 57.3 A / −3151 W / 99%. Next poll,
   15 s later: 03:04:01 UTC, **0.0 A / 0 W / 100%** — current and power both hit zero in one step,
   no ramp-down. The pack then sat flat at 100%/0 A/0 W for **~1h26m** until the slot's own end.
5. **Clean, boundary-exact termination, same as the discharge slot.** `power_switch` blipped
   `On → Off → On` over 04:30:00–04:30:12 UTC — the slot's own close, at the same ~15 s-early
   offset as the other edges — then self-use discharge resumed normally (~610–625 W / 11.3–11.7 A,
   in line with baseline house load). No overrun, no residual charge current.
6. **The RC path was untouched for the entire night** — 43135 `off`, mode `35` — confirming (5) from
   the 19:00–20:00 section generalises to a 7-hour window, not just one hour.

### Noise, disregarded

- `communication_health` flipped to `Degraded` intermittently (323 of ~2,595 rows) with no effect
  on entity availability — consistent with the live-fire's standing note that `Degraded` alone is
  not a problem.
- Two isolated rows (19:38:38–42 and 20:48:09–13 UTC, both **before** the charge window opened) read
  a spurious `SoC = 100` alongside stale-looking battery-power values, both flagged `Degraded`.
  Cache-staleness in the integration during a `Degraded` blip, not real inverter state — the real,
  sustained 100% (0 A / 0 W, `Healthy`) only begins at 03:04:01 UTC as in finding 4.
- One `sensor.solisac_battery_current` read `unavailable` for a single poll at 01:24:21 UTC,
  self-recovered next poll. No guard (health/mode/43135/power switch) was affected.
- `select.solisac_power_switch` read `unknown` for one poll at 22:59:45 UTC, mid-charge, unrelated
  to any programmed boundary — reverted to `On` immediately. Treated as sensor noise, not a real
  dispatch-off event.

### Summary against the observation's questions

| question | answer |
|---|---|
| Grid import through the window, charging above 20% SoC? | **Yes** — charge began at 53% SoC, held ~3.15 kW from the grid for 4h34m. Confirms neither mode nor SoC gates the timed-slot path; demotes both gate hypotheses to RC-path quirks. |
| Achieved current vs. commanded 60 A? | **58.0 A mean, never reaching 60 A** — the field controls rate below the 62.5 A ceiling (unlike the 90 A discharge test) but under-delivers by ~2 A throughout, consistently. |
| `meter_active_power` sign under a real ~3 kW import? | **Negative = import**, confirmed (~−4,000 W mean), completing the polarity read the live-fire couldn't settle from a near-zero grid baseline. |
| Does SoC reach 100%, and what happens at the top? | **Yes** — hard cliff to 0 A/0 W in one 15 s poll step at 99%→100%, no ramp, then flat until slot end. Matches the discharge taper table. |
| 43135 and the work-mode bitfield throughout? | **Unmoved** — `off` / `35` for the entire night. |
