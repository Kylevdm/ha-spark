# ADR-0013: The owner's durable opt-in counts as owner export supervision

Status: Accepted (#720 / #722).

## Context

A native export runs only under a live owner export supervision for that event
(#563). The owner had to send `supervise_export` by hand for every event. On
2026-11-17 a paid Axle export event exported nothing on a site in Optimise,
because no supervision had been sent.

Home Assistant's options flow does not tell the flow handler which user
submitted it. Its HTTP views check for an admin, but the handler only sees the
flow's source, and any code can start an options flow with no user at all.

## Decision

**Export during events** is a durable, site-wide owner opt-in. While it is on,
the integration supervises each export flexibility event itself, through the
same checks as a manual `supervise_export`: Optimise only, no manual session,
no fallback, authority known and current, limits and guard open, bounds
verified, and not tripped. It uses the per-inverter export ratings the owner
stored once; the confirmed rating is the device's export bound. The admin who
turned the opt-in on is the confirming user of every automatic supervision,
and each row records that its provenance is automatic.

The opt-in is turned on only by the owner service `set_export_during_events`.
Home Assistant stamps that call with the caller's user id, and the call must
resolve to a non-system admin, as the other owner services do. Calls with no
user, such as automations and scripts, are refused. Turning it on re-checks the
stored ratings against the manual supervision checks. The options step holds
only the ratings, and they can't be changed while the opt-in is on.

**Import during events** is a planner gate only. Import writes keep today's
gates: the operating mode, the charge boundary and the grid-charge permission.
No supervision is added for import.

## Consequences

- The "Native export is supervised only" rule now reads "a live owner
  supervision, sent by the owner or by the owner's durable export opt-in".
  Every other export safeguard is unchanged: the tripwire, the stop latch, the
  zero-confirmed clears, and never exporting in Observe, Protect, manual
  control or fallback.
- Turning the opt-in off stops further automatic supervisions. It does not
  end one that is already running.
- Alternatives rejected: an owner automation that sends `supervise_export`
  for each event (consent would live outside the authority owner, and a
  missed send is a silent missed event, as on 2026-11-17); the switch in the
  options flow (no trusted user id, so it could never be turned on);
  recording a generic "options flow admin" (no accountable user, and an
  options flow started from code bypasses the admin check).
