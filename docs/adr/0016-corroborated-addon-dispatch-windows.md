# ADR-0016: The integration may use the add-on's Kraken dispatch window, corroborated

Status: Accepted (#800, owner decisions 2026-11-28).

## Context

A dispatch hold on an integration-owned site starts only from the
integration's own evidence: the Octopus dispatching entity's
`planned_dispatches`, rated by the `dispatch_kept` ladder (ADR-0007). In Kraken
mode the add-on reads planned dispatches from Octopus directly and never looks
at that entity.

On 2026-11-28 the two disagreed during a real 12:14–14:30 dispatch. The add-on
had the window from Kraken, but the entity's lists stayed empty until someone
called the Octopus integration's refresh action, because this charger's
provider only fills them on request (`manually_refresh_dispatches`). The
integration saw only the car charging and formed an EV hold. An EV hold never
hands over (ADR-0015), so the battery sat idle through the cheapest window of
the day. #802 makes the integration refresh such entities, but a refresh can
still lag a dispatch or fail.

## Decision

From api_version 7 the add-on sends its trusted Kraken dispatch windows to the
integration. The integration treats them as untrusted input and uses one only
when its own evidence corroborates it.

- **What the add-on sends.** Only windows from a trusted Kraken read, with the
  read's `fetched_at`, at most 16, within the planning horizon. An untrusted
  read sends `null` (unknown), never an empty list.
- **Validation.** Aware timestamps, `end > start`, at most 12 h long,
  `fetched_at` not in the future and no older than the add-on's
  `octopus_dispatch_max_age_seconds` default (45 min), on a counted, current
  heartbeat. Anything else is ignored.
- **Corroboration.** A window counts only while it is active and the
  integration's own fresh evidence positively shows the car: EV status
  drawing (`charging`, `boosting`, `delivering`), EV power at least 1.4 kW,
  residual grid import at least 3 kW, or the plug connected. "Not
  contradicted" is not enough: `dispatch_kept`'s default-keep branch does not
  admit an add-on window.
- **Add only.** A corroborated add-on window can start or extend a dispatch
  hold, and counts as the live trusted dispatch for the handover (ADR-0015).
  It never ends one: an absent, stale or empty add-on list never clears,
  shortens or releases a dispatch hold. Release keeps using only the
  integration's own trusted dispatch entity or the last trusted end.

## Consequences

- The hold-start rule ("a hold starts only on fresh evidence") gains a second
  source, always behind the integration's own fresh car evidence. CLAUDE.md
  names it.
- A wrong Kraken window with a plugged-in car can start a dispatch hold the
  entity would not have started. That turns the inverter Off, which is the
  protective direction, and an EV hold would usually be in force anyway.
- An add-on outage only removes windows; holds already formed run to their
  last trusted end as before.
- Both components must move to api_version 7 together (shared with #803).
