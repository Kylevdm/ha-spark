# Future backlog

Undelivered work pulled out of `CLAUDE.md`. `ROADMAP.md` is the authoritative,
detailed source for direction and status; the GitHub
[milestones](https://github.com/Kylevdm/ha-spark/milestones) are the live
tracker. This is a short index of what is *not yet built*, so it stays out of
`CLAUDE.md` until it ships.

The deterministic planner still decides; every controllable device carries a
`control: observe | ha_spark | supplier` authority, and real writes need
`control == ha_spark` **and** `PROACTIVE_MODE == on`. Do not reintroduce
hardcoded Solis/Octopus/zappi assumptions into the planner.

- **Phase 9: EV drivers + supplier authority** (deferred,
  [#61](https://github.com/Kylevdm/ha-spark/issues/61)): EV charger drivers; EV
  defaults to `supplier` (observe & plan around) with an optional `ha_spark`
  path; reads V2L availability.
- **Phase 10.2: Half-hourly cadence + phone digest** (0.16.0): the plan recomputed
  every tariff slot; the phone surface gets a daily digest.
- **Phase 10.3: Reservations + Axle provider** (0.17.0): reservations as the
  planner's lookahead mechanism; flexibility events priced into the tariff
  schedule via an Axle provider.
- **Phase 10.4: V2L refill source** (0.18.0): the car battery as a refill source for
  the house battery (car → rectifier → house battery), chosen by cost and
  availability.
- **Phase 11: Heat pump (observe + model)** (1.1.0): heat-pump device fed into the
  load model; control deferred.
- **Phase 12: Driver-aware onboarding** (1.2.0): `onboard` proposes driver +
  provider + entity map + capability coverage; per-driver/supplier presets;
  multi-device.
- **Phase 13: MCP agent surface** (1.8.0): the remaining MCP work beyond the shipped
  agent surface. Inbound surface must require a token, bind to ingress not an
  open port, and gate read vs. act under the same authority/PROACTIVE_MODE as
  the CLI.

Shipped out of this list: Phase 7 (device-driver core, 1.0.0-rc1) and Phase 8
(multi-supplier tariffs: `fixed`, `dynamic`, `octopus_intelligent`); Phase 10.1
(derived base load, 0.15.0). V2L observe/tally/notify (#123) is on `master`
awaiting release.

## Later (post-1.0)

Heat-pump active coordination + hot-water tank, multi-inverter sites, Solcast
bias correction, EV-dispatch propensity prediction, more vendor presets/drivers.
