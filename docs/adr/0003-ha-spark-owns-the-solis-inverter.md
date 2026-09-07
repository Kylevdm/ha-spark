# ADR-0003: ha-spark is the sole owner of the Solis inverter

Status: Accepted (2026-09-06)

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
| 1 | Fixed overnight grid-charge, `On` 23:30 → 05:30 | **Replace** with planner-chosen dynamic charging (#84 force-charge + #46 replan cadence). Same outcome — cheap overnight charge — with the amount and timing chosen by the planner rather than a blunt fixed window. |
| 2 | Discharge-off during Octopus dispatch slots | **Keep** — already implemented: the planner emits dispatch `holds`. |
| 3 | Discharge-off while the car charges | **Keep as a safety floor.** Raise a stop-discharge hold whenever `ev_charging` is active (the Zappi input is already ingested via `ev_status_entity`). This covers ad-hoc boosts *outside* a formal dispatch slot, which today's dispatch-only `holds` miss. **Hard precondition of cutover.** The richer "charge the battery based on what the car is doing, weighing grid carbon" ambition is explicitly *not* this floor — it is a separate feature (#98). |
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
2. In **one coordinated step**: *disable* (not delete) the four incumbent
   automations **and** set `proactive_mode = on`. Disabling rather than
   deleting keeps them as an instant rollback if ha-spark misbehaves live.
3. **Invariant:** ha-spark is never in `proactive_mode = on` while the
   incumbent automations are enabled. The two controllers never write the
   switch at the same time.

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
- #86 is resolved: the map's "nothing ships until ownership is reconciled"
  blocker is cleared for #84, with the cutover gated on `simulate` validation
  rather than on further debate.
