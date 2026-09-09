# Predbat competitive analysis

Date: 2026-07-16
Subject: [springfall2008/batpred](https://github.com/springfall2008/batpred) ("Predbat"), researched for ha-spark competitive positioning.

Repo stats at time of writing: 305 GitHub stars, 609 open issues, most recent
release `v8.45.2` on 2026-07-13 ([releases page](https://github.com/springfall2008/batpred/releases)).
Predbat has shipped continuously since 2023 and the codebase has grown from
"350 lines in v1.0 to over 9,000 lines in v7.13.4" per a maintainer/contributor
retrospective ([Discussion #326](https://github.com/springfall2008/batpred/discussions/326)).

## 1. Features today

**Optimization approach.** Predbat is a simulation-based plan-search optimizer,
not an LP solver. It evaluates candidate charge/discharge/export window
combinations by running a battery-behaviour simulation over the forecast
horizon for each candidate and picking the lowest-cost plan. To keep this
tractable it uses a "two-pass coarse/fine" search — a coarse pass quickly
evaluates a reduced set of slot-length combinations to find approximately
optimal windows, then a fine pass refines only around those — and, as of
`v8.44.0` (2026-07-02), an optional compiled C++ prediction kernel that
replaces the Python simulation loop for most scenario evaluations, giving a
"3-4x speedup with identical results" ([release notes](https://github.com/springfall2008/batpred/releases/tag/v8.44.0)).
This is architecturally the "brute-force/simulation search" family EMHASS's
LP solver is explicitly contrasted against in the ha-spark ROADMAP.

**Tariff support.** Deepest support is for Octopus Energy: Agile, Intelligent
GO, Cosy, Flux, Go, and Tracker, via direct API, the HA Octopus Energy
integration, or direct URL config, plus saving-session and free-electricity-
event handling. Beyond Octopus: EDF/E.ON Next via the Kraken component,
Frank Energie (Netherlands), Danish Strømligning (15-minute pricing),
Energidataservice (Nordic/European), and Czech 15-minute spot pricing. A
generic "spot rate sensor" path and manual time-of-day rate bands cover
suppliers without dedicated integration. ([Energy rates docs](https://springfall2008.github.io/batpred/energy-rates/))

**Solar forecast.** Solcast is the primary integration; `v8.40.4` (2026-06-08)
added OpenMeteo as a backup/alternative to Forecast.Solar
([release notes](https://github.com/springfall2008/batpred/releases/tag/v8.40.4)),
and `v8.39.12` added multi-plane Forecast.Solar support for roofs with
multiple panel orientations.

**Load prediction.** Historical-averaging based by default (`days_previous`
style lookback), with an ML load-prediction path also documented
("LoadML") — `v8.44.1` (2026-07-05) enabled "prediction kernel and
days_previous_auto by default," and `v8.40.9`/`v8.40.10` shipped fixes to
"enable base load by default" and "PV Inverter & LoadML" respectively.

**Charge/discharge/export planning, iBoost, car charging.** The plan drives
charge windows, forced-discharge/export windows, and freeze-charge/freeze-
discharge holds. iBoost (solar-diverter) modelling is a documented, screenshotted
feature on the docs homepage. Car charging is a first-class planning target —
recent releases add EVC (EV charger) control via OCPP through a "Gateway"
component (`v8.42.2`, 2026-06-28: "feat: Gateway OCPP EVC controls beta").

**Operating modes.** A `select.predbat_mode` entity exposes modes including
Monitor (read-only), Control charge, and Control charge & discharge — i.e. a
partial "try before trust" read-only mode, matching what the ha-spark ROADMAP
comparison table already notes.

**Dashboard / apps.** Ships an auto-generated `predbat_dashboard.yaml` Lovelace
starter dashboard, plus a "Compact Dynamic Dashboard" built on the HACS
`auto-entities` and `lovelace-collapsable-cards` community cards for automatic
entity organization. A built-in Predbat Web Interface (plan view, config
editor, charts, logfile viewer) runs independent of HA's own dashboard. Mobile
presence is via HA's own Companion App notifications (critical alerts on
faults), not a dedicated Predbat mobile app.
([Output data docs](https://springfall2008.github.io/batpred/) navigation; HACS cards referenced from docs)

## 2. Inverter coverage

Predbat lists support for GivEnergy, Fox ESS, Solis, SolarEdge, Sofar,
Sunsynk, Huawei, SolaX, LuxPower, Sigenergy, Enphase (beta, `v8.45.0`), and
Tesla Powerwall, per the [repo README](https://github.com/springfall2008/batpred)
and [docs introduction](https://springfall2008.github.io/batpred/).

Architecturally, this is **one generic `Inverter` class** (`apps/predbat/inverter.py`),
not a family of per-brand subclasses that each implement a shared interface.
Per-brand behaviour is selected at init from an `INVERTER_DEF` configuration
dictionary keyed by an `inverter_type` string set in `apps.yaml`, which
declares each brand's capabilities (has REST API, has MQTT, register names,
etc.); the same `Inverter` class then reads/writes through whichever
transport that brand supports:

- **REST API** for brands that expose one (GivEnergy/GivTCP is the primary
  example — `self.rest_api = self.base.get_arg("givtcp_rest", ...)`).
- **HA entity writes with polling/read-back validation** as the generic
  fallback — most brands are ultimately controlled by writing to
  configurable HA entity names (numbers/selects/switches) rather than a
  native SDK, which is why `apps.yaml` for each brand is largely "map these
  entity names."
- **MQTT** where `has_mqtt_api` is set in the brand's definition.

In addition to `inverter.py`, the source tree carries separate
brand/cloud-integration modules for the higher-effort cloud-API paths —
`solax.py`, `solis.py`, `fox.py`, `enphase.py`, `sigenergy.py`, `gecloud.py`,
`kraken.py` — layered on top of the generic `Inverter` abstraction rather
than replacing it, plus core modules `predbat.py` (main loop), `prediction.py`
(simulation engine), `plan.py`, `execute.py`, `storage.py`, `octopus.py`,
`solcast.py`. (Source browsed at [`apps/predbat/`](https://github.com/springfall2008/batpred/tree/main/apps/predbat).)

Net: Predbat's abstraction is closer to "one generic class configured by a
brand capability table + HA entity mapping" than to a clean per-inverter
driver interface — which is consistent with the recurring "unable to read
charge window time as neither REST, charge_start_time nor charge_start_hour
are set" class of misconfiguration bug users hit (see §4).

## 3. Recent releases (roughly Aug 2025 – Jul 2026)

Predbat ships very frequently — multiple point releases most weeks
(`v8.27.x` through `v8.45.2` in this window, per the
[releases page](https://github.com/springfall2008/batpred/releases)).
Reading the trajectory across that run:

- **Cloud-API integrations are the dominant theme.** A large fraction of
  releases in this window are brand-specific cloud integration work and
  fixes: GE Cloud (`gecloud`) charge-rate/limit/reset fixes across a dozen+
  releases, Fox Cloud v2 scheduler fixes, SolaX cloud minimum-reserve fixes,
  Sigenergy cloud (onboarding, fixes across multiple releases), Solis Cloud
  OAuth, and new-in-window Enphase Cloud support (`v8.45.0`, beta) and Kraken
  API (EDF/E.ON Next) fixes.
- **A "Gateway" component emerged** as a distinct control path — e.g.
  `v8.42.2` "Gateway OCPP EVC controls beta," `v8.40.15` "Gateway read only
  status and plan controls," several Gateway-specific bugfix releases —
  suggesting Predbat is building a more unified device-gateway layer
  alongside the older per-brand cloud integrations.
- **Core optimizer performance work**: `v8.44.0` (2026-07-02) — "Major
  optimisation to add [optional] C++ kernel, 3-4x speedup" — is the single
  largest architectural change in the window, addressing long-standing CPU
  cost complaints (see §4).
- **Octopus remains the most actively maintained tariff integration**: fixes
  for Intelligent dispatch detection, flex/dispatch phantom slots, and — the
  very latest release, `v8.45.2` (2026-07-13) — "2-minute dispatch refresh
  and replan only on genuine slot changes."
- **Forecast breadth**: OpenMeteo added as a Forecast.Solar backup
  (`v8.40.4`), multi-plane Forecast.Solar support (`v8.39.12`).
- Overall direction: consolidating/broadening inverter and tariff cloud
  coverage (many small brand-specific reliability fixes, evidence of a
  fragile long tail) while investing in raw optimizer performance (C++
  kernel) and a newer unified "Gateway" abstraction — not a pivot toward
  simplification or UX, despite that being a recurring ask (§4).

## 4. User pain points

**Configuration complexity, long-standing and acknowledged by contributors.**
In [Discussion #326 "Future Development"](https://github.com/springfall2008/batpred/discussions/326),
contributor `iainfogg` argued for freezing the feature set: "I'd rather
freeze the feature set, to get time to improve both the UI and also code
quality," citing growth from 350 lines (v1.0) to 9,000+ lines (v7.13.4) with
long methods and no automated tests. Contributor `gcoan` separately noted
"too much configuration so newcomers get lost in the learning curve," and
`JonathanLew1s` asked for a "One Button Mode" with automatic tariff
detection. This is a multi-year, still-open critique — the codebase has
continued to grow substantially since (per §3, releases through mid-2026
keep adding cloud-brand-specific code paths).

**Inverter/config misconfiguration crashes the planner.** [Issue #3571](https://github.com/springfall2008/batpred/issues/3571)
("Predbat suddenly unable to produce a plan, 'Error: Exception raised'",
opened against `v8.34.3`, 24 comments) shows a GrowattSPH user hitting
`Inverter 0 unable to read charge window time as neither REST,
charge_start_time or charge_start_hour are set` after months of stable
operation with no config change on their end; the workaround suggested in
comments was setting `charge_start_time`/`discharge_start_time` to
placeholder values, which the reporter did "not really knowing what I'm
doing." This is a live example of the generic-entity-mapping fragility noted
in §2.

**State doesn't survive restarts/updates reliably.** [Issue #3259](https://github.com/springfall2008/batpred/issues/3259)
("After HA restart Predbat reverts to monitor mode, config settings default
to default and/or Predbat fails to reconnect") reports the `select.predbat_mode`
entity silently reverting from "Control charge & discharge" to "Monitor"
(a read-only mode) after an HA OS/Supervisor update, with no user action —
meaning the battery silently stops being controlled. One commenter's
workaround was a bespoke HA automation with a critical-alert push
notification to catch the regression and auto-revert it, because Predbat
itself doesn't guard against it.

**Inverter hardware timing out Predbat's commands.** The HA community forum
thread ["Recent issues with Predbat and Solaredge Batteries"](https://community.home-assistant.io/t/recent-issues-with-predbat-and-solaredge-batteries/791999)
documents SolarEdge batteries reverting to default operating mode ~60
minutes after Predbat issues a charge/discharge command, independent of any
Predbat-side config — i.e. a control loop that isn't robust to inverter-side
command expiry, root-caused to the inverter's own timeout rather than
something Predbat can straightforwardly fix.

**CPU cost on constrained hardware.** Web search of the docs/FAQ surfaces
recurring reports that planning "can use a lot of CPU power especially on
complex tariffs like Agile when run on lower power machines such as
Raspberry Pis," which the `v8.44.0` C++ kernel work (§3) was aimed at, three+
years into the project.

**Solar forecast trust issues.** [Discussion #3263 "Solcast v Predbat"](https://github.com/springfall2008/batpred/discussions/3263)
and related issues report the plan underestimating solar output relative to
what Solcast itself forecasts — a recurring "why doesn't my plan match my
forecast" class of confusion.

Taken together: the complaints cluster into (a) configuration/mapping
fragility inherent to the generic-entity abstraction, (b) state/mode not
being robustly persisted across HA restarts, (c) inverter-hardware quirks
outside Predbat's control that still surface as "Predbat is broken," and (d)
a multi-year, unresolved tension between feature growth and usability that
the maintainers themselves have flagged and not resolved.

## 5. Beat / match / trail vs ha-spark

Read against ha-spark's own `ROADMAP.md`, `CONTEXT.md`, and module docstrings
(`energy/sources.py`, `energy/planner.py`, `energy/chargers.py`,
`energy/supply_guard.py`, `router.py`, `copilot.py`, `intent_parser.py`,
`config.py`).

**Where ha-spark already beats Predbat:**

- **Explains itself in plain language.** ha-spark's copilot (`router.py`,
  `copilot.py`) grounds a local Ollama LLM in the actual computed plan and
  answers "why are you charging to 80%?" with real numbers, with a
  deterministic offline fallback (`intent_parser.py`) that never crashes.
  Predbat has no natural-language explanation layer at all — users read
  charts and log lines. This is a real, currently-shipped differentiator,
  not aspirational.
- **Hard control/explain separation as an architectural invariant.** ha-spark's
  LLM literally cannot reach `call_service` (CLAUDE.md, `copilot.py`
  docstring: "never claims to have changed a setting"). This isn't something
  Predbat needs, since it has no LLM tier, but it does mean ha-spark's NL
  surface can't degrade into an unsafe control path the way a bolted-on
  chat-to-automation feature might.
- **Actuation safety invariants are explicit and centrally enforced.**
  `energy/supply_guard.py` and the `control: observe|ha_spark|supplier`
  authority model plus `PROACTIVE_MODE` gating give ha-spark a single,
  auditable place where "is a write allowed right now" is decided. Predbat's
  #3259 (mode silently reverting to Monitor after restart, with no built-in
  detection) is exactly the failure class this design is meant to prevent —
  though ha-spark has not yet been proven at Predbat's scale of users/edge
  cases, so this is a design strength, not a demonstrated reliability win.

**Where they roughly match:**

- **Optimization sophistication in the currently-shipped strategy.** Predbat's
  simulation-based plan search is more general (handles arbitrary
  charge/discharge/export window shapes) than ha-spark's v1/v2 planner, which
  is a direct energy-balance calculation (`energy/planner.py` docstring) —
  simpler and fully auditable, but it does not search a space of alternative
  plans the way Predbat's coarse/fine simulation does. Call this "different
  philosophy, comparable current practical output for ha-spark's target
  household," per the ROADMAP's own footnote pointing at
  `docs/adr/0002-auditable-over-optimal-planning.md` for the reasoning.
- **Try-before-trust / simulate mode.** Both offer a non-destructive mode
  (Predbat's Monitor select option vs ha-spark's `proactive_mode = simulate`)
  — ha-spark's ROADMAP claims an edge here (backtest quantifying savings vs
  Predbat's "partial, read-only only"), but that backtest quality claim
  should be verified against what Predbat's dashboard already surfaces
  before treating it as a clear win; it may be closer to parity.

**Where ha-spark clearly trails:**

- **Inverter coverage.** ha-spark ships drivers only for AlphaESS and Solis
  (`ha_spark/devices/inverters/{alphaess,solis}.py`), with no general
  abstraction layer yet. Predbat supports on the order of a dozen brands
  (GivEnergy, Fox ESS, Solis, SolarEdge, Sofar, Sunsynk, Huawei, SolaX,
  LuxPower, Sigenergy, Enphase, Tesla Powerwall) through years of
  brand-specific hardening — visible in the sheer volume of per-brand
  cloud-integration fixes across the last year of releases (§3). This is the
  single largest gap and the one most likely to gate real-world adoption
  outside Kyle's own household.
- **Tariff breadth.** ha-spark's tariff work is Octopus-centric and
  multi-supplier support is explicitly still in flight ("Phase 8
  multi-supplier tariffs" per ROADMAP's "in flight" section). Predbat
  already covers Octopus's full product range plus EDF/E.ON Next (Kraken),
  Frank Energie, Danish and Nordic spot-price providers, and a generic
  spot-rate-sensor fallback.
- **Maturity/battle-testing.** Predbat has 3+ years of continuous shipping,
  609 open issues (a lot of surface area, but also a lot of real-world
  feedback absorbed), and public multi-year discussions about its own
  failure modes. ha-spark has none of that field exposure yet; the ROADMAP's
  own "Competitive MVP" bar (CONTEXT.md: "the point at which ha-spark fully
  runs Kyle's own household better than a configured Predbat could") is
  explicitly framed as not yet met.
- **EV charging control.** Predbat has EVC/OCPP control shipping (beta) as of
  `v8.42.2`. ha-spark's Phase 9 (EV charger drivers) is "formally deferred"
  per ROADMAP (issue #61) — a capability gap, not just an immaturity gap.
- **Ecosystem/community tooling.** Predbat has third-party HACS dashboard
  cards, a large HA-forum user base generating public troubleshooting
  threads, and a documented plugin/REST API surface for developers. ha-spark
  currently has none of this ecosystem, which is expected at its stage but
  is a real current gap, not just a maturity framing.

**One honest caveat on method:** Predbat's optimizer complexity growth and
per-brand fragility (§3, §4) is partly a *consequence* of covering a dozen+
inverter brands and a wide tariff matrix. ha-spark's cleaner current
architecture is real, but it has not yet been tested against that same
breadth of hardware and tariff edge cases — the fair comparison is "ha-spark
is simpler because it does less today," not "ha-spark has solved the problem
Predbat's complexity reflects."
