# Multi-inverter driver foundation: competitive survey

Date: 2026-07-16
Subject: how [Predbat](https://github.com/springfall2008/batpred), [evcc](https://github.com/evcc-io/evcc),
and others abstract over many inverter brands; the AlphaESS and Victron
control surfaces specifically; and what a driver foundation for ha-spark
needs, given AlphaESS + Victron support is coming and the project currently
ships only AlphaESS and Solis drivers (`ha_spark/devices/inverters/`).

Researched for the Phase-8+ decision on whether/how to generalize
`ha_spark/devices/` beyond its current two-driver, `@register`-keyed shape
(`ha_spark/devices/base.py`, `ha_spark/devices/registry.py`,
`ha_spark/devices/__init__.py`).

## 1. Predbat's inverter abstraction (deeper look at the driver layer)

`docs/research/predbat.md` §2 already covers the headline finding — one
generic `Inverter` class (`apps/predbat/inverter.py`) configured by a
per-brand `INVERTER_DEF` capability table, not a family of per-brand
subclasses. This section goes one level deeper into that table and the
control primitives, read from
[`apps/predbat/inverter.py`](https://github.com/springfall2008/batpred/blob/main/apps/predbat/inverter.py).

**`INVERTER_DEF` is a capability-flag dictionary, not a code path.** Each
brand entry sets booleans/strings the generic class branches on at every
control point: `has_rest_api`, `has_mqtt_api`, `has_charge_enable_time`,
`has_discharge_enable_time`, `has_target_soc`, `has_reserve_soc`,
`has_timed_pause`, `charge_time_format` (e.g. `"HH:MM:SS"`),
`charge_time_entity_is_option` (select vs. text/time entity),
`clock_time_format`, `soc_units` (`"%"` vs `"kWh"`),
`output_charge_control` (`"power"` vs `"current"`), `charge_control_immediate`,
`can_span_midnight`, `has_idle_time`, plus a display `name`. `inverter_type`
is a plain string set per-inverter in `apps.yaml`
(`self.inverter_type = self.base.get_arg("inverter_type", "GE", indirect=False, index=self.id)`),
and the constructor logs which brand table it resolved
(`self.log(f"Inverter {self.id}: Type {self.inverter_type} {INVERTER_DEF[self.inverter_type]['name']}")`).

**Control primitives are named methods on the one class**, gated internally
by the capability flags above: `adjust_charge_window(start_time, end_time,
minutes_now)`, `adjust_force_export(enable, start_time, end_time)`,
`adjust_battery_target(percent, is_percent)`, `adjust_charge_rate(rate_watts)`,
`adjust_discharge_rate(rate_watts)`, `adjust_reserve(reserve_percent)`,
`adjust_pause_mode(pause_charge, pause_discharge)`,
`disable_charge_window()`, and — notably — `mimic_target_soc(limit,
discharge)`, which **emulates** a native target-SoC primitive for brands that
lack `has_target_soc` by toggling enable/disable at the right moment instead.
Read-side: `update_status(minutes_now, quiet)` refreshes all state,
`get_current_charge_rate()`/`get_current_discharge_rate()` read back actuals,
and `find_battery_size()`/`find_charge_curve()` infer capacity/efficiency
from history rather than requiring the user to enter nameplate values (same
idea as EMHASS's `set_use_battery_identification`, per `docs/research/emhass.md` §1).

**Three transports, same method surface.** Preference order is REST (GivTCP,
where `self.rest_api` is set) → HA entity writes with poll-and-verify
(`write_and_poll_value(name, entity_id, new_value)`,
`write_and_poll_switch(name, entity_id, enable_state)`) → MQTT
(`self.mqtt_message(topic="set/reserve", payload=reserve)`) where
`has_mqtt_api` is set. For a capability a brand's INVERTER_DEF says it lacks,
Predbat **synthesizes a dummy HA entity** rather than erroring — e.g.
`if not self.inv_has_reserve_soc: self.create_missing_arg("reserve",
self.reserve)` creates a `battery`-device-class helper entity so the rest of
the planner can treat every inverter as if it had every primitive, with the
capability gap absorbed at the read/write boundary instead of forking the
call sites. This is the mechanism behind the `predbat.md` §4 finding
("Inverter 0 unable to read charge window time as neither REST,
charge_start_time nor charge_start_hour are set", issue #3571) — when *none*
of REST, the dummy-entity fallback, or a real HA entity resolves for a given
brand/config combination, the generic class has nothing left to read and
raises, and the user has to reverse-engineer which of the three paths their
config was supposed to satisfy.

**Where it's fragile, restated precisely:** the abstraction is sound in
principle (one interface, capability flags select behavior, transports are
interchangeable) but the flag table plus three-transport fallback chain is
itself the source of the `apps.yaml` misconfiguration class of bug — the
generic class can't tell "this brand genuinely has no reserve-SoC control"
apart from "the user's `apps.yaml` doesn't declare the entity that would
supply it," and both look identical from inside `adjust_reserve()`.

## 2. evcc's abstraction

evcc ([evcc-io/evcc](https://github.com/evcc-io/evcc)) is a Go project whose
core device contracts live in
[`api/api.go`](https://raw.githubusercontent.com/evcc-io/evcc/master/api/api.go)
as small, composable Go interfaces — the opposite shape from Predbat's one
big flag-driven class:

- **`Meter`** — `CurrentPower() (float64, error)`: the minimal read contract
  every power-producing/consuming device satisfies.
- **`MeterEnergy`** / **`MeterReturnEnergy`** — optional add-ons:
  `TotalEnergy() (float64, error)` (cumulative import kWh),
  `ReturnEnergy() (float64, error)` (cumulative export kWh). A device
  implements these only if it can report them — Go's structural interfaces
  let evcc runtime-`switch`/type-assert to detect which optional
  capabilities a concrete device satisfies, which is the Go-idiomatic
  equivalent of Predbat's boolean capability flags, except the compiler (and
  a type assertion) enforces the mapping instead of a hand-maintained table.
- **`Battery`** — `Soc() (float64, error)`: state of charge, the minimal
  battery read.
- **`BatterySocLimiter`** — `GetSocLimits() (min, max float64)`: configured
  charge/discharge SoC bounds.
- **`BatteryController`** — `SetBatteryMode(BatteryMode) error`: the actual
  control primitive. `BatteryMode` is a small enum (evcc's
  [`api/battery.go`](https://github.com/evcc-io/evcc/blob/master/api/battery.go)
  defines it as `Normal | Hold | Charge` in current evcc terminology) — evcc
  doesn't expose a raw watts/current setpoint through this interface at all;
  it exposes **behavioral modes** ("let the inverter self-regulate normally",
  "freeze at current SoC", "force-charge from grid") and leaves the actual
  power-level mechanics to the per-device plugin underneath.
- **`Charger`** (EV charging, not inverters, but the same interface-family
  pattern) — embeds `ChargeState` (`Status() (ChargeStatus, error)`) plus
  `Enabled()/Enable(bool)/MaxCurrent(int64)`. Optional add-ons:
  `CurrentLimiter.GetMinMaxCurrent()`, `PhaseSwitcher.Phases1p3p(int)`,
  `PhaseGetter.GetPhases()`, `Identifier.Identify()` (RFID/vehicle ID),
  `PowerLimiter.GetMinMaxPower()`.

**Devices are wired to hardware via a YAML template + plugin system, not
Go code per brand.** evcc's
[`templates/definition/meter/`](https://github.com/evcc-io/evcc/tree/master/templates/definition/meter)
holds ~150+ per-brand/model YAML files (naming convention
`manufacturer-model.yaml`, e.g. `huawei-sun2000-hybrid.yaml`,
`fronius-gen24.yaml`); each declares the device's Modbus registers (address,
scale, data type) or REST/MQTT endpoint plus parameter placeholders (host,
unit ID, credentials) that the plugin layer executes against at runtime — so
adding brand #151 is "write a YAML register map," not "write a Go type."
This is architecturally closer to Predbat's `INVERTER_DEF` table than it
first appears, except evcc's version is one file per exact model (richer,
more precise, more files to maintain) versus Predbat's one entry per brand
family (coarser, less file sprawl, more room for the entity-name-mismatch
class of bug).

**Multi-device aggregation happens at the `Site` level**, not the device
interface. `core/site.go`
([source](https://raw.githubusercontent.com/evcc-io/evcc/master/core/site.go))
holds `site.batteryMeters []api.Meter` (etc.) and `updateBatteryMeters()`
polls all configured batteries in parallel via `sync.WaitGroup`, then
aggregates: "a weighted sum of soc by capacity" when every battery reports
capacity, otherwise a simple average — i.e. the interface stays
single-device-shaped, and fleet-of-batteries is a site-level concern layered
on top, with per-device metrics kept in independent collectors
(`site.collectors[ref]`) so one battery's history doesn't get blended into
another's. `site.batteryMode` / `batteryModeExternal` /
`batteryModeExternalTimer` track a site-wide external-override state with its
own timeout, restored on shutdown — a "someone else took control, and only
for this long" pattern worth comparing to ha-spark's
`control: observe|ha_spark|supplier` authority model (§6).

**Net shape**: evcc's answer to "many inverters" is small orthogonal
interfaces (one method per concern) + optional-capability interfaces detected
by type assertion + a YAML template per exact model for the actual
register/endpoint mapping + site-level fleet aggregation. Predbat's answer is
one big class + a capability-flag table + three hardcoded transports with an
entity-synthesis fallback. Both put the actual brand-specific detail in
config/data (a table or a YAML file) rather than in per-brand code paths —
neither project asks contributors to write a new Go/Python class merely to
support "another SoC register at another address."

## 3. Others, briefly

**Home Assistant's own inverter integrations (`huawei_solar`, `solax`,
`sunsynk`, Solis-Modbus) don't share an abstraction at all — each is an
independent Modbus-polling integration with its own entity set.**
[`wlcrs/huawei_solar`](https://github.com/wlcrs/huawei_solar) polls a Huawei
SUN2000 over Modbus-TCP roughly every 30s (and, after a Huawei firmware
change in Dec 2021, only reachable via the inverter's own WiFi AP at
`192.168.200.1`, not the home LAN — a vendor-side lockdown, not an
integration bug). [`wills106/homeassistant-solax-modbus`](https://github.com/wills106/homeassistant-solax-modbus)
is explicitly one integration covering **several** rebadged/OEM-related
brands (SolaX, AlphaESS, Growatt, Sofar, Solinteg, Solis, SRNE, Swatten) —
i.e. HA's ecosystem already has a "one Modbus register-map integration,
multiple brand variants" pattern for hardware families that share a Modbus
dialect, which is a data point for how much brand consolidation is realistic
at the register-map level. A recurring practical constraint across all of
these: **Modbus is fundamentally single-master** — connecting two pollers
(e.g. HA plus a Modbus/TCP-to-RTU gateway plus ha-spark) to the same
RS485/TCP endpoint risks the inverter blocking the second connection or
serving corrupted reads, worked around with a hardware multiplexer or a
single shared gateway process, not a software-side fix.

**OpenEMS's Edge component model** is the most formally-typed abstraction
surveyed. Physical hardware is represented as OSGi *Components* implementing
Java interfaces called **Natures**
([Nature docs](https://openems.github.io/openems.io/openems/latest/edge/nature.html));
for storage the relevant Natures are `SymmetricEss` (read-only: SoC, power),
`ManagedSymmetricEss` (adds a controllable **power constraint channel** — the
docs describe it as holding "the currently maximum allowed charge power...
type Integer, unit Watt"), and `AsymmetricEss` for per-phase variants. A
control algorithm declares it needs "a `ManagedSymmetricEss`" and OpenEMS's
dependency injection wires in whatever concrete device satisfies that
interface at runtime — structurally the same idea as evcc's optional
interfaces, but formalized through OSGi service references rather than a
runtime type assertion. This is the heaviest-weight of the surveyed
approaches (a Java OSGi framework, not a Python/Go script), consistent with
OpenEMS being aimed at commercial/utility-scale ESS integration rather than a
single-household HA add-on — useful as an upper bound on "how formal could
this get," not a template ha-spark should imitate at its current scale.

**solar_assistant** (closed-source home-energy dashboard/controller,
mentioned for completeness): it presents itself to users as a single
polished dashboard across many inverter brands via native Modbus/CAN drivers
per brand, but its driver internals aren't published — no primary source to
cite beyond its own marketing site, so it can't inform the abstraction
question with the same rigor as the open-source projects above and is noted
here only as "exists, closed, not inspectable."

## 4. AlphaESS control surface

**Official Open API — cloud, OAuth-less key/secret auth, coarse-grained.**
Registration at [open.alphaess.com](https://open.alphaess.com/) yields an
`AppID`/`AppSecret` pair (a "Developer ID"/"Developer Secret"); the
maintained spec/SDK lives at
[alphaess-developer/alphacloud_open_api](https://github.com/alphaess-developer/alphacloud_open_api),
with the full endpoint reference behind
[open.alphaess.com/developmentManagement/apiList](https://open.alphaess.com/developmentManagement/apiList)
(registration-gated, not fetched directly for this survey — endpoint names
below are corroborated by the community Python wrapper
[CharlesGillanders/alphaess-openAPI](https://github.com/CharlesGillanders/alphaess-openAPI),
which implements the same official API). Endpoints include `getEssList`,
`getLastPowerData`, `getOneDayPowerBySn`, `getOneDateEnergyBySn` (read-only
telemetry) and — the actuation surface — `getChargeConfigInfo` /
`updateChargeConfigInfo` and `getDisChargeConfigInfo` /
`updateDisChargeConfigInfo` (read/write the charge and discharge schedule
config, i.e. windows + SoC targets, not a live power/current setpoint).
Auth is validated by an authenticated call to
`https://openapi.alphaess.com/api/getEssList`. **Rate limit**: community
guidance is a minimum ~10-second poll interval on AlphaCloud endpoints — this
is a cloud API with a real throttle, not a fire-at-will local interface.

**HA integration — cloud-backed, service-call actuation.**
[CharlesGillanders/homeassistant-alphaESS](https://github.com/CharlesGillanders/homeassistant-alphaESS)
wraps the same official API and exposes an `alphaess.setbatterycharge`
service — the exact call ha-spark's own driver already targets
(`ha_spark/devices/inverters/alphaess.py`). Confirmed field names from the
integration's own issue tracker/README: `serial`, `enabled` (bool), `cp1start`
/`cp1end` (charge period 1), `cp2start`/`cp2end` (a **second** charge period —
ha-spark's current driver only sets one window), and `chargestopsoc` (note
lowercase — ha-spark's driver docstring flags `chargeStopSOC` casing as
**unverified against the real services.yaml**, and this survey did not
independently confirm the exact casing either; still worth resolving before
relying on it). Per the integration's own issue history (#108, #138, #211)
users repeatedly report the service silently not taking effect or being hard
to disable — consistent with this being a coarse, infrequently-refreshed
cloud round-trip rather than an interface designed for tight control loops.

**Local Modbus — exists, but fragmented and hardware-dependent, not a single
official spec.** AlphaESS SMILE/STORION inverters expose Modbus over
RS485 (RJ45, pins 3/6, twisted A+/B−) and, on some SMILE models, Modbus-TCP;
coverage is inconsistent across the product line (some models RTU-only).
There is no single canonical open register map surfaced in this research —
multiple independent community efforts exist in parallel: a PyPI
`alphaess-modbus` package built from "a JSON definition file containing all
the ModBus registers," a separate register-list CSV/spreadsheet
circulating in the HA community forum thread ["Alpha ESS inverter and
battery data to Modbus without Cloud (locally)"](https://community.home-assistant.io/t/alpha-ess-inverter-and-battery-data-to-modbus-without-cloud-locally/755275),
and [dxoverdy/Alpha2MQTT](https://github.com/dxoverdy/Alpha2MQTT) (an ESP-based
RS485↔MQTT bridge, no-cloud). None of these carries AlphaESS's own
imprimatur the way Victron's register-list spreadsheet does (§5) — treat
local Modbus as a real but community-reverse-engineered option, useful as a
lower-latency alternative to the throttled cloud API, not as a documented
vendor contract.

## 5. Victron control surface

**Venus OS/GX devices expose the same internal D-Bus service tree over three
external transports** — Modbus-TCP, MQTT, and (indirectly) the VRM
cloud/VE.Bus — all fronting the same `com.victronenergy.*` D-Bus services
(`com.victronenergy.settings`, `com.victronenergy.vebus.*`,
`com.victronenergy.battery.*`, etc.) rather than three independent control
paths.

**Modbus-TCP is the officially sanctioned third-party control path.** The
[GX Modbus-TCP manual](https://www.victronenergy.com/live/ccgx:modbustcp_faq)
states plainly that Modbus-TCP is "an industry standard protocol, that can be
used to interface PLCs or other third party equipment," with the full
register list maintained as a spreadsheet,
[`CCGX-Modbus-TCP-register-list.xlsx`](https://raw.githubusercontent.com/victronenergy/dbus_modbustcp/master/CCGX-Modbus-TCP-register-list.xlsx)
in the `victronenergy/dbus_modbustcp` repo — i.e. the register list is
published and versioned by Victron itself, not reverse-engineered, which is
the single biggest maturity difference from AlphaESS's local-control story
(§4). Addressing uses a Modbus **Unit ID per logical device** on the GX
gateway (Unit-ID 100 recommended over 0, "many Modbus-TCP clients and PLCs do
not work with ID 0"); register ranges are organized by device class
(inverters ~3–60, solar chargers ~771–790, batteries ~259–319 per this
survey's excerpt of the manual).

**External control = ESS "Mode 3."** Per the
[ESS mode 2 and 3 doc](https://www.victronenergy.com/live/ess:ess_mode_2_and_3):
Mode 1 is stock ESS behavior; **Mode 2** layers custom logic (time-shifting,
load management) on top of the existing ESS control loop via grid-power
setpoint/charge-enable/inverter-enable, while Victron's own hardware still
runs the base control loop; **Mode 3** hands the control loop itself to the
external controller — "simple, remote controllable, bidirectional
inverter/chargers that can be set to either charge or discharge an x amount
of Watts," with control point explicitly at the AC input ("Power to/from
AC-input = Power to/from battery + Power to/from AC-output"). Setting
`Settings → ESS → Mode = External control` on the GX device switches into
Mode 3.

**The core primitive is `AcPowerSetpoint`** (register 37 for single-phase or
L1; registers 40/41 for L2/L3 on split/three-phase systems), range
±32767 W, positive = draw from grid, negative = feed to grid. **This
setpoint must be re-written at least once every 60 seconds or the Multi
falls back into Passthru mode** — a hard heartbeat/deadman requirement baked
into the hardware, not a config option. Companion registers 38
(`DisableCharge`) and 39 (`DisableFeedIn`) gate charge/feed-in independently
of the power setpoint; both set to 1 also forces Passthru.

**DVCC supersedes the older direct charge/discharge current registers.**
Registers 2701/2702 (older per-Multi charge/discharge current limits) "only
work when DVCC is disabled"; with DVCC enabled (the modern, recommended
setup — DVCC unifies current limiting across solar chargers and the
Multi/Inverter while respecting the connected BMS's own limits), the sanctioned
write points become register 2705 (system max charge current) and 2704
(system max discharge current) instead.

**HA integrations split cloud-modbus vs. local-MQTT, with community
consensus favoring MQTT for reliability.** The `hass-victron` custom
integration polls Modbus and multiple users report it needing periodic
restarts to keep receiving data; the emerging preferred pattern
([community thread](https://community.home-assistant.io/t/victron-venus-os-with-mqtt-sensors-switches-and-numbers/527931))
uses Venus OS's built-in MQTT broker (`dbus-flashmq`, TCP 1883/8883) with a
D-Bus→MQTT bridge publishing under `N/<SYSTEM_ID>/<service_type>/<device_instance>/<D-Bus_path>`
— i.e. the D-Bus tree re-exposed almost 1:1 over MQTT topics, giving a
lower-latency, no-restart-needed alternative to polled Modbus for read paths
(the write/control path for setpoints still goes through Modbus-TCP register
writes or an equivalent D-Bus/MQTT `W/` topic write, per the same topic
convention). `ha-victron-mqtt` ([tomer-w/ha-victron-mqtt](https://github.com/tomer-w/ha-victron-mqtt))
is a newer from-scratch MQTT-based HA integration reporting 400+ entities
surfaced this way.

**Net:** Victron's sanctioned external-control path is unambiguous and
vendor-documented — Modbus-TCP register 37 (+38/39) for the power setpoint,
2704/2705 for DVCC-aware current limits, ESS Mode 3 to hand over the loop —
with a real-time heartbeat constraint (60 s) that a driver **must** honor or
lose control silently to Passthru. This is a materially stronger, more
formal control contract than either AlphaESS surface (§4).

## 6. Synthesis: what a driver foundation needs

Ha-spark's current shape (`ha_spark/devices/base.py`) already has real bones
for this: a `Capability` `StrEnum` (`CHARGE_WINDOW`, `CHARGE_RATE`,
`STOP_DISCHARGE`) a `Device` `Protocol` (`apply`, `set_charge_rate`,
`read_charge_rate`, `planned_rate_w`), a `ControlAuthority` enum collapsed
with `proactive_mode` into one `effective_mode()` gate, and a `@register`
name→class lookup (`ha_spark/devices/registry.py`). Reading that against
Predbat/evcc/OpenEMS/Victron/AlphaESS above:

1. **Capabilities as an open set, discovered per-device — not a fixed
   enum everyone must satisfy.** Every source above (Predbat's `has_*`
   flags, evcc's optional interfaces, OpenEMS's `SymmetricEss` vs
   `ManagedSymmetricEss` split) treats "can this device do X" as something
   the driver declares, and callers check, rather than something the whole
   fleet is assumed to support. ha-spark's `Capability` enum + `frozenset`
   membership already does this correctly at the "does this device support
   `CHARGE_RATE`" level (`AlphaESSDevice.capabilities = frozenset({CHARGE_WINDOW})`
   vs `SolisDevice`'s three). The thing to add as more brands land is a
   capability for **primitive shape**, not just presence: Victron's native
   primitive is a power setpoint (W) with a **60-second write heartbeat**;
   Solis's is a current setpoint (A) with no heartbeat; AlphaESS's is a
   charge-window+SoC-target with no live rate at all. A driver foundation
   needs the capability declaration to carry *which* primitive family
   (window/target-SoC vs. live power vs. live current) and any timing
   contract (heartbeat interval, min hold time), not just a boolean.
2. **A capability a brand lacks must degrade, never crash the planner** —
   Predbat's dummy-entity synthesis and `mimic_target_soc()` emulation are
   exactly this; ha-spark's existing `AlphaESSDevice.set_charge_rate()`
   returning `"[SKIP] AlphaESS has no settable charge rate"` instead of
   raising is the same instinct already applied consistently. Keep doing
   that as the default answer to "what if a new brand can't do primitive
   X" rather than special-casing at call sites.
3. **Transport is a driver-internal detail, not a foundation-level type.**
   Predbat's REST→HA-entity→MQTT fallback chain, evcc's per-model YAML
   (Modbus register or REST or MQTT, chosen per template), and Victron's
   D-Bus surfaced identically over Modbus-TCP or MQTT all point the same
   way: the `Device` Protocol should stay transport-agnostic (as it already
   is — `HomeAssistantRest` is injected, not baked into the interface), and
   a future direct-Modbus or direct-cloud-API transport for AlphaESS/Victron
   should be a second constructor path into the *same* `Device` protocol,
   not a parallel interface. The one new requirement direct Modbus adds
   that HA-entity-mediated control doesn't have: **connection ownership** —
   Modbus is single-master (§3), so a Modbus-based driver must own or share
   a single poller/connection per physical bus, not open one per call.
4. **Units and sign conventions must be normalized at the driver boundary,
   not upstream.** Solis is current (A) at battery voltage; Victron's
   `AcPowerSetpoint` is signed watts at the AC input (positive = import,
   negative = export) with a materially different physical reference point
   (AC-input, not battery) than a DC charge-current primitive; AlphaESS's
   API is charge-window + SoC percent, no power/current unit at all. ha-spark
   already does exactly this conversion for Solis (`solis_current_a()`
   converts the planner's kWh/window target into A at `battery_voltage_v`);
   the same pattern — one conversion function per driver, planner stays in
   physical energy/SoC terms, never in a brand's native register unit —
   is the right template to extend to Victron's AC-input-referenced
   watts and any future brand.
5. **Read-back verification and per-action failure isolation are already
   the right invariants — the new requirement is a write-heartbeat
   primitive for brands like Victron that need one.** ha-spark's existing
   gate (`effective_mode` → `on`/`simulate`/`observe`/`off`, each write
   wrapped in its own `try`/`except` per `SolisDevice._set_current` and
   `AlphaESSDevice.apply`, `read_charge_rate` deliberately *not* catching so
   callers can isolate read failures) already matches what Predbat's
   poll-and-verify writes and Victron's mandatory Modbus round-trip both
   need. Victron's 60-second re-write-or-Passthru contract (§5) means a
   Victron driver's `apply()`/`set_charge_rate()` can't be a one-shot
   fire-and-forget the way Solis's is — it needs to either be called on a
   sub-60s cadence by whatever drives the planner loop, or the driver itself
   needs an internal keep-alive task, and either choice needs a capability
   flag (something like `write_heartbeat_s: 55`) so the foundation can tell
   drivers with this requirement apart from ones without it, and so a
   scheduling bug degrades to "control reverts to Passthru," not a crash.
6. **Capability discovery should stay declarative and inspectable**, not
   buried in `if` branches at call sites — ha-spark's `frozenset` +
   `Capability in capabilities` check is already the right level of
   ceremony for a project this size; Predbat's `INVERTER_DEF` dict and
   evcc's per-model YAML both show that *richer* per-brand declaration
   (exact register/entity names, time formats, unit conventions) belongs in
   per-driver config data, not in the shared `Capability`/`Device`
   abstraction itself — the shared layer should stay small and the brand
   detail should live where `ha_spark/devices/inverters/solis.py` and
   `alphaess.py` already put it, one file per brand.

**One honest caveat:** none of Predbat, evcc, or OpenEMS actually solves
"support a dozen+ brands" for free — Predbat's own release history
(`docs/research/predbat.md` §3) shows a large, continuing stream of
per-brand cloud-integration fixes even with its generic-class abstraction in
place, and evcc's ~150+ per-model YAML files represent a comparable amount of
brand-specific effort, just stored as data instead of code. A driver
foundation that gets the `Capability`/`Device`/unit-conversion/heartbeat
shape right removes *accidental* complexity (inconsistent gating, ad hoc
unit conversions, crashes on missing capabilities) but does not remove the
*essential* cost of onboarding each new brand's actual register map or API
quirks — that cost is real and roughly proportional to the number of brands
supported, in every project surveyed here.
