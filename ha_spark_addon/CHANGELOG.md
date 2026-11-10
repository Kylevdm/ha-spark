# Changelog

## 0.32.2

- **Native inverter entities on Home Assistant 2026.11 (#675).** HA 2026.11
  changed how device information is stored, and every native Solis inverter
  sensor and control failed to register, leaving no "Solis inverter" device.
  They now register on both 2026.10 and 2026.11. Supply-guard, SoC and
  battery-direction checks read the inverter directly and were not affected.

## 0.32.1

- **Safety: a reload can no longer corrupt integration authority (#672).** On
  integration-owned sites, reloading the companion integration (every add-on
  restart or update, every options save, Solcast reconfigure) could let a
  30-second tick from the old session finish after the reload and save its own
  out-of-date authority record. That could put the site into fallback charging
  nobody asked for, or undo an owner's change such as a request for Observe.
  Unloading now cancels the session's ticks first, lets an export stop that
  was already decided save its stop latch, and then closes the session's
  authority and ownership stores, which then save nothing and leave the repair
  issue alone. Every authority save also checks that the stored record is
  still exactly the one this session last read or wrote, and otherwise fails
  closed as unknown. The interim advice to avoid reloads in Protect or
  Optimise no longer applies.

## 0.32.0

- **Re-pairing after re-adding the integration (#645, #646, #601).** Removing
  and re-adding the companion integration gives it a new site id. The add-on
  now refuses the new entry's snapshots as `site_not_paired`, naming the fix,
  and `ha-spark health` shows the paired and the last refused site id. Run
  `ha-spark pair --replace --from <old id> --to <new id>` and type the new id
  again to confirm. The daemon replaces the pairing only in Observe (or with
  unknown authority) and with no pending ownership handshake or transition.
  The new entry's topology revisions start again from 1, the last accepted
  topology and limits stay in force until then, and snapshots from the old
  entry are refused. The authority owner and operating mode are unchanged.

- **Integration Modbus-hub fence (#647).** The integration now checks Home
  Assistant's own `modbus` integration for a YAML hub or HA-managed connection
  on the native gateway's claimed host and port. While one exists, or while
  `modbus` is loaded but its connections can't be read, every native write
  (charge, owner entity edits, holds, clock sync, fallback, supply guard and
  export) is refused with the new reason `modbus_hub_conflict` and a repair
  names the conflict; reads continue and the repair clears once the hub is
  gone. Health reports it as `gateway_not_owned`. Third-party Modbus
  integrations are not detected, so the runbook step to disconnect them stays
  mandatory.

- **Native hardening (#648).** An owner edit to a native charge, discharge or
  power control during a pending authority transition is now refused as
  `transition_in_progress` instead of `not_available`; a controller that is not
  ready yet is still `not_available`. Clock-drift repairs for inverters no
  longer in the accepted topology are removed on setup and after a topology
  change. The add-on supply guard's read-failure log no longer includes the
  error text; it still skips that pass as before.

- Native holds now require an explicit maximum age for every entity evidence source. Invalid stored evidence remains unknown and raises a repair asking the owner to re-save protection evidence options; it cannot start or clear a hold.
- Native hold reconciliation and removal release now check evidence freshness against the time after waiting for the control lock, so a long wait cannot make stale reports look fresh.

- **Integration supply guard in Observe (#650, #560).** On an integration-owned
  site in Observe the guard now shows the limit it would apply, marked
  `simulated` on its entities and in diagnostics, clears it when the import
  drops, and never latches or saves a cut; leaving Observe starts it clean.
  The add-on's plan status doesn't report a simulated limit as charging
  limited by the supply guard. Its warn reasons are now distinct:
  `authority_unavailable` only for a missing or stale authority mirror,
  `mode_observe` in Observe or a pending transition to Observe, and
  `writes_not_permitted` for any other state that permits no writes, such as
  a failed save. Protect and Optimise are unchanged.

- The companion integration has a new **Notify target** option: a bare notify service name, checked when you save it. Native hold, degraded-evidence, refused-release and fallback notices go to it once each, with one repeat after 30 minutes while the problem persists. With no target set, or when a send fails or times out, the notice falls back to the persistent notification as before. Delivery never delays or changes a protection or fallback decision.

- **Native SoC for integration-owned sites (#653, #495).** Once the
  integration owns the site, the add-on never reads the configured SoC entity
  or checks the legacy Solis write entities for entry preconditions: the
  integration checks entry itself when you request a mode. Without a live
  integration snapshot the mode page shows SoC as unavailable and mode
  requests are refused as `authority_unknown`, never handled by the add-on's
  retired authority store. `ha-spark health` adds a **Native SoC** row (the
  snapshot's SoC, trust and age) and stops warning about the SoC, battery
  voltage, charge current and power switch entities on those sites. Add-on-owned
  sites are unchanged.

- **Native export: a write that doesn't read back is held, not aborted (#654,
  #406 native path).** On an integration-owned site, an export window or
  discharge-current write that doesn't read back before the event starts now
  sends one **Axle export held: write not verified** notice for that event,
  with its safe result, and ha-spark retries every pass. **Axle export
  aborted** is sent only when the event starts without a verified arm or a
  cleanup fails; terminal refusals (invalid export, export bound, hold
  overlap, SoC, a trip) still abort immediately. Notices still go out once per
  event and step, across restarts. Once a held event is aborted it never arms
  again: later passes, a new supervision and restarts refuse it as
  `export_aborted`. Every other export gate is unchanged.
  Install the add-on and the integration together: an older add-on refuses a
  snapshot that carries the new transition.

- **Native previous-period energy sensors have no state class (#655).** The
  native inverter's yesterday, last-month and last-year energy sensors no
  longer claim `total_increasing`, so Home Assistant keeps no long-term
  statistics for them. They stay disabled by default, and the lifetime totals
  are unchanged. If you enabled one before, clear its "no longer has a state
  class" repair in **Developer tools → Statistics**.
- **Site device lookup scoped to the config entry (#559, #634).** The
  integration no longer calls Home Assistant's deprecated
  `device_registry.async_get_device`, which logged a deprecation warning when
  Solcast was configured and stops working in Home Assistant 2027.8. The
  Solcast device now links only to this entry's own site device.

- **Heating-only heat pumps can be entered (#558).** A heat pump's cooling
  demand may now be 0 ("0 if it doesn't cool"). Heating stays required and
  above 0, both fields stay required with no default, and the load still
  counts at the larger of the two.
- **A refused remove no longer traps the topology editor (#551).** Removing a
  connection or bank that is still in use, or the last required item, shows
  why on a confirm form; submitting it returns to that list's menu with your
  unsaved changes kept.

- **Carry over a lowered discharge ceiling (#657, #561).** `ha-spark health`
  warns when a native inverter's effective DC discharge limit, converted using
  its bank's nominal voltage, exceeds `battery_discharge_ceiling_kw`, whether
  set or the default. The read-only warning uses the durable accepted topology
  and never blocks control. The topology guide explains how to carry
  the lower ceiling over as a DC current limit, rounded down.
- **Discharge ceiling default rises to 3.68 kW (#657).** The default
  `battery_discharge_ceiling_kw` is now the G98 limit (16 A at 230 V) instead
  of 3.2 kW, on every site type. Installs that never set it get the higher
  ceiling on upgrade; if you chose 3.2 kW deliberately, set it again in
  Settings. G99-approved sites can set a higher value.

- **Same-day export events reserve up to the window opening (#658, #407).**
  When a paid export event ends before tonight's charge window and the window
  opens partway through a half-hour (say 23:45), the energy reserved after the
  event now also covers the house load in that half-hour before the window
  opens, with the same strict protection as the rest of the reservation (no
  solar or cheap-rate discount). Windows that open on the half-hour are
  unchanged.

- **Downgrade reason names the writing release (#660, #517).** The stored
  settings document may now carry an optional `written_by` release version.
  When a settings document holds unknown fields and records a different
  writer, the configuration repair reason names both versions, for example
  "Unknown configuration fields: 1 (written by ha-spark 0.33.0; this is
  0.32.0)". A malformed `written_by` is ignored. This release reads the key
  but never writes it, so downgrading from it still loads cleanly; a later
  release will start writing it.

- **Upgrade together.** Update the add-on and the integration to 0.32.0 at
  the same time: a 0.31 integration fails closed on a store holding the new
  `unverified` export row, and a 0.31 add-on refuses a snapshot carrying it.

## 0.31.0

- **Add-on and integration now speak contract `api_version` 4 (#564).** Both
  accept versions 1–4, so 0.30.x peers keep working, and the heartbeat body may
  now be up to 64 KiB. Snapshots are checked more strictly: one for another
  site, one sent more than five minutes off the clock or older than the last
  accepted one, or one whose authority epoch goes backwards is rejected and
  never refreshes the connection's freshness. Nothing changes on the
  hardware.

- **The daemon supply guard no longer cuts charging just because the topology
  record is unreadable (#555, #565).** While the accepted record can't be read,
  it uses `supply_max_current_a` as its limit and cuts to 0 A only while a fresh
  grid reading (in W, reported within the last 60 s) exceeds it; a missing,
  stale or future-dated reading, or an invalid legacy setting, writes nothing.
  The cut still follows the operating mode and is read back. **Known gap:** an
  accepted native connection limit lower than `supply_max_current_a` isn't
  enforced while the record is unreadable.

- **Battery banks gain a one-way charge efficiency (#566).** The new bank
  option defaults to 0.949 and must be above 0 and at most 1; it is separate
  from the round-trip `charge_efficiency`. Both supply guards now count the
  AC a charge draws as its DC current divided by this efficiency, so a
  clamped charge never draws more from the grid than the allowance. The term
  only ever lowers a charge current. The daemon guard uses the accepted
  native bank's value, or 0.949 otherwise.

- **`solis_control_hub` is empty by default (#567).** An existing install with
  a legacy Solis and no saved hub gets `solis_control` written explicitly on its
  first 0.31 start, so its control path is unchanged; importing a pre-0.31
  settings export without the hub does the same. Exports and `onboard --write`
  fragments now always include `solis_control_hub`.
- **`ha-spark health` reports the legacy Solis transport (#567).** One row per
  legacy Solis inverter: ok while the overlay hub's read-back sensors are
  readable, with a warning that HA 2027.10 removes YAML `modbus:` hubs; fails
  with "overlay hub missing" or, for an empty hub, "no control path" (the
  inverter clock check is then skipped).

- **The integration has its own durable authority store (#568).** It is kept
  per config entry with a separate initialisation marker, and every save is
  read back. A missing or corrupt store, a marker mismatch or a failed save
  makes authority unknown: nothing writes, a repair offers only Observe, and
  confirming it writes nothing. New read-only entities show the operating mode
  (including *activating*, *switching to Observe* and *authority unknown*),
  the manual session, fallback active with its causes, holds and the
  authority epoch. On an add-on-owned site the add-on's store stays the
  authority and these entities mirror it.

- **A site's authority owner can move to the integration (#569).** Each
  topology inverter now has a transport, `legacy` (the default) or `native`.
  A site with any legacy inverter stays add-on-owned; an all-native site
  switches to integration ownership through a durable handshake. Saving the
  switch is refused, with the unmet condition named, unless the add-on is in
  Observe with no holds, no transition and a known store. The integration then
  records the switch as pending, the add-on retires its own store, and the
  integration starts its store in Observe. Nothing writes while the switch is
  pending, a crash at any step resumes or stays safe, and the reverse switch
  runs the same steps back. Both peers must be 0.31 or later; an older peer
  never completes a switch.

- **Operating-mode transitions and temporary manual control on integration-owned
  sites (#570).** The integration now runs the Observe, Protect and Optimise
  transitions itself. *Activating* lasts until the first fresh plan verifies,
  leaving Optimise for Protect writes the fallback schedule as your starting
  schedule, and going to Observe makes a verified release. Manual control
  hands the controls to you for one hour, until tomorrow (07:00 on the
  household clock by default, set under **Manual control deadline**) or until
  you resume, and keeps protection on unless you say otherwise. Its expiry
  never replays an old plan. Reloading the integration keeps authority;
  disabling or removing it in Protect or Optimise first hands over to Observe,
  with a notification if that fails.
- **Three owner services leave automatic control (#570).** `ha_spark.set_observe`,
  `ha_spark.start_manual_control` and `ha_spark.resume_control` are for
  administrators only and can't enter Protect or Optimise. A refusal names its
  reason (not owner, authority unknown, transition in progress, no manual
  session, not in an active mode). Each accepted call is written to the
  logbook and the log with the user, the change and the new epoch.

- **Owner requests reach an integration-owned site (#571).** On such a site
  the integration fetches mode changes, manual control requests, clock syncs
  and re-probes from the add-on over its authenticated channel and posts the
  results back. The channel isn't reachable through ingress, caps every body
  and never logs payloads. A command runs at most once: a repeat returns the
  stored result, a stale or replayed one is refused with its sequence named,
  and a mode change waits for in-flight writes before it takes effect. In
  ingress and the CLI a request waits up to 10 s, then shows as pending and
  reports its outcome from the next snapshot; it is never resent
  automatically. The new data-free `ha_spark.commands_pending` service prompts
  a fetch.

- **The add-on writes no hardware on an integration-owned site (#572).** Once
  its store is retired, every direct write is refused and the daemon's supply
  guard, hold reconcile, SoC monitor, clock sync and V2L stay idle. Each plan
  is sent to the integration as per-inverter charge window and charge current
  intents, stamped with the plan's epoch, and the plan output shows each
  result: applied or unchanged, refused with its reason, deferred, or failed.
  A refusal for a stale epoch replans once under the new one. The planner
  takes a native bank's SoC from the integration only, and untrusted or
  missing SoC blocks SoC-based output. `ha-spark health` gains an
  **Authority owner** row with the owner, the handshake state and the
  snapshot's freshness.

- **The integration now requires `pymodbus` 3.13.1 or later (#573), the version
  Home Assistant itself ships.** It carries the
  native Solis Modbus connection: one paced connection per gateway, with
  writes queued ahead of polling reads and never resent after a disconnect.

- **Connect a native Solis inverter (#574).** Choosing the `native` transport
  in **Site topology and limits** adds a connect step that reads the
  inverter's identity, tests the gateway for caching and records a durable
  ownership claim on the gateway's host, port and unit id. The device is
  validated only on the supported identity with caching off; anything else,
  an unreachable gateway or a lost claim leaves it read-only with a repair,
  and readings continue. Any read-only device blocks Protect and Optimise.
  The gateway appears as its own device with connectivity, ownership claim and
  caching probe entities, and the administrator-only `ha_spark.reprobe`
  service, also sent from the add-on, runs the checks again.

- **Native Solis readings in Home Assistant (#575).** A native Solis inverter
  now gets read-only entities for SoC, battery voltage, power and direction,
  grid-port power, the grid-charge permission bit, the inverter clock, its
  identity, the export limit and the Energy-dashboard counters (PV, battery,
  grid import/export, consumption), plus a battery-bank SoC when the bank has
  exactly one native inverter. Frames are read about once every two seconds
  per inverter, never more than one request per second; other registers in
  those frames are disabled-by-default diagnostic entities. A frame that fails
  to read makes only its own entities unavailable. Nothing is written to the
  inverter.

- **One verified write path for native charge currents (#576).** On an
  integration-owned site every charge current the integration writes, whether
  from a plan, the fallback, a hold restoration, the supply guard or your own
  entity edit, goes through one path. It rechecks the operating mode, epoch,
  manual session, ownership and limits at write time, applies the supply-guard
  clamp, writes reductions before increases and reads the value back. A
  current is in whole amps rounded down and at most 62.5 A, and only changed
  values are written. Raising a current needs trusted SoC. A value that
  doesn't read back is reported unverified and triggers one caching probe.
  For two minutes after the integration starts, plan writes are refused
  (`not_available`) so that only protection writes run while it settles.

- **Native charge windows follow the legacy Solis rules (#577).** A plan's
  charge window is written to Slot 1 as one whole block, and is refused with
  `grid_charge_not_permitted` while the inverter's grid-charge permission is
  off. A live charge window is switched off and checked before its current
  changes, and the new window is set only once the current reads back. Slots 2
  and 3 are kept clear, and until supervised export is in force the discharge
  half of Slot 1 is cleared and zero-confirmed. Untrusted SoC refuses every
  window write.

- **Native Solis write entities (#578).** A native inverter gets charge and
  discharge current numbers, four Slot 1 charge and discharge start and end
  times and a power switch select, all configuration entities. Every write
  goes through the verified charge path and its supply-guard clamp; a time
  edit rewrites the whole Slot 1 block and an invalid window is refused.
  Charge edits are refused while ha-spark is in Optimise or fallback, discharge
  edits work only during manual control, and the power switch belongs to
  protection: you can set it in Observe, or in manual control without kept
  protection. Nothing is writable while authority is unknown, a save failed, a
  switch is pending or the device is read-only. Each write is logged with its
  caller.

- **Native Solis SoC trust (#579).** Each battery bank on a native Solis
  inverter now has a diagnostic *SoC trust* sensor (trusted, untrusted or
  recovering, with the reason) and reports its SoC, read time and trust to the
  add-on. The checks are the add-on's own: the first failed SoC check blocks
  SoC-based work and charge-rate increases at once, fallback is requested at
  the failure threshold, and recovery needs an unbroken run of passing minutes.
  A bank shared by more than one native inverter is never trusted. The maximum
  report age, failure threshold and recovery minutes are integration options
  with the add-on's defaults. Trust only narrows what is allowed; nothing is
  written to the inverter.

- **Holds on native Solis sites (#580).** **Native protection evidence** in
  the integration options sets the EV status and battery direction sources,
  which are required (direction defaults to the inverter's own battery power),
  the optional EV plug, EV power, live dispatch and adjusted-rate sources, and
  a maximum age for each. Holds then run as they do on the add-on: a hold
  starts only on fresh evidence and is recorded before the power switch goes
  Off, an EV hold ends after five advancing clear reports and a dispatch hold
  at its last trusted end, and a release restores and verifies the schedule
  while the switch is still Off before writing `On` to a readable switch.
  Holds survive restarts, a shutdown never releases one, and a hold that can't
  be applied raises a notification. Reconcile runs in Protect and Optimise and
  in manual control with kept protection, never in Observe.

- **Fallback charging and reachable Protect and Optimise on native sites
  (#581).** **Native fallback schedule** sets a cheap window on the household
  clock and a current for each inverter, with no default. A save is refused if
  a current is over its inverter's limit, the total is over a bank or supply
  limit, the window is invalid or a control can't be read back. In Optimise,
  fallback starts when the add-on has sent no heartbeat for two minutes or SoC
  integrity fails, is checked against the live limits before each apply, and
  counts only once it reads back. It clears after five minutes of fresh
  heartbeats from a new add-on session or the SoC recovery window, followed by
  a verified fresh plan. Notifications cover fallback entered, cleared and
  unverified. With every check in place, a fully validated native site can now
  enter Protect and Optimise.

- **The integration's supply guard now writes on integration-owned sites
  (#582).** In Protect, Optimise, fallback and manual control it lowers and
  restores the charge current through the verified charge path; it never
  starts a charge window. Under manual control it only caps your latest charge
  current and restores up to it, never above. After a restart a saved cut is
  replayed only under the same epoch and connection, and only ever lowers the
  current. On add-on-owned sites it stays display only.

- **Native inverter clock sync (#583).** On an integration-owned site the
  integration sets the inverter clock automatically at a household clock
  change, such as the start or end of summer time, in Protect and Optimise
  including fallback; never in Observe or manual control. A drifted
  clock raises a repair whose fix, like `ha-spark solis sync-clock`, syncs in
  Observe, Protect, Optimise or manual control. Every sync is read back, and
  none runs while authority is unknown, a save failed, a switch is pending or
  the device is read-only.

- **Supervised native export (#584).** On an integration-owned Solis site in
  Optimise, a planned Axle export runs only after you arm it with **Arm
  supervised export** in ingress or `ha-spark export supervise`, confirming
  each inverter's rated AC output; both are administrator only. The
  supervision covers that one event and ends at its end, on a cancel, on
  leaving Optimise, or with **End supervision** or `ha-spark export end`. An
  unsupervised export is refused as not supervised, and export never runs in
  fallback, Protect, manual control or Observe. The device export bound is the
  confirmed rating, no higher than the site export limit, and is checked again
  just before each window is armed. Export follows the legacy driver's rules
  step for step; every refusal clears and zero-confirms the discharge half.
  New diagnostic entities show the export bound, the supervision and the
  export record.

- **Native export lifecycle notices (#585).** On an integration-owned Solis
  site the integration now keeps a durable, ordered log of each supervised
  export's transitions (accepted, armed, started, cleanup verified, aborted,
  clock hold), and the add-on turns it into the usual Axle export notices. A
  transition made while the add-on is down is still notified exactly once,
  after it reconnects. Start and cleanup are only ever notified after the
  inverter read back the change; an export you did not supervise is reported
  as aborted (not supervised).

- **Native export next to holds, fallback and restarts (#586).** On an
  integration-owned Solis site a hold now always beats a supervised export,
  and every way out of an export (SoC fallback, leaving Optimise, manual
  control, disabling the integration, ending supervision, a `none` request)
  clears the inverter's discharge window and confirms it stayed at zero for
  five seconds before the export counts as cleaned up; a failed clear is
  retried every tick, including during fallback. After a restart an export
  window is only kept if the inverter still holds exactly that window, and is
  never re-armed from memory. A changed inverter identity or gateway claim
  now ends export supervision for that inverter.

- **Live export tripwire (#587).** While a supervised export is live on an
  integration-owned Solis site, the integration watches the whole-site grid
  reading; two consecutive fresh readings more than 200 W over the site export
  limit stop every export on that connection for the rest of its window (a
  durable stop latch written before cleanup, honoured across restarts and
  Re-probes), end every export bound on that connection until the owner
  confirms a new supervision, and log `tripped` for the add-on's notice. A
  missing or stale grid reading never cuts: it warns after 60 s and raises a
  repair after two minutes. PV-only export is never curtailed.

- **`ha-spark health` reports native Solis transports (#588).** Each native
  inverter gets a **Solis transport** row: ok when validated, a warning with
  the reason while it is read-only, and a failure, "inconsistent", while an
  overlay hub is still configured. ADR-0012 makes the integration's own Modbus
  connection the native Solis control path in place of the overlay hub, and
  `docs/runbooks/solis-native-migration.md` gives the ordered migration and
  rollback steps.

## 0.30.0

- **The companion integration now describes your site's topology and limits (#533).**
  **Options** gains **Site topology and limits**, where you record grid
  connections, battery banks, inverters, a V2L charger and major loads with
  their limits. **Save topology** validates the whole document, stores it with a
  new revision, reloads the integration and requests a replan, without changing
  the operating mode or authority. The add-on stores the last accepted topology
  durably and keeps using it across restarts. Removing a connection or bank that
  is still in use, or the last one, is refused; a site holds at most 4
  connections, 6 banks, 8 inverters and 12 major loads.
- **Plans and charge commands now use one set of effective limits (#533).** Each
  inverter is capped by its owner setting and its driver's known ceiling (62.5 A
  for the Solis), and unconfirmed limits count as zero; each bank is capped by
  its DC limits and its inverters, and each connection by its supply import and
  site export limits. Charge currents now round down to whole amps (42.6 A gives
  42 A, not 43), so rounding never exceeds a ceiling. A topology with exactly one
  connection, bank and inverter of the add-on's `inverter` type drives the
  hardware; any other shape is a preview only, and hardware keeps the last
  accepted supported limits or the legacy settings. `ha-spark health` and
  `ha-spark plan` show the effective limits and their source.
- **Major loads are now part of the plan (#533).** Declare each EV charger (7.4
  kW preset) or heat pump (heating and cooling demand, counted at the larger),
  optionally with a power sensor. Every slot leaves the larger of the biggest
  idle major load and half of all idle major loads unused; a running load counts
  at its measured draw, and a load with no or stale reading stays in the reserve.
  The plan says when the reserve or a hardware limit capped the charge, and
  `health` warns when the reserve exceeds the supply room after the forecast
  overnight base load.
- **The integration runs its own supply guard, display only for now (#533).**
  Once a topology is saved it watches each grid connection's grid-power reports
  and shows when site import has run over the supply limit, with the charging
  allowance that would apply, through new site-device entities and diagnostics.
  In 0.30.0 it has no hardware writer: the add-on's daemon guard, now measured
  against the effective connection import limit, still lowers the charge
  current.
- **Unconfirmed limits block Protect and Optimise (#533).** Until every saved
  inverter's limits are confirmed, entering either mode is refused with the
  reason; leaving a mode is never blocked. Once a native topology is accepted
  the add-on never returns to the legacy settings. Update the add-on and
  integration together: both support `api_version` 1–3, either order is safe,
  and nothing changes until you save a topology.
- **Known limitations in 0.30.0 (#533).** Several banks or connections are
  preview-only until per-bank SoC arrives. Re-adding the integration restarts
  its topology revision at 1, and the add-on keeps the old accepted topology
  until a save passes that revision. V2L refills and bank capacity, minimum SoC
  and target cap still come from the add-on settings.

## 0.29.1

- **The native forecast can now be picked in the Energy dashboard (#529).** The
  dashboard's solar forecast list only offers service integrations, so it
  skipped ha-spark's forecast. The integration now declares itself a service.
  The Solcast device also links to the site device by its device id, ahead of
  Home Assistant 2027.8 removing the old way.
- **`ha-spark mode set` no longer prints a stray colon (#529).** A refusal
  without reasons, such as asking for the mode the site is already in, now
  reads `[REFUSED] no_change`.
- **Cutover runbook corrections (#529).** It now names the Solcast sensors'
  entity ids (`sensor.ha_spark_forecast_today`, `…_forecast_tomorrow`,
  `…_last_fetch`, `…_api_calls_used`), includes YAML-mode dashboards in the
  inventory, clears the legacy forecast settings with `ha-spark config set`
  (the Supervisor Configuration tab no longer edits them), notes that native sensors have no `estimate` attribute,
  and warns that *API calls used* counts only ha-spark's own calls.

## 0.29.0

- **Native Solcast in the companion integration (#519).** Add your Solcast API
  key in **Reconfigure** and the integration finds your rooftop sites itself.
  The **Options** flow sets a legacy account's 50-call daily limit, and a
  rejected key asks you to reauthenticate. Every call is counted before it is
  sent, the count survives restarts and resets at midnight UTC, a restart reuses
  the saved forecast, one poll is reserved for 20 minutes before the charge
  window, and the rest are spread from sunrise to sunset. The integration adds
  today's and tomorrow's P50 forecast sensors with P10/P90 attributes, diagnostic
  fetch and quota sensors, an Energy dashboard solar forecast, and redacted
  diagnostics. The key and site ids stay out of logs, errors and add-on output.
- **The add-on plans from that forecast once it is configured (#519).** It no
  longer reads `solar_today_entity` or `solar_tomorrow_entity` then, and it
  remembers the native source across its own restarts so a restart cannot fall
  back to them. `solar_percentile` still picks P10, P50 or P90 and
  `solar_haircut_k` still applies. A missing, incomplete or older-than-36-hours
  forecast counts as zero solar, which makes the charge bigger rather than
  smaller. Sites without native Solcast keep the legacy entity path unchanged.
- **Same-day daytime Axle export slots (#201).** With fresh native P90 evidence,
  a today-dated daytime slot is admitted when the fixed discharge command plus
  that slot's P90 solar stays within the DNO and supply limits. The check gives
  no credit for house load and uses P90 without the haircut. Without fresh
  evidence the existing daylight refusal remains, and today's solar still never
  funds an event slot.
- **`ha-spark health` now reports the solar source (#519).** On a native site it
  also shows the forecast status and fetch age, and no longer requires
  `solar_tomorrow_entity`. The wire contract moves to `api_version` 2, while both
  halves still speak version 1, so a version skew degrades to no native forecast
  instead of a broken link.
- **Update both halves.** Update the add-on and the companion integration to
  0.29.0 together. Either order is safe: until both speak `api_version` 2 the
  link stays up and the add-on keeps using the legacy forecast entities.

## 0.28.1

- **Refused hold releases back off (#448).** In Optimise, when the battery
  schedule a hold release needs keeps failing its read-back, ha-spark now
  retries the release at most every 10 minutes after five refusals in a row,
  instead of rewriting the schedule every minute. The battery stays held
  throughout, and starting a hold is never delayed.
- **Quieter, more private log (#493).** At the default `log_level: INFO` the add-on
  log no longer lists every HTTP request. Those lines came to about five a minute
  and some URLs carried your Octopus account number or home coordinates, which
  then ended up in any log you shared. Failed requests are still reported by
  ha-spark's own error lines.
- **Charge reason quotes the planned charge (#494).** *Next planned change* and
  *Plan status* now say how much the plan will actually charge (the same figure
  as *Charge needed*), not the size of the reservation it covers.

## 0.28.0

- **App shell and plan view (#381).** **Open Web UI** now opens an app with a
  side rail: **Plan** is the default and **Mode** is the existing operating-mode
  page. Every screen shows the site's operating mode, whether authority is known
  and the fallback state. A reload keeps the screen you were on, and the app
  follows your light or dark preference.
- **Plan screen.** The plan's headline and cost tiles sit above a half-hour
  chart on your household clock: the expected SoC against the minimum and target
  lines, the raw solar forecast dashed, the plan's after-haircut solar dotted,
  house load, one lane each for charge, hold, export, skip and V2L, and import
  and export price steps. The reason lines below are the planner's own wording;
  selecting one highlights its span, and selecting a half-hour shows that slot's
  numbers. A "today so far" span compares the forecast recorded before each
  half-hour with measured solar when `derive_solar_generation_entity` has Home
  Assistant five-minute statistics. When the SoC reading isn't trusted, the
  SoC-derived lane, numbers, costs and actions are blanked with the planner's
  warning; the daily planning model still shows the headline, costs and reasons.
- **Replan.** A **Replan** button asks for a fresh plan through the same path as
  the CLI's `run` and the agent's `run_plan`, so it plans exactly as the
  operating mode allows and unchanged setpoints write nothing. Presses close
  together combine into one replan, and each request is recorded under your Home
  Assistant user. A "still queued" reply means reload rather than press again.
- **Grid carbon strip (optional).** Off until you pick a region in the plan
  view. It then polls NESO every 30 minutes and draws NESO's own index labels,
  with passed periods marked estimated, a "carbon forecast ends here" tick and a
  note when the data is stale. Clearing the region stops the calls. Carbon never
  affects the plan, the operating mode, Protect or Optimise, or readiness.
- The app and its routes are ingress-only; the agent port and standalone listener
  do not serve the plan view, its timeline, Replan or carbon. No add-on
  configuration options changed.
- **Security (#496):** at `log_level: DEBUG`, the websockets library's frame trace
  logged Home Assistant's WebSocket authentication message, including its access
  token. That trace is now kept off at every log level. If you ran at DEBUG and
  shared or kept those logs, rotate the token they contain (standalone `ha_token`).
- Downgrading: once a carbon region has been saved in the plan view (including
  turning it off), the stored configuration carries `carbon_region`, which
  0.26.x doesn't recognise. A downgrade to 0.26.x then starts in configuration
  repair until `ha-spark config import` restores a configuration without it.

## 0.26.1

- **Security (#488):** `notify_service` and `v2l_notify_service` accept only a bare
  service name (lowercase letters, digits and underscores, with an optional
  `notify.` prefix), and every Home Assistant service call refuses any other
  domain or service name before sending a request. Before 0.26.1 a name with
  path characters could reach another Home Assistant REST endpoint. Any name
  that worked as a notify service is still valid. A stored name with other
  characters puts the add-on into configuration repair on update; check
  `ha-spark config show` first if yours has one.
- The agent port answers a bearer token with non-ASCII characters with 401
  instead of 500 (#489).
- `ha-spark health` no longer ends with a traceback when the daemon closes its
  socket first (#490).

## 0.26.0

- **Update the add-on first, then the integration through HACS, then restart Home
  Assistant (#472).** The add-on no longer pushes any Home Assistant states, so
  install the companion integration to keep the plan sensors. Either order keeps
  today's entity ids; see *Updating to 0.26.0* in the docs for a leftover `_2` id.
- **Companion integration (read-only).** Install it from HACS as a custom
  repository and accept the discovered **ha-spark** integration. The add-on
  announces itself through Supervisor discovery with a per-boot token held only
  in memory; the integration polls `POST /integration/heartbeat` on port 8099
  every 30 seconds. One entry per site; standalone installs get no integration
  yet. It shows the plan numbers, *Plan status*, *Next planned change*, *Last
  run*, the V2L session totals, *Add-on connected*, and read-only mirrors of the
  operating mode, fallback and holds on one **ha-spark** device. Nothing it shows
  can change the mode, authority or any hardware write.
- `sensor.ha_spark_plan_status` now shows the current action (`hold`, `export`,
  `charge` or `idle`) instead of the model name, and no longer carries
  `operating_mode`: change automations that matched model names, and use
  *Operating mode* for the mode.
- **Removed sensors:** `sensor.ha_spark_soc_now` (use the inverter's SoC entity),
  `sensor.ha_spark_soc_integrity` (*Fallback active*, and SoC status in *Plan
  status*), `sensor.ha_spark_v2l_power_w` (your V2L power source entity) and
  `sensor.ha_spark_predictions` (none yet).
- An incompatible add-on/integration API version raises a Home Assistant repair;
  the integration's diagnostics redact the token and host. `ha-spark health` and
  `GET /api/health` show the integration link; a missing integration is a
  warning, not a failure.

## 0.25.0

- **Before updating to 0.25.0: switch to Observe, copy your options YAML from
  the add-on Configuration tab (Edit in YAML), and keep it, then update (#456).**
  The first start seeds ha-spark's own store once from `options.json` or the
  Supervisor's stored options (the expected source on current Supervisors).
  `Configuration committed ... actor=seed source=options|supervisor|defaults`
  identifies the source. If none is found while the mode is Protect or Optimise,
  configuration repair asks you to import a saved configuration: run
  `ha-spark config import <file>` on your saved copy (in Observe it seeds
  defaults instead, so import it then too). Check
  `ha-spark config show`, then return to your previous mode. The Supervisor's
  `Option '<key>' does not exist in the schema` warnings for retired keys every
  15 minutes are expected and harmless.
- **Settings now belong to ha-spark, not the Supervisor form.** Only `log_level`
  remains there. Runtime settings live in a versioned settings document and a
  separate `0600` secrets file in the data directory, committed as one revision.
  Revision checks prevent silent overwrites; failed validation leaves the
  committed files and running settings unchanged. Unknown fields are refused,
  and runtime environment variables no longer override settings. Integration-owned
  fields remain in this store for now, tagged for their later move.
- **Owner configuration commands:** `ha-spark config show|export|set|set-secret|import`
  use the same settings service as ingress. Writes require the running daemon;
  reads can inspect the files without it. Exports contain no secrets. Enter
  secrets through `set-secret`'s no-echo prompt or stdin, never argv; `--clear`
  explicitly unsets one. Import accepts saved Supervisor JSON/YAML or an export,
  replaces non-secret inputs and preserves omitted secrets. `onboard --write`
  prints a secret-free import fragment and its import command.
- Safe planning and tariff edits apply live and request a replan. Device
  addressing, listener, token and startup-bound client changes are reported as
  **pending restart**, with the whole candidate waiting together. A save never
  moves the database, changes the operating mode or authority, or writes hardware;
  fallback-schedule validation and stale-plan fencing remain in force.
- **Configuration repair keeps a broken install reachable, not protected.**
  Missing, invalid or untrusted configuration starts only repair and diagnostics:
  no plan, drivers, scheduler tick, hardware writes or mode transitions. Health
  fails and mode status shows stored authority as suspended. While in repair,
  a switch already Off stays Off, the EV is not protected, resident export windows
  are not supervised, and ha-spark applies neither holds nor fallback charging.
  Take manual control as needed. A valid `config import` ends repair and runs
  normal startup, including hold reconciliation before the first plan. A document
  written by a newer schema is not overwritten.
- Ingress-only `GET/POST /api/config` and `POST /api/config/import` expose masked
  reads and revision-checked owner saves with ingress identity and request checks.
  They are absent from the published agent port and standalone listener. Agent
  `set_config` continues to refuse every change with `unavailable` until phase 14's
  owner-approved proposals; no agent token grants configuration access.

## 0.24.1

- **Security: agents can no longer change configuration (#457).** The config
  endpoints (`GET`/`POST /api/config`) now exist only on ingress, where an owner
  save needs the ingress proxy's user header, `X-HA-Spark-Request: config` and a
  JSON body. They are no longer served on the published agent port or the
  standalone listener. The agent's `set_config` (`POST /agent/config` and the
  MCP tool) refuses every change with `unavailable` until owner-approved config
  proposals exist. If you enabled the published agent port, review your
  options and rotate `agent_api_token`.

## 0.24.0

- **Site operating mode (#409).** One site-wide mode, Observe, Protect or
  Optimise, replaces `proactive_mode` and per-device `control`, which are
  retired and ignored with a warning. The mode lives in a durable authority
  store under `/data`, not in the add-on options, so no settings save, preset
  or agent request can change it. Only an owner changes it, from the new
  admin-only ingress page or `ha-spark mode set|confirm-observe`
  (`ha-spark mode show` reports status and unmet preconditions). Each
  change carries the epoch the owner saw, is refused with named reasons unless
  its preconditions hold, and leaving an active mode runs an ordered,
  read-back-verified handover whose failures stay visible. A missing or corrupt
  store after first use stops every hardware write until the owner confirms
  Observe.
  - **Observe** plans, learns, publishes and logs every intended write as
    `[SIMULATE]`, with no hardware-actuating call.
  - **Protect** owns only the hold that stops the house battery feeding the
    car, plus DST clock sync when enabled. It never writes your charging
    schedule.
  - **Optimise** runs the full plan: charge window and current, supply guard,
    holds, the fallback charging schedule and supervised Axle export.
- The daemon is the only actuator. `plan --apply`, `run --once`,
  `solis sync-clock`, the agent's `run_plan` and `POST /agent/run` submit work
  to it over a `0600` Unix socket and refuse hardware writes when no daemon is
  running.
- Holds start only on fresh evidence. Each evidence source has a
  `*_max_age_seconds` option, and `ev_liveness_entity` /
  `dispatch_liveness_entity` can vouch for sensors that report only on change.
  A confirmed EV hold clears after five advancing fresh not-charging reports,
  or on an explicit switch to Observe, and the count survives restarts. A
  clean shutdown never releases a hold the car still needs. `sensor.ha_spark_operating_mode`
  publishes the mode, transition, epoch, holds and fallback state read-only.
- Readiness recording and signal (#53): in Observe, ha-spark records what it
  would have done slot by slot, with rates, guard coverage and forecasts. Once
  the latest `readiness_window_days` qualifying days clear the configured bar,
  it notifies you once that Optimise makes sense. `ha-spark readiness
  show|coverage|slots` shows the evidence. It never changes the mode.
- **Stricter than 0.23.0:**
  - Upgrading to 0.24.0 starts in Observe, including an install running
    `proactive_mode: on` with `control: ha_spark`. Nothing writes until you
    choose a mode; later upgrades keep the mode you chose.
  - AlphaESS sites cannot leave Observe until a validated hold surface exists
    (#196), so they lose automatic writes.
  - Sites with more than one inverter stay in Observe.
  - Without `solis_fallback_current_a`, a Solis site can enter Protect but not
    Optimise.
  - A car charging without fresh battery-direction evidence now holds the
    battery, even inside the overnight charge window.
  - The power switch is written `On` only when it reads back, including at
    handover and shutdown; a refused or failed `On` stays visible.
- **Upgrade steps:**
  1. Remove `proactive_mode` and any device `control` from your options. They
     are ignored either way.
  2. To enable Protect, configure `inverter_power_switch_entity`,
     `ev_status_entity`, `battery_power_entity` with
     `battery_power_discharge_sign`, and the max-age options for each evidence
     source. Set `solis_model_confirmed` only if `ha-spark inverter show`
     cannot read the model.
  3. To enable Optimise, also set `solis_fallback_current_a`, and disable any
     Home Assistant automation or manual schedule that writes the same
     controls.
  4. Check `ha-spark mode show` (or the mode page) for unmet preconditions,
     then request Protect or Optimise.
- A settings save can no longer change `db_path`; set it in the options or environment and restart (#441).
- The overnight EV-hold exception counts the inverter's resident charge window only when its overlay sensors report fresh, directly or through the hub's inverter-clock sensor within `battery_power_max_age_seconds`; a cached window no longer stops a new hold (#444).
- Show a held battery whose release is refused (power switch unreadable, or the
  schedule to restore not verified) as a `ha-spark health` warning, with one
  notification per episode after `hold_degraded_minutes` (#449).
- Refuse a switch to Optimise while an SoC fallback or recovery is in progress (#452).

## 0.23.0

- Recover AlphaESS programming only after stable, advancing SoC reports and a fresh plan for the remaining cheap-rate window; never request a fallback program on AlphaESS (#116).
- Restore SoC failure and fallback state safely after restart, requiring a fresh observation and Solis hardware confirmation before charge-rate increases.
- Document SoC integrity operating states (#119): DOCS.md explains each state
  of `sensor.ha_spark_soc_integrity` (blocked, verified fallback, failed or
  unconfirmed fallback, recovery) and how a computed plan differs from a
  program confirmed on the inverter. The add-on schema now rejects a
  `soc_failure_threshold` below 1, which ha-spark already refused at startup.
- Validate configured notification services during health checks and add a
  `ha-spark notify-test` onboarding command to confirm delivery (#243).
- Send the planned stop-SoC in AlphaESS charge calls using the integration's
  `chargestopsoc` service field (#260).
- Read the Solcast day total from the configured percentile attribute (#261),
  falling back to the sensor state with a warning when that attribute is invalid.
- Clarify that `solar_haircut_k` stacks with Solcast dampening and hard limits,
  and document how to use only one correction (#262).
- Discover current Octopus import tariff and product codes from the account API; use the existing `octopus_mpan` option when an account has multiple import meter points (#273).
- Report Axle snapshots with no scheduled event as healthy, including the direct API and Home Assistant calendar mirror (#275).
- Reject MCP requests carrying an Origin header on the published agent port,
  after bearer-token authentication (#279).
- Keep newly generated agent API tokens out of logs; log the token file path
  instead (#328).
- Plan from the current half-hour, sizing the next charge from the battery level
  expected at the window start, including after-midnight replans (#200, #376).
- See the evening drain as a named `reach-charge-window` reservation, with any
  shortfall explained (#376).
- Forecast solar per calendar date; configure the recommended optional
  `solar_today_entity` to count today's solar, otherwise it safely counts as zero (#376).
- Plan flexibility events later today in the same horizon as events after the
  charge window (#376).

## 0.22.0

- Plan reservation-backed V2L house-battery refills when cheaper than the grid
  (#160), with explicit plugged-in availability and a per-plan energy budget.
  Add `v2l_max_rate_kw` (3.0) and `v2l_preferred_rate_kw` (1.5), validated at startup.
- Axle export events now use `export_event_provider: axle`, independently of
  `tariff_provider` (#177). The default `none` disables Axle reads even with
  a configured API key. Fixed, dynamic, and Octopus Intelligent tariffs keep
  their import prices and dispatch holds while Axle adds event export prices.
  Health reports the two providers separately.
- **Upgrade order:** if using `tariff_provider: axle`, switch it to `fixed`
  (or `octopus_intelligent`, with its required credentials) **before updating**.
  Then update and set `export_event_provider: axle`. `axle` is no longer a
  valid tariff value; leaving it in place makes the Supervisor refuse the
  configuration after updating. Existing Axle source and rate options remain
  unchanged.

## 0.21.4

- Persist the last observed proactive mode so switching to `on` through the
  Supervisor logs the conflicting-automation reminder after restart (#197).
  A first startup already `on` also warns; unchanged `on` restarts stay quiet.
  API hot reload shares the check, and mode-record failures never block control.

## 0.21.3

- Add direct EV power and separate grid-import evidence to dispatch ratings
  (#222), including conservative mid-slot release after ten minutes.

## 0.21.2

- SoC integrity now checks jumps and stuck readings against integrated battery
  power (#244). Configure `battery_power_entity` (W); the Solis preset supplies
  it. Implausible readings block writes and count toward fallback. Two repeated
  changed readings can establish a new baseline with a BMS recalibration warning.
  Missing evidence skips integration; health warns when protection is disabled.

## 0.21.1

- Security: the ingress HTTP API (port 8099) now answers only Home
  Assistant's ingress proxy (`172.30.32.2`) and returns `403` to every other
  peer, so other containers on the Supervisor network can no longer read the
  plan or change options through `POST /api/config`. The check uses the
  connection's address, never forwarding headers. In standalone/dev mode the
  API binds to `127.0.0.1`. The token-protected agent port (8098) is
  unchanged.

## 0.21.0

- V2L top-up request (#208): when a paid Axle slot stays unfunded after the
  post-event-reserve trade, ha-spark sends a notification through
  `notify_service`: "short by X kWh, start V2L by HH:MM". It is sent only when
  the overnight charge was capped by the current ceiling, or when on the event
  day live SoC is more than `v2l_soc_tolerance_pct` (default 5) points below
  the overnight plan's path. It is also sent only when V2L energy costs less
  than the event pays. The request is sized at `v2l_charge_kw` (default 2.15
  kW DC) through `v2l_rectifier_efficiency` (default 0.94), never above
  `v2l_budget_kwh`, and repeats only if the shortfall grows by 0.5 kWh. It
  needs `v2l_power_entity`. ha-spark never actuates V2L.
- The planner now caps the overnight charge at what the window can add at
  `max_charge_current_a`. A day-ahead Axle event is funded only from that, so
  a slot the battery can't reach is skipped the evening before instead of
  failing on the day. Plans also carry the expected SoC path from the window
  end.
- `sensor.ha_spark_v2l_net_saving_gbp` and `ha-spark v2l` now count only the
  car energy that reaches the house: rectifier and battery discharge losses
  apply, so reported savings fall.

## 0.20.0

- Solis fallback recovery (#117): an active fallback ends after
  `soc_recovery_minutes` (default 10) of continuously passing SoC observations
  whose `last_reported` never goes backwards and advances at least once. Any
  failed observation restarts the wait; SoC movement is allowed. Each minute
  after that, ha-spark computes a fresh plan from the current SoC and the
  remaining cheap-rate window and applies it. The fallback stays the reported
  state until the apply reads back clean; a `[FAILED]`, `[BLOCKED]` or
  `[WARNING]` result keeps it, marks it unconfirmed so charge-rate increases
  stay blocked, and retries next minute. The transition is logged as
  `[RECOVERED]` and published on `sensor.ha_spark_soc_integrity`
  (`recovery_since`, `recovery_ready`, `recovery_action`). Recovery progress is
  not persisted, so a restart starts the wait over.
- Verified Solis SoC fallback (#115): after `soc_failure_threshold` consecutive
  failed observations, the optional `solis_fallback_current_a` programs only the
  configured cheap-rate window through the normal gated, transition-safe Solis
  path. The fallback is reported as confirmed only after current and window
  read-backs match; failed attempts retry on the next minute. No configured
  current means no fallback write. Supply reductions remain available while
  unconfirmed, and restoration under a verified fallback stays under its current
  ceiling.
- Octopus hold re-rating (#228): the per-minute reconcile re-rates planned
  daytime dispatch holds from local HA evidence, so a plug change updates the
  inverter hold within a minute. It uses the plan's dispatch windows and never
  fetches planned dispatches from Kraken between plans.
- Octopus dispatch evidence (#221): dispatches are rated from an `on` dispatch
  entity, a true adjusted-rate marker, EV status, and plug state before they
  affect holds, cheap-slot sizing, or paid Axle slots. Only a readable
  disconnected plug with no stronger evidence drops a dispatch; unreadable or
  unrecognised evidence keeps it.
- Axle funding skips report the SoC needed at the first counted event slot
  (#214). The plan report and HA plan-status sensor include the estimate for
  event house load, skipped and selected export energy, and buffered post-event
  reserve. DNO, hold, daylight, and no-capacity refusals are excluded. Estimates
  above 100% say that more than a full battery is needed.
- Solis zero read-back confirmation (#216): zero current and zero slot targets
  now need matching reads at least five seconds apart before verification or a
  write skip succeeds. A mismatch resets confirmation; non-zero targets retain
  first-match behavior.
- Mid-event replans no longer interrupt a paid Axle export (#218): a charge
  current change used to zero the whole Slot 1 block, discharge half included,
  stopping export for 10–15 s at each replan of the 2026-10-05 event. It now
  zeroes only the charge half and writes the planned export window back in the
  same block. The export half is kept only when it reads back exactly as
  planned; otherwise the whole block is zeroed as before.
- A failed or blocked apply is now retried (#168): an apply that reported a
  `[FAILED]` or `[BLOCKED]` action still counted as applied, so the next
  half-hourly replan skipped an unchanged plan. On 2026-10-03 that left Slot 1
  zeroed with no charge window. Such an apply, or a replan that raised, now
  leaves no baseline, so the next replan applies again. Simulate mode is
  unaffected.
- An Axle event can spend the post-event reserve when that pays (#207): a slot
  the battery can't otherwise fund may use the energy reserved for house load
  after the event, when the event rate beats the worst import price where the
  shortfall would be bought back, divided by round-trip efficiency. House load
  before and during the event, `min_soc` and every actuation invariant stay
  protected. The plan reports the trade, e.g. "£1.00 event vs £0.33 buy-back".
  A same-day event prices the buy-back at the standard rate.

## 0.19.6

- Day-ahead Axle events lost funding at midnight (#206): the horizon now starts
  at the charge window already in progress, so the event remains in the next
  plan after midnight. A same-day window before opening anchors to today's
  start.
- Shared notification target (#159): V2L notices now go to `notify_service`,
  the one target for every ha-spark notification. `v2l_notify_service` remains
  as a deprecated fallback when `notify_service` is blank, and ha-spark logs a
  warning at startup when it is used. It will be removed in a later release.

## 0.19.5

- Same-day Axle export events (#198): the planning horizon starts at tonight's
  charge window, so from local midnight an evening export event earlier the
  same day fell before it. Every day-of plan skipped it ("it contains no
  complete slot in the planning horizon"), and the midnight replan cleared any
  window armed the evening before. The 2026-10-04 event was never armed for
  this reason. An event that ends before tonight's window is now planned from
  the live SoC: house load until the event ends, plus the buffered load until
  the window opens, stays in the battery. Today's solar isn't forecast, so a
  same-day slot is refused wherever tomorrow's forecast shows daylight, because
  the DNO limit can't be checked there. Evening events after sunset are
  unaffected. A replan during the event counts only what is left of the slot
  in progress and keeps the window's start, so Slot 1 isn't rewritten
  mid-event. Tonight's charge is sized after the energy the event spends.

## 0.19.4

- EV charging holds (#170): when `ev_status_entity` is configured, the
  per-minute reconcile turns the inverter `Off` while the car is Charging,
  Boosting or Delivering, even without a matching Octopus dispatch. It releases
  after five consecutive clear reads. Eco+ Diverting does not start a hold, and
  EV holds are suppressed during the configured overnight charge window.
  Unreadable EV status is reported by `health` and does not change dispatch
  hold trust. Simulate mode reports the same decision without writing.
- DNO export default (#193): `dno_export_limit_kw` now defaults to 3.68 kW, the
  G98 fit-and-inform limit (16 A single-phase), instead of 7.36 kW. A site
  without G99 approval could otherwise plan export above its legal limit.
  Existing installs keep their stored value.
- Octopus tariff fallback (#176): with dispatch credentials configured, an empty
  product or tariff code no longer prevents daemon startup; missing live rates
  fall back to the fixed schedule, matching the health warning.
- Solis simulate safety checks (#175): untrusted SoC, a disallowed or
  unreadable grid-charge work mode, and an off or unreadable power switch now
  block simulated actions just as they block real programming, instead of
  showing actions that cannot be applied safely.
- SoC trust publication (#174): SoC-dependent plan sensors become
  `unavailable` when the measurement is untrusted, while independent forecasts
  remain numeric. Restart replay also masks older cached values with an
  untrusted plan status.
- Plan input reads (#171): optional Home Assistant entities with an empty ID
  are skipped without a REST request or warning; configured entities still warn
  when their read fails.

## 0.19.3

- Solis charge current mid-window (#181): a replan inside the charge window
  now sizes the current over the time left until the window ends, not the full
  window. On 2026-10-04 the 03:30 replan programmed 14 A where ~41 A was needed
  for the 2 h left, and the battery stopped at 60% against a 70% plan. The
  apply line names the hours used (`... 41 A over 2.0 h of the window`); the
  62.5 A ceiling is unchanged.
- Charge current deadband (#173): an unchanged target no longer leaves a stale
  current programmed as SoC moves. The plan re-applies when the current it
  needs rises by 1 A or more, or falls by 5 A or more, against the last
  *applied* current, so small reductions don't rewrite the inverter every half
  hour but can't add up unseen. Amps are compared only for a trusted SoC.

## 0.19.2

- SoC freshness (#169): an unchanged SoC is no longer treated as stale while
  its source is live. Integrations (solax-modbus, HA Modbus) never re-report an
  unchanged value, so an idle or slowly discharging battery's SoC went "stale"
  after 10 minutes and blocked charge programming (6 of 15 plans on the
  2026-10-03 commissioning run). A recent report from `battery_voltage_entity`
  now proves the source live, for up to 12 hours of unchanged SoC. Without it,
  or when it is unavailable or stale itself, the 10-minute rule applies as
  before.

## 0.19.1

- Solis read-back (#164): the driver now watches the overlay for up to ~15 s
  after each write, instead of ~0.2 s. On live hardware a write that had
  landed took ~5.2 s to appear (the overlay's 5 s scan interval). Every verified
  write was therefore reported as failed, the dependent charge-current and
  window writes were blocked, and Slot 1 could be left with no charge window.
  A stale overlay still gives up within ~15 s and warns.
- Add-on stop timeout raised to 45 s, so a clean shutdown can confirm both of
  its safe-state writes (power switch `On`, Slot 1) before the Supervisor stops
  the add-on.

## 0.19.0

- Opt-in daylight-saving clock sync (#161): with the new
  `inverter_clock_dst_sync` option (default off), the first per-minute pass
  after the household zone's UTC offset changes runs the same write and
  read-back as `ha-spark solis sync-clock`, whatever the error beforehand. It
  writes only in `on` with control authority; `simulate` logs "would sync". The
  change is found from the zone rules within a six-hour lookback, not from
  memory, so a restart across the change still syncs, a `timezone` change is
  never mistaken for one, and switching to `on` within the lookback still syncs.
  A failed sync retries each minute until the lookback ends and notifies once
  after 30 minutes. Success notifies with the error before and after. Drift is
  never corrected automatically.

## 0.18.0

- Inverter-clock gate (#161): the Solis driver refuses to arm an export
  window when the inverter clock disagrees with the household clock
  (`timezone`) by more than the new `inverter_clock_tolerance_minutes` option
  (default 5), when its reading is over 60 s old, or when it is unreadable. It
  sends a deduplicated, retryable "Axle export held: inverter clock" notice.
  The check gates arming only: an export window that is already verified and
  recorded is not re-checked. `simulate` takes the same decision, so a
  rehearsal shows the refusal. The option is bounded below the 30-minute
  `health` failure threshold. The charge path is unchanged.
- Overlay (#161): add the `Solis Control Inverter Clock` sensor from
  `docs/solis-control-modbus-overlay.yaml`. It reads input registers
  33022–33027 in one transaction. Export is refused until it reads.
- `health` (#161): new `Household clock` check (warns when HA's `time_zone`
  differs from `timezone`) and `Inverter clock` check (warns above the
  tolerance, fails at 30 minutes or more).
- `ha-spark solis sync-clock` (#161): operator-run. It writes the household
  clock to holding register 43000 and confirms the read-back. It writes only
  when `proactive_mode` is `on` with control authority, and otherwise exits 1.
- Runbook: the supervised Axle export clock check now uses `health` and
  `sync-clock`, records the error before and after, and aborts if the export
  starts more than the tolerance away from the event.

## 0.17.0

- Supervised Axle lifecycle notices (#133): the generic `notify_service` option
  sends deduplicated accepted, verified-start, verified-cleanup, and terminal
  abort notices through Home Assistant, with no Telegram-specific credential or
  claim that a notice proves a hardware write. The operator runbook covers
  enablement, observation, abort, cleanup, restart recovery, and return to
  simulate.
- Per-minute hold reconcile (#140): hold reconciliation now runs through its
  own `reconcile_holds`/`reconcile_tick` seam, independently of the half-hourly
  replan. Holds that start and end between replans, failed switch writes, and
  external switch changes now converge within a minute.
- Hold trust (#140): the asymmetric pre-read may close a hold when state is
  unreadable, but never opens one. Degraded dispatch reads are ignored rather
  than believed, new export windows are refused while holds are untrusted, and
  the dispatch entity is re-read each minute on the Home Assistant entity path
  only.
- Relinquish safe state (#140): when ha-spark relinquishes control, including
  on clean shutdown, Solis is returned to `On`, the configured cheap charge
  window (default `23:30`-`05:30`), and an empty discharge window
  (`00:00`-`00:00`).
- Plan diff purity (#140): `setpoint_changed` is again a pure plan diff; hold
  edges belong to the per-minute reconcile seam.
- Power-switch write behaviour (#140): `select.solisac_power_switch` is a
  `solax_modbus` select whose Home Assistant state is set optimistically on
  write. The per-minute pre-read therefore sees a successful write at once,
  keeping steady-state register writes at zero. A rejected write is retried
  once per minute by design. Native register 43007 is #146.

- Slot energy reservations (#47): slot plans carry a buffered, capped
  reach-next-cheap-slot reservation, and the required charge and its report
  explanation trace to it. The reservation is a floor on the overnight buy, not
  the whole obligation — a short daytime dispatch cannot refill the battery for
  the evening, so the buffered horizon deficit still stands alongside it.
- Axle flexibility events as a tariff overlay (#48): a new `axle` tariff
  provider accepts only a fresh, explicit export window from Axle's documented
  Home Assistant event route or a configured Home Assistant mirror, and overlays
  the configured event rate into per-slot export prices. Planning and control
  authority are unchanged — Axle supplies event facts and prices, never
  setpoints. New `axle_api_key`, `axle_api_url`, `axle_event_entity`, and
  `axle_event_rate_gbp_kwh` options; `axle_api_key` is marked `password`.
  `updated_at` is treated as Axle's change timestamp, not a freshness
  heartbeat, so a normally-published event is not rejected on every poll.
- Supervised Axle export delivery (#51): the planner calculates the maximum
  export available under a new `dno_export_limit_kw` after protecting house
  load and the post-event reservation, selects only complete half-hour slots it
  can fund, and emits an inverter-agnostic export intent that is part of the
  scheduler's setpoint comparison. The Solis driver programs Slot 1 timed
  discharge at the measured 62.5 A ceiling, in inverter-local wall-clock time,
  and requires the power switch to be `On` — it never turns it on itself. Every
  write keeps the authority gate, fresh finite in-range SoC check, read-back
  verification, and per-action failure isolation. New
  `battery_discharge_ceiling_kw` and `dno_export_limit_kw` options.
  **Prototype status:** export has not yet been actuated on hardware; the
  supervised paid-event proof is #134.
- Full power-switch ownership (#140): ha-spark now drives both edges of the
  Solis whole-inverter enable through a declarative reconcile — `Off` while a
  dispatch hold is active, `On` otherwise — instead of only ever writing `Off`.
  Desired state is a pure function of the clock and the plan's holds, so nothing
  is remembered and a restart mid-hold converges on the next tick. This fixes
  two defects: once the incumbent Home Assistant automations are disabled at
  cutover nothing else ever sent `On`, and the old loop wrote `Off` immediately
  for a hold hours in the future. A hold boundary now counts as a changed
  command, so the reconcile is not skipped as an unchanged setpoint, and it is
  exempt from the SoC-unreadable guard because it commands no SoC-derived
  magnitude — charge programming stays blocked. It settles before anything else
  reads the switch, so a hold ending does not refuse an export event against a
  state the same tick is correcting. A hold that *overlaps* an export window
  refuses the whole event with an explicit reason; a hold elsewhere in the day
  leaves it alone.
- No day-early export (#144): the Solis Slot 1 discharge registers hold a clock
  face and no date, while the planner offers an event as soon as it enters the
  24 h horizon. Programming tomorrow's 18:30 event at noon today would have
  discharged the battery at 18:30 *today* — intentional export outside a paid
  window, costing a peak-rate refill and earning nothing. The driver now refuses
  a window whose clock face would next come round before its own event, and says
  so. It also refuses while an earlier day's window is still open on the clock:
  tomorrow's 18:30-19:30 programmed at 18:31 today would put the inverter inside
  it at once and export unpaid until 19:30. The refusal is a deferral, not an
  abort: the event is re-offered every tick and arms itself once the next time
  its window opens is the event's own, so nothing is remembered. An event already under way stays armed, so a day-of pickup still
  delivers the remainder. A pending export event now always counts as a changed
  setpoint: arming is a function of the clock, not the plan, and an event
  announced a day ahead yields an identical intent every tick — so the
  unchanged-setpoint skip would have suppressed the very tick that arms the
  window, missing the event outright. An event inside a fall-back night's
  repeated hour is refused rather than armed, because the register holds a clock
  face that comes round twice and fires on the first.
- Half-hourly replanning (#46): the daemon recomputes the current plan on each
  local half-hour slot, including after startup during the day. It skips the
  inverter call when the commanded target, window, and holds are unchanged, so
  fresh SoC observations do not cause repeated device writes.
- Proactive-mode handoff warning (#104): changing `proactive_mode` from `off` or
  `simulate` to `on` logs a reminder to disable pre-existing automations or
  manual schedules that write the same devices before proceeding.
- Checked SoC measurements (#113): the Boolean `soc_valid` contract is
  replaced by one immutable checked measurement produced from a single
  Home Assistant observation. It records the observed value or read
  failure, the integrity verdict, the observation and report times, the
  measured age, and the violated threshold. Planning and actuation
  consume the value from that exact measurement, so one read can never
  certify a different read, and a failed read degrades to a blocked
  plan instead of crashing.
- Freshness is judged solely on Home Assistant's top-level
  `last_reported` for the SoC entity — never `last_updated` or an
  attribute — so an unchanged but actively reported SoC stays usable.
  Missing, malformed, naive, and future timestamps are rejected.
- New `soc_max_report_age_minutes` option (default `10.0`) sets how
  stale that report may be. Operator-visible output now names the
  concrete integrity reason: the plan-status sensor publishes
  `soc_status`/`soc_reason` in place of `soc_valid`, and a `[BLOCKED]`
  line quotes the reason.
- SoC integrity monitoring (#114): the daemon observes the configured
  `soc_entity` once per minute in every operating state, and that one
  checked measurement is reused by planning, device application,
  publication, and supply-guard work — one observation can increment
  the failure count at most once. The first failed observation enters
  **pending failure**: the resident program is left untouched, new
  SoC-based programming and charge-rate increases are blocked for both
  supported inverter types, while valid supply-guard reductions remain
  available. Consecutive failures are counted and persisted across
  restarts; any passing observation resets the count. New
  `soc_failure_threshold` option (default `3`) sets how many consecutive
  failures reach the fallback-entry threshold (fallback programming
  itself lands with #115). Monitoring state is visible as the new
  `sensor.ha_spark_soc_integrity` (operating state, failure count,
  threshold, integrity reason and evidence), in the daemon log, and in
  the plan report's untrusted-SoC line.
- V2L observe + tally + notify (#123): when `v2l_power_entity` is set, the
  daemon reads the car's V2L discharge power (W) each tick, integrates it
  into the kWh delivered this session, values it against
  `v2l_peak_rate_gbp` less a `v2l_round_trip_efficiency`-discounted
  cheap-rate refill cost, and publishes `sensor.ha_spark_v2l_power_w` /
  `_energy_kwh` / `_net_saving_gbp`. Three fire-once notifications go through
  `v2l_notify_service` (HA `notify`): unplug at `v2l_cutoff_time` (the cheap
  window is starting), plug in to recharge when V2L stops, and a predictive
  heads-up against `v2l_budget_kwh`. The session tally is persisted across
  restarts and resets daily once the car is idle; `ha-spark v2l` prints the
  live tally. V2L is a manual physical adapter with no control API, so this
  is read/observe + notify only: the planner and the drivers are untouched.

## 0.15.0

- Derived base load (ADR-0001, #45): the load forecast can now be sourced
  from base load **derived by energy balance** over the HA long-term
  component statistics (`grid_import - grid_export + solar_generation +
  battery_discharge - battery_charge - ev_charge`) instead of a single
  user-supplied consumption sensor. Both the derived path and the
  source-entity path write the same external statistic
  (`ha_spark:house_load`), so the forecast chain is unchanged — no
  need to repoint `consumption_energy_entity` between paths. A
  one-time `ha-spark backfill-load --derive` (re)builds the history
  from the configured components.
- New `derive_*_entity` options for each of the six components (grid
  import is required; the others are optional and contribute zero with
  a degradation note when unset) plus per-component `derive_invert_*`
  flags for the explicit sign convention. The same unit handling as
  the source-entity backfill (`W`/`kW` mean-power or `kWh`/`Wh`
  energy change) — an unsupported unit disables that component with a
  clear reason, preserving old behaviour on a partial setup. The
  per-component conversion preserves sign through to the formula so
  the explicit `invert` flag actually changes the contribution (a
  positive source with `invert=true` reaches the formula as a
  non-zero negative, not silently zeroed).
- New CLI flag `ha-spark backfill-load --derive` runs the energy-balance
  backfill. `--from` (source-entity path) and `--derive` are mutually
  exclusive at dispatch; the two paths write to the same external id,
  so switching between them cannot collide on stale rows. The backfill
  report names rows written, date range, every component's coverage
  range (with omitted components stated as such), sign-convention
  warnings, and omitted-component notes. The wizard (`ha-spark onboard`)
  now surfaces candidates for every `derive_*_entity` field.
- Scheduled rolling re-derivation: after every daily plan run the daemon
  re-derives the trailing 48 h from component statistics and **upserts**
  every derivable hour in that window (late-arriving or corrected
  component rows overwrite their previous target values), with the
  cumulative `sum` anchored on the latest target row strictly *before*
  the recompute window so the running total never drops. Failures are
  logged/reported and never block planning. The shared `derive_specs_from_settings`
  helper feeds both the CLI and the scheduler so the per-component
  entity-id / invert-flag mapping cannot drift.

## 0.14.2

- Solis forced charge is implemented as a **native** timed-slot control path
  (#84). ha-spark writes the charge window and current directly to the inverter
  holding registers via the `modbus.write_register` service on the thin
  `solis_control` overlay hub, and reads them back through that hub's
  `sensor.solis_control_*` entities — no dependency on the solax integration's
  `number`/`button` entities for control. The window is a single 8-register
  block write at 43143 (which *is* the commit — there is no separate commit
  step); the charge current (43141) is written as DC amps ×10. Non-driven slots
  (charge 2/3, all discharge) are zero-guarded (written to zero only when a
  stale window is present, to spare register endurance). Forced charge is
  refused unless the work-mode bitfield permits grid charging (bit 5).
- All writes stay behind the existing actuation invariants — real writes only
  under `proactive_mode: on` + `control: ha_spark`, refusal on an invalid SoC,
  per-write read-back verification, per-action failure isolation, write-if-
  changed. No live behaviour changes until the manual cutover (ADR-0003).
- **Config:** added `solis_control_hub` (default `solis_control`) and
  `solis_modbus_slave` (default `1`); **removed** the never-wired
  `charge_window_start_entity` / `charge_window_end_entity` options (they wrote
  the wrong HA domain and only ever ran in `simulate`). The `solis_control`
  overlay is a one-time manual HA-config step — see
  `docs/solis-control-modbus-overlay.yaml`.

## 0.14.1

- Agent surface gating is now evaluated per request instead of being frozen at
  startup: lowering `agent_exposure` (in the add-on options, or via
  `POST /api/config`) immediately shrinks the `/agent/*` routes, the `/mcp`
  tools, and what `/openapi.json` advertises, where before the old surface
  stayed live until the next restart.
- `agent_surface: off` — the shipped default — is now a true master switch.
  With it off, `/agent/*` and `/mcp` return 404 behind ingress as if never
  mounted; previously it only withheld the extra published port, leaving both
  reachable. **If you were using the agent surface over ingress without setting
  `agent_surface`, set `agent_surface: on` to keep it.** (#93)

## 0.14.0

Version re-baseline. The `1.0.0-rc1`–`rc4` line is retired: after live use,
v1.0.0 is redefined as the full competitive MVP (see #94), so the pre-1.0 line
**continues the `0.x` series from its last real release, `0.13.0`** (the rc jump
was premature). The competitive-MVP phases ship as `0.15.0` (derived base load)
through `0.18.0` (V2L), then `1.0.0` once the MVP is complete and validated on
real hardware. This release is the hardened foundation those phases build on —
deterministic planner, device drivers, multi-supplier tariffs, plus the P8.1
tariff-schedule pipeline — and carries the `rc3`/`rc4` fixes, which never
reached the store (the store stayed on `rc2`); they remain documented in their
sections below. Upgrading from `rc2` is a version *decrease*, so the store won't
auto-offer it — reinstall the add-on once to move onto the `0.x` line.

## 1.0.0-rc4

- Backtest costing fix: `ha-spark backtest` now rates stored grid import against
  the current tariff schedule — its per-slot cheap pattern — instead of the two
  flat fixed rates, so on a `dynamic`/`octopus_intelligent` install the
  readiness signal is measured on the tariff the site is actually on (a `fixed`
  install is unchanged). The command reads the live schedule from Home
  Assistant to do this; an empty consumption store still reports offline. (#92)
- Plan consistency: every surface that describes "the plan" — the scheduler,
  CLI, agent tools, copilot, and the offline parser — now runs through one
  plan-pipeline seam, so on a `dynamic`/`octopus_intelligent` install they all
  report the same plan the daemon applies instead of silently reverting to the
  `fixed` two-rate provider. (#91)

## 1.0.0-rc3

- Fix add-on startup: pin `mcp<2` (v2 renamed `FastMCP` to `MCPServer`).
- Octopus options: add the missing `octopus_*` option defaults so the fields
  render in the add-on config UI.
- Health check: drop the optional `ha_template_charge_needed_entity` from the
  required-entity list, so `ha-spark health` no longer fails when it is unset.

## 1.0.0-rc2

- Multi-supplier tariffs: the planner and `ha-spark backtest` now cost every
  plan against a normalised per-slot price schedule instead of a fixed
  two-rate window. Three providers, selected with `tariff_provider`: `fixed`
  (default, reproduces the previous behaviour exactly — existing configs need
  no changes), `dynamic` (half-hourly prices from an HA price sensor via
  `dynamic_rates_entity`), and `octopus_intelligent` (Octopus API prices +
  planned dispatch windows via `octopus_api_key`/`octopus_account_number`/
  `octopus_product_code`/`octopus_tariff_code`). A misconfigured provider is
  rejected at startup with a message naming the bad field; a live price
  read that fails at runtime falls back to `fixed` rather than blocking a
  plan. See DOCS.md "Tariff" for the full option reference.
## 1.0.0-rc1

Release candidate for 1.0.0 — pre-release for testing before promotion.

- Device-driver core (Phase 7): inverter adapters now live in a `devices/`
  driver package behind a registry, each advertising a `Capability` set, and
  every controllable device carries a `control` authority
  (`observe | ha_spark | supplier`). A real write now requires BOTH
  `control: ha_spark` and `proactive_mode: on` — `observe`/`supplier` compute
  and log (`[OBSERVE]`) but never actuate. Config gains a structured `devices:`
  list; existing flat setups are migrated in memory automatically (no config
  change needed, options.json is never rewritten). The agent surface's
  `get_state` now reports each device's control authority. No behaviour change
  for the existing Solis install.
- (Back-note) The multi-inverter charge contract (`ChargeIntent`, the
  inverter selector, the AlphaESS adapter) landed in an earlier build without a
  changelog entry; it is the foundation this phase formalises.

## 0.13.0

- Agent surface: ha-spark now exposes its data and a few gated actions to an
  external model. The add-on HTTP API moved to FastAPI; in addition to the
  existing ingress API it serves OpenAPI tool routes under `/agent/*` (for
  open-webui / curl) and an MCP server at `/mcp` (for Claude). A new
  `agent_exposure` option (`read` | `read_act` | `read_write`, default
  `read_act`) controls how much is exposed; act/write still pass the existing
  PROACTIVE_MODE gate, and the LLM never reaches `call_service`. Enable with
  `agent_surface: on`; for external (non-ingress) clients set
  `agent_expose_port: true` and map port 8098 — requests then require the
  bearer token (`agent_api_token`, auto-generated and logged once if blank).

## 0.12.0

- Daemon HTTP API serving: the daemon now exposes a read-only REST API through
  add-on ingress (no open port, authenticated via HA's ingress proxy) for the
  future companion integration. Hot config reload when `/data/options.json`
  changes — the daemon detects the change and reloads without restart. API
  endpoint: `GET /api/plan` (returns the current computed plan).

## 0.11.0

- Add the proactive orchestrator: a single decision/audit path that, after each
  daily run, computes the habit API's predictions for tomorrow, honors the
  `proactive_mode` flag (`off`/`simulate`/`on`), logs each as a `Proactive
  decision`, and publishes them to Home Assistant as a new
  `sensor.ha_spark_predictions` (count + per-prediction action/confidence/
  reason/outcome). Predictions stay advisory for now — nothing is actuated yet;
  this builds the seam future proactivity hangs off. The away-period prediction
  now uses the learned away load factor when enough history exists (best-effort,
  so a history-fetch hiccup never suppresses predictions).

## 0.10.0

- ha-spark now publishes its computed charge plan back to Home Assistant as
  `sensor.ha_spark_*` entities (charge needed, target/current SoC, overnight
  current, forecast load/solar, deficit, planned/baseline cost, plan status,
  last run) after every run, so the numbers are visible in HA instead of only
  in logs/SQLite. The last-published values are cached to `/data` and
  re-pushed on daemon startup so a restarted add-on doesn't show `unknown`
  until the next scheduled run.
- The add-on's entity config options (`soc_entity`, `charge_current_entity`,
  etc.) now ship blank instead of defaulting to the original author's Solis
  setup; use `ha-spark onboard` (entity auto-discovery) or
  `ha-spark onboard --preset solis` to fill them in. `ha-spark health` now
  flags any required entity left unset.

## 0.9.1

- Fix add-on build failure on the Python 3.13 / Alpine (musllinux) base image:
  drop the explicit `scikit-learn`/`numpy` install. scikit-learn ships no
  musllinux wheel, so pip was source-compiling it and failing for lack of a C
  compiler. The optional ML load model (`load_model: ml|auto`) is import-guarded;
  the forecast degrades to the slot-profile median. No CLI/option changes.

## 0.9.0

- Onboarding wizard (Phase 4): `ha-spark onboard` now scans Home Assistant's
  entities and proposes which one maps to each ha-spark config field (battery
  SoC, battery voltage, Solcast forecast, Octopus rate/dispatch, EV sensors,
  household consumption, grid power, charge-current control, inverter switch,
  heat-pump energy, weather), ranked by device class, unit, attributes, and
  name. Each proposal shows the configured value, the best match with why it
  matched, and whether they agree.
- `--json` emits the proposals for tooling; `--write` prints a ready-to-paste
  options fragment; `--preset solis` fills fields discovery can't match from
  the reference Solis/Solcast/Octopus/zappi setup. Proposals are advisory —
  you review and set the options yourself; the wizard never rewrites config.
- `onboard` still reports load-history readiness and keeps its exit code.

## 0.8.0

- NL copilot (Phase 5): `ha-spark ask` now grounds the Ollama tier in the live
  computed plan. Before answering, it feeds the model the same plan the `plan`
  command prints (SoC, solar, load forecast + source, deficit, charge current,
  projected cost/saving, active context), so chat explains the actual decision
  — "why 42 A", "what does tonight cost" — instead of guessing. Scoped to the
  home-energy domain; the model explains and reports only, never claims to have
  changed a setting (the deterministic planner still decides and acts).
- Grounding is best-effort and the probe runs first: if Ollama is down the
  offline parser answers as before, and if the plan can't be computed the model
  is told so rather than inventing figures.

## 0.7.0

- Occupancy habits + learned factors (Phase 6E):
  - Tomorrow's occupancy is predicted from the day-type pattern of recorded
    `occupancy_home_frac` samples and fed to the ML model as a real feature
    for the target day (previously the history mean).
  - The `away` load factor is learned from past away periods (actual load on
    those days vs same-day-type normal days) and applied automatically once
    enough away history exists, falling back to `away_load_factor` otherwise.
    The plan report marks a learned factor with `(learned)`.
  - `ha-spark learn-factors` reports the learned away factor, tomorrow's
    predicted occupancy, and the advisory habit predictions.
  - The daemon logs `predict_actions` advisories each run (gated by, and
    labelled with, `PROACTIVE_MODE`) — the seam later proactivity builds on.
    Nothing is executed yet; the model still only ever records reviewable
    facts and the deterministic planner still decides.

## 0.6.0

- Natural-language context (Phase 6D): `ha-spark ask "I'm on holiday for the
  next two weeks"` now records a context fact and replies with its planner
  effect and an undo command. When the Ollama tier is reachable it extracts
  the dates as strict JSON (validated before anything is stored); offline, a
  deterministic parser handles ISO dates and phrases like "next week", "this
  weekend", and "for a fortnight".
- `ha-spark ask "what do you know about my holidays?"` lists stored facts,
  answered directly from the context store.
- The router runs this extraction/query pass before plain chat. The language
  model only ever records reviewable facts — it never actuates hardware, and
  every recorded fact is echoed back and removable via `ha-spark context`.

## 0.5.0

- Context store (Phase 6C): record date-ranged household facts the planner
  consumes as a deterministic load multiplier. `ha-spark context add away
  --from 2026-07-01 --to 2026-07-14` lightens the overnight charge for a
  holiday; `guests` heightens it; `high_usage`/`low_usage --factor X` apply a
  custom multiplier. `context list` / `context remove <id>` round-trip.
- Active facts scale tomorrow's load forecast (both the median and ML
  candidates by the same factor, so accuracy scoring and the quantile buffer
  are unaffected) and are named in the plan report's forecast line.
- `away_load_factor` (default 0.4) and `guests_load_factor` (default 1.3)
  options set the multipliers; Phase 6E will learn them from history.

## 0.4.0

- Weather-aware ML load model (Phase 6B, optional): gradient-boosted quantile
  regression (P50/P90) over the hourly load history plus Open-Meteo
  temperatures, occupancy signals, and UK bank holidays. Needs the `[habits]`
  Python extra; without it everything falls back to the median profile.
- `load_model` option: `median` (previous behaviour), `ml` (always prefer the
  model), or `auto` (default — use ML only once `forecast-eval` shows it
  beating the median over the trailing 14 days; both forecasts are
  shadow-recorded nightly so the comparison accumulates automatically).
- `buffer_mode: quantile` replaces the fixed `charge_buffer_pct` with the
  model's own uncertainty, (P90 − P50)/P50, whenever the ML forecast drives
  the plan.
- `latitude`/`longitude` options for Open-Meteo (default: read from HA's own
  configured location). Fetched past temperatures are cached into the signal
  ledger so the model still runs from recorded data when Open-Meteo is down.

## 0.3.0

- Forecast ledger (Phase 6A): the daemon now records each night's load
  forecast (model, total kWh, per-slot breakdown) alongside the date it
  predicted, so accuracy can be scored later.
- `ha-spark forecast-eval [--days N]` joins recorded forecasts against actual
  consumption and reports MAE/MAPE per model — the baseline a future ML model
  (Phase 6B) must beat before it can drive plans.
- New signal sampler records `occupancy_home_frac` (from `person_entities`),
  `heatpump_kwh` (from `heatpump_energy_entity`), and `temp_out_c` (from
  `outdoor_weather_entity`) every 30 minutes, building training data for later
  phases. All three are optional and degrade silently if unconfigured or
  unreadable.

## 0.2.0

- Live supply guard (Phase 3, EV-aware): when `grid_power_entity` is set, the
  daemon throttles the battery's timed-charge current whenever whole-house AC
  draw exceeds `supply_max_current_a` (default 75 A) — e.g. an EV dispatch
  landing mid-window — and restores it as headroom returns. Gated by
  `proactive_mode` like all writes; disabled until the sensor is configured.
- Plan report now shows the EV energy Octopus plans to deliver across the
  upcoming dispatches.
- `ha-spark health` gains a supply-guard sensor check.

## 0.1.0

- Initial add-on release: daily charge-plan daemon (`ha-spark run`) with
  startup health report, full options schema (planner knobs, tariff, entity
  IDs, Octopus API, Ollama), persistent SQLite store under `/data`.
