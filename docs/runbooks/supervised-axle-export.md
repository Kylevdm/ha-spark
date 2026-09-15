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
- [ ] The four clocks agree before real control is enabled. Confirm that the
      configured `timezone` matches Home Assistant's timezone, the add-on
      container's UTC clock matches a trusted UTC source, and the Solis RTC
      shows the same local date, hour, and minute. Record any seconds offset
      and its direction. Do not continue if any comparison has an unexplained
      offset, or if the known offset can cross a Slot 1 minute or date
      boundary. Slot 1 stores a date-less local wall-clock window, so a
      timezone or clock mismatch can arm it on the wrong day or at the wrong
      time.
- [ ] SoC is fresh, finite, and in the configured 0–100% range.
- [ ] No dispatch hold overlaps the planned export window.
- [ ] The event came from a fresh explicit Axle API/HA source. Never type an
      inferred start or end time.
- [ ] `notify_service` names the intended HA `notify.<service>` target. A blank
      value disables notices; notifications are reminders, not proof of a write.
- [ ] Record a baseline timestamp, SoC, battery power, solar power, house load,
      grid power, inverter power switch, work mode, and the Solis Slot 1 window.

Record the clock check before proceeding. Use UTC for the record timestamp and
write down the exact timezone and clock readings. The Solis RTC reading must
come from the supported local inverter interface, not an inferred event time.

| Check | Result |
| --- | --- |
| Checked at (UTC) | |
| Configured `timezone` | |
| Home Assistant timezone | |
| Add-on container UTC | |
| Solis RTC and timezone basis | |
| Largest unexplained offset | |
| Operator / outcome | |

The largest unexplained offset must be zero before the event. A known offset
must be recorded with its direction and remain within the same date and minute
as the reference clock. Keep the record with the event evidence for #134. A
failed or incomplete clock check is an abort condition. Leave
`proactive_mode` at `simulate` and do not enable real control until the check
passes.

The accepted-event notice is the preparation prompt. It includes the event
window, planned export, DNO limit, and the requirement to keep a person present.
If the event is changed, the changed identity receives a new accepted notice.
Repeated polls of the same identity do not repeat it.

## Enable and observe

1. Confirm the accepted notice and the planner output identify the same event.
2. Set `proactive_mode` to `on` through the normal add-on configuration surface.
3. Confirm the control authority is still `ha_spark`, the power switch is `On`,
   and the fresh SoC guard passes.
4. Watch the log for the verified Solis timed-discharge current and Slot 1
   window. The start notice is sent only after those writes have read back.
5. At the event start, record the timestamp and the first battery/grid response.
   Confirm the direction is export and that grid export remains below the DNO
   limit. Capture the command, read-back, battery power, solar power, house
   load, grid power, SoC, and event identity.

The start notice means the production driver observed a successful read-back; it
does not replace telemetry confirmation.

## Abort ladder

Stop the run and leave the installation in the safe state if any of these occur:

- event identity, direction, or window changes unexpectedly;
- event data is stale, malformed, or no longer explicit;
- SoC, power switch, control authority, work mode, or overlay health fails;
- a current or Slot 1 write is rejected or does not read back;
- battery, solar, house-load, or grid telemetry cannot confirm the expected
  direction or the export limit is approached;
- any incumbent automation or manual writer changes the same control surface;
- export continues outside the paid event window.

An unreadable Axle read is not a cancellation: ha-spark preserves a still-live
verified export rather than clearing it blindly. Do not override that protection
with a manual window. Wait for recovery or use the operator's safe shutdown
procedure.

The abort notice names the failed guard or operation and the safe result. A
notice is not evidence that cleanup succeeded; verify the Slot 1 read-back.

## Cleanup and handoff

1. After the event ends, allow the normal provider transition to drive cleanup.
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
  authorised long enough to verify the safe state. A forced process kill is not
  guaranteed to run cleanup and requires a human overlay check before real
  control is enabled again.

## Evidence record

Keep one row per transition with UTC timestamp, event identity, event window,
planner output, command/read-back result, SoC, battery power, solar power,
house load, grid power, notification transition, and operator action. Attach
the completed record to the event proof ticket [#134](https://github.com/Kylevdm/ha-spark/issues/134).
