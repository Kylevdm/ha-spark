# Solis: migrate the overlay to native Modbus, or roll back

This is an owner-operated transport migration, not a command to cut over an
existing household. Phase 9 owns the household cutover and live proof.
[ADR-0012](../adr/0012-native-modbus-replaces-the-overlay-hub.md) replaces
ADR-0003's overlay-install step for native Solis devices.

Keep a private rollback record of the existing topology, hub setting, YAML,
manual charging schedule, current limits and power-switch state. Do not publish
gateway addresses or credentials. Arrange a supervised maintenance period;
protection cannot be assumed while its transport is disconnected. Keep any
conflicting inverter-writing automations disabled throughout.

## Migrate, in this order

1. **Enter Observe and wait for the verified handover to finish.** It cancels
   automatic export and releases legacy holds through the verified release
   protocol. Do not continue with a hold, transition or unknown authority.
   Observe stops automatic writes; it does not erase an inverter-resident
   charging schedule.
2. **Stop the other Modbus client for this gateway.** Disconnect third-party
   Modbus integrations and direct tools, not just their writing automations.
   The native integration must be the sole client on the bus.
3. **Remove the overlay YAML and restart Home Assistant.** Remove the hub
   installed from [`docs/solis-control-modbus-overlay.yaml`](../solis-control-modbus-overlay.yaml)
   and its include if appropriate. Confirm the old hub no longer connects.
4. **Clear `solis_control_hub`** in the add-on's saved settings. This must be
   explicitly empty, not left at a former default. A nonempty setting is still
   reported to the integration as an overlay and blocks native validation.
5. **Add the native device:** select the Solis native transport in the
   integration's topology options and complete connect, identify, caching
   probe and gateway claim. Storage-type caching must be off. An unreachable
   or unsupported device can remain read-only; do not treat that as successful
   validation. A gateway claim does **not** prove the other client is gone.
   Check that yourself before continuing.
6. **Complete the ownership handshake.** Both peers must support API v4.
   Every controllable inverter must be native for integration ownership;
   mixed/legacy sites remain add-on-owned. From Observe, with no holds or
   transition, the integration persists `pending_integration`, the add-on
   durably retires its authority store, and the integration initialises fresh
   authority in Observe. Wait for `integration` ownership and known authority.
   If either peer is absent or a save fails, leave it pending; do not bypass
   the handshake or edit authority files. Nothing writes while pending.
7. **Validate in Observe.** Run `ha-spark health` and inspect the integration's
   repairs and device readings. Each native `Solis transport (<id>)` row must
   be OK, `validated`. Read-only is WARN with a fixed reason code;
   `inconsistent` is FAIL and means an overlay is still configured.
   `validation unknown` is WARN: the daemon has no fresh usable validation
   for that device. Restore the channel and resolve repairs; never interpret
   unknown as validated. Confirm the integration link is connected, ownership
   is settled, identity and caching checks pass, and telemetry is plausible.
   Health only reads diagnostics: it neither probes Modbus nor writes a test
   schedule, and OK is not permission to escalate.
8. **Re-escalate through readiness.** Confirm limits and evidence, and, for
   Optimise, fallback and trusted SoC. Review readiness before the owner
   explicitly chooses Protect or Optimise. Watch the first verified actions.
   Do not enable unattended export: Slot 1 repeats daily without a resident
   expiry; native export requires live owner supervision under ADR-0007.

## Reverse rollback

Reverse the migration without ever reconnecting competing clients:

1. Undo escalation: request Observe from the integration and wait for verified
   export cleanup and hold release. Do not remove the integration to bypass a
   failed handover. Check the recorded resident schedule and switch state.
2. Validate the safe Observe state before changing transport. Keep both peers
   running for the reverse ownership handshake; no holds, transition or manual
   session may remain.
3. Select legacy transport in the topology options to request `pending_addon`.
   Wait for the add-on to initialise fresh authority in Observe, report active,
   and for the integration to retire its store and persist owner `addon`.
   Neither side writes while pending. Do not restore a retired authority file.
4. Remove the native gateway configuration/claim and ensure its connection is
   closed before bringing the overlay or another client back. All sites remain
   in Observe during this work.
5. Restore the recorded explicit `solis_control_hub` value in the add-on.
6. Restore the saved overlay YAML and restart Home Assistant. Verify its own
   read-back sensors. A legacy transport row must be OK (with the HA 2027.10
   removal warning); `overlay hub missing` or `no control path` means stop.
7. Restore only the legacy sensing clients needed by the former setup. Do not
   restart the native client or any competing writer. The legacy arrangement
   is temporary: the YAML path is removed in HA 2027.10.
8. Validate in Observe, review readiness, then explicitly re-escalate if wanted.
   Check the first verified actions. Restoring YAML, a saved schedule or old
   automations alone is not an authority handover.

At any failed verification, stop in Observe (or the visible pending state),
resolve the fault and retry the relevant step. Never claim a completed
migration or rollback merely because a connection succeeds.
