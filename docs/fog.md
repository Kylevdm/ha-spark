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
