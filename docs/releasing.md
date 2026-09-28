# Releasing

Non-obvious: both must agree or the add-on build fails. The add-on Dockerfile
installs `git+…@v${BUILD_VERSION}` where `BUILD_VERSION` =
`ha_spark_addon/config.yaml` `version`. A release needs BOTH:

1. `version` bumped in `config.yaml` **on `master`** (the store advertises the
   default branch's version, and a tag alone won't surface an update), and
2. a matching annotated `vX.Y.Z` git **tag pushed** (or the build fails with
   `pathspec 'vX.Y.Z' did not match`).

Sequence: commit bump → tag `vX.Y.Z` → push branch + tag → merge to `master`.
Keep `config.yaml` `options`/`schema` in sync with `config.py` `_OPTION_KEYS`
(a test enforces this); bump the version + `CHANGELOG.md` + `DOCS.md` for any
option/behaviour change.

## Add-on base image

Supervisor 2026.04.0+ ignores `build.yaml`/`BUILD_FROM`; set the base with
`FROM` in the Dockerfile. The `[habits]` ML extra (scikit-learn/numpy) has no
musllinux wheel, so the base is glibc (`python:3.13-slim-bookworm`); no
s6/bashio, so `run.sh` is plain shell and `config.yaml` sets `init: true`.

## v1.0.0 release gate

Moved here from the v1.0.0 release umbrella (#94) so it outlives the tracker.

**Definition.** v1.0.0 is the competitive MVP, validated on real hardware. It
ships when a zero-export house (battery, solar, a V2L car, Axle flexibility
events) runs on autopilot: clean base-load forecasting, half-hourly
replanning, backward-computed reservations, automated Axle event delivery, V2L
refill, a phone digest, and a readiness signal. All of it sits behind the
existing safety gates and is proven on the real Solis. The feature spec is #43.

**Sequence.** The pre-1.0 line continues the `0.x` series; the retired
`1.0.0-rc1`–`rc4` tags stay in git history, unused (see `CHANGELOG.md`
`0.14.0`). Each phase ships as a minor release tracked by its GitHub
milestone: `0.15.0` derived base load (10.1), `0.16.0` half-hourly cadence +
phone digest (10.2), `0.17.0` reservations + Axle (10.3), `0.18.0` V2L refill +
readiness signal (10.4), then `1.0.0` once this gate passes.

Before tagging `v1.0.0`, ruff, mypy strict and pytest must be green, **and**
every item below confirmed on real hardware or live suppliers.

### Safety (must never regress)

- [ ] `proactive_mode: simulate` (default) computes a plan, logs `[OBSERVE]`,
      performs **zero** real writes.
- [ ] A `control: observe` / `supplier` device never actuates, even with
      `proactive_mode: on`.
- [ ] A real write happens **only** with `control: ha_spark` **and**
      `proactive_mode: on`; read-back verified; a failed action is isolated.
- [ ] Invalid SoC reading → no actuation.
- [ ] No secret (`SUPERVISOR_TOKEN`, `HA_TOKEN`, supplier API keys, Axle API
      key) in logs, plan output, or the agent surface.

### Foundation

- [ ] Existing flat (pre-`devices:`) config boots unchanged; `options.json` not
      rewritten.
- [ ] Misconfigured `tariff_provider` rejected **at startup**, naming the bad
      field.
- [ ] `fixed` / `dynamic` / `octopus_intelligent` providers read live prices; a
      runtime price-read failure falls back to `fixed` and still produces a
      plan.
- [ ] AlphaESS `setbatterycharge` field names verified against the
      integration's `services.yaml`, **or** the AlphaESS driver marked
      experimental in `DOCS.md`.
- [ ] Agent surface: `get_state` reports each device's `control` authority;
      `/agent/*` writes still pass the gate; the LLM cannot reach
      `call_service`; an external non-ingress client is rejected without the
      bearer token.

### MVP

- [ ] Derived base load repairs the battery-polluted history; forecast error
      materially improves on the clean series.
- [ ] Half-hourly replan absorbs a day-of event / plug-in with no new trigger
      logic; unchanged setpoints produce zero writes.
- [ ] Reservations trace every plan line to a sentence-shaped reason.
- [ ] **Axle event delivery proven end to end on the real Solis:** a real (or
      replayed) event → held energy → commanded export within the inverter's
      export/discharge limits → house not stranded before the next cheap slot.
      Malformed/absent Axle payloads degrade to the base schedule.
- [ ] V2L refill scheduled only when cheaper than grid and a reservation needs
      it; unplugged car → no V2L; plug-in notification fires.
- [ ] The readiness signal evaluates simulate history against the measured bar
      and never flips `proactive_mode` itself.

### Release mechanics (do last)

- [ ] `ha_spark_addon/config.yaml` → `1.0.0` **on `master`**; `CHANGELOG.md`
      `1.0.0` entry; `DOCS.md` + schema updated.
- [ ] Matching `v1.0.0` tag pushed (see the top of this file).
- [ ] Supervisor build (`pip install ...@v1.0.0`) succeeds from a clean pull.
