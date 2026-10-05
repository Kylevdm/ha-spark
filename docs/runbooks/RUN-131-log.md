# Commissioning log — #131, 2026-10-02 to 2026-10-05

[Commission the shipped Solis forced-charge path for Axle preparation](https://github.com/Kylevdm/ha-spark/issues/131):
supervised prototype commissioning of the native timed-slot charge path, not
unattended cutover.

> Compiled after the fact. The 2026-10-02 to 2026-10-04 rows summarise evidence
> already posted on #164, #173 and #134. The overnight run of 2026-10-04/05 comes
> from the add-on log the owner saved on 2026-10-05 (00:31 to 13:34 BST). There
> was no recorder CSV. **Timestamps are BST** (the add-on log's local time).

## Checklist

| Item | Result | Evidence |
| --- | --- | --- |
| Gateway: multi-host on, storage-type caching off | Pass | Owner confirmed, 2026-10-02 |
| Driver-issued write at 43143 lands and reads back | Pass | v0.19.1, 2026-10-03 14:30: deactivate, current and window all `[APPLIED]` with read-back ([#164](https://github.com/Kylevdm/ha-spark/issues/164#issuecomment-5969665164)) |
| Settling delay for the timed-slot registers | Pass | About 5–6 s per value, about 11 s for the 8-register deactivate. Read-back waits up to about 15 s since v0.19.1 ([#164](https://github.com/Kylevdm/ha-spark/issues/164)) |
| 43141 ×10 scale away from 60 A | Pass | 17 A written as raw 170, read back as 17.0 ([#164](https://github.com/Kylevdm/ha-spark/issues/164)) |
| First replan overwrites the resident Slot 1 program | Pass | Deactivate, then window write, replaced the resident 23:30–05:30 @ 90 A program ([#164](https://github.com/Kylevdm/ha-spark/issues/164)) |
| Write-if-changed suppresses a no-op replan | Pass, with caveat | `[SKIP] charge setpoint unchanged` observed. A failed or blocked apply is still not retried: [#168](https://github.com/Kylevdm/ha-spark/issues/168) |
| Physical charging response | Pass | Overnight 2026-10-04/05, below |
| Second, independent confirmation | Decided: operator check | See Decisions |
| Return to `simulate` | Pass | 17:33Z on 2026-10-04, before the manual Axle window ([#134](https://github.com/Kylevdm/ha-spark/issues/134)). The owner re-enabled `on` for the overnight run below |
| Write-count telemetry | Not done | Optional, not a gate. Carried in [`docs/fog.md`](../fog.md) |

## Overnight run, 2026-10-04/05 (v0.19.5, `proactive_mode = on`)

The first overnight on a build with the #173 current re-sizing and the #181
remaining-window sizing. The add-on restarted at 23:38.

| Time | Event |
| --- | --- |
| 22:31 | `[APPLIED]` deactivate Slot 1, 40 A, window 23:30–05:30 plus export 20:00–21:00 at 62.5 A, zero Slot 2 |
| 23:00 | `[BLOCKED]` SoC last reported 939 s ago; recovered 23:01. Next plan: SoC 30%, charge to 73% |
| 23:30 | `[APPLIED]` 42 A |
| 00:00 | Midnight replan: Axle event skipped, target cut to 69%, export window cleared. `[APPLIED]` 37 A, window 23:30–05:30 |
| 00:30–05:01 | Current re-sized each half-hour as SoC rose: 38, 31, 33, 28, 30, 25 A, all `[APPLIED]` |
| 05:30 | SoC 69% against a 69% target. `[APPLIED]` 0 A |

SoC went from 30% to 69%, meeting the target. Every apply read back. The three
`[BLOCKED]` applies (23:00, 06:00, 08:00) were all the SoC staleness gate refusing correctly.

The midnight replan dropped the 2026-10-05 Axle event. That is a planner defect,
not a charge-path one, and is tracked separately (see Follow-ups).

## Decisions

- **Second, independent confirmation:** an operator check, not a runtime check.
  The supervised runbook now asks the operator to confirm the sign of
  `sensor.solisac_battery_power` within 1–2 minutes of a charge or export
  starting. Register read-back stays the driver's only automatic check. A
  runtime cross-check is fog for unattended running.
- **Another overnight run before closing:** not needed. The 2026-10-04/05 run on
  v0.19.5 met its target.

## Caveats carried forward

- A failed or blocked apply is never retried while the setpoint is unchanged:
  [#168](https://github.com/Kylevdm/ha-spark/issues/168).
- The SoC staleness gate tripped repeatedly through the 2026-10-05 daytime
  (runs of up to 15 minutes) while the battery was idle, despite
  `battery_voltage_entity` liveness: see
  [#169](https://github.com/Kylevdm/ha-spark/issues/169) and
  [#179](https://github.com/Kylevdm/ha-spark/issues/179).
- `sensor.solisac_battery_power` does not see V2L current, which reaches the
  battery through the R48 rectifier. Only BMS current and SoC do.

## Follow-ups

Filed on [the supervised Axle export map](https://github.com/Kylevdm/ha-spark/issues/128)
when this log was written: the midnight-funding defect (blocks the paid-event
proof), funding an event from the post-event reserve on price, and a V2L top-up
request for an underfunded event.
