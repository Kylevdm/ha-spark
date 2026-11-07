# Runbook: supervised Axle export

Human-present procedure for a planned Axle export. Prepare in Observe, explicitly
choose Optimise, supervise the event, then return to Observe only after verified
cleanup. This is not evidence of unattended readiness. Do not enter an export
window by hand or use a parallel automation to write the inverter.

## Prepare in Observe

- [ ] Be present throughout and able to stop the run. Keep the operating-mode
      page and an independent view of battery and whole-site grid power open.
- [ ] Finish any handover to Observe. Check known authority, settled ownership,
      no transition and no outstanding cleanup. Observe writes nothing; it
      does not erase an inverter-resident charging schedule.
- [ ] Run `ha-spark health`. Resolve authority, transport, topology and provider
      failures and inspect the integration's repairs. Health is diagnostic,
      not an authority grant or proof of a live hardware write.
- [ ] On a native site, each inverter is validated, gateway caching is off,
      ha-spark is the sole gateway client, and `solis_control_hub` is explicitly
      empty. Complete the [native migration runbook](solis-native-migration.md)
      before the event, not during it.
- [ ] Disable conflicting inverter-writing automations. Confirm effective
      inverter and bank discharge limits, and the site export limit against
      the installation's permission and supply limit. Do not enable an
      inverter export limit to bypass native bound validation.
- [ ] Confirm protection evidence is configured and fresh, SoC is trusted and
      recovered, and the fallback charging schedule has explicit currents.
      No applicable hold may overlap the export; holds beat exports.
- [ ] Confirm the household clock and inverter clock agree. An unreadable,
      stale or untrusted clock is an abort condition; do not bypass its guard.
- [ ] Confirm the current plan names an explicit fresh Axle export event with
      the expected direction and window. Do not type inferred event times.
- [ ] Set `notify_service` to the intended bare HA notify service, such as
      `mobile_app_<device>`, without `notify.`. Blank disables native export
      notices; the deprecated `v2l_notify_service` is not their target. Check
      notification delivery before relying on it as a reminder.
- [ ] Record a baseline: timestamp, event identity and window, plan, SoC,
      battery power, solar power, house load, whole-site grid power, power
      switch, resident charging and discharge windows, and ownership/mode.

## Enter Optimise and arm native supervision

1. Request Optimise on the ingress operating-mode page. Check the reported
   preconditions and transition; acceptance alone does not prove activation.
   Optimise needs a fresh verified plan. A refused or deferred export with a
   safe resident state does not itself prevent plan verification, so an
   Optimise mode indication is not proof of export.
2. Under **Supervise this export**, confirm the displayed planned event and
   every inverter's rated AC output in watts, then choose **Arm supervised
   export**. This is the owner's event-scoped export supervision, carried by
   the authenticated add-on command `supervise_export`, not an HA service.
   Every inverter on the connection needs a bound tied to its validated
   identity and gateway ownership claim. Their rated outputs must together
   fit the site export limit. An enabled inverter export limit is not a
   native device export bound, and ha-spark never writes it.
3. Keep the supervision id from **Export supervision** ready
   for **End supervision**. Native export runs only in Optimise with live
   supervision, never in Protect, Observe, fallback or temporary manual
   control. A new supervision does not clear a stop latch.
4. Inspect **Export bound**, **Export supervision** and **Export record**.
   `armed` means the window was verified before its start; `active` records
   an export verified inside the window. Slot 1 has no date: programming is
   deferred when its clock face would fire before the event. Do not work
   around a deferral by programming the inverter yourself.
5. At the start, confirm battery discharge and whole-site grid direction from
   fresh telemetry, not just the plan or notice. Keep grid export below the
   site export limit and watch SoC, solar, house load and guard state until
   cleanup completes. Do not wait for the tripwire if anything looks unsafe.

The fixed native export command is 62.5 A per exporting inverter. It must fit
each effective inverter limit and the combined bank discharge limit; an owner
rating does not relax those limits. A hold, untrusted SoC, failed power-switch
check or failed read-back can still refuse an otherwise supervised export.

The CLI exposes `ha-spark export supervise --rating <inverter_id>=<watts>`
(repeat `--rating` per inverter), optional `--event`, and
`ha-spark export end <supervision_id>`. Supervision and ending it require the
integration to recognise an owner: the owner-only local CLI is accepted as
`cli`; an ingress identity must resolve to a non-system administrator. Neither
path bypasses the export guards.

## Watch lifecycle notices

The integration keeps an ordered durable transition log; the add-on delivers
notices after reconnecting, including transitions made while it was down.
Notices are deduplicated by event and lifecycle step. Delivery is best-effort
and does not control hardware. No notice replaces telemetry or cleanup checks.

| Integration transition | Notice and meaning |
|---|---|
| `accepted` | **Axle export accepted**: supervision was saved; prepare and stay present |
| `armed` | No notice; inspect the verified export record |
| `started` | **Axle export started**: timed-discharge current and window read back inside the window |
| `cleanup_verified` | **Axle export cleanup verified**: resident export window cleared and read back |
| `clock_hold` | **Axle export held: inverter clock**: retryable refusal, not a terminal abort |
| `unverified` | **Axle export held: write not verified**: the window or discharge current did not read back before the event start; retried every pass, check its safe result. An event that starts without a verified arm is then **aborted** |
| `aborted` | **Axle export aborted**: terminal refusal or failed operation; inspect its reason and safe result |
| `tripped` | **Axle export aborted**, reason **site export over limit**: the stop latch is saved, not proof cleanup succeeded |

`not_supervised` generates a terminal abort notice. A trip shares the abort
notice's deduplication step; do not expect a second abort message for the same
event. A failed or unverified cancel can report **the Solis export state was
not verified; switch to Observe and inspect it**. Follow the verified handover
and inspect the device; do not read this as confirmation that it is safe.

## Tripwire and stop latch

While supervised export is live, two consecutive advancing fresh whole-site
grid readings more than 200 W above the site export limit stop every export
on that connection. Readings must be finite and no more than 60 s old, with
up to 5 s of future clock skew allowed. A missing or stale reading never cuts
and never resets the count: it warns after 60 s of missing evidence and raises
a repair after 2 minutes. Export continues on the device export bound; PV-only
export is never curtailed. Missing telemetry is a reason for the person
supervising to abort, not a reason to weaken this guard.

The integration saves a durable stop latch before any cleanup write, drops all
supervision bounds on the connection and marks the stopped records
`stop_pending` with `stop_reason: tripped`. If the save fails it writes
nothing. The latch survives restarts and Re-probes. Even if a new supervision
is confirmed, the same event is refused as `tripped` until the stopped export
window's end. Do not retry that event to bypass the stop.

`stop_pending` means cleanup is still owed. Only exact zero confirmation makes
the record `cleared` with `cleanup_verified: true`. A latch can remain after
successful cleanup; cleared hardware is not permission to resume that event.

## Abort

Abort if the event changes unexpectedly, evidence becomes untrustworthy, a
hold applies, telemetry cannot confirm safe direction and limits, a writer
conflicts, a write fails, or export runs outside the paid window.

1. Choose **End supervision**, using the recorded id, to end export early.
   Alternatively request Observe on the mode page or use the administrator's
   `ha_spark.set_observe` service. These are requests for verified cleanup,
   not evidence that the inverter has already stopped.
2. Watch **Export record**, the resident discharge window and battery/grid
   telemetry. If the command is refused or cleanup fails, keep the person
   present and inspect the device using the installation's safe shutdown
   procedure. Do not kill or unload the integration as a cleanup shortcut.
3. Request Observe and wait for the handover to finish. Leaving Optimise
   cancels export, writes and verifies the fallback charging schedule as the
   starting schedule, and releases owned holds before committing Observe.
   A failed handover remains visible; committed Observe itself writes nothing.

An unreadable event source is not a trusted cancellation. Only a still-live,
verified record with current supervision can be preserved; untrusted reads
cannot authorise a new window. Use the explicit abort path rather than
manually replacing the resident window.

## Verify cleanup and return to Observe

1. At the event end let normal cleanup run, or end supervision early. Check
   the discharge half is zero, not merely that the paid event ended. The
   native clear requires exact read-back plus five further matching reads a
   second apart. Transition cancellation and fallback-entry cancellation also
   zero and verify the other discharge slots. A fallback charge write alone
   never retires an export record.
2. Check **Export record** is `cleared`, `cleanup_verified` is true, the
   resident discharge window is empty and the battery has stopped the
   scheduled export. Record the cleanup notice, but do not use it alone as
   proof. Solar can still export; the tripwire never curtails PV-only export.
3. If clearing fails, the record remains outstanding and retries in non-Observe
   modes, including fallback. Keep supervision of the installation until the
   device is safe. Observe does not perform cleanup writes after commitment.
4. Request Observe, wait for a completed handover, then record final mode,
   switch, resident windows, SoC and grid power. Only then end the supervised
   session. Do not assume a restart or unload cleared a repeating window.

## Restart and fallback recovery

A saved export is not permission to re-arm it. After restart, the integration
re-verifies a still-live record against the actual resident discharge half.
Only an exact match with current authority and supervision may be preserved;
otherwise it is a cleanup obligation. Observe never writes, even for recovery.
A failed startup reconciliation must be resolved before ordinary control.

Fallback entry ends supervision and attempts cancellation before charging;
failed cancellation is retried while fallback proceeds. Recovery applies a
fresh plan, never a consumed command, and re-offered export needs fresh owner
supervision. A changed identity or gateway claim ends the affected bound.
The stop latch is independent of all of these recovery paths.

## Legacy add-on-owned sites

The legacy overlay path still ships. It uses the daemon's authority store,
`<solis_control_hub>` read-back sensors and `ha-spark mode set optimise` /
`ha-spark mode set observe`. The native supervision command, tripwire and stop
latch above are not guards on that legacy path. Keep the same human-present
Observe → Optimise → verified cleanup → Observe procedure.

Before enabling Optimise, verify the overlay sensors and household clock with
`ha-spark health`. Once writes are authorised, `ha-spark solis sync-clock`
requests verified clock sync through the daemon. Legacy arming refuses an
unreadable clock, a reading over 60 s old or drift beyond
`inverter_clock_tolerance_minutes`. Watch the verified current and Slot 1
window, then battery/grid telemetry. At cleanup confirm Slot 1 discharge reads
`00:00-00:00` and wait for the verified Observe handover. Do not mistake native
diagnostics or a notification for legacy device read-back.

## Evidence record

Keep a row per transition: timestamp, event identity/window, authority owner,
mode and epoch, supervision id, plan, command and read-back, record state,
cleanup verification, stop reason, SoC, battery/solar/house/grid power, notice
and operator action. Keep gateway addresses and credentials out of shared
records. Live hardware proof belongs to the live-check ticket; this runbook
does not claim it has passed.
