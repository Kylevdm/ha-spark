# Future backlog

Undelivered work pulled out of `CLAUDE.md`. `ROADMAP.md` is the authoritative,
detailed source for direction and status — this is a short index of what is
*not yet built*, so it stays out of `CLAUDE.md` until it ships.

## v1.0 — Modular ecosystem + agent surface

Open ha-spark beyond the single Solis/Solcast/Octopus/zappi setup. The
deterministic planner still decides; every controllable device carries a
`control: observe | ha_spark | supplier` authority, and real writes need
`control == ha_spark` **and** `PROACTIVE_MODE == on`. Do not reintroduce
hardcoded Solis/Octopus/zappi assumptions into the planner.

- **7 — Device-driver core** (1.0.0): `devices/` driver layer + registry,
  `Capability`/`ControlAuthority`, structured per-device config + migration shim
  off the flat 0.9.0 config; actuation routed through drivers + the authority
  gate. Zero behaviour change for the current setup.
- **8 — Multi-supplier tariffs** (1.1.0): `TariffProvider` yielding a normalised
  per-slot import/export price schedule + controlled windows (`fixed`,
  `time_of_use`, `dynamic`, `export`, `octopus_intelligent`); planner costed
  against the schedule, not a fixed window.
- **9 — EV drivers + supplier authority** (1.2.0): EV charger drivers; EV
  defaults to `supplier` (observe & plan around) with an optional `ha_spark`
  path; reads V2L availability.
- **10 — Multi-source charging + notifications** (1.3.0): R48 rectifier drivers
  (grid-wired + V2L-fed); planner chooses charge source by cost/availability; a
  `NOTIFY` action via HA `notify`.
- **11 — Heat pump (observe + model)** (1.4.0): heat-pump device fed into the
  load model; control deferred.
- **12 — Driver-aware onboarding** (1.5.0): `onboard` proposes driver + provider
  + entity map + capability coverage; per-driver/supplier presets; multi-device.
- **13 — MCP agent surface** (1.6.0): authenticated, ingress-bound MCP server —
  read tools (plan/state/eval/predictions/health) and gated act tools (context,
  run-plan, notify). Inbound surface must require a token, bind to ingress not an
  open port, and gate read vs. act under the same authority/PROACTIVE_MODE as the CLI.

## Later (post-1.0)

Heat-pump active coordination + hot-water tank, multi-inverter sites, Solcast
bias correction, EV-dispatch propensity prediction, more vendor presets/drivers.
