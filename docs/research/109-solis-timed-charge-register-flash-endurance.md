# Research: are Solis registers 43141 / 43143–43146 flash-backed?

Issue: [#109](https://github.com/Kylevdm/ha-spark/issues/109), part of map [#78](https://github.com/Kylevdm/ha-spark/issues/78).
Date: 2026-09-09.

## Question

ha-spark's Solis driver (`ha_spark/devices/inverters/solis.py`, shipped in
[#84](https://github.com/Kylevdm/ha-spark/issues/84)) writes charge current
(register **43141**) and the charge/discharge window block (**43143–43146**,
an 8-register `WRITE_MULTI_MODBUS` commit at 43143) on every replan cycle
where the computed value differs from the current one. [#46](https://github.com/Kylevdm/ha-spark/issues/46)
sets replan cadence up to half-hourly — a theoretical ceiling of ~48 writes/day
per register if every replan changed the value. A community report (unconfirmed
at the time #109 was opened) claims this register block is flash-backed with a
10k–100k write-cycle ceiling, which at 48/day would exhaust a 10k-cycle part in
under seven months.

This document establishes what is actually confirmed, from what source, and
what remains open.

## Verdict

**Plausible but unconfirmed** that 43141/43143–43146 are flash-backed on the
S5 / RAI-3K-48ES-5G firmware family specifically. No tier-1 (Solis/Ginlong)
or tier-2 (integration source code) source directly names these exact
registers as flash or RAM. The evidence is:

- A genuine, verbatim manufacturer warning exists for a **related but
  different** register range (3051–3054, 3073, 3130–3146) on an **older**
  Solis protocol generation, explicitly documenting flash-backing with a
  stated "less than 10000 times" write endurance and a caution against
  writing frequently. This is real tier-1 evidence of Solis's engineering
  practice, but it does not name 43141/43143–43146, and I cannot confirm the
  4-digit and 5-digit register ranges map to the same underlying flash cells.
- No official register manual, datasheet, or firmware note for the S5 /
  RAI-3K-48ES-5G family that documents storage medium for the 43xxx range
  was reachable.
- Both community-maintained HA integrations that implement this register
  block (`solax_modbus`'s `plugin_solis.py` and `Pho3niX90/solis_modbus`)
  contain **zero** mentions of flash, EEPROM, NVRAM, wear, endurance,
  throttling, or debouncing anywhere in their source — confirmed by direct
  file inspection and a repository-wide GitHub code search for those terms
  (0 hits in both repos). This is evidence of *absence of documented
  awareness* by the maintainers, not evidence that the registers are safe.
- One community forum thread specifically about this question was located,
  but could not be fetched in full (403 to automated fetch); only a
  search-engine snippet was recoverable. Treated as low-confidence hearsay,
  not a citation-quality source (see Tier 3 below).

**Confidence label for this document's claims:**
- 43141/43143–43146 storage medium (flash vs RAM) on S5/RAI-3K-48ES-5G: **unknown / unconfirmed**.
- Solis's general practice of flash-backing *some* configuration registers with a ~10k-cycle-class endurance, on at least one other (older) protocol generation: **confirmed** (tier-1, verbatim, see below).
- Community claim that the 43143 window block specifically is flash and wears in the 10k–100k range: **plausible-but-unconfirmed**, sourced only from forum hearsay.

## Sources

### Tier 1 — Solis / Ginlong official documentation

1. **"Solis Grid-tied Inverters 2021 RS485 MODBUS Communication Protocol"** (translated 2021-05-06), hosted by Loxone's document library:
   `https://api.library.loxone.com/downloader/file/1197/RS485_MODBUS%20Communication%20Protocol_Solis%20Inverters.pdf`
   - Covers Solis 4G/5G **string inverters** and their EPM meter, register ranges in the 3000s–8000s and 36000s. Does **not** contain registers 43141 or 43143–43146 at all — this document is for a different inverter class (grid-tied string, not hybrid battery) and a different register-numbering generation than the S5/RAI-3K-48ES-5G hybrid used here.
   - Contains register **4X 3069 "Power-off saving function"**, a bitfield controlling whether writes to registers **3051, 3052/3149/3150, 3053, 3054, 3073, and 3130–3146** persist across a power cycle (i.e., are committed to flash) or are RAM-only ("Power off not saving" / "Power off saving" per bit). Verbatim, from the document text (page ~29 of the extracted text, around the 3069 register description):
     > "Note：Don't set 1 too frequently，the flash has a limited write and read lifespan. Less than 10000 times."
   - Also, for the related power-limit-flag register (3081-area), the doc states: "When HMI or external 485 set once, ARM will save this flag in the flash and detect it after power on and send DSP the command" — i.e. some settings are explicitly saved to flash on every write, unconditionally, no toggle.
   - **What this proves:** Solis's own engineering documentation, for at least one product generation, explicitly treats certain "settings"-class holding registers as flash-backed with a stated ~10,000-cycle-class endurance and explicitly warns against frequent writes. It establishes this is a real, manufacturer-acknowledged hardware behavior *pattern* in the Solis product line.
   - **What this does not prove:** that registers 43141/43143–43146 on the S5/RAI-3K-48ES-5G specifically are in this same category. The register ranges don't overlap and I have no cross-reference tying the two numbering schemes together.

2. **"RS485_MODBUS RTU Hybrid Inverter Protocol"** (translated 2020-09-15, explicitly subtitled "Without Control"), hosted by akkudoktor.net:
   `https://akkudoktor.net/uploads/short-url/kbwIjN8pl8idrCP04UstaxtSmlC.pdf`
   - This is a hybrid-inverter protocol document, closer in spirit to the S5, but it predates the timed-charge/RC control registers entirely (title says "Without Control") and uses a **33xxx** register range for battery charge/discharge current (e.g. 33205–33207), not 43xxx. No mention of flash/EEPROM/NVRAM/endurance near the battery-current registers; the only "FLASH" hits in the whole document are unrelated DSP fault codes ("DSP Chip FLASH Fault").
   - **What this proves:** nothing about 43141/43143–43146 — wrong document generation, no control registers present.

3. **Solis North America support article, "Modbus TCP/IP"**: `https://usservice.solisinverters.com/support/solutions/articles/73000660897-modbus-tcp-ip`
   - Covers physical/transport setup (S2-WL-ST logger, Modbus Poll config) only. No mention of register persistence, storage medium, or write endurance anywhere.

**No official Solis/Ginlong register manual or firmware release note specific to the S5 / RAI-3K-48ES-5G 43xxx holding-register range (the one ha-spark actually uses) was reachable.** This is the central gap: the strongest primary-source evidence found is for a different register range/model generation, not the one in question.

### Tier 2 — Integration source code

4. **`wills106/homeassistant-solax-modbus`, `plugin_solis.py`**:
   `https://raw.githubusercontent.com/wills106/homeassistant-solax-modbus/main/custom_components/solax_modbus/plugin_solis.py`
   - Confirms the register map ha-spark relies on: **43141** = "Timed Charge Current" (number + sensor, scale 0.1 → DC amps ×10), **43142** = "Timed Discharge Current", and a `WRITE_MULTI_MODBUS` button "Update Charge/Discharge Times" at register **43143** driving `value_function_timingmode`, covering 43143–43150 for slot 1 (charge start h/m, charge end h/m, discharge start h/m, discharge end h/m), with slot 2 at 43153 and slot 3 at 43163 (+10 offset per slot). This matches #84's implementation notes exactly.
   - Direct grep of the full raw file for `flash|eeprom|nvram|wear|endur|debounce|throttle|cooldown` near these registers and file-wide: **zero matches**.
   - GitHub code search (`gh api search/code`) across the entire repo for `flash OR eeprom OR wear`: **0 results**.
   - **What this proves:** the maintainers' code contains no comment, attribute flag, or write-throttling logic suggesting they are aware of (or have designed around) a flash-wear concern for these specific registers. Notably, the file *does* contain an autorepeat/keep-alive mechanism for the RC (43135) path (per #85's earlier finding) but nothing analogous for the timed-slot block — consistent with the timed-slot path being treated as a normal one-shot "settings" write rather than something requiring rate-limiting.

5. **`Pho3niX90/solis_modbus`**:
   `https://github.com/Pho3niX90/solis_modbus` (checked `number.py`, `time.py`, `modbus_controller.py`, and a repo-wide code search)
   - Same result: zero mentions of flash/EEPROM/NVRAM/wear/endurance/throttle/debounce anywhere in the source, and its ReadTheDocs site (`https://solis-modbus.readthedocs.io/`) has no page addressing register persistence or write-frequency limits.

**Absence-of-evidence caveat:** neither integration's silence is proof the registers are RAM-resident. Both integrations write these registers relatively rarely in normal use (a human changing a time-of-use schedule occasionally), so a wear problem might simply never have surfaced for their maintainers or users to comment on.

### Tier 3 — Community forum / GitHub issue reports

6. **DIY Solar Power Forum, "Read/Write Cycle reliability of Solis Modbus Registers"**:
   `https://diysolarforum.com/threads/read-write-cycle-reliability-of-solis-modbus-registers.75809/`
   - Could not be fetched directly (HTTP 403 to automated fetch tooling). Only a search-engine snippet summary was recoverable, so the following is **secondhand and low-confidence** — not a verified quote, and I could not confirm the posters' identities, expertise, or whether any of them cite an official Solis source:
     - Claim: Solis "may be using flash memory to store the modbus register map," and that battery charge/discharge schedule registers specifically are flash-backed; repeated daily writes "could potentially damage the inverter" / "burn the flash and brick the inverter" after a large number of writes.
     - Claim: "Remote Control Force Battery Charge/Discharge" registers (i.e., the RC/43135 path ha-spark already evaluated and rejected as a control surface in #83/#100 — unrelated to which registers this ticket writes) are believed to be RAM-resident/real-time-safe, unlike the timed-slot/schedule registers.
     - Claim: if flash-backed, expected endurance is "at least 10,000 writes per register," which the posters reason is adequate for typical usage (updating charge times/current "3-4 times a day on average") over "several years."
     - Suggested (but **not performed here, per this task's constraints**) empirical test: change a setting, power-cycle the inverter, and see if it's retained — this is a hardware probe and explicitly out of scope for this research task.
   - **Framing:** this is community lore, not a citation-quality primary source. It is directionally consistent with the tier-1 evidence found in source 1 above (Solis has, elsewhere in its product line, documented ~10k-cycle flash-backed settings registers with a "don't write too often" warning), which makes the claim plausible rather than fabricated — but it remains unconfirmed for the exact registers in question.
   - Related threads found but not substantively useful: "Modbus Register Backflow Power for Solis Hybrid Inverter" (page 2) — not fetchable (403); "Time of use MODBUS registers on Solis RHI-3K-48ES — SOLVED SUCCESS" — not fetched in full for this document (would need a follow-up pass if deeper community corroboration is wanted).

## What could NOT be confirmed, and why

- **No public Solis/Ginlong register manual or firmware note exists (or is reachable) that documents storage medium for the 43xxx holding-register range** used by the S5/RAI-3K-48ES-5G. The two official protocol PDFs located cover different inverter classes/generations and different register ranges (3000s/36000s for string+EPM meter; 33000s for an older "without control" hybrid variant). This is the single biggest gap: the strongest tier-1 evidence I found is precedent from a different part of the Solis product line, not a direct statement about these registers.
- **The DIY Solar Forum thread most directly on-topic could not be read in full** (blocked by the site to automated fetching); only a search-snippet summary was available, which limits how much weight it can carry.
- **No datasheet for the inverter's MCU/flash chip was identified.** The protocol documents don't name a specific part number for the persistent-storage IC, so there's no way to independently look up a manufacturer-rated endurance figure for the actual silicon.
- **I did not test the hardware** (power-cycle-and-check-retention), per this task's explicit constraint against probing the live inverter. That test would resolve flash-vs-RAM directly but is not appropriate to run as an unattended research task.

## Write-if-changed assessment

Per #84's implementation notes, ha-spark's Solis driver already reads back the
current register values (via the `solis_control` overlay's own read-back
sensors) before writing, and only issues a `modbus.write_register` /
`WRITE_MULTI` call when the computed charge current or window differs from
what's already on the inverter. This decouples **replan frequency** from
**write frequency**: half-hourly replanning (#46) does not imply half-hourly
writes — it implies half-hourly *checks*, most of which should be no-ops on a
typical night where the planned window and current don't change between
checks.

- **Does it bound real-world writes far below the 48/day theoretical ceiling?**
  Almost certainly yes, in the sense that a write only happens when the plan's
  output actually changes, and the plan's charge window/current is not
  designed to churn every replan tick — it's driven by slower-moving inputs
  (tariff/dispatch slots, SoC trajectory, forecast revisions). A realistic
  estimate is low single digits of actual writes per day per register in
  steady state (e.g. one write when the overnight window is first set, maybe
  one or two more if a dispatch slot or forecast shifts it), not 48. **This is
  an estimate, not a measured number** — there is currently no telemetry
  counting how many times solis.py actually issues a register write in
  production.
- **Is that mitigation sufficient on its own, given the endurance confidence
  level?** Not fully, no. Write-if-changed is real and valuable risk
  *reduction*, but "sufficient to make the question moot" requires knowing
  both sides of the inequality: the actual write rate (estimated, not
  measured) and the actual endurance ceiling (unconfirmed for these specific
  registers, plausibly ~10k cycles by analogy to a related Solis register
  family). Two unconfirmed-but-plausible numbers multiplying together do not
  produce a confirmed safety margin — they produce a *lower risk than the
  worst case*, which is not the same as "moot." If the true write rate turned
  out to be higher than assumed (e.g. a noisy forecast input causing the
  window to jitter by a few minutes every replan), even write-if-changed
  could accumulate meaningful wear over a multi-year deployment against a
  10k-cycle part.

## Recommendation for #46 and follow-ups

1. **Do not redesign #46's replan cadence around this risk right now.**
   Write-if-changed already means cadence and write-count are not the same
   number, and the underlying hardware fact is unconfirmed in either
   direction — there's nothing concrete yet to design against.
2. **Add lightweight write-count telemetry**, not hardware probing: a counter
   or structured log line each time `solis.py` actually issues a register
   write to 43141 or the 43143 block (distinct from a no-op read-then-skip).
   This converts "we assume write-if-changed keeps this low" from an
   assumption into a measured fact within a few weeks of real operation, at
   zero additional hardware risk.
3. **Ask Solis/Ginlong support directly** for the storage medium and rated
   endurance of the 43141 and 43143–43146 registers on the S5/RAI-3K-48ES-5G
   firmware family. This is the one path that could turn "plausible but
   unconfirmed" into "confirmed" without touching hardware.
4. **Do not adopt the community "≤1 write/day" folklore as a hard constraint**
   in code — it's unconfirmed and would be a speculative abstraction against
   an unproven risk. It's reasonable to leave a code comment noting the open
   question and pointing at this document, so a future contributor doesn't
   have to re-derive the context.
5. **Revisit if new evidence surfaces** — an official register manual for this
   firmware family, a Solis support reply, or corroborating detail from the
   DIY Solar Forum thread read in full — rather than closing this out as
   settled either way.
