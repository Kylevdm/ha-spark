# Releasing

The add-on build fails unless two things agree. The add-on Dockerfile installs
`git+…@v${BUILD_VERSION}`, where `BUILD_VERSION` is the `version` in
`ha_spark_addon/config.yaml`. A release needs both:

1. `version` bumped in `config.yaml` **on `master`**. The store advertises the
   default branch's version, and a tag alone won't show users an update.
2. A matching annotated `vX.Y.Z` git **tag pushed**. Without it the build fails
   with `pathspec 'vX.Y.Z' did not match`.

The order is:

1. Commit the version bump.
2. Tag it `vX.Y.Z`.
3. Push the branch and the tag.
4. Merge to `master`.

Keep `config.yaml` `options`/`schema` in sync with `config.py` `_OPTION_KEYS`
(a test enforces this). For any option or behaviour change, bump the version
and update `CHANGELOG.md` and `DOCS.md`.

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

The pre-1.0 line continues `0.x`; retired rc tags remain unused. Historical
phase-to-version assignments are superseded. #247 owns phase sequencing.
No unchecked item is claimed as validated.

Before tagging `v1.0.0`, ruff, mypy strict, and pytest must be green, **and**
every item below must be confirmed on real hardware or live suppliers.

### Safety (automatic control; must never regress)

These gates cover add-on automatic control and its UI/CLI actions. The
integration warns but remains manually usable without the add-on. #248 and
#255 must settle the standalone/manual service and override contracts; no
bypass is granted here.

- [ ] `proactive_mode: simulate` (default) computes a plan, logs `[SIMULATE]`
      action lines, and performs **zero** real writes.
- [ ] A `control: observe` or `supplier` device never actuates, even with
      `proactive_mode: on`.
- [ ] A real write happens **only** with `control: ha_spark` **and**
      `proactive_mode: on`, is verified by read-back, and a failed action is
      isolated.
- [ ] Invalid SoC blocks normal SoC-based programming. The narrow configured
      Solis fallback exception must remain guarded and read-back verified
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
- [ ] Native AlphaESS local Modbus control is verified on supported hardware
      against #250, including read-back and failure handling. Native Solis
      verifies firmware, gateway caching and register semantics (#249/#259).
      The integration is the sole Modbus client; #248 owns its service contract.
- [ ] Agent surface: `get_state` reports each device's `control` authority;
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
- [ ] V2L refill is scheduled only when it is cheaper than grid and a
      reservation needs it; an unplugged car means no V2L; the plug-in
      notification fires.
- [ ] The readiness signal evaluates simulate history against the measured bar
      and never changes `proactive_mode` itself.

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

### Release mechanics (do last)

- [ ] `ha_spark_addon/config.yaml` set to `1.0.0` **on `master`**, with a
      `CHANGELOG.md` `1.0.0` entry and `DOCS.md` and the schema updated.
- [ ] Matching `v1.0.0` tag pushed (see the top of this file), with a GitHub
      Release for HACS, lockstep add-on/integration versions and a runtime
      `api_version` handshake per #252.
- [ ] Supervisor build (`pip install ...@v1.0.0`) succeeds from a clean pull.
