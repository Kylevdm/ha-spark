# Solis: migrate the overlay to native Modbus, or roll back

This is an owner-operated, two-pass cutover procedure, not a command to cut
over an existing household. The household's run, checklists and results live on
[#663](https://github.com/Kylevdm/ha-spark/issues/663); record both rollback
rehearsals there. [ADR-0012](../adr/0012-native-modbus-replaces-the-overlay-hub.md)
replaces ADR-0003's overlay-install step for native Solis devices.

**Stop at any failed verification**, in Observe or the visible pending state.
Resolve the fault and retry the relevant step; a connection alone proves neither
migration nor rollback. Never edit authority files or restore a retired authority
store. Pending or unknown authority permits no writes. Never run competing
Modbus clients: close the native connection before restoring the overlay or old
client, and stop those clients before connecting native again. Keep conflicting
inverter-writing automations disabled throughout.

## 1. Prepare

Keep a private rollback record of the existing topology, hub setting, overlay
YAML, manual charging schedule, current limits, power-switch state and every
setting/entity mapping to restore. Keep the owner's old→new entity list private:
not in the repo, issues or PRs. Never publish household device/service names,
gateway addresses, credentials or real entity ids. Examples below are placeholders.

Arrange a supervised maintenance period; protection cannot be assumed while its
transport is disconnected. Plan both 24-hour Observe periods and the Protect
period. Back up before starting. Update **both the add-on and integration to
0.32.2 or later, together**: the native export `unverified` row introduced by #654
is rejected by a mixed-version pair, and on Home Assistant 2026.11 or later
earlier versions register no native inverter entities (#675).

- [ ] Private rollback record and backup are complete.
- [ ] Supervised periods are arranged and conflicting writers disabled.
- [ ] Both peers run 0.32.2 or later and the integration link is connected.

## 2. First go-native, starting from add-on-owned Observe

1. **Enter Observe and wait for the verified handover to finish.** It cancels
   automatic export and releases legacy holds through the verified release
   protocol. Do not continue with a hold, transition or unknown authority.
   Observe stops automatic writes; it does not erase an inverter-resident
   charging schedule. Once the site is integration-owned, an explicit owner clock sync is the one
   write Observe permits there; do not request one during the read-only
   validation.
2. **Disable, do not delete, the other Modbus integration.** Disconnect direct
   tools and all other clients for this gateway, not just their writing
   automations. Leave the old entities and statistics in place for now.
3. **Remove the overlay YAML and restart Home Assistant.** Keep a private copy
   of the hub installed from
   [`docs/solis-control-modbus-overlay.yaml`](../solis-control-modbus-overlay.yaml)
   and its include, then remove them as appropriate. Confirm the old hub no
   longer connects.
4. **Clear `solis_control_hub`** in the add-on's saved settings
   (`ha-spark config set solis_control_hub=` inside the add-on container) and
   restart the add-on. This must be explicitly empty, not left at a former
   default. A nonempty setting is reported as an overlay and blocks native
   validation; once cleared, health reports `no control path` until the native
   device is added.
5. **Add the native device:** in integration Options → Site topology and
   limits → Inverters, edit the existing Solis inverter (don't add a second
   one: a remaining legacy inverter keeps the site add-on-owned), choose
   `transport: native`, `driver_kind: solis`, and
   enter `gateway_host`, `gateway_port` and `gateway_unit_id`. Confirm the
   inverter limits, complete **Connect native Solis** (connect, identify,
   caching probe and gateway claim), then save the topology. Storage-type caching must be off. An unreachable or
   unsupported device can remain read-only; that is not successful validation.
   A gateway ownership claim does not prove the other client is gone: check
   that yourself.
6. **Wait for the ownership handshake triggered by that save.** On a site where
   every controllable inverter is native, saving the all-native topology starts
   the handshake immediately: there is no add-on-owned native validation stage.
   From Observe with no holds or transition, the integration persists
   `pending_integration`, the add-on durably retires its authority store, and
   the integration initialises fresh authority in Observe. Wait for
   `integration` ownership, known authority and Observe. If either peer is absent
   or a save fails, leave it pending; never bypass the handshake. A mixed site
   that keeps a legacy inverter stays add-on-owned.

The native integration must be the sole client on the bus. It refuses every
native write as `modbus_hub_conflict` and raises a self-clearing repair while
HA's own `modbus` integration has a hub or connection on the claimed gateway
host and port, or is loaded but its connection data cannot be read. Reads
continue; health reports `gateway_not_owned`. Matching is case-insensitive on
the host as written, not DNS: a hostname and IP address do not match.
Third-party Modbus integrations are **not detected**, so disabling them remains
mandatory.

- [ ] Other clients are stopped; the overlay is removed and its hub disconnected.
- [ ] `solis_control_hub` is explicitly empty and the add-on has restarted.
- [ ] Native connection, identity, caching probe and gateway claim are complete.
- [ ] All-native ownership is `integration`, authority is known, mode is Observe,
      and no hold or transition remains.

## 3. Repoint settings at each go-native

Do this on the first and second go-native, and again when returning from a
rollback. Use the private record when restoring the old settings. These are
**add-on settings, not topology fields**: the integration's inverter form has
no field for them. Edit them inside the add-on container with
`ha-spark config set KEY=VALUE` (several assignments may share one command),
after recording the old values with `ha-spark config show`:

- `soc_entity`, `battery_voltage_entity`, `battery_power_entity` and its
  `battery_power_discharge_sign`;
- `dispatch_grid_power_entity` and `dispatch_grid_power_invert`;
- `consumption_energy_entity`;
- derive statistic ids: `derive_grid_import_entity`, `derive_grid_export_entity`,
  `derive_solar_generation_entity`, `derive_battery_charge_entity`,
  `derive_battery_discharge_entity`, `derive_ev_charge_entity`, and their
  `derive_invert_*` flags;
- `grid_power_entity` if it names an old-integration sensor, and any other
  setting still naming an old entity.

On an integration-owned site, SoC and its trust come from the native snapshot
(health's `Native SoC` row); still repoint `soc_entity`, which the dashboard reads.
Native Battery power reads charging positive and discharging negative, so set
`battery_power_discharge_sign` to `negative` when repointing to it; health's
`Evidence battery_power` row should then report the direction you observe.

In Options → Site topology and limits, repoint connection `grid_power_entity`
and inverter `grid_port_power_entity` and `battery_power_entity`. Check the unit
and sign of **each power entity** against observed import/export and
charge/discharge. Set inverter `power_sign` to `positive_import` or
`negative_import` as appropriate. On a native inverter it also applies to the
integration's own register reads of Battery power and Grid-port power, which
the supply guard and battery-direction evidence use instead of the entities. The connection's `grid_power_entity` has
**no sign setting** and must read import positive: if native Meter active power
reads the other way, keep the current grid sensor or use a template sensor
that negates it. Never repoint to a mis-signed sensor.

Native readings include PV power, Meter active power, Battery power, Grid-port
power, Battery voltage and Battery SoC. Lifetime kWh totals (PV, battery
charge/discharge, grid import/export and consumption) keep `total_increasing`;
previous-period sensors have no state class. Use lifetime totals for continuing
Energy statistics, for example `sensor.<native>_grid_import_total_energy`.
The owner re-selects native sensors in the Energy dashboard; do not rename
native entities to preserve old entity ids.

- [ ] Every applicable add-on setting and derive statistic id is repointed.
- [ ] Topology grid, grid-port and battery power entities are repointed.
- [ ] Each power entity's unit and sign is checked; sign settings or templates
      are correct, including import-positive connection grid power.

## 4. Validate read-only in Observe for 24 hours

For an all-native site this validation is **integration-owned**, after the
handshake. Include an overnight cheap window. Run `ha-spark health` and inspect
repairs and device readings. Each native `Solis transport (<id>)` row must be OK,
`validated`. Read-only is WARN with a fixed reason code; `inconsistent` is FAIL
and means an overlay is still configured. `validation unknown` is WARN: the
daemon has no fresh usable validation for that device. Restore the channel and
resolve repairs; never interpret unknown as validated.

Health only reads diagnostics: it neither probes Modbus nor writes a test
schedule, and OK is not permission to escalate.

- [ ] 24 hours in Observe completed, including an overnight cheap window.
- [ ] No bus or read errors occurred.
- [ ] At most one transient caching or pacing warning occurred (for example a
      **Gateway caching detected** repair that cleared, or a read-timeout warning
      in the Home Assistant log).
- [ ] SoC, PV, load and grid readings are plausible against add-on history.
- [ ] Step 3's sign and unit checks are complete (`power_sign` set where needed).
- [ ] Every native Solis transport health row is `validated`.
- [ ] No **Home Assistant Modbus uses the Solis gateway** or **Home Assistant
      Modbus connections unreadable** repair remains; the integration link is
      connected.

## 5. Rollback rehearsal 1: full reverse rollback before history migration

The first save already switched ownership, so this is a **full reverse
handshake**, not a transport-only rehearsal. Follow [Reverse rollback](#reverse-rollback)
in Observe, before migrating any statistics. Record the result on #663.

- [ ] Reverse ownership completed through `pending_addon` to `addon`, in Observe.
- [ ] Native connection is closed before the old clients return.
- [ ] Overlay, explicit hub setting and old integration are restored.
- [ ] Settings are restored from the private record; legacy read-backs and health pass.
- [ ] No statistics have been migrated; rehearsal result is recorded.

## 6. Second go-native and Carry history over

Repeat sections 2 (including another ownership handshake) and 3. Only now run
[Carry history over](#carry-history-over). Keep Observe throughout: statistics
migration changes neither authority nor hardware.

- [ ] Native is again the sole client; ownership is `integration`, authority known,
      mode Observe, with no hold or transition.
- [ ] Settings and sign/unit checks are complete again.
- [ ] History migration checklist is complete and native Energy sensors re-selected.

## Carry history over

1. **Take a backup first.** Rehearse on one pair using template sensors or a
   restored backup before touching the real history.
2. **Migrate only at the second go-native**, never the first. Migrate every
   energy, power and voltage pair whose unit and sign match. List sign-mismatched
   pairs on the cutover ticket using placeholders; they start fresh. Keep the
   actual old→new list private, out of the repo, issues and PRs.
3. Check the old entities' last report time before starting. An entity-id rename
   moves states and long-term statistics, including the 5-minute rows. A sensor
   continues its sum from the last 5-minute row only while that row is within
   recorder's `purge_keep_days` window (default **10 days**). Old entities stop
   reporting at the first go-native: migrate within that window, or raise
   `purge_keep_days` beforehand so the row is retained. Do not assume a purged
   row can be recovered by increasing retention afterwards.
4. For each matched pair, use **Settings → Devices & services → Entities →
   entity settings → Entity ID**. These ids are placeholders:
   1. Rename native `sensor.native_grid_import` to
      `sensor.native_grid_import_tmp`.
   2. Rename old `sensor.old_grid_import` to `sensor.native_grid_import`.
   3. Remove that old entity (now named `sensor.native_grid_import`).
   4. Rename `sensor.native_grid_import_tmp` back to `sensor.native_grid_import`.
   5. Delete the temporary statistics for `sensor.native_grid_import_tmp` in
      **Developer tools → Statistics**. Keep the migrated native-id history.
   6. After the first hourly compile, check the change-over hour and smooth any
      oversized change-over sum with **adjust sum** in Developer tools → Statistics.

HA refuses to rename onto an entity id that is in use; that is why the temporary
id and removal are necessary. Stop on a refused rename rather than accepting an
unexpected replacement id. Re-select native sensors in the Energy dashboard;
this procedure moves history to native ids, not native entities to old ids.

- [ ] Backup taken and one-pair rehearsal passed.
- [ ] Only the second go-native is used for migration; retained 5-minute rows checked.
- [ ] Every matching energy, power and voltage pair is migrated and checked.
- [ ] Sign-mismatched pairs are listed with placeholders and start fresh.
- [ ] Temporary statistics are deleted; migrated history remains under native ids.
- [ ] First hourly compile checked and change-over sums adjusted where needed.
- [ ] Native Energy sensors re-selected; actual mapping remains private.

## 7. Observe for 24 hours after the second handshake

- [ ] Another 24 hours in Observe completed with known integration authority.
- [ ] No SoC staleness occurred; health's `Native SoC` row passes.
- [ ] No false supply-guard cuts occurred; simulated Observe limits clear when
      import drops rather than leaving a latched cut.
- [ ] Transport validation and integration link remain healthy; results recorded on #663.

## 8. Rollback rehearsal 2: full reverse rollback after history migration

Before Protect, follow [Reverse rollback](#reverse-rollback) again in Observe.
Re-enabling the old integration re-creates its entities as **new registry
entries with fresh history** for every migrated pair (their old entities
were removed); un-migrated entities return with their ids and history. Check each returned under its old id and never
claimed a native id; **stop if one did**. Repoint settings back to these old
entities from the private record, not to the native ids holding migrated history.

Then return to native by repeating steps 2 and 3 (another handshake). **Do not
repeat the history migration.** Native ids retain the migrated history, with a
gap for the rehearsal. Record the rollback and return results on #663.

- [ ] Full reverse handshake and transport rollback completed in Observe.
- [ ] Re-created old entities use their old ids and took no native id.
- [ ] Settings restored from the private record and legacy read-backs checked.
- [ ] Return to native completed with no competing clients and settings repointed.
- [ ] Ownership is `integration`, authority known, Observe settled and health passes.
- [ ] Migrated history stays on native ids with only the rehearsal gap; migration not repeated.

## 9. Protect for 24 hours, then Optimise

Before Protect, set Options → Notify target and configure native protection
evidence, the fallback charging schedule and SoC trust policy. Confirm limits
and source freshness, review readiness, and let the owner explicitly choose
Protect. Watch the first verified hold and release actions. After a successful
24 hours, review readiness again before explicitly choosing Optimise, with a
live add-on and trusted SoC; watch the first verified plan actions.

Do not enable unattended export: Slot 1 repeats daily without a resident expiry;
native export requires live owner supervision under
[ADR-0007](../adr/0007-protection-evidence-and-outage-boundary.md). The live Axle
proof remains an RC-line item, not a prerequisite for this code release.

- [ ] Notify target, native protection evidence, fallback schedule, SoC trust policy
      and limits are configured and readiness reviewed before Protect.
- [ ] Protect ran for 24 hours; holds, releases, read-backs and notices checked.
- [ ] Owner chose Optimise through readiness; first plan actions verified.
- [ ] Results recorded on #663; close #562 and #495 when the cutover completes.

## Reverse rollback

Use this procedure for both rehearsals and for recovery. Reverse the migration
without ever reconnecting competing clients:

1. Request Observe from the integration and wait for verified export cleanup
   and hold release. Do not remove the integration to bypass a failed handover.
   Check the recorded resident schedule and switch state.
2. Validate safe Observe before changing transport. Keep both peers running for
   the reverse handshake; no hold, transition or manual session may remain.
3. Select legacy transport in topology options to request `pending_addon`.
   Wait for the add-on to initialise fresh authority in Observe and report
   active, and for the integration to retire its store and persist owner `addon`.
   Neither side writes while pending. Never restore a retired authority file.
4. Remove the native gateway configuration/claim and ensure its connection is
   closed before bringing back the overlay or another client. Stay in Observe.
5. Restore the recorded explicit `solis_control_hub` value in the add-on and
   restart it. Restore the private overlay YAML and restart Home Assistant.
6. Re-enable only the old sensing integration and clients needed by the former
   setup, never a competing writer. Restore settings from the private record.
   After history migration, check the re-created old ids as required in step 8.
7. Verify legacy read-back sensors. The legacy transport row must be OK (with
   the HA 2027.10 removal warning); `overlay hub missing` or `no control path`
   means stop. This legacy arrangement is temporary: YAML hubs are removed in
   HA 2027.10.
8. Validate in Observe. A saved schedule, restored YAML or old automations
   alone is not an authority handover. Rehearsals return to native via sections 2
   and 3 of this runbook; any later escalation requires readiness and an explicit
   owner choice.

## Recovery if the integration must be re-added

Removing the integration does **not** release holds. Call `ha_spark.set_observe`
and verify the handover before removal, or disable it first and inspect its
handover result; never use removal to bypass a failed release.

A re-added entry has a new site id and is refused as `site_not_paired`. Recover
using `ha-spark pair --replace --from <paired site id> --to <new site id>` (#646),
with the validated ids from health and the requested typed confirmation. This
is allowed only in Observe or with unknown authority, with no pending ownership
handshake or transition. See [DOCS: Re-pairing after re-adding the integration](../../ha_spark_addon/DOCS.md#re-pairing-after-re-adding-the-integration).
Re-pairing is not an ownership handover or permission to write; resolve unknown
authority and complete the normal handshake before proceeding.
