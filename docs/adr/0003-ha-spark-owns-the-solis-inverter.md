# ADR-0003: ha-spark is the sole owner of the Solis inverter

Status: Accepted (2026-09-06); premise corrected 2026-09-07 (see "The
overnight charge is inverter-resident")

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

## Decision

**ha-spark becomes the sole owner of the Solis inverter** — of both control
surfaces: the `select.solisac_power_switch` on/off scheduling *and* the
`select.solisac_inverter_battery_control_override` force-charge path. The four
incumbent power-switch automations are the predecessor being retired.

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
| 1 | Fixed overnight grid-charge, 23:30 → 05:30 at 60 A — **inverter-resident, not automation-driven** (see above) | **Replace** with planner-chosen dynamic charging (#84 force-charge + #46 replan cadence). Same outcome — cheap overnight charge — with the amount and timing chosen by the planner rather than a blunt fixed window. **Replacing it requires disarming the inverter's own timed window** (#100); disabling the mirror automations does not stop it. |
| 2 | Discharge-off during Octopus dispatch slots | **Keep** — already implemented: the planner emits dispatch `holds`. |
| 3 | Discharge-off while the car charges | **Demoted, 2026-09-08 (map owner): not a cutover precondition.** The owner's supplier already controls EV charge timing, so ha-spark forcing a discharge-off floor is redundant control, not a safety gap. What's actually wanted is *awareness*, not a hold — most plausibly smart-charging visibility via the Octopus integration/API — which is unscoped and parked in map #78's fog rather than tracked on #84. The richer "charge the battery based on what the car is doing, weighing grid carbon" ambition remains a separate feature (#98). |
| 4 | 05:30 "if dispatch still active, stay Off" boundary | **Drop** — an artefact of the fixed-window design that dissolves under dynamic planning plus rule 2. |

The force-charge override path (register 43135) is uncontested — no incumbent
writes it — so ha-spark owns it cleanly; that implementation is #84.

## Cutover runbook

Executed by a human, in Home Assistant. ha-spark never programmatically
enables or disables Home Assistant automations — that write surface is out of
scope and stays with the operator.

1. Run ha-spark in `proactive_mode = simulate`. Watch its intended
   power-switch / force-charge actions against the live incumbent until the
   behaviour ledger above checks out (rules 1–3 satisfied, rule 4 confirmed
   irrelevant).
2. **Disarm the inverter's own timed charge window** (#100) — slot 1,
   23:30 → 05:30 at 60 A. This is a *separate controller from the automations*
   and survives disabling them; leaving it armed means the inverter grid-charges
   at 60 A every night regardless of what the planner decides. The mechanism
   (clear the slot registers, clear the timed bit, or change the storage-control
   mode) and its reversibility are decided in #100. Record the pre-change values
   first — they are the rollback.
3. In **one coordinated step**: *disable* (not delete) the four incumbent
   automations **and** set `proactive_mode = on`. Disabling rather than
   deleting keeps them as an instant rollback if ha-spark misbehaves live.
4. **Invariant:** ha-spark is never in `proactive_mode = on` while the
   incumbent automations are enabled **or while the inverter's timed window is
   armed**. Only one controller drives the battery at a time — and there are
   three candidate controllers here, not two.

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
  requires (it never writes `On`, and its stop-discharge is dispatch-only).
  Completing that is #84's work, tracked there. The rule-3 car-charging
  discharge floor is **not** part of it — demoted 2026-09-08, see the ledger
  above.
- Carbon-aware, EV-coupled battery charging — "green now vs dirtier later" — is
  a genuinely new optimization objective (cost and carbon can conflict) and is
  spun out as **#98**, sequenced after #84. It does not block this decision or
  #84, and must stay compatible with ADR-0002 (auditable-over-optimal): any
  carbon lookahead expressed as a named reservation with a sentence-shaped
  reason, not an opaque multi-objective score.
- **Disarming the timed window is a hard cutover precondition**, tracked
  outside this ADR (#100). Until the window is disarmed, `proactive_mode = on`
  is unsafe in a way `simulate` validation cannot reveal: simulate compares
  ha-spark's intentions against the incumbent, and the incumbent it is being
  compared against is partly the inverter itself.
- The rollback surface is larger than "re-enable four automations". A full
  rollback also restores the timed-window registers, so their pre-change values
  must be recorded before step 2.
- #86 is resolved: the map's "nothing ships until ownership is reconciled"
  blocker is cleared for #84, with the cutover gated on `simulate` validation
  rather than on further debate.
