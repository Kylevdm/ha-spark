# Human next steps

The owner's ordered list of the hands-on steps, in one place. It points at
the tickets rather than restating them; the ticket is the source of truth and
the place to report. Updated 2026-11-03 (rulings posted). Agents: keep this current when a
human ticket is created, folded or closed.

**Where things stand.** Phases 1–7 shipped (0.23.0–0.30.0). Phase 8
(`feat/563-native-solis`, [PR #591](https://github.com/Kylevdm/ha-spark/pull/591))
has 22 of 27 tickets merged and ships as 0.31.0. The household runs 0.29.1
in Observe on the legacy overlay driver. Every human check before 0.31.0 is
either done (#529) or folded into #590 (#511, #241).

## 1. Now, while phase 8 finishes

No rulings outstanding. Settled 2026-11-03: #587 trips per connection
(every live export on it latches); #532 ships as a 0.30.1 patch from
`master` now; #562 closes at the phase 9 cutover. The Supervisor schema
warning is fixed.

- [ ] Install 0.30.1 when it ships and confirm `health` shows live Octopus
      Intelligent rates again (#532). No other check.

## 2. When 0.31.0 is released: #590

One console session (about an hour) plus one overnight. Steps and tick boxes
are on [#590](https://github.com/Kylevdm/ha-spark/issues/590).

1. Upgrade the add-on and the integration (HACS); confirm the integration
   loads with its new `pymodbus` requirement. Go straight from 0.29.1; 0.30.0
   needs no pass of its own.
2. Ingress smoke: the app loads on desktop and phone with no console error,
   the Mode dialog works, Replan in Observe writes nothing.
3. Overnight in Observe: same owner, mode and holds; legacy writes and
   read-backs unchanged; hub setting `solis_control` and `health` shows the
   HA 2027.10 warning; no 0 A guard write on an unavailable topology record.
4. From the existing read-only entities, note the identity tuple
   (33000/35000/33289) and the rated output. **Do not add the native
   device**; it would be a second Modbus client on the gateway.
5. Report on #590.

Stay in Observe. Protect and Optimise wait for phase 9, because #495 (the
SoC source goes quiet for up to ~36 min) would trip fallback.

## 3. Before phase 9's cutover: decisions for its spec

Phase 9 ([#384](https://github.com/Kylevdm/ha-spark/issues/384), 0.32.0) is
the household's move onto the native module: the riskiest step in the
programme and mostly hands-on. The grilling for its spec needs these from
you:

- [ ] **Energy dashboard continuity.** The native entities replace the old
      integration's. Decide how long-term statistics carry over (statistic
      ids, re-selection, the gap). A hole in long-term statistics can't be
      repaired afterwards (#312, #311 Q3).
- [ ] **Validation window before the handshake.** How long the native device
      runs read-only in Observe (identity, caching probe, pacing, Energy-tier
      parity against the old integration) before authority switches. A day is
      the proposal.
- [ ] **Rollback rehearsal.** Whether to run the migration doc's reverse
      order once before relying on it.
- [ ] **Phase order after 9.** Phase 17's planner half (daytime cheap and
      free charging) is unblocked and worth the most to this household;
      phases 10–14 make ha-spark installable by others. Say which goes first.

## 4. Phase 9 stage 2: the cutover (after 0.32.0)

The runbook is `docs/runbooks/solis-native-migration.md` (on the phase 8
branch until 0.31.0 merges) plus #311 stage 2 as rewritten on #384. In
order:

1. Observe. The verified handover releases the legacy holds.
2. Stop the other Modbus integration for the gateway.
3. Remove the overlay YAML and restart HA.
4. Clear `solis_control_hub`.
5. Add the native device: connect, identify, probe, claim. Validate for the
   agreed window (step 3 above). This is where pymodbus-on-HA, read-back
   timing and the caching probe get their hardware confirmation.
6. Run the ownership handshake.
7. Validate in Observe. #495 should be gone (SoC comes from the bus).
8. Protect, then Optimise, through readiness.
9. Live Axle proof on the first real event (RC line; never blocks code).

Rollback runs in reverse.

## 5. Standing: the RC line

`docs/releasing.md` → *v1.0.0 release gate* lists every item that must be
confirmed on real hardware or live suppliers before `1.0.0`. Nothing there is
claimed as validated until you tick it. Items that need an event to happen
(the Axle proof, a same-day daytime export) are recorded when one occurs.
