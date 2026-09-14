# Fog ledger

Fog is the dim view ahead of an active map: in-scope areas you can tell are coming but cannot yet phrase sharply enough to ticket. The map's **Not yet specified** section is the store for active-map fog. This ledger is the live index across maps and sessions.

Last swept: Never.

## Where to put things

| It is… | It goes… |
| --- | --- |
| Unsharp and in scope for an active map | That map's **Not yet specified** |
| Sharp enough to state as a question | A ticket on its map, even if blocked |
| Already decided | The map's **Decisions so far**, linking the resolution |
| Past one map's destination but wanted and owned by no current map | **Deferred efforts** |
| Conditional, within a closing map's destination, unowned, and not triggered | Mark **CARRIED** on the map and index it under **Carried** |
| A random wanted idea with no source map or owner | **Deferred efforts** |

Closing a map requires marking every patch in **Not yet specified**:

- **ANSWERED** — current evidence resolved it; state and link the answer.
- **REHOMED** — another map, ticket, or scope owns it; link the owner.
- **CARRIED** — still within the destination, still unowned, and its trigger did not fire; preserve the human's rationale and index it below.

A patch that blocks the destination prevents closure. Work explicitly beyond the destination is never Carried.

Every live row states what it is, cites where it touches the build when applicable, and names the observable event that makes it ready for owned work. `Trigger: none yet` is valid.

## Carried

Live conditional fog from closed maps, grouped by the map that raised it.

<!--
### From [Map title](map link) (closed)

| Patch | Trigger |
| --- | --- |
| What remains unknown, with evidence such as `path:line` or a linked issue | Observable trigger, or none yet -->

## Triaged

Closed maps whose **Not yet specified** patches are all marked. Record maps with zero patches too.

<!-- - [Map title](map link) — triaged YYYY-MM-DD: 0 ANSWERED, 0 REHOMED, 0 CARRIED. -->

## Deferred efforts

Wanted work outside every current map's destination and owned by nobody. An idea that never came from a map belongs here too. Group into subject subsections once there are enough entries to scan; other entries then refer to them by name.

### Derived base-load correctness (from PR #106 review, 2026-09-09)

Raised by the owner review on
[PR #106](https://github.com/Kylevdm/ha-spark/pull/106#issuecomment-5593336109).
The Solis findings from that review are resolved in commits `849c706` and
`2ac51c8`; these derived-load findings remain unowned.

- **Reject nonfinite component statistics before import.**
  `build_component_series` converts external timestamps and values with
  `float(...)` but does not reject NaN or infinity
  (`ha_spark/energy/derived_base_load.py:337-339`), so nonfinite data can
  contaminate cumulative recorder imports. Add explicit finite-value validation
  and regression coverage proving invalid timestamps and values never reach
  `recorder/import_statistics`. Trigger: before derived base-load imports are
  enabled in production.
- **Preserve cumulative continuity across retained gap rows.** Historical and
  rolling rebuilds derive only hours having grid-import data, while existing
  target rows at skipped hours remain stored; later regenerated sums can
  therefore fall below a retained middle row
  (`ha_spark/energy/derived_base_load.py:487,583`). Reconcile retained target
  rows into the running sum and cover an internal grid-import gap plus repeat-run
  idempotence. Trigger: before PR #106 is merged.
- **Disable derivation on any configured component's unsupported unit.**
  `_gather_components` currently catches `ValueError` and omits the invalid
  component (`ha_spark/energy/derived_base_load.py:390-392`), which can import a
  materially wrong load history as if that component were zero. Propagate this
  failure for configured components while preserving the optional-unconfigured
  path, with a non-grid component regression test. Trigger: before derived
  base-load imports are enabled in production.

### Architecture deepening (2026-08-18 review)

Raised by the 2026-08-18 architecture review
([docs/arch-review/architecture-review-2026-08-18.html](arch-review/architecture-review-2026-08-18.html)).
Verified against code that day. Not owned by active maps #78 (destination:
Solis forced charge) or #67 (destination: competitive roadmap). Four of the
review's seven candidates are owned elsewhere and are **not** here:
candidate 7 (reservations) by #47 and #51, and candidates 1–3 — sharp on
capture — rehomed 2026-08-18 to tickets #91 (plan-pipeline module /
dropped tariff schedule), #92 (backtest tariff contract), and #93
(agent-surface gating).

- **Device seam leaks per-device config.** Drivers read identity and limits
  from flat `Settings` instead of `DeviceConfig`
  (`ha_spark/devices/inverters/alphaess.py:67` serial;
  `ha_spark/devices/inverters/solis.py:28-37` battery params), so two devices
  of one driver are unrepresentable; `ha_spark/energy/scheduler.py:234` still
  keys guard reconfig off flat `settings.inverter`; `capabilities` needs a
  live REST client to answer a no-I/O question
  (`ha_spark/energy/scheduler.py:181-200`). Deepening: push per-device knobs
  into `DeviceConfig`, flat keys feed synthesis only. Map #78 touches
  `solis.py` but its destination (forced charge) rules this out of scope.
  Trigger: a second device of one driver, or the first non-inverter device
  type lands (#58 heat pump, #61 EV chargers). Two dead-code deletions ride
  along with no trigger needed: `ha_spark/energy/chargers.py` (13-line
  re-export shim marked "removed next release") and both `supports_live_rate`
  attrs (superseded by `Capability.CHARGE_RATE`).
- **Habits "orchestrator" is shallow and duplicated.**
  `ha_spark/energy/orchestrator.py` exports `decide_outcome` (11 lines of
  docstring wrapping `return "advisory"`) and `decisions_for` (a list
  comprehension); the real behaviour is `_gather_context`, which
  `ha_spark/cli.py:346-402` reimplements inline *without* the statistics-fetch
  try/except (`orchestrator.py:89-100`). The name collides with the actual
  plan orchestration in `scheduler.py`. Deepening: one
  `predictions_for_tomorrow(settings)` in a renamed `habit_predictions`
  module; delete the CLI copy. Trigger: none yet.
- **`run_forever` has no seams.** One function owns two uvicorn server
  lifecycles, supply-guard detection, the daily plan trigger, guard tick, and
  signal sampling (`ha_spark/energy/scheduler.py:203-289`); tests monkeypatch
  six module attributes to run one loop iteration
  (`tests/test_scheduler.py:156-162` et al.), i.e. they test past the
  interface. Deepening: extract an `api_servers(state, settings)` context
  manager and a `tick(state, now)` function. Pairs with the device-seam
  entry's no-REST `capabilities` fix. Trigger: the next scheduler feature that
  needs loop tests.

### Solis control provisioning (from #84, 2026-09-08)

- **Zero-touch install of the `solis_control` modbus overlay — up to a
  supporting companion integration.** #84 ships native Solis forced charge
  driving the `solis_control` overlay hub
  (`ha_spark/devices/inverters/solis.py`), but the overlay itself
  (`docs/solis-control-modbus-overlay.yaml`) is a one-time **manual** HA-config
  step: an HA add-on cannot inject a `modbus:` block into a user's
  `configuration.yaml`. Want: make it as easy as possible to install/use so
  users don't hand-edit YAML. Two shapes weighed in-session: (a) the add-on
  writes a package file into the HA config dir and the user enables a
  `packages:` include (still one manual enable + restart); (b) a **companion
  custom-integration** doing native `pymodbus`, limited to only the registers
  ha-spark needs — with the map's standing constraint that it **must not**
  rebuild solax/solis_modbus. Beyond map #78's destination (deliver forced
  charge), which #84 met with the manual overlay; adjacent to #59 (driver-aware
  onboarding) but distinct — onboarding maps existing entities, this provisions
  a control surface. Trigger: none yet (a user hitting the manual-install
  friction, or a decision to invest in the companion integration).

### Axle adoption (from map #128, 2026-09-12)

- **Discuss a supported ha-spark adoption path with Axle at v1.0.0.** The
  supervised prototype can use Axle's Events Only Home Assistant endpoint, but
  the documented dispatch webhook and site/asset REST surfaces are
  OEM/partner-scoped or require organisation credentials and an onboarded asset
  (`docs/research/129-axle-event-contract.md:19-40`). Establish whether Axle
  will support ha-spark adoption and, if so, which authenticated per-user or
  partner integration contract applies. This is beyond map #128's one-event
  prototype destination. Trigger: ha-spark reaches v1.0.0.

### Axle export loose ends (from #51, 2026-09-14)

Raised while verifying
[Event delivery: planner exports during an Axle slot via reservations](https://github.com/Kylevdm/ha-spark/issues/51)
against its acceptance criteria before closing it. Neither blocked that ticket:
both concern unattended operation, which is explicitly beyond map #128's
one-supervised-event destination.

- **The persisted export event's identity is write-only.** `_persist_export_state`
  saves the accepted event identity and verified end
  (`ha_spark/devices/inverters/solis.py:681`). *Updated 2026-09-14 (#140):* the
  record now has production readers, but only of its **verified end**. The
  untrusted-holds export guard keeps a resident window whose recorded end is
  still ahead (`solis.py:665-667`, #143 §3), and the relinquish safe state keeps
  the discharge half on the same rule, refusing a blind Slot 1 write while such
  an event is live (`solis.py:571-573`, #143 §5). The identity (`event_id`) is
  still read by nothing. Restart-safe cleanup still works without it: the first
  authorized `apply` with no fresh event zeros slot 1's discharge half and
  read-back verifies, not gated on grid-charge permission (`solis.py:273`).
  Remaining decision: whether the identity earns its place — comparing a
  resident window against the *last accepted event* rather than "a verified end
  is still ahead" is what would let ha-spark tell its own leftover schedule from
  a human's manual one — or reduce the record to the end time. Trigger:
  unattended event delivery, where a wrong resident window is not caught by a
  supervising human.
- **No scheduler test exercises a day-of Axle event.** #51's own test list asks
  for "next-replan pickup of a day-of event"; the behaviour falls out of
  `should_run`'s half-hourly slot logic (`ha_spark/energy/scheduler.py:87`) plus
  the export value in `setpoint_changed`, each tested separately, but nothing
  tests the composition — the exact path a paid event arrives on. Trigger: the
  supervised proof (#134) is the live test; write the unit test if that proof
  surfaces a pickup problem, or before unattended delivery.
  **Raised in priority by #144 (2026-09-14):** the day-early arming guard makes
  the day-of tick the *only* tick on which an export window is programmed, so
  this untested composition is now the whole delivery path rather than one
  route into it.

### Register write endurance (from #140 step 5, 2026-09-14)

- **Back off re-writes the inverter keeps rejecting.** Every Solis write is
  read-first, so broken *reads* cost no register writes (and since #143 §5 the
  relinquish path's one blind write is the only exception). But when reads work
  and show a persistent mismatch — the inverter or overlay accepts the service
  call yet never takes the value — each pass writes again: the per-minute power
  switch reconcile (`ha_spark/devices/inverters/solis.py:472-512`) reaches
  1,440 writes a day, and `apply`'s window and current writes
  (`solis.py:233`, `:401`, `:428`) 48. An owner away for two weeks would see
  ~20,000 writes to a register with finite EEPROM endurance
  (research #109). Want: a bounded backoff (e.g. doubling 1→60 min) on
  repeated confirmed mismatches, plus a surfaced warning so the fault is noticed.
  It needs remembered failure state, which the reconcile deliberately does not
  hold today (it remembers nothing, so restarts converge), so decide where that
  state lives. Out of #140 step 5's scope, which only changed the relinquish
  path. Trigger: a read-back mismatch observed persisting across passes in the
  add-on log, or before unattended operation.

### Release and installation observability (2026-09-12)

- **Show the running ha-spark build version in `ha-spark health`.** The doctor
  currently reports the Home Assistant version but not the application build
  (`ha_spark/health.py:60-68`); the package metadata and add-on version also
  disagree today (`ha_spark/__init__.py:3`, `pyproject.toml:7`,
  `ha_spark_addon/config.yaml:3`). Expose an unambiguous installed version or
  build reference in the health output, and align its source of truth with the
  add-on release mechanism so operators can verify which code is running during
  upgrades and supervised control trials. Trigger: before the next add-on
  migration or real-control trial.
