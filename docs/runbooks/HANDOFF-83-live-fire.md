# Handoff: run the Solis forced-charge live-fire (#83)

For a **new session** picking this up cold. Written 2026-09-07 ~16:25 BST.

## Orientation in one paragraph

[Wayfinder map #78](https://github.com/Kylevdm/ha-spark/issues/78) is finding a working
forced-charge path for Kyle's Solis S5 AC-coupled inverter. Its frontier ticket is
[#83 — live-fire](https://github.com/Kylevdm/ha-spark/issues/83): prove **by hand, in the HA UI,
with a person present** that a forced grid charge can be commanded, before any driver code depends
on the sequence. Everything downstream ([#82](https://github.com/Kylevdm/ha-spark/issues/82) control-surface
decision, [#84](https://github.com/Kylevdm/ha-spark/issues/84) driver) is blocked on it. #83 is
already claimed/assigned. Work is on branch **`docs/83-live-fire`** (local only — not pushed).

## Opening prompt to paste into the new session

> We're running the Solis forced-charge live-fire for issue #83 on branch `docs/83-live-fire`.
> Read `docs/runbooks/HANDOFF-83-live-fire.md` and then `docs/runbooks/solis-forced-charge-live-fire.md`.
> I'm at the inverter and will make every write myself in the HA UI — you record and interpret,
> and you make no writes to Home Assistant. Start the recorder and take the step 0 baseline.

## The one thing to understand before starting

Kyle reports that forced charge **via the solax integration never worked unless SoC was below
~20%** — which is exactly `number.solisac_battery_minimum_soc`. So the likely story is an **SoC
gate in firmware**: the inverter may honour grid charge only below its reserve floor, as an
emergency recharge.

That observation was made through solax, where register 43135 is **write-only** — so "it didn't
work" could never be decomposed. The #90 modbus overlay now gives read-back, which splits it:

| 43135 read-back | battery current | conclusion |
|---|---|---|
| reads `1` | current flows | force works at this SoC — gate disproved |
| **reads `1`** | **no current** | **write accepted, actuation refused → gate is real, in firmware** |
| reads `0` | no current | the write itself was rejected — a different problem |

**The middle row is the whole point of this run.** At the SoC this test will run at (~65–70%), a
"nothing happened" outcome is a *likely and valuable* result, not a failure — provided the
read-back state and the current are recorded **independently at every step**. Conflating them is
what left this question open for two months.

## State snapshot (2026-09-07 15:21 UTC / 16:21 BST)

| | |
|---|---|
| SoC | **82%**, falling ~11%/hr under a ~3 kW house load → expect ~65% by 18:00 BST |
| battery | discharging ~3022 W / 57.9 A (positive = discharging) |
| grid | ~0 W (negative = importing) |
| 43135 / 43136 / 43282 | `off` / `0 W` / `0` |
| work mode (33132) | `35` — must stay 35, any change is an abort |
| `battery_minimum_soc` | `20` — the suspected gate. **Record it; do not write it** |
| comms | `Healthy`, `power_switch` `On` |

Sign conventions, confirmed arithmetically: **battery_power negative = charging**,
**meter_active_power negative = importing**. #83's original check 1 has this backwards.

## Timing

| window (BST) | |
|---|---|
| → **18:00** | ✅ **the slot** |
| 18:00–19:00 | ⚠️ buffer, don't start |
| 19:00–20:00 | ❌ Axle **export** event — observe only, write nothing |
| 20:00–23:00 | ✅ fallback slot |
| 23:30–05:30 | ❌ inverter's own timed charge (60 A) |

## Recorder

```bash
python3 docs/runbooks/live_fire_recorder.py --interval 2 --csv run.csv
```

Read-only, stdlib only, reads `.env` from the repo root. Verified working 2026-09-07. Prints one
row per change across all instrumentation entities and flags abort conditions.

⚠️ **It timestamps in machine-local time, which is UTC — one hour behind the BST times in the
runbook.** Note the offset when correlating, or the settling-delay figures will be an hour out.

## Then do this

Follow `docs/runbooks/solis-forced-charge-live-fire.md` — preconditions table first, then steps 0–8.
It carries the write sequence, the abort ladder, and a recording table to fill in.

## Do not

- **Make any HA write from the agent side.** This ticket is HITL; every write is Kyle's, by hand.
- **Write `battery_minimum_soc`** to probe the gate on this first sitting. If it is a "keep above
  this" floor, raising it may make the inverter grid-charge to that floor on its own, at a rate
  nobody controls, through a register the RC path cannot stop. Deferred deliberately.
- **Run inside 19:00–20:00 or after 23:30.**
- Trust a single successful force. Writes to this device stick only sometimes — step 8 repeats it.

## When the run is done

1. Post the filled recording table + findings as a resolution comment on
   [#83](https://github.com/Kylevdm/ha-spark/issues/83), then close it.
2. Append a one-line gist to the map's Decisions-so-far ([#78](https://github.com/Kylevdm/ha-spark/issues/78)).
3. That unblocks [#82](https://github.com/Kylevdm/ha-spark/issues/82) — the control-surface decision,
   now on measured facts.

A **negative result is a valid resolution** and redirects the map. Record what failed, not just
what worked.

## Optional, free, no writes

The Axle export event (19:00–20:00 BST) is a third-party **forced discharge on this exact inverter
at high SoC**, and the map has never watched one. Leaving the recorder running across it can show
which control surface actually actuates, and whether the ~20% gate is charge-specific or general.
Record separately from the live-fire run — it is evidence about the control surface, not a test of
our own write. See the runbook section "The Axle export event is worth observing in its own right".
