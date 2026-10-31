# ADR-0012: Native Modbus replaces the Solis overlay hub

Status: Accepted (#562 / #588, phase 8 of #563).

## Context

The legacy Solis driver uses Home Assistant's YAML `modbus:` overlay hub
from #90 for writes and read-back. Home Assistant removes YAML Modbus hubs
in HA 2027.10. Other Modbus integrations also refuse to share a gateway with
a YAML hub. Keeping the overlay is not a durable control path.

[ADR-0005](0005-integration-authority-and-operating-modes.md) requires the
companion integration to be the sole Modbus client on each inverter bus.
Gateway multi-client support is not permission to share control.

## Decision

The native Solis module's own Modbus connection replaces the overlay hub as
the Solis control path: one pymodbus TCP client per gateway, shared by reads,
writes and read-back. This supersedes #90's overlay for native devices and
amends the overlay-install step in
[ADR-0003's cutover runbook](0003-ha-spark-owns-the-solis-inverter.md#cutover-runbook).
Use the [native migration and rollback runbook](../runbooks/solis-native-migration.md)
instead of installing an overlay for a native device.

This is a transport replacement, not a new grant of authority. Ownership,
write gates and read-back requirements are unchanged. An add-on-owned legacy
site retains its add-on authority store and writer. An all-native site becomes
integration-owned only through the durable Observe-only ownership handshake;
neither side writes during a pending switch. A connection, a gateway claim,
a snapshot or a successful health check is never write permission.

The native module reads an unvalidated device but refuses writes. Validated
identity, caching off, a durable gateway claim and no reported legacy overlay
are all required. The claim records the owner's intent; it does not prove that
another client has disconnected. The operator must stop those clients.

## Consequences

- The overlay remains supported for legacy sites until an explicit migration;
  this release does not cut over the reference household. Household cutover
  and live hardware proof remain phase 9 work.
- No concurrent overlay/native control is supported. `health` reports a native
  transport with an overlay still configured as `inconsistent`.
- `health` stays read-only: native validation comes from the daemon's accepted
  snapshot, never a diagnostic connection or a test write to the inverter.
- Rollback also requires Observe and the reverse ownership handshake. Restoring
  YAML or changing a transport setting alone cannot transfer authority.
- Historical register semantics and hardware evidence in ADR-0003 remain
  relevant; neither a new transport nor a clean health row establishes
  unattended export safety (see
  [ADR-0007](0007-protection-evidence-and-outage-boundary.md)).
