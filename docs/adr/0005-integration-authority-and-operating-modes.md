# Integration authority and operating modes

Status: accepted (2026-10-10), confirmed by the owner during the live discussion of
[ADR: the integration/add-on split and the reversal of the HA-as-transport decision](https://github.com/Kylevdm/ha-spark/issues/248).
Detailed lifecycle contracts remain follow-up decisions on the v1 map.
This is a planning record, not implemented behavior.

The companion integration owns device I/O, entities, command validation,
device limits, write authority, and read-back verification. The add-on owns
energy planning, learning, and the ingress UI. The integration exposes services
for add-on commands and entities for observed results. Without the add-on,
it continues exposing entities and accepting authorised manual changes, with
an availability warning.

## Operating modes

- Observe collects data, learns from eligible history, and previews plans
  without hardware writes.
- Protect continues learning and prevents battery-to-car discharge. Ordinary
  charging schedules remain under manual control; protection owns the controls
  necessary to apply and release a hold.
- Optimise continues learning and executes the full automatic energy plan.

Learning remains subject to existing evidence and calibration trust gates.
These are proposed product modes; their mapping to existing proactive-mode
and device-authority gates remains to be specified, without silently enabling
writes for existing observe or simulate configurations.

## Manual control

Direct HA edits to automatically owned controls require explicit handover.
Temporary manual control offers one hour (default), until tomorrow at a
displayed household-clock time, or until explicitly resumed. A persistent
banner displays the current handover and deadline. Deadlines survive restarts.
At expiry, return to the previous operating mode using current conditions and
a fresh plan, never replaying an old plan.

Keep battery-to-car protection is an option, enabled by default during manual
control. When enabled, its required controls remain owned by ha-spark and the
UI identifies them. Disabling protection is explicit and lasts only for that
manual session. Limits, command validation, and read-back still apply.

## Fallback during add-on unavailability

The integration persists the manual-control deadline. When it expires while
the add-on is unavailable and the previous mode was Protect or Optimise, the
integration applies and verifies an owner-configured fallback charging schedule:
conservative charging limits, known charging times, and scheduled battery
export disabled. Setup must establish those values; there is no universal
charging time or limit. Show fallback active and notify through HA. Returning
to Observe does not authorise fallback writes. When the add-on returns, check
current conditions before resuming the previous mode.

The integration also executes battery-to-car protection without the add-on
when enabled. This refines the original no-decisions split: device safeguards,
fixed fallback execution, and this narrow protection rule belong to the
integration; energy optimisation belongs to the add-on. On hardware where a
hold disables the whole inverter, an applicable hold outranks fallback charging.

These guarantees require HA and the integration to remain running. An HA outage
can only rely on device-resident behavior, which needs separate specification.

## Liveness and recovery

During ordinary Protect or Optimise operation, two minutes without an add-on
heartbeat triggers fallback. An active manual session keeps its agreed deadline,
including an explicit-until-resumed session. Observe never starts writing because
a heartbeat is missing. Recovery requires fresh inputs and verified application
before fallback active clears.

A validated fallback schedule is required before enabling Protect or Optimise.
Onboarding establishes charging times, device/bank limits, and read-back support.
Observe remains available immediately.

Switching from active control to Observe first performs a verified handover:
cancel automatic export, release protection holds, and restore the configured
fallback charging schedule. Show switching to Observe until this succeeds;
failures remain visible rather than claiming completion. Once in Observe,
ha-spark makes no writes; the inverter may run its resident charging schedule.
This is an explicitly authorised transition, not a write exception inside Observe.

## One owner per inverter bus

The companion integration is the sole Modbus client for each physical inverter
bus, with one shared connection for reads, writes, protection, fallback, and
read-back. Disconnect old integrations, overlay hubs, and direct clients at
cutover. Multi-client gateway support does not grant concurrent control authority.
The add-on uses the integration's services/entities, not a direct Modbus connection.
Gateway caching must be disabled, following
[Research: the full Solis Modbus register map over a Waveshare RS485-to-TCP gateway](https://github.com/Kylevdm/ha-spark/issues/249).

## Supersession and retained constraints

- Supersedes the transport policy in
  [Grilling: multi-inverter driver foundation architecture](https://github.com/Kylevdm/ha-spark/issues/75):
  native device I/O becomes the companion integration's responsibility, rather
  than an exception requiring a defect in a third-party integration. HA services
  and entities remain the add-on-to-integration boundary. The shared connection
  per bus rule remains, with the companion integration as owner.
- Supersedes the narrow-overlay / do-not-rebuild-integration scope underlying
  [the native Solis charge implementation](https://github.com/Kylevdm/ha-spark/issues/84)
  and [Is solax-modbus the right integration to build the driver on?](https://github.com/Kylevdm/ha-spark/issues/87).
  The companion replaces Solis, AlphaESS local I/O, and Solcast responsibilities;
  full Solis parity remains separately scoped. Octopus remains consumed.
- Amends [ADR-0003](0003-ha-spark-owns-the-solis-inverter.md)'s permanent
  ownership model with explicit temporary handover and integration-enforced
  authority. Historical hardware evidence, coordinated cutover, exclusive
  ownership of each control, and verified writes remain requirements. Manual
  schedule editing and protection can coexist only with explicit control ownership.
- The LLM never commands hardware. SoC integrity, site/bank/device limits, secret
  handling, failure isolation, and write-if-changed requirements remain. Standalone
  manual writes require explicit authority and validation. Detailed gate mapping
  remains to be specified; this planning decision changes no runtime gates.

## Follow-up decisions

The following questions remain required for the complete build-ready spec:

- **Operating-mode and authority lifecycle:** transition table, restart order,
  persistent deadlines/ownership, authenticated and fresh heartbeats, concurrent
  commands, partial apply failures, exact until-tomorrow time, and mapping from
  existing proactive-mode/authority settings. Include fallback validation and
  shared site/bank limits. The agreed modes and two-minute timeout are inputs.
- **Protection evidence and whole-HA outage behavior:** fresh/stale EV and
  dispatch evidence, hold entry/release and overnight exceptions, hybrid-inverter
  side effects, fallback and invalid-SoC interactions, and device-resident
  guarantees when HA cannot run. Hardware claims require validation.
- [Manual override: force charge or stop charging by hand](https://github.com/Kylevdm/ha-spark/issues/255)
  retains command parameters and precedence, using this authority/expiry
  contract. Temporary manual ownership differs from an individual override command.
- [Config store: what leaves the Supervisor options, where it lives, and who edits it](https://github.com/Kylevdm/ha-spark/issues/253)
  must cover authoritative fallback settings, the integration's durable offline
  copy, and deadline persistence. It owns storage/synchronisation details.
