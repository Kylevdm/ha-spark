# ADR-0015: A dispatch hold may hand its switch to a dispatch purchase window

Status: Accepted (#769, owner decisions 2026-11-23).

## Context

A hold turns the Solis power switch Off so the house battery never feeds the
car. Solis has only that coarse control, so the planner refused every daytime
purchase inside a hold (#697) and the integration refused a dated charge window
on a held inverter (`hold_active`). That also refused the cheapest purchase
there is: a started Octopus dispatch with the car drawing, at the off-peak rate.

The #704 live run on 2026-11-22 showed that on this firmware a forced Slot 2
charge window keeps the battery charging, not feeding the car, for the whole
window while the car draws. When the window ends the battery feeds the car
within one 5 s sample. A window can therefore stand in for the hold while it
lasts, provided the hold takes the switch back before the window ends.

A first design (durable handover fields on the hold records, a half-hour
look-ahead and live extension) grew past 1,800 lines and three review rounds
kept finding real defects in its state. This ADR records the smaller design
that replaced it.

## Decision

An owner opt-in, `dispatch_handover` in the integration's options, off by
default. The integration sends it to the add-on in the site snapshot from
api_version 6; the add-on reads it only from a v6 snapshot carrying `true`.

- **Planner.** With the opt-in, a started dispatch purchase on a coarse-hold
  inverter is admitted as on a no-hold driver. Every other purchase inside a
  hold stays a refused diagnostic. The purchase is still clipped to the current
  half-hour, so a long dispatch is bought one half-hour per plan.
- **Wire.** From api_version 6 the dated window carries `target_soc_pct`.
- **Handover.** For a window with a target on an inverter with an owned,
  confirmed dispatch hold, the integration arms Slot 2 with the switch still Off
  (the usual write-ahead record and exact read-back), records the handover in
  memory, then writes On under a fresh write-time recheck. The conditions,
  checked at the start and on every hold tick: a window of at most half an hour
  (one Slot 2 occurrence); the opt-in; a counted heartbeat
  at api_version 6; Optimise with no manual session, fallback or transition; a
  live trusted dispatch; a charge current verified above zero on this connection
  and not since polled at zero; a fresh inverter clock within 60 s of the
  household clock, sampled at the same UTC offset; the window active on the
  inverter's clock and more than two minutes from its end there; trusted SoC
  more than 2 % below the target; and, once handed over, the armed record and its
  verified Slot 2 face, with no newer poll showing zero current, another face or
  grid charging not permitted. The same window under a new plan takes the new
  target at once.
- **Take-back.** While the handover is live the hold leaves the switch On and
  keeps its records confirmed, adding any newly wanted cause. Once any
  condition fails it writes Off as for any hold. Before any Slot 2 write on a
  handed-over inverter (a new window, a clear, fallback, a transition), and
  before a zero charge current, the switch is written Off first under the hold's
  own refusal; toward Observe or in manual control without protection that
  refusal stands and the change goes ahead without it. If that Off fails while
  the hold stands, the Slot 2 change fails and the window stays: it is the only
  protection left. A verified Off, or an Off found on the switch, ends the
  handover, and that window end is never handed over again.
- **Failures fail towards the hold.** A failed arm writes no On. An On refused
  or with the switch unreadable drops the handover; any other failed On keeps it,
  so the hold verifies Off. A failed take-back is retried every tick, sends the
  "could not be applied" notice and raises the repair `hold_reclaim_failed`.
- **Memory only.** No handover state is persisted. A restart forgets it, and
  the hold's next tick finds the switch On and writes Off.

## Consequences

- The CLAUDE.md native daytime window invariant ("no hold on that inverter")
  gains one exception, named there.
- Both components must move to api_version 6 together.
- A long dispatch turns the switch Off for a few minutes around each half-hour
  boundary: the take-back two minutes before the end, until the next plan arms
  the next half-hour. Owner-accepted cost of the simpler design.
- If the integration stops during a handover, nothing takes the switch back
  until it restarts, and the battery can feed the car once the window ends.
  A failed authority save likewise fences every write. Owner-accepted.
- After a restart the "never again" latch and the repair are gone; the hold's
  own Off and notice remain.
- The 2 % target edge is still untested live (#704).
