# CONTEXT

Ubiquitous language for ha-spark. Glossary only — no implementation detail.

## Terms

**The plan** — the *current* plan, recomputed every half-hourly tariff slot,
not "tonight's plan" computed once a day. Simulate mode logs, the savings
backtest, and copilot explanations all refer to the latest revision; drivers
write only when a recomputation actually changes a setpoint.
(Decision: Q7, 2026-07-12.)

**Competitive MVP** — the point at which ha-spark fully runs Kyle's own
household better than a configured Predbat could: zero-export site, heavy
load, solar, V2L charging, and flexibility payments. Distinct from the
shipped v0.9.0 "MVP" roadmap milestone (planner + Solis actuation +
simulate mode).

**Zero-export site** — a household where export is *permitted* (G98) but
*unpaid* (no MCS, so no export tariff). Exported energy earns nothing
outside flexibility events. The tariff schedule expresses this as an
export price of £0; flexibility events are the only nonzero export slots.

**Base load** — what the house consumes with all *plannable* sources and
sinks stripped out: no battery charging, no EV charging, no V2L input, no
heat-pump flex. The load forecast predicts base load only; the planner adds
plannable loads back itself, because it knows its own plan. Occupancy swings
it hard (~300 W away vs ~900–1000 W home). Base load is *derived* by ha-spark
from component energy statistics (grid ± battery ± solar ± EV), never
trusted from a single user-supplied sensor. (Decision: Q4, 2026-07-12.)

**Car battery (V2L)** — a slow refill *source for the house battery*, not a
parallel battery: car → rectifier → house battery only, never direct to
house loads or grid, at ≤3 kW with 1.5 kW preferred (rectifier thermals — a
calibration knob, not a constant). Its energy is priced at what it cost to
fill plus round-trip losses. Role: peak-shaving extension — it covers heavy
periods the house battery cannot (winter heat-pump days ~20 kWh extra), is
not cycled unless the plan needs it, and is unavailable when unplugged (the
planner may *ask* via notification, never assume). A hard car-SoC floor,
owned by mobility not economics, is never planned below. Cycling wear is
accepted. The car has no HA integration, so its SoC is *not* read: the
planner assumes a fixed configurable **V2L budget** when the car is plugged
in and never plans deeper; the car's own V2L discharge cutoff is the hard
floor, and mobility (does it need charging for a long trip) stays the
owner's judgement, outside the planner. (Decisions: Q6+Q9+Q11, 2026-07-12.)

**Phone surface** — the primary way the household talks to ha-spark:
Home Assistant's Telegram bot, not the terminal. Outbound notifications
("plug in the car") go through HA `notify`; inbound messages ride HA's
telegram events into the same copilot/ask pipeline with the same rules —
chat can query the plan and contribute validated context facts, never
setpoints. ha-spark holds no Telegram credentials; HA does.

**Readiness signal** — a recommendation, never a gate: when simulate-mode
history clears a measured bar (a clean multi-week backtest beating the
incumbent setup, no simulated guard breaches, improved forecast error),
ha-spark *tells* the owner it makes sense to enable real control. The flip
itself is always the owner's informal decision. (Decision: Q11, 2026-07-12.)

**Reservation** — a named battery-energy target at a specific time,
computed *backwards* from an obligation (a flexibility event, reaching the
next cheap slot) with a sentence-shaped reason. Reservations are how the
merit-order planner does lookahead; every plan line must trace to one.
(Decision: Q8, 2026-07-12.)

**Flexibility event** — a time window from an aggregator (Axle) during
which imported or exported energy earns an event rate (~£1/kWh). Modelled
as price slots overlaid on the tariff schedule — not a separate planner
concept and not a control authority; the planner responds to the prices
and ha-spark's own drivers actuate. (Decision: Q2 of the 2026-07-12
grilling session.)
