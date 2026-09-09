# ADR-0002: Auditable-over-optimal planning

Status: Accepted (2026-07-12)

## Context

The Competitive MVP (epic #43) adds two things the existing single-window
planner cannot express: flexibility events that reward holding battery
energy back for a future window, and a car battery that can refill the house
battery on request. Both need genuine lookahead — the planner has to know
about an obligation ahead of the current slot and shape today's dispatch
around it. ha-spark's founding design rule is that a deterministic,
auditable planner decides and an LLM only explains (ROADMAP.md); any
lookahead mechanism has to keep every decision traceable to a plain-language
reason, not just to a decision that improves under the hood.

## Decision

Keep the planner **merit-order dispatch plus named, backward-computed
reservations**:

- Per slot, forecast load is met from the cheapest available source in
  order — solar → house battery → car-refilled energy → grid — subject to
  floors and limits.
- All lookahead is done exclusively through **reservations**: named
  battery-energy targets at specific times, computed backwards from a
  concrete obligation (a flexibility event; reaching the next cheap slot).
  Each reservation carries a sentence-shaped reason that attaches to the
  plan actions it produces (e.g. "reserve 3 kWh for the 17:00 event, plus
  3.5 kWh to reach the 23:30 cheap slot").
- No general-purpose optimizer sits behind this: no LP solver, no DP value
  function. The accepted product principle is: **suboptimal in corner
  cases, auditable everywhere** — every plan line traces to a reservation or
  a price, and that trace is what the copilot and simulate-mode logs surface.

## Alternatives considered

- **LP solver** (the EMHASS/Predbat-style approach: optimize the whole
  horizon against a cost function). Rejected: an LP optimum is a black box —
  "why 3 kWh and not 3.2 kWh" has no sentence-shaped answer, only "the solver
  said so." This is the explicit trade-off ha-spark makes against tools that
  already do LP optimization well; auditability was chosen as the
  differentiator (ROADMAP "How it compares").
- **DP value function** (dynamic-programming lookahead over a value/cost
  table). Rejected for the same reason: a learned or computed value function
  is opaque per-decision, and reproducing "why this setpoint" requires
  unwinding the whole table rather than reading one reservation's reason.

## Consequences

- The planner can be suboptimal in corner cases an LP/DP approach would
  catch (e.g. a marginal reallocation across two events that neither
  reservation alone would find) — accepted as the cost of auditability.
- Every new lookahead feature (flexibility events, V2L refill) has to be
  expressible as a reservation with a backward computation and a
  sentence-shaped reason, which constrains future planner design but keeps
  the existing pure-function test seam (inputs + config in, plan out — no
  mocks) unchanged.
- The copilot and simulate-mode logs can always ground an answer in a
  concrete reservation or price rather than an opaque score, matching the
  "explains itself" positioning in ROADMAP.md.
