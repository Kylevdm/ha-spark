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

This section moved here from the v1.0.0 release umbrella (#94) so it outlives
the tracker.

**Definition.** v1.0.0 is the competitive MVP, validated on real hardware. It
ships when a zero-export house with a battery, solar, a V2L car, and Axle
flexibility events runs on autopilot. That means clean base-load forecasting,
half-hourly replanning, backward-computed reservations, automated Axle event
delivery, V2L refill, a phone digest, and a readiness signal, all behind the
existing safety gates and proven on the real Solis. The feature spec is #43.

**Sequence.** The pre-1.0 line continues the `0.x` series. The retired
`1.0.0-rc1` to `rc4` tags stay in git history, unused (see `CHANGELOG.md`
`0.14.0`). The original plan gave each Phase 10 milestone its own minor
release, `0.15.0` to `0.18.0`. Releases have since moved away from that plan:
`0.15.0` shipped derived base load (10.1), there was no `0.16.0`, `0.17.0`
shipped half-hourly replanning, reservations, and the supervised Axle prototype
together, and `0.18.0` and `0.19.x` shipped inverter-clock and Solis
commissioning work. The milestones, not version numbers, now track what
remains: the phone digest (10.2), and V2L refill plus the readiness signal
(10.4). `1.0.0` follows once this gate passes.

Before tagging `v1.0.0`, ruff, mypy strict, and pytest must be green, **and**
every item below must be confirmed on real hardware or live suppliers.

### Safety (must never regress)

- [ ] `proactive_mode: simulate` (default) computes a plan, logs `[SIMULATE]`
      action lines, and performs **zero** real writes.
- [ ] A `control: observe` or `supplier` device never actuates, even with
      `proactive_mode: on`.
- [ ] A real write happens **only** with `control: ha_spark` **and**
      `proactive_mode: on`, is verified by read-back, and a failed action is
      isolated.
- [ ] An invalid SoC reading causes no actuation.
- [ ] No secret (`SUPERVISOR_TOKEN`, `HA_TOKEN`, supplier API keys, Axle API
      key) appears in logs, plan output, or the agent surface.

### Foundation

- [ ] Existing flat (pre-`devices:`) config boots unchanged, and
      `options.json` is not rewritten.
- [ ] A misconfigured `tariff_provider` is rejected **at startup**, naming the
      bad field.
- [ ] The `fixed`, `dynamic`, and `octopus_intelligent` providers read live
      prices; a runtime price-read failure falls back to `fixed` and still
      produces a plan.
- [ ] AlphaESS `setbatterycharge` field names verified against the
      integration's `services.yaml`, **or** the AlphaESS driver marked
      experimental in `DOCS.md`.
- [ ] Agent surface: `get_state` reports each device's `control` authority;
      `/agent/*` writes still pass the gate; the LLM cannot reach
      `call_service`; an external non-ingress client is rejected without the
      bearer token.

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

### Release mechanics (do last)

- [ ] `ha_spark_addon/config.yaml` set to `1.0.0` **on `master`**, with a
      `CHANGELOG.md` `1.0.0` entry and `DOCS.md` and the schema updated.
- [ ] Matching `v1.0.0` tag pushed (see the top of this file).
- [ ] Supervisor build (`pip install ...@v1.0.0`) succeeds from a clean pull.
