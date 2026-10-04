# ADR-0002: Auditable-over-optimal planning

Status: Accepted (2026-07-12)

## Context

The Competitive MVP (epic #43) adds two things the existing single-window
planner can't express: flexibility events, which reward holding battery energy
back for a future window, and a car battery that can refill the house battery
on request. Both need real lookahead. The planner has to know about an
obligation ahead of the current slot and shape today's dispatch around it.
ha-spark's founding design rule is that a deterministic, auditable planner
decides and an LLM only explains (then in ROADMAP.md, now the README). Any
lookahead mechanism has to keep every decision traceable to a plain-language
reason, not only to a better result that nobody can explain.

## Decision

Keep the planner as **merit-order dispatch plus named, backward-computed
reservations**:

- In each slot, forecast load is met from the cheapest available source in
  order (solar, then house battery, then car-refilled energy, then grid),
  subject to floors and limits.
- All lookahead goes through **reservations**: named battery-energy targets at
  specific times, computed backwards from a concrete obligation (a flexibility
  event, or reaching the next cheap slot). Each reservation carries a
  one-sentence reason that attaches to the plan actions it produces (e.g.
  "reserve 3 kWh for the 17:00 event, plus 3.5 kWh to reach the 23:30 cheap
  slot").
- There is no general-purpose optimizer behind this: no LP solver and no DP
  value function. The accepted product principle is **suboptimal in corner
  cases, auditable everywhere**. Every plan line traces to a reservation or a
  price, and the copilot and simulate-mode logs show that trace.

## Alternatives considered

- **LP solver** (the EMHASS/Predbat approach: optimize the whole horizon
  against a cost function). Rejected because an LP optimum can't explain
  itself. "Why 3 kWh and not 3.2 kWh" has no one-sentence answer beyond "the
  solver said so." This is the trade-off ha-spark makes against tools that
  already do LP optimization well, and auditability is how it differs from
  them (README, "How it compares").
- **DP value function** (dynamic-programming lookahead over a value/cost
  table). Rejected for the same reason. A learned or computed value function
  gives no per-decision reason, and answering "why this setpoint" means
  unwinding the whole table instead of reading one reservation's reason.

## Consequences

- The planner can be suboptimal in corner cases an LP or DP approach would
  catch (e.g. a marginal reallocation across two events that neither
  reservation alone would find). That is the accepted cost of auditability.
- Every new lookahead feature (flexibility events, V2L refill) has to be
  expressible as a reservation with a backward computation and a one-sentence
  reason. That constrains future planner design, but it keeps the existing
  pure-function test seam (inputs and config in, plan out, no mocks)
  unchanged.
- The copilot and simulate-mode logs can always base an answer on a specific
  reservation or price rather than an opaque score, which matches the
  "explains itself" positioning (then in ROADMAP.md, now the README).
