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

**Battery calibration**:
Revising planner input estimates, such as effective capacity and charge
efficiency, from evidence in a household's own history. It is repeatable
rather than a one-time commissioning result: ha-spark may relearn an estimate
as the battery or its configuration changes.

**BMS measurement integrity**:
A reported SoC is believed only when the energy that flowed through the battery
since the last believed SoC can explain it. More broadly, whether the
battery-management system's reported SoC agrees with independent
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

**Inverter current limit**:
The maximum DC battery current an individual inverter can charge or discharge,
with a separate limit for each direction.

**Battery bank**:
A group of batteries operated as one energy store, with shared state of charge
and charge/discharge limits. Multiple inverters may connect to the same bank.

**Battery-bank current limit**:
The maximum combined DC charge or discharge current a bank can accept or
supply across its connected inverters, with a separate limit for each direction.

**Site export limit**:
The maximum combined grid-export power permitted at the household's grid
connection, including battery and solar output after house consumption. It is
the stricter of the DNO export permission and the export direction of the
supply current limit, and every consumer (planner, device bound, live checks)
uses this one value.
(Decision: #283, 2026-10-12.)

**Device export bound**:
An export limit held and enforced by the inverter itself, covering its solar
and battery output together. The owner or installer sets it; ha-spark verifies
it and never writes it. Scheduled battery export needs a verified bound on
every export-capable inverter, and those bounds must together stay within the
site export limit.
(Decision: #283, 2026-10-12.)
_Avoid_: EPM (vendor term), export guard

**Charging allocation**:
The charging power assigned to a bank and divided across its connected
inverters within device, bank and shared site limits.

**Supply guard**:
The safeguard that cuts controlled grid charging quickly when measured site
import exceeds the supply current limit, and restores it slowly once headroom
has held. It never raises charging above the requested target, and it applies
whenever charging is controlled, including temporary manual control.
(Decision: #165, 2026-10-11.)
_Avoid_: fuse guard, throttle

**Major load**:
A household load large enough to use up supply headroom by itself, such as
the car charger, a heat pump heating or cooling, or an induction hob.
Onboarding records each one's demand.
_Avoid_: big load, heavy appliance

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
a dispatch explains it. Unknown evidence cannot start a hold; a confirmed EV
hold needs fresh clear evidence or explicit handover to end, while a dispatch
hold ends at its last trusted end time. A hold outranks fallback charging and
a flexibility-event export.
(Decisions: #170, 2026-10-04; #267, 2026-10-12.)
_Avoid_: dispatch hold (when meaning either cause), pause, block

**Fresh protection evidence**:
An EV, dispatch, or battery-direction observation whose source has reported
within its validated age limit. An unchanged cached Home Assistant state alone
does not establish freshness.
(Decision: #267, 2026-10-12.)

**Operating mode**:
The scope of automatic hardware control authorised by the owner: Observe,
Protect, or Optimise. There is one operating mode for the whole site, covering
every inverter and battery bank. Data collection and eligible learning can
continue in all three modes and during manual control.
(Decision: #266, 2026-10-12.)

**Observe**:
An operating mode that gathers data, learns from eligible history, and previews
plans without writing to hardware.

**Protect**:
An operating mode that prevents the house battery feeding the car while
ordinary charging schedules remain under manual control. It owns the controls
needed to apply and release that protection. It does not depend on the
add-on, so it never uses the fallback charging schedule.

**Optimise**:
An operating mode that authorises the full automatic energy plan, subject to
control authority and safety checks.

**Temporary manual control**:
An explicit handover of the site's controls to the owner. It expires by
default and returns to the previous operating mode after current conditions
have been checked. A return to Optimise needs a fresh plan, or uses the
fallback charging schedule when the planner is unavailable; a return to
Protect leaves the owner's charging edits in place. The owner may instead
choose to resume explicitly. Battery-to-car protection may remain enabled,
retaining ownership of the controls it needs. Learning may continue during the
handover.
(Decision: #266, 2026-10-12.)

**Override**:
An owner instruction that the plan must obey until it ends: **Charge now**
(to a target battery level or until a set time, whichever comes first),
**Don't charge**, or **Keep battery** (no discharge). It exists only in
Optimise, where ha-spark keeps the controls and plans around it; it is not a
handover. Every override ends by itself, there is one at a time for the whole
site, and limits, the supply guard and an applicable hold all outrank it. While
the battery level is untrusted or the fallback charging schedule is in force,
it is suspended and its end time keeps running.
(Decision: #255, 2026-10-12.)
_Avoid_: hold, pause, force, boost; manual control (that is the whole-site
handover)

**Fallback charging schedule**:
An owner-configured charging window and conservative limits that keep charging
in Optimise when automatic planning is unavailable or the reported SoC cannot
be trusted. None of its values depend on SoC, so an untrusted SoC does not
block it. It does not authorise scheduled battery export and remains
subordinate to applicable protection. A request is not proof that the inverter
accepted it; the schedule is in force only once read-back matches. It stays in
force until every cause has cleared and a fresh plan has been applied and read
back; recovery being *ready* does not end it.
(Decision: #266, 2026-10-12.)
_Avoid_: Solis fallback

**Owner**:
A Home Assistant administrator acting for the household through ha-spark's
own page or its command line. A household can have several owners, and each
has the same standing. Only an owner can approve a config proposal, change the
operating mode or start an override.
(Decision: #278, 2026-10-12.)
_Avoid_: HA owner, admin user, the user

**Config proposal**:
A change to agent-editable settings that the agent has asked for and that
has not yet been applied. It is fixed once made. Only an owner's approval on
ha-spark's own page applies it, and approving it applies it at once. It expires
unapproved after 15 minutes or when the add-on restarts, and a newer proposal
from the same agent replaces it.
(Decision: #278, 2026-10-12.)
_Avoid_: pending change, approval token

**Agent-editable setting**:
A household setting the owner has allowed the product agent to request
changing. That permission does not itself approve a particular change.

**Replan request**:
A request for ha-spark to plan now instead of waiting for the next regular
planning time. It carries no authority: planning and any resulting change
happen exactly as the operating mode allows, and the regular schedule
continues. Requests come from the owner, from the agent, or from saving a
dated household fact. Requests that arrive close together are combined, and the agent's
requests are spaced out; the owner's are not. A request counts as the agent's
whenever the assistant chose to make it, wherever it was asked from.
(Decision: #269, 2026-10-12.)
_Avoid_: run plan, force replan, apply now

**Add-on command**:
A request that ha-spark's add-on makes of the integration with the add-on's
authority, such as applying the plan or carrying an owner's request from
ha-spark's own page. The integration acts on it only when the integration
collected it from the add-on itself, during the current add-on session and
authority epoch. A Home Assistant service call never counts as one, whoever
makes it.
(Decision: #291, 2026-10-13.)
_Avoid_: add-on service call, Supervisor call

**Grid carbon intensity**:
The estimated or forecast carbon emitted per unit of grid electricity for the
owner's chosen region and half-hour. It is display-only: the plan never
considers it, and it plays no part in readiness. Past values are regional
estimates, not metered actuals.
(Decision: #305, 2026-10-14.)

**Setup**:
The guided process that takes a new household from install to a saved
configuration, ending in Observe. It is a process, not a place: once finished
it is gone, except to resume a half-saved run. The owner may choose to run it
again from Settings.
(Decision: #306, 2026-10-14.)
_Avoid_: onboarding screen, setup screen (for post-setup editing)

**Settings**:
Where the owner changes configuration after Setup. Changing a setting never
changes the operating mode.
(Decision: #306, 2026-10-14.)

**Battery savings**:
What the household's bills would have cost over a span with the same measured
house load and solar but no battery, minus what they did cost. Each half-hour
is priced at the import and export rates in force at the time; standing
charges are excluded. It credits the battery and ha-spark's use of it
together, not ha-spark alone, and never includes aggregator payments.
(Decision: #309, 2026-10-14.)
_Avoid_: ha-spark savings, money saved (unqualified)

**Coverage**:
The share of a span's half-hours that have trustworthy measurements (and, for
cost figures, captured rates). Statistics always state their coverage;
missing half-hours are left out, never filled from a forecast.
(Decision: #309, 2026-10-14.)

**Assistant**:
ha-spark's own chat on its page: an agent that reads the plan and the
household's state through the same tools as outside agents, and may record
household facts, ask for a replan or propose a settings change, as far as the
owner allows. It never changes a device. Each conversation belongs to one Home
Assistant user.
(Decision: #310, 2026-10-14.)
_Avoid_: copilot, bot, chat agent

**Chat provider**:
The language model service the assistant uses: the owner's own Ollama server,
or an opt-in third-party service the owner chooses. Only one is used at a time.
Choosing a third-party service sends the conversation and its context out of
the house.
(Decision: #310, 2026-10-14.)
_Avoid_: LLM tier, cloud tier

**Offline answer**:
A reply from ha-spark's built-in parser when the chat provider can't answer.
It is always marked as such, and it never mixes with a part-finished assistant
reply.
(Decision: #310, 2026-10-14.)
_Avoid_: fallback reply
