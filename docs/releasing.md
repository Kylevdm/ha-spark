# Releasing

The add-on build fails unless two things agree. The add-on Dockerfile installs
`git+…@v${BUILD_VERSION}`, where `BUILD_VERSION` is the `version` in
`ha_spark_addon/config.yaml`. A release needs both:

1. `version` bumped in `config.yaml` **on `master`**. The store advertises the
   default branch's version, and a tag alone won't show users an update.
2. A matching annotated `vX.Y.Z` git **tag pushed**. Without it the build fails
   with `pathspec 'vX.Y.Z' did not match`.

The order is:

1. Commit the version bump. In the same commit, fold the changelog fragments
   (below).
2. Tag it `vX.Y.Z`.
3. Push the branch and the tag.
4. Merge to `master`.

Keep `config.yaml` `options`/`schema` in sync with `config.py` `_OPTION_KEYS`
(a test enforces this). For any option or behaviour change, bump the version,
add a changelog fragment and update `DOCS.md`.

## Changelog fragments

PRs never edit `ha_spark_addon/CHANGELOG.md`; parallel PRs would all insert at
the same line and conflict. Instead, each user-visible change adds
`ha_spark_addon/changelog.d/<issue>.md` (use a short slug when there is no
issue). It holds the final Markdown bullet(s), written as they will appear in
the changelog.

To fold them at release, in the version-bump commit:

1. Add `## X.Y.Z` directly under `# Changelog`.
2. Under it, paste the contents of every `changelog.d/*.md` in ascending issue
   order, adding any release-wide notes (such as upgrade order) at the end.
3. Delete the folded fragment files, keeping `changelog.d/.gitkeep`.

## Add-on base image

Supervisor 2026.04.0+ ignores `build.yaml` and `BUILD_FROM`, so set the base
with `FROM` in the Dockerfile. The `[habits]` ML extra (scikit-learn, numpy)
has no musllinux wheel, so the base is glibc (`python:3.13-slim-bookworm`).
There is no s6 or bashio, so `run.sh` is plain shell and `config.yaml` sets
`init: true`.

## v1.0.0 release gate

The v1 programme is now [#247](https://github.com/Kylevdm/ha-spark/issues/247):
an ingress app, companion HACS integration and UI-owned configuration for new
households. Compatible #94 requirements remain below. See the individual
[carry-over dispositions](258-v1-gate-carryover.md).

The pre-1.0 line continues `0.x`; the misused `v1.0.0-rc1`–`rc4` tags were
deleted on 2026-10-18, so those names are free for the real release
candidates. No unchecked item is claimed as validated.

[#366](https://github.com/Kylevdm/ha-spark/issues/366#issuecomment-6253526844)
sequences the build into 16 phases (#376–#391, one milestone each). Each ships
as a `0.y.x` minor, and each phase issue lists the gate items it must pass in
tests. Then:

- **`1.0.0-rc1`** (tag `v1.0.0-rc1`): every phase shipped, ruff, mypy and
  pytest green, and the reference household cut over and running Observe, then
  Protect. Feature-complete; no new features after rc1.
- **`1.0.0-rcN`**: fixes found while confirming the items below on hardware.
- **`1.0.0`**: every item below confirmed on real hardware or live suppliers.

Before tagging `v1.0.0`, ruff, mypy strict, and pytest must be green, **and**
every item below must be confirmed on real hardware or live suppliers.

### Safety (automatic control; must never regress)

These gates cover add-on automatic control and its UI/CLI actions. In phase 2
only the daemon actuates, through the site operating mode and write-time gate.
Integration-owned control and temporary manual handover arrive in later phases;
no bypass is granted here.

- [ ] Observe (the install and upgrade default) computes a plan, logs `[SIMULATE]`
      action lines, and makes **zero** hardware-actuating calls, including on shutdown.
- [ ] Protect writes only the hold and its release, read-back verified, plus
      clock sync when authorised. It never writes the owner's charging schedule.
- [ ] A real plan write happens **only** in Optimise, is verified by read-back,
      and a failed action is isolated.
- [ ] AlphaESS and multi-inverter sites cannot leave Observe in phase 2.
- [ ] Authority unknown or a failed authority save permits no hardware writes,
      releases included. Settings and agent requests never change the operating mode.
- [ ] Invalid SoC blocks SoC-derived writes. The fixed, configured Solis fallback
      charging schedule remains guarded, read-back verified and Optimise-only
      under #112/#119; AlphaESS never receives that fallback.
- [ ] No secret (`SUPERVISOR_TOKEN`, `HA_TOKEN`, supplier API keys, Axle API
      key) appears in logs, plan output, or the agent surface.

### Foundation

- [ ] UI-owned configuration and bootstrap-only Supervisor options follow
      #253. Validate the reference-household cutover and supported config;
      general legacy flat-config migration is not required.
- [ ] A misconfigured `tariff_provider` is rejected **at startup**, naming the
      bad field.
- [ ] The `fixed`, `dynamic`, and `octopus_intelligent` providers read live
      prices; a runtime price-read failure falls back to `fixed` and still
      produces a plan.
- [ ] Native Solis verifies firmware, gateway caching and register semantics
      (#249/#259).
      The integration is the sole Modbus client; #248 owns its service contract.
- [ ] Agent surface: `get_state` reports the site's operating mode;
      `/agent/*` writes still pass the gate; the LLM cannot reach
      `call_service`; an external non-ingress client is rejected without the
      bearer token. UI mutations use ingress and the same functions/gates as
      CLI actions. The separate discovery/API channel (#252) is authenticated
      and version-checked; the unauthenticated port is locked down.

### MVP

- [ ] Derived base load repairs the battery-polluted history, and forecast
      error materially improves on the clean series.
- [ ] Half-hourly replanning absorbs a same-day event or plug-in with no new
      trigger logic; unchanged setpoints produce zero writes.
- [ ] Reservations trace every plan line to a one-sentence reason.
- [ ] **Axle event delivery proven end to end on the real Solis:** a real (or
      replayed) event leads to held energy, then to a commanded export within
      the inverter's export and discharge limits, and the house is not left
      short before the next cheap slot. Malformed or absent Axle payloads fall
      back to the base schedule.
- [ ] V2L refill is planned only when it is cheaper than grid and a
      reservation needs it; an unplugged car means no V2L; the plug-in
      notification fires. Actuation is 1.0.x (below).
- [ ] The readiness signal evaluates Observe history against the measured bar
      and never changes the operating mode or hardware behaviour.

- [ ] The phone digest delivers the compatible MVP summary; #247 owns its
      sequencing alongside the ingress app.

### New programme validation

- [ ] Onboarding preserves driver/provider suggestions, entity mapping,
      capability coverage, presets and multi-device onboarding (#247/#257).
- [ ] Planner and drivers share #241's per-inverter, per-bank and site limits,
      shared-bank allocation and missing-reading behavior. The full spec
      defines concrete headroom allowances and near-limit criteria.
- [ ] The ingress app, integration and UI-owned config satisfy the completed
      #247 specification, including entity/history cutover and UI/CLI parity.
      Unresolved full Solis parity and version sequencing are settled there.
- [ ] #165 validates timely supply-guard reductions without racing apply writes.
      #128 retains its supervised-event scope; its success alone does not
      establish unattended readiness.

### 1.0.x (not required for 1.0.0)

Deferred by #366 and the [v1.0.x map](https://github.com/Kylevdm/ha-spark/issues/374).

- [ ] Native AlphaESS local Modbus control is verified on supported hardware
      against #250, including read-back and failure handling.
- [ ] V2L refill actuation through the rectifier driver (#49, #52) passes the
      same gates and read-back as the inverter.

### Release mechanics (do last)

- [ ] `ha_spark_addon/config.yaml` set to `1.0.0` **on `master`**, with a
      `CHANGELOG.md` `1.0.0` entry and `DOCS.md` and the schema updated.
- [ ] Matching `v1.0.0` tag pushed (see the top of this file), with a GitHub
      Release for HACS, lockstep add-on/integration versions and a runtime
      `api_version` handshake per #252.
- [ ] Supervisor build (`pip install ...@v1.0.0`) succeeds from a clean pull.
