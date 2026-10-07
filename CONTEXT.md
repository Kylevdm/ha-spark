# CONTEXT

The ubiquitous language for ha-spark. This file defines terms only, with no
implementation detail.

## Terms

**The plan**:
The *current* plan, recomputed every half-hourly tariff slot, not "tonight's
plan" computed once a day. Simulate-mode logs, the savings backtest, and
copilot explanations all refer to the latest revision. Drivers write only when
a recomputation changes a setpoint.
(Decision: Q7, 2026-07-12.)

**Competitive MVP**:
The point at which ha-spark runs Kyle's own household better than a configured
Predbat could: a zero-export site with heavy load, solar, V2L charging, and
flexibility payments. It is not the shipped v0.9.0 "MVP" roadmap milestone
(planner, Solis actuation, and simulate mode).

**Zero-export site**:
A household where export is *permitted* (G98) but *unpaid* (no MCS, so no
export tariff). Exported energy earns nothing outside flexibility events. The
tariff schedule expresses this as an export price of £0, so flexibility events
are the only slots with a nonzero export price.

**Base load**:
What the house consumes once every *plannable* source and sink is removed: no
battery charging, no EV charging, no V2L input, no heat-pump flex. The load
forecast predicts base load only, and the planner adds plannable loads back
itself because it knows its own plan. Occupancy moves it a lot (about 300 W
away against 900 to 1000 W at home). ha-spark *derives* base load from
component energy statistics (grid ± battery ± solar ± EV) and never takes it
from a single user-supplied sensor. (Decision: Q4, 2026-07-12.)

**Car battery (V2L)**:
A slow refill *source for the house battery*, not a parallel battery. Energy
flows from the car through a rectifier into the house battery only, never
directly to house loads or the grid, at up to 3 kW with 1.5 kW preferred.
Rectifier temperature limits that rate, so it is a calibration setting rather
than a constant. The car's energy is priced at what it cost to fill plus
round-trip losses. Its role is peak-shaving: it covers heavy periods the house
battery can't (winter heat-pump days need about 20 kWh extra). The planner
doesn't cycle it unless the plan needs it. It is unavailable when unplugged,
and the planner may *ask* for it by notification but never assumes it. The
planner never plans below a hard car-SoC floor, which the car's travel needs
set rather than economics. Cycling wear is accepted. The car has no HA
integration, so ha-spark does *not* read its SoC. Instead the planner assumes
a fixed, configurable **V2L budget** while the car is plugged in and never
plans deeper than that. The car's own V2L discharge cutoff is the hard floor,
and whether the car needs charging for a long trip stays the owner's decision,
outside the planner. (Decisions: Q6+Q9+Q11, 2026-07-12.)

**Phone surface**:
The main way the household talks to ha-spark: Home Assistant's Telegram bot,
not the terminal. Outbound notifications ("plug in the car") go through HA
`notify`. Inbound messages arrive as HA telegram events and go through the same
copilot/ask pipeline under the same rules, so chat can query the plan and add
validated context facts but never set a setpoint. HA holds the Telegram
credentials; ha-spark holds none.

**Readiness signal**:
A recommendation, never a gate. When simulate-mode history clears a measured
bar (a clean multi-week backtest beating the incumbent setup, no simulated
guard breaches, improved forecast error), ha-spark *tells* the owner it makes
sense to enable real control. Switching it on is always the owner's own
decision. (Decision: Q11, 2026-07-12.)

**Reservation**:
A named battery-energy target at a specific time, computed *backwards* from an
obligation (a flexibility event, or reaching the next cheap slot), with a
one-sentence reason. Reservations are how the merit-order planner looks ahead,
and every plan line must trace to one.
(Decision: Q8, 2026-07-12.)

**Flexibility event**:
A time window from an aggregator (Axle) during which imported or exported
energy earns an event rate (about £1/kWh). It is modelled as price slots laid
over the tariff schedule, not as a separate planner concept or a control
authority. The planner responds to the prices, and ha-spark's own drivers write
to the hardware. (Decision: Q2 of the 2026-07-12 grilling session.)

**SoC freshness**:
Whether a reported SoC can be believed *now*. An unchanged SoC is fresh while
its source is shown to be live (a sibling reading from the same source
reported recently) and the value has not stayed unchanged past a hard
ceiling. A frozen value from a live source is normal; a frozen source is not.
"Stale" means there is no evidence the source is live.

**Solis fallback**:
The explicitly configured charge-current ceiling and cheap-rate window kept in
use after sustained SoC-integrity failures. A request is not proof that the
inverter accepted it; fallback is confirmed only by matching hardware read-back.
It remains in place until recovery: SoC integrity has passed continuously for
the recovery duration with an advancing report time, and a fresh plan from the
current SoC and the remaining cheap-rate window has been applied and read back.
Recovery being *ready* does not end the fallback; only that verified apply
does. AlphaESS has no fallback program.

**Battery calibration**:
Revising planner input estimates, such as effective capacity and charge
efficiency, from evidence in a household's own history. It is repeatable
rather than a one-time commissioning result: ha-spark may relearn an estimate
as the battery or its configuration changes.

**BMS measurement integrity**:
Whether the battery-management system's reported SoC agrees with independent
evidence such as pack voltage and observed energy flow. A discrepancy is not,
by itself, evidence of battery degradation or capacity change. It can be a BMS
setting or calibration problem, as with nominally identical packs that are
configured differently.

**Calibration trust gate**:
The safeguard that stops ha-spark from automatically replacing capacity or
efficiency estimates while BMS measurement integrity is in doubt. It keeps the
last trusted estimate and shows the conflict to the owner instead of reading it
as degradation.

**Calibration lifecycle**:
Continuous relearning from qualifying local history once the calibration trust
gate is clear. ha-spark publishes the current estimate with its confidence and
evidence, and the owner may reset it to the prior taken from the spec sheet.
ha-spark never writes BMS settings.

**Learning authority**:
ha-spark may automatically feed a learned estimate that meets its confidence
bar into its own planner, which changes future plans. Learning never
reconfigures a BMS, inverter, or any other system ha-spark does not own. Those
systems' measurements are evidence, not configuration targets.

**Supply current limit**:
The site's maximum AC current in either the import or export direction. It is
distinct from the inverter's DC battery-current ceiling and from any stricter
DNO export limit.

**Export ceiling**:
The maximum grid-export power for a slot, after applying the relevant site or
DNO limit and the power the battery and solar can supply once house load is
served.

**Export priority**:
During a paid flexibility event, export revenue ranks above discretionary
battery charging. It does not rank above house supply or the reservation that
protects the household after the event.

**Household clock**:
The one wall-clock time zone that every local time in ha-spark refers to:
tariff slots, the local day, and every time written to an inverter. It is the
zone the owner configures. ha-spark *checks* Home Assistant's zone and the
inverter clock against it and never substitutes either for it.
(Decision: #155, 2026-09-28.)
_Avoid_: system time, HA time, local time (unqualified)

**Inverter clock**:
The inverter's own real-time clock, a bare wall-clock reading with no zone.
Inverter-resident windows fire on it, so it must agree with the household
clock before ha-spark trusts one. It drifts, and it can't be relied on to
change hour when the clocks change.
(Decision: #155, 2026-09-28.)
_Avoid_: RTC (in prose), inverter time

**Stale schedule**:
An inverter-resident window that keeps firing after ha-spark has stopped
supervising it because ha-spark crashed, was powered off, or was uninstalled.
A restart doesn't count. A stale *charge* window is a harmless fallback,
because the house keeps charging cheaply. A stale *export* window is the costly
case: it repeats every day and, on a zero-export site, gives energy away
unpaid.
(Decision: #110, 2026-10-01.)
_Avoid_: orphaned force (that is the in-memory RC keep-alive's failure, not a
resident window's)

**Hold** — a period, outside the overnight charging window, during which
ha-spark stops the house battery discharging so it never feeds the car. A
hold is caused by an Octopus dispatch, or by the car actively charging
(charging, boosting or delivering; *not* eco+ solar diversion) whether or not
a dispatch explains it. Hold evidence that can't be read is ignored rather
than believed, and a hold outranks a flexibility-event export.
(Decision: #170, 2026-10-04.)
_Avoid_: dispatch hold (when meaning either cause), pause, block
