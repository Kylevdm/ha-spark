# ADR-0003: ha-spark is the sole owner of the Solis inverter

Status: Accepted (2026-09-06); premise corrected 2026-09-07 (see "The
overnight charge is inverter-resident"); control surface corrected 2026-09-08
(see "The driven control surface is the timed-slot registers, not RC")

## Context

Under wayfinder map #78 (forced charge on the Solis S5 AC-coupled), a live
inventory found that ha-spark is **not** the only writer to the inverter.
Four Home Assistant automations actively drive it, and reconciling that
ownership was raised as a blocker (#86) — nothing on the map ships until it is
settled.

The live picture (read-only inventory, 2026-09-06): all four incumbent
automations write **exactly one entity, `select.solisac_power_switch`
(`On`/`Off`)** — a coarse whole-inverter enable. None of them touch the
force-charge override (`select.solisac_inverter_battery_control_override`,
register 43135) or any setpoint. Their combined logic:

- **Overnight (23:30 → 05:30):** turn `On` — let the inverter grid-charge.
- **Daytime (05:30 → 23:30):** turn `Off` whenever an Octopus dispatch slot is
  live *or* the Zappi is actively charging the car; back `On` when either ends.
- A 05:30 boundary rule: if a dispatch is still active at 05:30, stay `Off`.

The collision is concrete: ha-spark's own Solis driver
(`ha_spark/devices/inverters/solis.py`) writes `select.solisac_power_switch =
"Off"` during dispatch `holds`, via the configured `inverter_power_switch_entity`
— the *same entity*, on the *same off-peak trigger* — and never writes `On`
back, so it would rely on the incumbent to re-enable. Two controllers on one
entity is exactly how the map's most-expensive failure — a forced grid charge
left running into a peak-rate period — happens. A switch that was flagged as a
possible handover mechanism (`switch.enable_grid_to_battery_sensors`) no longer
exists on the device, so there is no arbitration surface today.

The founding purpose of ha-spark is to take control of the home's energy away
from these coarse and sometimes-broken automations. That frames the decision:
ha-spark takes over, rather than coexisting with a boundary.

### The overnight charge is inverter-resident (established 2026-09-07)

The inventory above described the automations correctly but mis-attributed the
overnight charge to them. Register reads and recorder history (under #90/#100)
show the inverter runs **its own timed charge schedule**, held in inverter
registers, independent of Home Assistant:

- Slot 1 is configured **23:30 → 05:30 at 60.0 A** (registers 43143-43146,
  43141) and armed — bit 1 of the work-mode bitfield (33132) is set.
- On the night of 2026-09-06, charging began at **23:29:44**, sixteen seconds
  *before* `automation.turn_solis_on` fired at 23:30:00, and
  `select.solisac_power_switch` did not change state across the boundary.
- Charge current clamped at **59.2-60.5 A** — the timed-charge setpoint
  (60.0 A), not the `battery_charge_current_limit` (62.5 A). No automation
  sets a charge current; only the timed window does.

The automations therefore **mirror** the inverter's schedule at the same
boundaries rather than causing it. This does not change the decision — ha-spark
still takes over — but it changes what taking over requires: disabling the
automations alone leaves the 23:30 charge running.

### The driven control surface is the timed-slot registers, not RC (established 2026-09-08)

Live-fire (#83, `docs/runbooks/RUN-83-log.md` and `OVERNIGHT-83-observation.md`)
tested both candidate control surfaces under real load and found RC (register
43135) the weaker one: the overlay's `switch.solis_control_rc_force_charge`
held 0/5 write attempts (a bug in the entity's write target, not inverter
behaviour), and while `modbus.write_register` and the solax `select` do reach
the register, the only confirmed command code was `1` = **force discharge** —
the force-*charge* code was never found. A charge attempt via RC was refused
at 79% SoC with the flag held set for 3+ minutes with no current flowing.

The timed-slot surface (the same registers behind the inverter's own
23:30–05:30 schedule) measured the opposite: precise rate control below the
62.5 A hardware ceiling, clean sub-15s ramps, and boundary-accurate
self-termination with no residual current, across both a one-hour discharge
test and a seven-hour overnight charge test. Neither mode nor SoC gated it —
the overnight run charged 53%→100% with no gate encountered, which also
demoted the RC-path refusal to a quirk of that path rather than a property of
the inverter.

**Resolution ([#100](https://github.com/Kylevdm/ha-spark/issues/100)):
ha-spark drives the battery via the timed-slot registers (Slot 1) as the sole
control path.** RC (43135/43136/43282) is demoted to backlog investigation —
not a shipped fallback — pending discovery of its force-charge command code.
This also answers [#82](https://github.com/Kylevdm/ha-spark/issues/82)'s
control-surface question, which closes pointing here.

## Decision

**ha-spark becomes the sole owner of the Solis inverter** — of both control
surfaces: the `select.solisac_power_switch` on/off scheduling *and* the
timed-slot registers (Slot 1: start/end/current + commit button) that carry
the inverter's own grid-charge schedule. The four incumbent power-switch
automations, and the inverter's own manually-programmed slot-1 schedule, are
the predecessor being retired.

Takeover is staged, not immediate. Two things gate it:

**Interim posture — no new guard code; the existing simulate gate is the
guard.** ha-spark stays in `proactive_mode = simulate` (the standing config
gate — see `config.py` / CLAUDE.md) until cutover. In `simulate` the Solis
driver logs its intended writes and issues none, so it cannot fight the
incumbent. This makes the interim ownership boundary true in the running system
without touching code.

**Behaviour ledger — what takeover must honour before it may go `on`.** The
incumbent's behaviour is decomposed and each rule assigned a disposition:

| # | Incumbent rule | Disposition |
|---|---|---|
| 1 | Fixed overnight grid-charge, 23:30 → 05:30 at 60 A — **inverter-resident, not automation-driven** (see above) | **Replace** with planner-chosen dynamic charging, driven through the *same* timed-slot registers the incumbent schedule occupies (#84's driver + #46's replan cadence, write-if-changed). Same outcome — cheap grid charge — with amount and timing chosen by the planner instead of a fixed window. **No separate disarm step**: cutover's first replan simply overwrites Slot 1 with ha-spark's own computed values ([#100](https://github.com/Kylevdm/ha-spark/issues/100)); disabling the mirror automations alone would not have stopped the old schedule, but taking over the registers it lived in does. |
| 2 | Discharge-off during Octopus dispatch slots | **Keep** — already implemented: the planner emits dispatch `holds`. |
| 3 | Discharge-off while the car charges | **Keep as a safety floor.** Raise a stop-discharge hold whenever `ev_charging` is active (the Zappi input is already ingested via `ev_status_entity`). This covers ad-hoc boosts *outside* a formal dispatch slot, which today's dispatch-only `holds` miss. **Hard precondition of cutover.** The richer "charge the battery based on what the car is doing, weighing grid carbon" ambition is explicitly *not* this floor — it is a separate feature (#98). |
| 4 | 05:30 "if dispatch still active, stay Off" boundary | **Drop** — an artefact of the fixed-window design that dissolves under dynamic planning plus rule 2. |

The RC path (register 43135) is uncontested — no incumbent writes it — but
live-fire found it the weaker surface (see above); it is not the driven path
and stays a backlog investigation, not part of cutover.

## Cutover runbook

Executed by a human, in Home Assistant. ha-spark never programmatically
enables or disables Home Assistant automations — that write surface is out of
scope and stays with the operator.

1. Run ha-spark in `proactive_mode = simulate`. Watch its intended
   power-switch / timed-slot actions against the live incumbent until the
   behaviour ledger above checks out (rules 1–3 satisfied, rule 4 confirmed
   irrelevant).
2. Record the inverter's current slot-1 values (60 A, 23:30 → 05:30) as the
   rollback baseline. No other action needed on the slot itself — it is
   superseded, not cleared: ha-spark's first replan cycle after cutover
   overwrites it with its own computed start/end/current, using #57's
   write-then-commit recipe.
3. **Make sure any automations that write this inverter are disabled** before
   flipping the gate — operator responsibility, not ha-spark's; ha-spark does
   not enable or disable HA automations itself, and which automations exist
   varies by installation. In **one coordinated step**: *disable* (not
   delete) the four incumbent automations **and** set `proactive_mode = on`.
   Disabling rather than deleting keeps them as an instant rollback if
   ha-spark misbehaves live.
4. **Invariant:** ha-spark is never in `proactive_mode = on` while the
   incumbent automations are enabled, or while the timed-slot registers are
   being driven by anything other than ha-spark's own replan loop. Only one
   controller drives the battery at a time.

The four incumbent automations, for the rollback record:
`Solis on - grid charge slot starts (23:30)`,
`Solis - grid charge slot ends (05:30)`,
`Solis off - dispatch slot or car charging starts (daytime)`,
`Solis on - dispatch slot or car charging ends`.

## Alternatives considered

- **Clean split by entity** — incumbent keeps `power_switch` permanently,
  ha-spark only ever drives the force-charge override. Rejected: it caps
  ha-spark at "smarter overnight top-up bolted onto a coarse scheduler" and
  leaves the sometimes-broken daytime logic in place, which is the opposite of
  the project's purpose.
- **Coexist on `power_switch` with foreign-write back-off / a handover
  switch.** Rejected: there is no handover surface on the device any more, and
  two live writers on the single most expensive action in the system is a risk
  taken for no benefit once full takeover is the goal.
- **Immediate takeover (flip `on` now, delete the automations).** Rejected:
  #84/#90/#83 are open, there has been no live-fire, and the incumbent encodes
  a car-charging rule ha-spark does not yet enforce. Staging behind `simulate`
  and the behaviour ledger is the safe path to the same destination.

## Consequences

- The current `solis.py` `power_switch = Off` write during `holds` is harmless
  while `simulate` holds, but it is **not** the full lifecycle takeover
  requires (it never writes `On`, and its stop-discharge is dispatch-only, not
  car-aware). Completing that — including the rule-3 `ev_charging` safety floor
  as a cutover precondition — is #84's work, tracked there.
- ha-spark must reproduce, before cutover, behaviour the incumbent got for free
  from raw HA state (the car-charging discharge floor). The Zappi input is
  already wired, so this is a planner rule, not a new integration.
- Carbon-aware, EV-coupled battery charging — "green now vs dirtier later" — is
  a genuinely new optimization objective (cost and carbon can conflict) and is
  spun out as **#98**, sequenced after #84. It does not block this decision or
  #84, and must stay compatible with ADR-0002 (auditable-over-optimal): any
  carbon lookahead expressed as a named reservation with a sentence-shaped
  reason, not an opaque multi-objective score.
- **Owning the timed-slot registers is the cutover mechanism, not a
  precondition to work around.** [#100](https://github.com/Kylevdm/ha-spark/issues/100)
  found the timed-slot path outperforms RC in live-fire testing (reliable,
  boundary-accurate, self-terminating) while RC's force-charge command code
  was never confirmed working — so #84's driver targets Slot 1 directly, and
  there is no separate disarm-then-own sequencing to get right.
- The rollback surface is larger than "re-enable four automations". A full
  rollback also restores Slot 1 to its last manual values (60 A,
  23:30 → 05:30, recorded at step 2) — by hand, via the same write-then-commit
  recipe, since ha-spark does not automatically restore a prior schedule on
  rollback.
- **The four incumbent automations are Solis-specific and vary by
  installation** — disabling them at cutover is an operator step, not
  something ADR-0003 can mandate generically. A cross-driver warning at the
  `proactive_mode` transition (reminding the operator to check for conflicting
  automations before going `on`) is tracked as a separate, driver-agnostic
  feature, not part of this ADR.
- #86 is resolved: the map's "nothing ships until ownership is reconciled"
  blocker is cleared for #84, with the cutover gated on `simulate` validation
  rather than on further debate.
