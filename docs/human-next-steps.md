# Human next steps

The owner's ordered list of the hands-on steps, in one place. It points at
the tickets rather than restating them; the ticket is the source of truth and
the place to report. Updated 2026-11-07 (cutover procedure settled). Agents: keep this current when a
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

- [x] **Energy dashboard continuity.** The native entities replace the old
      integration's. Decide how long-term statistics carry over (statistic
      ids, re-selection, the gap). A hole in long-term statistics can't be
      repaired afterwards (#312, #311 Q3).
- [x] **Validation window before the handshake.** How long the native device
      runs read-only in Observe (identity, caching probe, pacing, Energy-tier
      parity against the old integration) before authority switches. A day is
      the proposal.
- [x] **Rollback rehearsal.** Whether to run the migration doc's reverse
      order once before relying on it.
- [x] **Phase order after 9.** Phase 17's planner half (daytime cheap and
      free charging) is unblocked and worth the most to this household;
      phases 10–14 make ha-spark installable by others. Say which goes first.

All four were settled in the #384 grilling (phase 17 goes next). The
cutover as shipped is in section 4 and the runbook: on an all-native site the
read-only validation runs after the ownership handshake, in Observe.

## 4. Phase 9 cutover after 0.32.0: #663

The household's run, exit checklists and results live on
[#663](https://github.com/Kylevdm/ha-spark/issues/663). Follow the reusable
[Solis native migration runbook](runbooks/solis-native-migration.md), including
its stop-on-failure and no-competing-clients rules. In order:

1. Prepare a private rollback record and backup, arrange supervision, and update
   both the add-on and integration to 0.32.0 together.
2. First go-native from add-on-owned Observe after verified handover: disable
   (don't delete) the other Modbus integration, remove the overlay, clear the
   hub setting and add native. Saving an all-native topology starts the
   ownership handshake immediately; wait for known integration authority in
   Observe, then repoint settings and check every power entity's sign and unit.
3. Validate read-only in Observe for 24 h, including an overnight cheap window,
   against the runbook's telemetry, transport and repair exit criteria.
4. Rehearse full reverse rollback in Observe, including the reverse ownership
   handshake, before any statistics migration. Record the result on #663.
5. Second go-native: repeat the handshake and settings checks, then carry history
   over using the runbook's backup-first, one-pair-rehearsed registry renames.
   Keep the actual entity mapping private; re-select native Energy sensors.
6. Observe for 24 h after the handshake: no SoC staleness (`Native SoC` passes)
   and no false guard cuts.
7. Rehearse full reverse rollback again in Observe, after migration and before
   Protect. Verify re-created old entities use old ids, never native ids.
   Return to native with another handshake and settings checks, but do not
   repeat migration: native history keeps a gap for the rehearsal.
8. Configure the notify target, native protection evidence, fallback schedule
   and SoC trust policy; enter Protect through readiness for 24 h.
9. Review readiness and explicitly choose Optimise; verify the first actions.
   Record completion on #663, closing #562 and #495. Live Axle proof stays on
   the RC line and never blocks code.

If removal and re-addition are necessary, follow the runbook's handover and
`ha-spark pair --replace` recovery instructions (#646); removing the integration
is not a way to release holds or transfer authority.

## 5. Standing: the RC line

`docs/releasing.md` → *v1.0.0 release gate* lists every item that must be
confirmed on real hardware or live suppliers before `1.0.0`. Nothing there is
claimed as validated until you tick it. Items that need an event to happen
(the Axle proof, a same-day daytime export) are recorded when one occurs.
