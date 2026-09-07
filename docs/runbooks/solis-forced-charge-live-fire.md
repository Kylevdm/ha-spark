# Runbook: Solis forced-charge live-fire

Executable procedure for [#83](https://github.com/Kylevdm/ha-spark/issues/83) — prove by hand,
with a person present, that a forced grid charge can be commanded on the Solis S5 AC-coupled
inverter, before any ha-spark driver code depends on the sequence.

**HITL.** Every write here is made by a human in the Home Assistant UI. ha-spark writes nothing;
`proactive_mode` is irrelevant to this run.

**No tier-A source exists for any Solis storage register.** Everything below is community-grade
evidence about a model that is not on the integration's confirmed list. That is why this run
exists — it is load-bearing, not a formality. A negative result is a valid resolution.

## Sign conventions — read this first

Derived from the measured overnight charge in
[#100](https://github.com/Kylevdm/ha-spark/issues/100#issuecomment-5571695125) and re-confirmed
arithmetically on 2026-09-07 (`-444 W` at `8.3 A × 53.6 V = 445 W`):

| reading | charging / importing | discharging / exporting |
|---|---|---|
| `sensor.solisac_battery_power` | **negative** | positive |
| `sensor.solisac_meter_active_power` | **negative** (import) | positive (export) |
| `sensor.solisac_battery_current` | unsigned magnitude — no direction | — |

> **Correction to check 1 on #83** (2026-07-30), which says to watch `battery_power` go *positive*.
> That polarity is backwards. A successful forced charge drives `battery_power` **negative**.
> Reading it the old way would score a working force as a failure.

Re-verify both polarities against the baseline in step 0 before trusting them.

## Preconditions

| # | precondition | why | status 2026-09-07 15:35 BST |
|---|---|---|---|
| 1 | Run **well outside 23:30–05:30** | The inverter's own timed charge is armed at 60 A. A run inside the window cannot distinguish our write from its schedule ([#100](https://github.com/Kylevdm/ha-spark/issues/100)). | ✅ any time before ~23:00 |
| 2 | **SoC ≤ ~60%** | At high SoC the BMS tapers, and "write rejected" is indistinguishable from "battery declined the current" — the exact ambiguity behind this map's history. | ❌ **84%** — not yet |
| 3 | **Low or no solar** | Grid import must be unambiguously caused by the force. | ❌ daylight — wait for dusk |
| 4 | **No Octopus dispatch slot during the run** | `automation.solis_off_dispatch_slot_starts_daytime` writes `select.solisac_power_switch` → `Off` mid-run, which would stop the test and confound it. Check `binary_sensor.octopus_energy_..._intelligent_dispatching` is `off` and no slot is imminent. | ⚠️ `off` now, but smart charge is `on` (target 08:00) — recheck at start |
| 5 | **SoC reading is trustworthy** | The `12%` misreport of 2026-07-30 was the BMS lying plausibly. A false low SoC makes any negative result inconclusive. | ✅ 84% consistent with the overnight charge to 99% |
| 6 | Overlay healthy | The instrument. | ✅ 43135=0, 43136=0 W, 33132=35, 43282=0 |
| 7 | Person present, ready to abort | Forced grid charge is the most expensive thing this project can get wrong. | — |

**Practical slot:** SoC falls through the day from the 99% overnight peak and the timed window
re-arms at 23:30, so the natural window is **after dusk and before ~23:00**, on a day with enough
house load to pull SoC down. Do not trade SoC headroom away to save money — 1.5 kW for 10 minutes
is ~0.25 kWh, pennies even at the current peak rate of ~30 p/kWh.

## Instrumentation

Watch throughout. Baseline all of them with a wall-clock timestamp before step 1.

| what | entity | at rest |
|---|---|---|
| RC enable read-back | `switch.solis_control_rc_force_charge` | `off` |
| RC power read-back | `sensor.solis_control_rc_force_charge_power` | `0` |
| RC timeout read-back | `sensor.solis_control_rc_timeout` | `0` |
| work mode bitfield | `sensor.solis_control_work_mode_bitfield` | `35` — **any change means something wrote 43110: abort** |
| battery power | `sensor.solisac_battery_power` | signed, negative = charging |
| battery SoC | `sensor.solisac_battery_soc` | |
| battery current | `sensor.solisac_battery_current` | unsigned |
| grid | `sensor.solisac_meter_active_power` | negative = importing |
| bus health | `sensor.solisac_communication_health` | `Healthy` |
| incumbent writer | `select.solisac_power_switch` | `On` — if it flips, an automation intervened |

**Timestamp every step.** The settling delay is one of the answers being bought here.

### Write surfaces available

| register | what | surface | writable? |
|---|---|---|---|
| 43135 | RC force charge enable | `switch.solis_control_rc_force_charge` | ✅ overlay switch |
| 43136 | RC charge power | `sensor.solis_control_rc_force_charge_power` | ⚠️ **read-only** in the overlay — write via `modbus.write_register` |
| 43282 | RC timeout (watchdog) | `sensor.solis_control_rc_timeout` | ⚠️ read-only — write via `modbus.write_register` |
| 43135 (alt) | force charge | `select.solisac_inverter_battery_control_override` | write-only, no read-back; the fallback path |

`number.solisac_battery_control_override_charge_power` **does not exist** on this instance, so the
solax path has no native rate control. Rate bounding, if needed, is `number.solisac_battery_charge_current`
— **DC amps**, multiply by battery voltage (~53.5 V) before comparing against anything on the AC side.

## Sequence

Ordering follows #87's companion-write rule: **enable first, then setpoints.** Solis firmware is
reported not to latch RC setpoints written before the enable. If they latch this way and not the
other, that is the root cause of the write-rejection history, confirmed.

### Step 0 — baseline

Record every instrumentation row above with a wall-clock time. Confirm the sign conventions:
if the battery is idle, nudge nothing — just check that `battery_current × battery_voltage`
matches `|battery_power|`.

### Step 1 — enable RC

Turn on `switch.solis_control_rc_force_charge` (writes 43135 = 1). **Note the wall-clock time.**

### Step 2 — measure the settling delay

Poll the switch until it reads `on` from a genuine register read, not its own optimistic write.
**Record how long that took** — that is the real `verify: delay`, currently a 20 s guess in the overlay.

### Step 3 — write the power setpoint (~1500 W)

```yaml
action: modbus.write_register
data: {hub: solis_control, slave: 1, address: 43136, value: 150}
```

`value: 150` assumes the register unit is **10 W — unverified**. Check
`sensor.solis_control_rc_force_charge_power`:

- reads **1500 W** → scale is 10 W, as assumed. Continue.
- reads **150 W** → scale is 1 W. Harmless; record it.
- reads **15000 W** → scale is 100 W. **Back off immediately** — write 43135 = 0.

### Step 4 — write the watchdog

```yaml
action: modbus.write_register
data: {hub: solis_control, slave: 1, address: 43282, value: 5}
```

Confirm `sensor.solis_control_rc_timeout` reads **5**. If it still reads `0`, the setpoint did not
latch even in the correct order — a significant negative result. Record it and stop.

### Step 5 — observe actuation

Within a minute or two expect grid import ≈ setpoint + house load, and `battery_power` to go
**negative**. Record:

- time from step 1 to first battery movement
- actual charge power vs. the 1500 W commanded
- whether it holds steady or drifts
- SoC at the time

### Step 6 — let the watchdog expire

**Do not disable manually.** Wait past 5 minutes and record whether the force self-terminates and
43135 returns to `0` on its own.

This is the highest-value observation in the run. The overlay has **no keep-alive** — core `modbus:`
writes once, unlike solax-modbus which resends every ~15 s ([#85](https://github.com/Kylevdm/ha-spark/issues/85)).
So if the force persists past the timeout, 43135 latches in hardware with nothing sustaining it and
nothing scheduled to stop it: that is the peak-rate runaway, reachable with no ha-spark bug at all.
If it self-terminates, the hardware watchdog is real and the dead-man's-switch design is available.

### Step 7 — re-run and revert manually

Repeat steps 1–4, then turn the switch **off**. Record:

- time from write to read-back `0`
- whether charging actually stops, and how fast
- **whether 43136 retains its value after RC goes off** — this settles #90's open question of whether
  43136 reads `0` at rest because it is unset, or because RC setpoints latch only while enabled

### Step 8 — repeat the force (check 10)

**A single successful force proves nothing.** Writes to this device are known to stick only
sometimes (`number.solisac_force_charge_soc` rejects most attempts). Run steps 1–2 at least three
more times and record how many took. Note any pattern against SoC, mode, or time since last write.

## Recording table

Copy this per attempt into the #83 resolution comment.

| step | wall clock | 43135 | 43136 | 43282 | mode | batt W | batt A | SoC | grid W | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 baseline | | | | | 35 | | | | | |
| 1 enable | | | | | | | | | | |
| 2 read-back on | | | | | | | | | | ← settling delay |
| 3 power write | | | | | | | | | | ← scale check |
| 4 timeout write | | | | | | | | | | |
| 5 actuation | | | | | | | | | | |
| 6 expiry | | | | | | | | | | ← watchdog real? |
| 7 revert | | | | | | | | | | |

## Abort conditions

- **Work-mode bitfield ≠ 35** — something wrote 43110. Switch off, stop.
- **Power read-back implausible** (step 3, 15000 W case).
- **Mass `solisac_*` unavailability** or `communication_health` not `Healthy` — bus trouble. Switch off, stop.
- **`select.solisac_power_switch` flips to `Off`** — an incumbent automation intervened mid-run. Results
  from that point are confounded; stop and rerun outside the dispatch slot.
- **Charge power materially exceeds the commanded setpoint** — the site has no rate ceiling you control.

**Hard stop ladder:** switch off `switch.solis_control_rc_force_charge` (43135 = 0) → failing that
`select.solisac_inverter_battery_control_override` → `Off` → last resort `select.solisac_power_switch` → `Off`.

## Questions this run answers

- Does a write to 43135 actually take, with read-back proof? *(the map's central unknown)*
- What is the real settling delay? → fixes `verify: delay` in the overlay.
- Does 43282 govern 43135 — is the hardware watchdog real? → decides the dead-man's-switch design.
- Does companion-write ordering matter? → likely root cause of the write-rejection history.
- Is 43136 honoured, what is its scale, and what rate ceiling does the site allow? → bounds the forced-charge rate.
- Does 43136 persist after RC goes off? → #90's open question.
- Do forced-charge writes stick *reliably*? → check 10.

## Checks deliberately not in this run

From the original check list on #83, still open but orthogonal to the write path:

- **Check 7 — photograph the model label.** The integration reports `model: None` and exposes no
  serial entity, so the exact designation is unconfirmed. Do it while you are at the inverter.
- **Check 9 — characterise the `force_charge_soc` rejection.** Worth its own sitting; it is a
  different register with a different failure mode.
- **Check 11 — restart HA mid-force.** Its premise has changed. It was written against solax-modbus's
  ~15 s resend loop, where a restart silently drops the keep-alive. The overlay has no resend loop at
  all, so **step 6 already tests the same latch-vs-transient question** without a restart. Re-run
  check 11 only if the force is driven through the solax `select` rather than the overlay switch.
- **Check 6 — timed-slot register conflict.** Settled by [#100](https://github.com/Kylevdm/ha-spark/issues/100):
  43141 is charge current, confirmed against the observed 60 A clamp.

## After the run

1. Post the filled recording table and findings as a resolution comment on
   [#83](https://github.com/Kylevdm/ha-spark/issues/83), then close it.
2. Append a one-line gist to the map's Decisions-so-far ([#78](https://github.com/Kylevdm/ha-spark/issues/78)).
3. Unblocks [#82](https://github.com/Kylevdm/ha-spark/issues/82) — the control-surface decision, now on measured facts.
