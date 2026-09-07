# Overnight observation — timed charge, 23:30–05:30 (#83 follow-on)

**Read-only. No writes to Home Assistant, by anyone, at any point.** Purely an observation of
the inverter's own pre-existing timed-charge slot.

Prereq context: `RUN-83-log.md` (the live-fire results). This document is self-contained enough
to start from cold.

## Start the recorders

From the repo root, with `HA_URL` + `HA_TOKEN` in `.env`:

```bash
nohup python3 docs/runbooks/live_fire_recorder.py --interval 2 --csv docs/runbooks/run-83-overnight.csv \
  > /tmp/rec.log 2>&1 &
nohup python3 docs/runbooks/axle_observer.py docs/runbooks/axle-83-overnight.csv \
  > /tmp/obs.log 2>&1 &
```

Both are read-only and stdlib-only. Verify with `tail -3 docs/runbooks/axle-83-overnight.csv` —
rows appear only on change, so a few seconds may pass before the first one.

⚠️ **Timestamps are machine-local = UTC, one hour behind BST.** 23:30 BST = 22:30 UTC.

## The question this answers

The live-fire found that a forced **charge** through the RC register (43135) is refused at 79% SoC —
the flag reads back set for minutes with no current — while a forced **discharge** actuates fine at
the same SoC. Two candidate mechanisms, currently indistinguishable:

1. **Mode gate** — `select.solisac_energy_storage_control_switch` is `Self-Use`, and its option list
   names grid-charging permission explicitly (`… - No Grid Charging` variants exist).
2. **SoC gate** — firmware permits grid charge only below `battery_minimum_soc` (20) as an
   emergency recharge.

**The overnight timed charge discriminates them, for free.** It runs 23:30–05:30 at 60 A with no
PV available, so any charge is necessarily **from the grid**. [#100](https://github.com/Kylevdm/ha-spark/issues/100)
recorded it taking the pack from ~50% to 100%.

| observation | conclusion |
|---|---|
| grid import through the window, charging well above 20% SoC | Grid charging is **not** blocked by mode or by SoC in the **timed-slot** path. The refusal is **specific to the RC path** — which makes the timed slot the control surface for #82/#84, and demotes both gate hypotheses to RC-path quirks. |
| charging stops at/below some SoC threshold | An SoC gate is real and applies to the timed path too. Record the threshold. |
| no grid import at all | The premise is wrong — re-examine #100. |

The first row is the expected outcome and would be the **most decisive result available without
writing anything**.

## What to record

- **Wall-clock start of charging vs. the 23:30 boundary.** The live-fire measured the inverter's RTC
  running **~15 s ahead** of HA (symmetric on both edges of the 19:00–20:00 slot). Confirm on a
  second, independent slot.
- **Achieved current vs. the commanded 60 A** (`number.solisac_timed_charge_current`). The
  hardware ceiling is **62.5 A DC**, so 60 A should be delivered in full — unlike the 90 A discharge
  command, which clamped to 61.3 A. This is the clean test of whether the slot current field
  actually **controls** rate below the ceiling, which the live-fire could not settle.
- **`sensor.solisac_meter_active_power` sign.** Negative = importing. The live-fire could not
  re-verify this polarity because grid sat at ~0; a 3 kW import settles it.
- **Whether SoC reaches 100% and what happens at the top** — the measured taper table shows a cliff
  to 0 A at full, not a ramp.
- **43135 and the work-mode bitfield throughout.** Both should stay `off` / `35`. If either moves,
  something else is writing.

## Abort / alarm conditions

None require action — nothing is being commanded. But flag in the write-up if seen:

- work-mode bitfield ≠ `35`
- `select.solisac_power_switch` → `Off` (an Octopus dispatch automation intervening)
- mass `solisac_*` unavailability

⚠️ `sensor.solisac_communication_health` reading `Degraded` is **not** on its own a problem — during
the live-fire it read `Degraded` for ten minutes while data flowed normally throughout. Judge by
entity availability, not by that sensor.

## Housekeeping

The **timed-discharge slot is armed 19:00–20:00 and repeats daily.** It was set by hand for the
2026-09-07 Axle event. If it was event-specific, zero `number.solisac_timed_discharge_start_hours`
and `…_end_hours` and press `button.solisac_update_charge_discharge_times` to commit — otherwise it
will export again at 19:00 every day.
