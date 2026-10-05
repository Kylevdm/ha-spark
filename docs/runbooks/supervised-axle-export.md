# Runbook: supervised Axle export

Human-present procedure for the first production-path Axle export event. This
runbook covers the prototype in [#128](https://github.com/Kylevdm/ha-spark/issues/128)
and the implementation in [#133](https://github.com/Kylevdm/ha-spark/issues/133).

This is supervised hardware control. The operator enables real control shortly
before the event, watches the inverter and household telemetry throughout, and
returns ha-spark to `simulate` after verified cleanup. Do not use a parallel HA
automation or enter an event window by hand.

## Before the event

- [ ] A person is present and can stop the run immediately.
- [ ] `proactive_mode` is `simulate` while the installation is prepared.
- [ ] The configured inverter device has `control: ha_spark` and the Solis
      overlay is healthy.
- [ ] The incumbent automations that write the Solis power switch or timed
      discharge window are disabled for this run.
- [ ] Clock check steps 1 to 3 pass in `simulate`. The sync (steps 4 and 5)
      runs right after real control is enabled, before the window arms (see
      [Clock check](#clock-check)). The configured `timezone` is the
      **household clock**, and everything else is checked against it. Slot 1
      stores a local wall-clock window with no date, which the inverter fires
      on its own clock, so a clock mismatch can arm it on the wrong day or at
      the wrong time.
- [ ] SoC is fresh, finite, and in the configured 0 to 100% range.
- [ ] `dno_export_limit_kw` matches the export limit in the installation's DNO
      approval. The default is the G98 limit, 3.68 kW.
- [ ] No dispatch hold overlaps the planned export window.
- [ ] The event came from a fresh explicit Axle API/HA source. Never type an
      inferred start or end time.
- [ ] `notify_service` names the intended HA `notify.<service>` target. A blank
      value disables notices. Notifications are reminders, not proof of a write.
- [ ] Record a baseline timestamp, SoC, battery power, solar power, house load,
      grid power, inverter power switch, work mode, and the Solis Slot 1 window.

### Clock check

Steps 1 to 3 run while still in `simulate`. Steps 4 and 5 run shortly before the
event, right after real control is enabled (the sync writes only when
`proactive_mode` is `on`) and before the export window arms.

1. **First native read only:** in Home Assistant, briefly enable the disabled
   `sensor.solisac_rtc` entity. Confirm `sensor.solis_control_inverter_clock`
   (`yy,mm,dd,hh,mi,ss`) shows the same date and time, then disable
   `sensor.solisac_rtc` again. The register map's only source is solax-modbus
   `plugin_solis.py`, so this cross-checks it once.
2. Confirm the add-on container's UTC clock matches a trusted UTC source.
3. Run `python -m ha_spark health`. `Household clock` must be ✓: HA's
   `time_zone` equals the configured `timezone`. Record the `Inverter clock`
   line; this is the error **before** the sync.
4. Run `python -m ha_spark solis sync-clock`. It must exit 0 and end with
   `[APPLIED] sync inverter clock to <timezone> (now … )`. Record the error
   **before** and **after** from its output. Exit 1 means nothing was
   confirmed: do not continue.
5. Run `health` again. `Inverter clock` must be ✓.

ha-spark itself refuses to arm the export window if the inverter clock is off
by more than `inverter_clock_tolerance_minutes` (default 5), if its reading is
over 60 s old, or if it is unreadable. It then sends an "Axle export held:
inverter clock" notice and retries each pass. Syncing clears the refusal.

Record the clock check before proceeding. Use UTC for the record timestamp.

| Check | Result |
| --- | --- |
| Checked at (UTC) | |
| Configured `timezone` / HA `time_zone` | |
| Add-on container UTC vs trusted source | |
| `solisac_rtc` cross-check (first read only) | |
| Inverter clock error before sync | |
| Inverter clock error after sync | |
| Operator / outcome | |

Keep the record with the event evidence for #134. A failed or incomplete clock
check is an abort condition. Return `proactive_mode` to `simulate`.

The accepted-event notice is the preparation prompt. It includes the event
window, planned export, DNO limit, and the requirement to keep a person present.
If the event changes, ha-spark sends a new accepted notice for the changed
event. Repeated polls of the same event do not repeat it.

## Enable and observe

1. Confirm the accepted notice and the planner output identify the same event.
2. Set `proactive_mode` to `on` on the add-on's **Configuration** tab.
3. Run [Clock check](#clock-check) steps 4 and 5 (sync, then `health`) and fill
   in the record. If the sync is not confirmed, return to `simulate`.
4. Confirm the control authority is still `ha_spark`, the power switch is `On`,
   and the fresh SoC guard passes.
5. Watch the log for the verified Solis timed-discharge current and Slot 1
   window. The start notice is sent only after those writes have read back.
6. At the event start, record the timestamp and the first battery/grid response.
   Confirm the direction is export and that grid export remains below the DNO
   limit. Capture the command, read-back, battery power, solar power, house
   load, grid power, SoC, and event identity.
7. Within 1–2 minutes of any charge or export starting, confirm the sign of
   `sensor.solisac_battery_power`: negative while charging, positive while
   discharging or exporting. This is the second confirmation on top of register
   read-back ([#131](https://github.com/Kylevdm/ha-spark/issues/131)). The
   sensor does not see V2L current, which enters the battery through the R48
   rectifier, so while V2L runs confirm with BMS current and SoC instead.

The start notice means the production driver saw a successful read-back. It
does not replace confirmation from telemetry.

## Abort ladder

Stop the run and leave the installation in the safe state if any of these occur:

- event identity, direction, or window changes unexpectedly;
- event data is stale, malformed, or no longer explicit;
- SoC, power switch, control authority, work mode, or overlay health fails;
- a current or Slot 1 write is rejected or does not read back;
- battery, solar, house-load, or grid telemetry cannot confirm the expected
  direction or the export limit is approached;
- any incumbent automation or manual writer changes the same controls;
- export continues outside the paid event window;
- the export visibly starts more than `inverter_clock_tolerance_minutes` away
  from the event start (the inverter clock is not where the sync left it).

An unreadable Axle read is not a cancellation: ha-spark preserves a still-live
verified export rather than clearing it blindly. Do not override that protection
with a manual window. Wait for recovery or use the operator's safe shutdown
procedure.

The abort notice names the failed guard or operation and the safe result. A
notice is not evidence that cleanup succeeded; verify the Slot 1 read-back.

## Cleanup and handoff

1. After the event ends, let the normal provider transition run cleanup.
2. Confirm the cleanup notice arrives only after the resident timed-discharge
   window reads back as `00:00-00:00`.
3. Record the cleanup timestamp, final Slot 1 values, final SoC, grid power,
   and any remaining plan or guard warnings.
4. If cleanup is not verified, keep the person present, leave real control
   disabled, and inspect the overlay before any retry. Do not assume a notice
   was proof of cleanup.
5. Set `proactive_mode` back to `simulate` and confirm the log reports computed
   actions without Home Assistant write services.

## Restart recovery

- Before restarting during a live event, capture the current event identity and
  verified end from the operator log.
- On restart, do not manually recreate the event. The persisted verified export
  record protects the resident window while the Axle read is unavailable, and
  the next trusted plan either continues it or performs verified cleanup.
- If the process is intentionally stopped after the event, keep writes
  authorised long enough to verify the safe state. A forced process kill may
  skip cleanup, so a person must check the overlay before real control is
  enabled again.

## Evidence record

Keep one row per transition with UTC timestamp, event identity, event window,
planner output, command/read-back result, SoC, battery power, solar power,
house load, grid power, notification transition, and operator action. Attach
the completed record to the event proof ticket [#134](https://github.com/Kylevdm/ha-spark/issues/134).
