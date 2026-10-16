# Research: Intelligent Octopus Go dispatch facts (off-peak cap, notice, cancellations)

Issue: [#356](https://github.com/Kylevdm/ha-spark/issues/356), part of map
[#247](https://github.com/Kylevdm/ha-spark/issues/247). Needed by the dispatch
top-up and clean-dispatch charge in
[#329](https://github.com/Kylevdm/ha-spark/issues/329).
Date: 2026-10-16. All sources fetched read-only. No credentials were printed or
committed, and nothing was written to Home Assistant or Octopus.

## Question

What does Intelligent Octopus Go (IOG) guarantee about daytime dispatches, so
that charging the house battery during one never costs money? Specifically:

1. **Off-peak cap.** Is the off-peak rate for dispatches outside 23:30–05:30
   capped? How is the cap counted, what is billed past it, and can ha-spark see
   how much is used?
2. **Notice.** How far ahead do daytime dispatches appear in planned
   dispatches, and how often are they moved or cancelled?
3. **Billing scope.** Does the off-peak rate cover all house import during a
   dispatch, including battery charging? Are bump or boost charges excluded?

## Verdict

| Question | Answer | Confidence |
| --- | --- | --- |
| Cap exists | **Yes.** 6 h of car smart charging per day, measured **midday to midday**. | Documented by Octopus [O1][O2] |
| What is counted | The **actual time the car smart-charges**, not half-hour slots, and not time plugged in but idle. Solar-only charging with no grid import does not count. | Documented [O2] |
| Past the cap | The car's rate goes back to the standard (day) rate. The house gets the off-peak bonus in a dispatch half-hour **only "when it's within your car allowance"**. So house import in a dispatch half-hour past the cap is billed at the day rate. | Documented [O1][O2] |
| Clock / per-device | Neither is stated. "Midday" is presumably UK local time; the community tools all use local noon. Per-device versus per-account is not stated. | **Undocumented** |
| Cap visible to HA | **No first-party usage counter.** The public GraphQL exposes only the Charge Cap *preference* (`isChargingDurationCapped`). The BottlecapDave "Charge Cap Hours" sensor is the integration's own estimate, and it over-counted in this household's history. | Schema checked; sensor source read [B3][B5]; one local observation |
| Notice | **Undocumented by Octopus.** The schedule is built at plug-in and revised again on re-plug [O2]. The integration warns that planned dispatches "can be removed at the last minute" [B2]. Local history showed daytime slots 8–16 h ahead, and also churn within minutes of plug-in. | Anecdote plus one local session |
| House import during a dispatch | **Yes, all metered import** in that half-hour, house and car, is off-peak while within the allowance. Billing is in 30-minute blocks: "If your car charges for just 10 minutes, that entire half-hour is billed at the off-peak rate for both your car and your home" [O2]. Home battery charging is not singled out. It is ordinary house import, and "the cap only applies to energy used by your EV" [O2]. | Documented [O2] |
| Bump / boost | **Excluded.** Bump charges are billed at the day rate [O3], and a Boost charge "flips the car rate back" to the standard rate [O2]. | Documented |
| Dispatch must actually charge | **Yes.** If the car was unplugged before a scheduled half-hour started, "the home (and car) won't get that lower rate in that half hour" [O2]. A planned dispatch is not a billing guarantee. | Documented [O2] |

The conflict with the legal terms, explained in Q3 below, is the main caveat.

## Q1: the off-peak cap

### Documented (Octopus)

- **Smart Tariffs T&Cs §2.3.1.8** (v2.6, 16 Feb 2026) [O1]: "Intelligent
  Octopus Go is subject to a fair use policy with a maximum of six hours of
  managed charging per 24 hours. Should your charging schedule request more than
  six hours per 24 hours, we reserve the right to charge any incremental usage
  above six hours at the day rate."
- **Octopus blog "Smart charging and Charge Cap explained"** (7 May 2026) [O2]
  describes the newer "4-rate system":
  - "Your home off-peak: Your house always gets the cheap off-peak rate between
    23:30 and 05:30."
  - "Your car allowance: Your car gets up to 6 hours of off-peak charging per
    day (measured midday to midday). We might schedule this during your home's
    off-peak window, or at other times of the day…"
  - "Your freebie (the bonus home rate): If we schedule your car to charge
    outside of the 23:30–05:30 window, your home also gets the off-peak rate for
    that half-hour when it's within your car allowance."
  - "If your car charges for more than 6 hours (or you decide to Boost charge),
    the car rate flips back to this [standard] rate (even if it happens at 2:00
    AM while your house is still getting the off-peak rate)."
  - FAQ: "the actual amount of time the car is charged for is what's counted
    towards the 6 hour cap"; being plugged in without charging does not count.
    Solar-only charging with no import "won't count toward your 6-hour off-peak
    allowance".
  - FAQ: "the first 6 hours of smart charging will be at off-peak, and the last
    one billed at peak rate regardless of what time of the day it is scheduled."
  - **Charge Cap**: an app toggle. With it on, "your car will automatically stop
    charging before it hits the expensive peak rate."
  - Rollout: "keep an eye on the Devices tab in your Octopus app to see if your
    account has been enabled". So the rules apply per account once that account
    is migrated.
- **Product page** [O3]: "up to 6 hours of super-cheap smart charging every 24
  hours", and when those hours fall outside the window "the electricity for your
  whole home will also be at the discounted rates."

### Not documented

- Which clock "midday" uses (UK local is the natural reading), and whether the
  allowance is per device or per account.
- How the fixed 23:30–05:30 window interacts with the cap for the house. The
  car's own overnight charging does draw on the allowance (blog Scenario 3).
- Any API or entity that reports allowance used or remaining. Usage appears
  only in the online consumption dashboard, "a day or so" behind [O2].

### What Home Assistant can see

- **Octopus public GraphQL** (schema introspected 2026-10-16): the only
  cap-related field is `SmartFlexDevicePreferences.isChargingDurationCapped`
  ("The status of the device's charging duration cap"), plus the mutation
  `updateIsChargingDurationCapped`. **There is no usage or remaining-allowance
  field.** `completedDispatches` is documented as "All completed device
  dispatches **12 hours behind**", so it cannot drive a same-day count.
- **BottlecapDave integration** [B3][B5]:
  - `sensor…intelligent_charge_cap_hours` gives "the number of hours used for
    dispatches during the current daily cap period (e.g. 12 noon to 12 noon)".
    It is built from the integration's own *started* dispatches, rounded out to
    half-hour boundaries, plus completed dispatches. It uses HA local time
    (`get_dispatch_hours_for_intelligent_day`, `now()`).
  - Its cost and rate logic (`is_within_intelligent_cap`) treats
    `hours <= 6` as within the cap.
  - Service `octopus_energy.set_charging_duration_capped` toggles Charge Cap.
  - It is an **estimate**, not Octopus's count. It counts dispatch slots, not
    actual car charging time, and rounds every dispatch out to whole half-hours.
- **Prior art.** Predbat [P1] caps cheap dispatch slots at 12 × 30 min per
  midday-to-midday period. It enables the cap automatically only for tariff
  codes containing `IOG-SMB` and treats older `INTELLI-VAR` codes as uncapped
  (`has_six_hour_cap`). That is a community inference, not an Octopus
  statement.

### Local observation (this household, read-only)

- The import tariff code is an `IOG-SMB` product, and GraphQL reports
  `isChargingDurationCapped: ENABLED` on the charge point. This account is on
  the capped regime with **Charge Cap on**, so Octopus should stop the car
  before the allowance runs out.
- HA history of the integration's Charge Cap Hours sensor (one session,
  2026-10-06/07) showed two things:
  - It **reset at 12:00 BST**, which confirms local noon.
  - It reached **4.5 h** by 09:03 BST. Over the same span the scheduled
    dispatches totalled about **1 h 03 min** (one 3-minute slot and two
    30-minute slots).
  - So it over-counts heavily. That errs on the safe side, but it would starve
    a top-up. Treat it as an upper-bound hint at most.

## Q2: notice and cancellations

### Documented

- Octopus: the schedule "is always displayed in your app after your plug in"
  [O4]. On re-plug "the system will revise the best schedule again" [O2].
  Octopus gives **no notice period and no commitment not to move or cancel a
  slot**. T&Cs §2.3.1.9(b): "this service cannot be guaranteed" [O1].
- BottlecapDave FAQ [B2]: "Planned dispatches can be removed at the last
  minute." A removed dispatch may still be treated as active if the data was
  not refreshed before it started. That is why the integration derives
  *started* dispatches in 30-minute increments, "because Octopus Energy might
  stop a dispatch early." Rate adjustment only uses planned dispatches "still
  available when they start". The intelligent poll runs every 3 minutes by
  default, and the account's shared API budget is 100 calls per hour.
- The planned dispatch `source` / `type` field distinguishes `SMART`, `BOOST`
  and `TEST` [B1]. Completed dispatches do not reliably carry the source [B2].

### Community anecdote

- "planned slots are removed from the API, often the same day" (energy-meter
  tracker wiki) [C1].
- Predbat issue #5492 [C2]: a failed planned-dispatch fetch (`KT-CT-4340`) was
  read as "no dispatches". An API error must not be mistaken for a
  cancellation.
- Predbat PR #5341 [C3]: Octopus keeps returning stale planned dispatches while
  Smart Control is off.
- The Homey Octopus app models a moved start time as a cancellation plus a new
  dispatch [C4].

### Local observation (one plug-in session, 2026-10-06/07, BST)

| Dispatch (BST) | First seen | Outcome |
| --- | --- | --- |
| 09:00–09:30, 09:30–10:00 next day | 22:51–22:53 | **Removed within 6–8 min** (replan right after plug-in) |
| 23:00–23:30, 02:30–03:00 | 22:51 | Removed within 2–8 min |
| 22:57–23:00 | 22:59 | **Appeared after it had started** (3-minute slot) |
| 07:00–07:30, 08:00–08:30 | 22:59 | Kept, ran (about 8–9 h notice) |
| 14:30–15:00 | 22:59 | Kept, ran (about 15.5 h notice) |

On 2026-10-16 a next-day 11:30–12:00 BST slot was visible about 14 h ahead.
HA recorder history for these entities stops at 2026-10-07, so this is a
single session. It is an anecdote, not a rate.

Pattern: the plan churns in the first minutes after plug-in, then settles. Slots
that survive the settle ran. Very short slots can appear only once already
running.

## Q3: billing scope

- **All import during a dispatch half-hour is off-peak, within the car
  allowance.** Sources: the blog's "freebie" rule, the 30-minute block rule, and
  "we bill based on what your smart meter imports from the grid" [O2]. That
  includes a home battery charging from the grid. "Can the charge cap
  distinguish between electricity used to charge my EV and used to charge my
  home battery? Yes, the cap only applies to energy used by your EV" [O2]. So
  battery charging does not consume the allowance.
- **Conflict with the legal terms.** T&Cs §2.3.1.4 [O1] says: "If we schedule
  your electric vehicle to charge outside of the off-peak hours, we will apply
  the night rate for your EV charging and any underlying household usage will be
  charged in the relevant half hour billing periods." That reads as house usage
  billed at whatever the half-hour's normal rate is. The newer blog [O2] and the
  product page [O3] both say the home gets the off-peak rate in that half-hour.
  The T&Cs (Feb 2026) predate the blog (May 2026). The marketing pages are the
  more specific and more recent statement, but the binding text is the less
  generous one. **Treat house off-peak during daytime dispatches as
  likely-but-not-contractual.**
- **The car must actually charge.** "If we had scheduled the car charging but
  you then unplugged before that half hour started then the home (and car)
  won't get that lower rate in that half hour" [O2]. Octopus marks "smart
  charging" periods when it is "in control of your device and we schedule some
  smart charging" and also infers the car's share from when it "started and
  stopped charging" [O2].
- **Bump / boost excluded.** "Bump charges will be charged at your Intelligent
  Octopus Go day rate" [O3]. "any charging we haven't scheduled is charged at
  your normal home energy rate" [O4]. Boost flips the car rate to standard
  [O2]. The integration likewise ignores `BOOST` planned dispatches for rate
  adjustment, and Predbat ignores `bump-charge` / `BOOST` [B5][P1].

## Recommendation for the dispatch top-up / clean-dispatch charge spec

1. **Gate on the capped regime and Charge Cap.** Only treat daytime dispatch
   half-hours as off-peak for the house when both of these hold:
   - the account is on the 4-rate regime (tariff code heuristic `IOG-SMB`,
     overridable in config);
   - Charge Cap is `ENABLED` (read-only GraphQL `isChargingDurationCapped`, or
     a setup confirmation).

   With Charge Cap on, Octopus stops the car before the allowance runs out, so
   a dispatch half-hour that actually happens is within the allowance by
   construction. Without Charge Cap, the last dispatches of the day can be
   past-cap and billed at day rate for the house too.
2. **Still keep a conservative ha-spark-side count as a backstop.** Count from
   ha-spark's own observed started dispatches: the sum of dispatch durations
   per local-noon-to-noon day, with SMART source only. Do not top up in a slot
   that would take the day past **5.5 h**, leaving a half-hour margin for clock
   skew and rounding. Do not rely on the integration's Charge Cap Hours sensor
   as truth: it over-counted about 4× here. At most, use it as a "stop" signal
   if it reports ≥ 6.
3. **Only act on a dispatch half-hour once it has actually started.** Two
   conditions apply:
   - the dispatch is still present in a fresh refresh at or after the
     half-hour's start;
   - there is fresh evidence the car is drawing power, because a scheduled
     slot the car does not use is not discounted.

   Planned-but-not-started slots may inform the plan, but must not commit the
   battery to grid charging.
4. **Minimum notice for planning, not for actuation.** Ignore planned
   dispatches first seen less than **10 minutes** ago (the post-plug-in churn).
   Treat a dispatch that disappears as cancelled only after a *successful*
   fetch; a `KT-CT-4340` or any other API error is not a cancellation. No
   notice period makes a slot safe to pre-commit, so the top-up acts per
   half-hour as in (3) and stops at the half-hour boundary when the dispatch is
   gone.
5. **Exclude `BOOST`/`bump-charge` and `TEST` sources** from any off-peak
   assumption.
6. **Cost guard.** A dispatch top-up is only worth it if the stored kWh
   displaces later peak import, net of round-trip loss. Because of the T&Cs
   §2.3.1.4 ambiguity, keep the energy a top-up may buy bounded (e.g. the
   expected evening deficit). Then a mis-billed half-hour costs at most the
   day-rate minus off-peak spread on that bounded amount, never an open-ended
   charge.
7. **Verify against the bill.** Once live, reconcile one or two top-up days
   against the Octopus consumption dashboard / half-hourly import rates, to
   confirm the house half-hours were billed off-peak.

## Sources

Octopus (first-party):

- [O1] Smart Tariffs Terms and Conditions §2.3.1, v2.6, 16 Feb 2026 —
  https://octopus.energy/policies/smart-tariffs-terms-and-condition/
- [O2] "Intelligent Octopus Go: Smart charging and Charge Cap explained",
  7 May 2026 — https://octopus.energy/blog/intelligent-octopus-go-charge-limit/
- [O3] Intelligent Octopus Go product page —
  https://octopus.energy/smart/intelligent-octopus-go/
- [O4] "Intelligent Octopus Go: Customer questions answered" —
  https://octopus.energy/blog/intelligent-go-faqs/
- Octopus public GraphQL schema (`https://api.octopus.energy/v1/graphql/`),
  introspected 2026-10-16: `SmartFlexDevicePreferences.isChargingDurationCapped`,
  `Query.flexPlannedDispatches`, `Query.completedDispatches`,
  `UpsideDispatchMetaType`.

BottlecapDave HomeAssistant-OctopusEnergy (source read at `7089181`, v19.2.1):

- [B1] Intelligent entities —
  https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/
- [B2] FAQ — https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/faq/
- [B3] `custom_components/octopus_energy/intelligent/charge_cap_hours.py`
- [B4] Electricity entities (off-peak sensor, rate adjustment) —
  https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/electricity/
- [B5] `custom_components/octopus_energy/intelligent/__init__.py`
  (`get_applicable_dispatch_periods`, `adjust_intelligent_rates`,
  `get_dispatch_hours_for_intelligent_day`, `is_within_intelligent_cap`)

Prior art and community (anecdote):

- [P1] Predbat `apps/predbat/octopus.py` (`has_six_hour_cap`,
  `get_octopus_slot_max`, `dispatch_billed_off_peak`) and
  `docs/car-charging.md` (`octopus_slot_max`), commit `213cb2e` —
  https://github.com/springfall2008/batpred
- [C1] energy-meter-tracker wiki —
  https://github.com/RGx01/energy-meter-tracker-addon/wiki/Intelligent-octopus-go
- [C2] https://github.com/springfall2008/batpred/issues/5492
- [C3] https://github.com/springfall2008/batpred/pull/5341
- [C4] Homey Octopus Energy app thread —
  https://community.homey.app/t/app-dev-pro-octopus-energy-integration/136685
- Netzero (commercial integrator) docs, for corroboration of midday-to-midday
  and migration "between July and late August 2026" —
  https://docs.netzero.energy/docs/tariffs/Intelligent-Octopus-Go.html
