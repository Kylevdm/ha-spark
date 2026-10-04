# ADR-0003: ha-spark is the sole owner of the Solis inverter

Status: Accepted (2026-09-06). Amended since:

- 2026-09-07: premise corrected (see "The overnight charge is
  inverter-resident")
- 2026-09-08: control surface corrected (see "The driven control surface is
  the timed-slot registers, not RC")
- 2026-09-09: reconciled with the implemented driver (see the runbook)
- 2026-09-15: power-switch lifecycle completed (see "The power-switch
  lifecycle is closed")

## Context

Under wayfinder map #78 (forced charge on the Solis S5 AC-coupled inverter), a
live inventory found that ha-spark is **not** the only writer to the inverter.
Four Home Assistant automations drive it, and resolving that ownership was
raised as a blocker (#86): nothing on the map could ship until it was settled.

The live picture (read-only inventory, 2026-09-06): all four incumbent
automations write **exactly one entity, `select.solisac_power_switch`
(`On`/`Off`)**, a coarse whole-inverter enable. None of them touch the
force-charge override (`select.solisac_inverter_battery_control_override`,
register 43135) or any setpoint. Together they do this:

- **Overnight (23:30 to 05:30):** turn `On`, letting the inverter grid-charge.
- **Daytime (05:30 to 23:30):** turn `Off` whenever an Octopus dispatch slot is
  live *or* the Zappi is charging the car, and back `On` when either ends.
- **At 05:30:** if a dispatch is still active, stay `Off`.

The two controllers collided directly. ha-spark's own Solis driver
(`ha_spark/devices/inverters/solis.py`) wrote `select.solisac_power_switch =
"Off"` during dispatch `holds`, through the configured
`inverter_power_switch_entity`. That is the *same entity*, on the *same
off-peak trigger*. The driver never wrote `On` back, so it relied on the
incumbent to re-enable the inverter. Two controllers on one entity is how the
map's most expensive failure happens: a forced grid charge left running into a
peak-rate period. A switch once flagged as a possible handover mechanism
(`switch.enable_grid_to_battery_sensors`) no longer exists on the device, so
nothing on the device can arbitrate between them.

ha-spark exists to take control of the home's energy away from these coarse
and sometimes broken automations. So ha-spark takes over, rather than sharing
the inverter across a boundary.

### The overnight charge is inverter-resident (established 2026-09-07)

The inventory above described the automations correctly but wrongly credited
them with the overnight charge. Register reads and recorder history (under
#90/#100) show the inverter runs **its own timed charge schedule**, held in
inverter registers, independent of Home Assistant:

- Slot 1 is configured **23:30 to 05:30 at 60.0 A** (registers 43143-43146,
  43141) and armed: bit 1 of the work-mode bitfield (33132) is set.
- On the night of 2026-09-06, charging began at **23:29:44**, sixteen seconds
  *before* `automation.turn_solis_on` fired at 23:30:00, and
  `select.solisac_power_switch` did not change state across the boundary.
- Charge current held at **59.2 to 60.5 A**, the timed-charge setpoint
  (60.0 A), not the `battery_charge_current_limit` (62.5 A). No automation
  sets a charge current; only the timed window does.

The automations therefore **mirror** the inverter's schedule at the same
boundaries; they don't cause it. ha-spark still takes over, but taking over
requires more, because disabling the automations alone leaves the 23:30 charge
running.

### The driven control surface is the timed-slot registers, not RC (established 2026-09-08)

Live-fire (#83, `docs/runbooks/RUN-83-log.md` and `OVERNIGHT-83-observation.md`)
tested both candidate control surfaces under real load and found RC (register
43135) the weaker one. The overlay's `switch.solis_control_rc_force_charge`
held 0 of 5 write attempts, because of a bug in the entity's write target
rather than inverter behaviour. `modbus.write_register` and the solax `select`
do reach the register, but the only confirmed command code was `1`, **force
discharge**. The force-*charge* code was never found. A charge attempt through
RC was refused at 79% SoC: the flag stayed set for more than 3 minutes with no
current flowing.

The timed-slot surface (the same registers behind the inverter's own 23:30 to
05:30 schedule) measured the opposite: precise rate control below the 62.5 A
hardware ceiling, clean ramps under 15 s, and self-termination exactly at the
boundary with no residual current, across both a one-hour discharge test and a
seven-hour overnight charge test. Neither mode nor SoC gated it. The overnight
run charged from 53% to 100% without hitting a gate, which also reduced the
RC-path refusal to a quirk of that path rather than a property of the
inverter.

**Resolution ([#100](https://github.com/Kylevdm/ha-spark/issues/100)):
ha-spark drives the battery through the timed-slot registers (Slot 1) as the
sole control path.** RC (43135/43136/43282) is demoted to a backlog
investigation, not a shipped fallback, until its force-charge command code is
found. This also answered [#82](https://github.com/Kylevdm/ha-spark/issues/82)'s
control-surface question, and #82 closed pointing here.

## Decision

**ha-spark becomes the sole owner of the Solis inverter**, including both
control surfaces: the `select.solisac_power_switch` on/off scheduling *and*
the timed-slot registers (Slot 1: start, end, and current, written as one
block) that hold the inverter's own grid-charge schedule. ha-spark replaces
the four incumbent power-switch automations and the inverter's manually
programmed Slot 1 schedule.

Takeover is staged, not immediate. Two things gate it.

**Interim posture: simulate suppresses actuation.** ha-spark stays in
`proactive_mode = simulate` (the standing config gate; see `config.py` and
CLAUDE.md) until cutover. In `simulate` the Solis driver logs its intended
writes and issues none, so it can't fight the incumbent. No takeover-specific
interlock is needed before cutover. This is an ownership boundary, not a
promise that simulation skips safety checks.

**Simulation preserves safety decisions
([#175](https://github.com/Kylevdm/ha-spark/issues/175), 2026-10-03).**
`simulate` uses the same decision path as `on` and only suppresses side
effects. Safety checks that decide whether a charge setpoint is valid
therefore still apply in simulation. When SoC is untrusted, the Solis driver
reports charging as blocked instead of previewing a current or window sized
from a placeholder `soc_now = 0`. The simulate gate is still the actuation
guard; this check sends no hardware write.

**Behaviour ledger: what takeover must honour before it may go `on`.** The
incumbent's behaviour is split into rules, and each rule has a disposition:

| # | Incumbent rule | Disposition |
|---|---|---|
| 1 | Fixed overnight grid charge, 23:30 to 05:30 at 60 A. **Inverter-resident, not automation-driven** (see above) | **Replace** with planner-chosen dynamic charging through the *same* timed-slot registers the incumbent schedule uses (#84's driver plus #46's replan cadence, writing only on change). The outcome is the same cheap grid charge, with the planner choosing amount and timing instead of a fixed window. **No separate disarm step**: cutover's first replan overwrites Slot 1 with ha-spark's own computed values ([#100](https://github.com/Kylevdm/ha-spark/issues/100)). Disabling the mirror automations alone would not have stopped the old schedule, but taking over the registers it lived in does. |
| 2 | Discharge-off during Octopus dispatch slots | **Keep.** Implemented: the planner emits dispatch `holds`, and since #140 the per-minute `reconcile_holds` pass drives both edges of the switch instead of only ever writing `Off`. |
| 3 | Discharge-off while the car charges | **Demoted on 2026-09-08 by the map owner: not a cutover precondition.** The owner's supplier already controls EV charge timing, so a ha-spark discharge-off floor would be redundant control, not a safety gap. What's wanted is *awareness* rather than a hold, most likely smart-charging visibility through the Octopus integration or API. That is unscoped and parked in map #78's fog rather than tracked on #84. Charging the battery based on what the car is doing, weighing grid carbon, remains a separate feature (#98). |
| 4 | 05:30 "if dispatch still active, stay Off" boundary | **Drop.** It is an artefact of the fixed-window design and has no purpose under dynamic planning plus rule 2. |

The RC path (register 43135) is uncontested, since no incumbent writes it, but
live-fire found it the weaker option (see above). It is not the driven path
and stays a backlog investigation, outside cutover.

The implemented charge path (#84, merged as PR #105) writes the charge current
at 43141 and the eight-register Slot 1 window block at 43143 through HA's
`modbus.write_register`, and reads both back through the overlay's own
sensors. It writes zeros to slots 2 and 3 so a stale manual window can't run
behind the plan, and keeps Slot 1's discharge half empty unless an export
window is in use. It writes only when values differ and isolates failures per
action. Slot 1 is activated only after the current is confirmed. Real charge
writes still require `proactive_mode = on`, `control = ha_spark`, and a valid
SoC. This completed the charge control surface. Commanded export (#57) was
decided separately and ships as the supervised Axle export prototype (#51).

### The power-switch lifecycle is closed

This section supersedes the ADR's earlier statement that the driver "writes
`Off` for holds but not `On` back". Since
[#140](https://github.com/Kylevdm/ha-spark/issues/140) (decided in
[#143](https://github.com/Kylevdm/ha-spark/issues/143), merged 2026-09-15),
ha-spark owns **both** edges of `select.solisac_power_switch`, so the takeover
this ADR decided is implemented, not pending.

The mechanism is a declarative reconcile, `reconcile_holds`, on its own
per-minute cadence in `run_forever`. It is deliberately *not* part of `apply`,
because a hold boundary follows the clock and can't wait for a plan field to
change. The desired state is a pure function of `now` and the holds: `Off`
while a hold is active, `On` otherwise. Nothing is remembered, so after a
restart or a crash mid-hold the next tick restores the right state, and a
foreign or failed write is corrected the same way within a minute. In steady
state each tick costs one *cached* `GET` of the select and no register writes.

Two properties matter for takeover safety:

- **Untrusted hold data is ignored, not believed.** Trust travels on the
  intent alongside the SoC measurement. An untrusted tick evaluates the clock
  against the last *trusted* hold set, and a failed pre-read may close a hold
  but never open one.
- **The reconcile is exempt from `apply`'s SoC guard.** It commands no
  SoC-derived magnitude, and freezing the inverter `Off` because of an
  unrelated dead sensor is the failure it exists to remove. A refused *force*
  is safe; a refused *release* is not, so the gates are asymmetric on purpose.

When ha-spark stops controlling the inverter (past every known hold end with
no trusted picture, plus a best-effort pass at clean shutdown),
`write_safe_state` leaves the inverter managing itself: power switch `On`,
Slot 1 charging across the configured cheap window, and the discharge half
zeroed unless a verified export is still running. It never fires on an
ordinary degraded tick, where defaulting to `On` would release a live hold.

## Cutover runbook

A human carries this out in Home Assistant. ha-spark never enables or disables
Home Assistant automations itself; that stays with the operator.

1. Run ha-spark in `proactive_mode = simulate`. Compare its intended
   power-switch and timed-slot actions with the live incumbent until the
   behaviour ledger above checks out: rules 1 and 2 satisfied, rule 4
   confirmed irrelevant. Rule 3 is demoted and not a validation gate. The
   power-switch lifecycle is **no longer a blocker** here, because #140 closed
   it (see above). What still stands between a supervised run and retiring the
   incumbent for *unattended* operation is narrower. An inverter-resident
   Slot 1 program outlives ha-spark, and nothing clears it if ha-spark never
   comes back. [#110](https://github.com/Kylevdm/ha-spark/issues/110) settled
   that a stale charge window is a harmless fallback. A resident *export*
   window still matters, because the register holds a wall-clock time with no
   date, so the window repeats daily; bounding it is a precondition for
   unattended operation. Supervised commissioning is
   [#131](https://github.com/Kylevdm/ha-spark/issues/131). It returns
   `proactive_mode` to `simulate` and does not by itself establish unattended
   readiness.
2. Install the [native overlay](../solis-control-modbus-overlay.yaml) by hand
   if it isn't already present. Confirm Modbus TCP (`type: tcp`), gateway
   multi-host mode, storage-type caching OFF, and healthy overlay reads. The
   driver checks grid-charge permission through the work-mode sensor (33132,
   input) and never changes work mode. Record the rollback baseline: the
   charge current, all three window blocks, work mode, power-switch state, and
   the automation states. Do nothing else to Slot 1. It is superseded, not
   cleared, and the timed-mode bit stays armed. ha-spark's first replan after
   cutover overwrites Slot 1 with its own computed start, end, and current.
   Writing the 8-register window block is itself the commit (#84), so there is
   no separate commit step.
3. **Make sure any automations that write this inverter are disabled** before
   you change the gate. That is the operator's job, not ha-spark's: ha-spark
   doesn't enable or disable HA automations, and which automations exist varies
   by installation. In **one coordinated step**, *disable* (not delete) the
   four incumbent automations **and** set `proactive_mode = on`. Disabling
   rather than deleting keeps them available for an instant rollback if
   ha-spark misbehaves live. Watch the first replan replace Slot 1 and verify
   its current, its window, and the unused slots. A failed or blocked program
   is not a successful handover. Confirm that charging starts and stops on
   battery and grid telemetry as well as in the register reads.
4. **Invariant:** ha-spark is never in `proactive_mode = on` while the
   incumbent automations are enabled, or while anything other than ha-spark's
   own replan loop drives the timed-slot registers. Only one controller drives
   the battery at a time. Returning to `simulate` stops new writes but doesn't
   erase the resident program. To roll back: stop ha-spark's writes, restore
   and verify the recorded register and power-switch state through HA, then
   re-enable the incumbent automations, with the operator watching the result.

The four incumbent automations, for the rollback record:
`Solis on - grid charge slot starts (23:30)`,
`Solis - grid charge slot ends (05:30)`,
`Solis off - dispatch slot or car charging starts (daytime)`,
`Solis on - dispatch slot or car charging ends`.

## Alternatives considered

- **Clean split by entity.** The incumbent keeps `power_switch` permanently
  and ha-spark only drives the force-charge override. Rejected because it
  limits ha-spark to a smarter overnight top-up attached to a coarse scheduler
  and leaves the sometimes broken daytime logic in place, the opposite of the
  project's purpose.
- **Share `power_switch`, with back-off on foreign writes or a handover
  switch.** Rejected because the device no longer has a handover switch, and
  two live writers on the most expensive action in the system is a risk with
  no benefit once full takeover is the goal.
- **Immediate takeover (switch to `on` now, delete the automations).**
  Rejected because #84, #90, and #83 were still open, there had been no
  live-fire, and the incumbent encoded a car-charging rule ha-spark didn't yet
  enforce. Staging behind `simulate` and the behaviour ledger reaches the same
  end safely.

## Consequences

- **The control surface is native Modbus, not the solax entities (#84,
  2026-09-08).** #82's provisional write list used the solax
  `number.solisac_timed_*` entities plus the `update_charge_discharge_times`
  commit button. #84 instead applies the #87/#90 principle to the surface
  live-fire chose. ha-spark writes the timed-slot holding registers *natively*
  via `modbus.write_register` on the `solis_control` overlay hub and verifies
  them through that hub's own `sensor.solis_control_*` entities, so the
  control path doesn't depend on the solax integration. The solax
  `update_charge_discharge_times` button is itself a `WRITE_MULTI` block write
  starting at 43143 (source: `plugin_solis.py`), so writing that 8-register
  window block *is* the commit. There is no separate commit step, and
  #57/#84's "shrink-before-grow" ordering rule doesn't apply to a single
  atomic block write, so it was dropped. ha-spark now owns the register
  semantics (there is no tier-A source), and the map covers the
  flash-endurance and inverter-clock drift caveats. The overlay YAML is a
  one-time manual HA-config step (`docs/solis-control-modbus-overlay.yaml`).
  Provisioning it automatically from the add-on is an open follow-up, not part
  of #84.
- **Superseded 2026-09-15 by #140.** This bullet used to record that
  `solis.py` only ever wrote `power_switch = Off`, falling short of the full
  lifecycle takeover, with completion tracked on #84. ha-spark now owns both
  edges through `reconcile_holds` (see "The power-switch lifecycle is
  closed"). One limitation from the original wording remained: the
  stop-discharge was **dispatch-only**. On Intelligent Octopus Go a dispatch
  *is* Octopus charging the car, so holds came from dispatches on purpose. The
  uncovered case was car charging that Octopus didn't dispatch (a manual or
  boost charge, or eco+ surplus diversion), tracked as
  [#141](https://github.com/Kylevdm/ha-spark/issues/141). **Superseded
  2026-10-04 by [#170](https://github.com/Kylevdm/ha-spark/issues/170):** #141
  closed without evidence either way, but on 2026-10-03 a Zappi boost with no
  Octopus dispatch drained the battery into the car at about 3.2 kW. Holds now
  also start whenever the car is actively charging (charging, boosting or
  delivering), outside the overnight window and whether or not a dispatch
  explains it. Eco+ diversion is still not covered. An unreadable EV status is
  ignored rather than believed, and never triggers relinquishing control. The
  rule-3 car-charging discharge floor is **not** part of it; it was demoted on
  2026-09-08 (see the ledger above).
- **The hold mechanism is a coarse whole-inverter enable, and it doesn't
  generalise.** `select.solisac_power_switch` is the only control that stops
  self-consumption discharge (zeroing the timed discharge slots doesn't), so
  #140 holds by disabling the inverter outright. On this household that has no
  side effects. The Solis S5 is battery-only (AC-coupled), with PV on a
  separate Growatt inverter, so a hold never touches solar harvest; the only
  cost is not *storing* surplus during the hold window. On a hybrid inverter,
  or any unit whose coarse enable also gates PV, the same hold would cut solar
  outright. This is tracked for the approved-inverter list as
  [#142](https://github.com/Kylevdm/ha-spark/issues/142). This ADR covers one
  device and doesn't decide it.
- Carbon-aware, EV-coupled battery charging ("green now vs dirtier later") is
  a new optimization objective, since cost and carbon can conflict. It is
  split out as **#98**, sequenced after #84. It doesn't block this decision or
  #84, and it must stay compatible with ADR-0002 (auditable over optimal): any
  carbon lookahead must be a named reservation with a one-sentence reason, not
  an opaque multi-objective score.
- **Owning the timed-slot registers is the cutover mechanism, not a
  precondition to work around.**
  [#100](https://github.com/Kylevdm/ha-spark/issues/100) found that the
  timed-slot path beat RC in live-fire testing (reliable, accurate at the
  boundary, self-terminating), while RC's force-charge command code was never
  confirmed. So #84's driver targets Slot 1 directly, and there is no separate
  disarm-then-own sequence to get right.
- Rollback involves more than re-enabling four automations. A full rollback
  also restores Slot 1 to its last manual values (60 A, 23:30 to 05:30,
  recorded at step 2) by hand, with the same block write, because ha-spark
  doesn't restore a prior schedule automatically.
- **The four incumbent automations are Solis-specific and vary by
  installation.** Disabling them at cutover is an operator step that ADR-0003
  can't mandate generically. Since 0.17.0, ha-spark logs a driver-agnostic
  reminder to check for conflicting automations whenever `proactive_mode`
  changes to `on` ([#104](https://github.com/Kylevdm/ha-spark/issues/104)).
  That reminder is separate from this ADR.
- #86 is resolved. The map's "nothing ships until ownership is reconciled"
  blocker is cleared for #84, and cutover is gated on `simulate` validation
  rather than further debate.
