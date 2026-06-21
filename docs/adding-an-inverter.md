# Adding an inverter adapter

`ha-spark` drives any inverter through one small adapter contract
(`ha_spark/energy/chargers.py`). The planner never talks to the device
directly — it builds an inverter-agnostic `ChargeIntent`, hands it to whichever
adapter `settings.inverter` selects, and the adapter realizes the intent as
real (or simulated/logged) HA service calls and entity writes.

This doc covers the contract, how to register a new adapter, the preset
pattern for onboarding, and the rate-tier rule that decides whether an
inverter gets the live supply guard. It ends with two worked sketches —
Sunsynk/Deye and Victron — that are **not shipped**: they're a starting point
for a contributor with that hardware to build and test against, not code you
can flip on today.

## The `Charger` contract

```python
class Charger(Protocol):
    supports_live_rate: bool

    async def apply(self, intent: ChargeIntent) -> list[str]: ...
    async def set_charge_rate(self, watts: float) -> str: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...
```

- **`apply(intent)`** — realize the full intent: write the charge window,
  size and set the charge rate (or stop-SOC for floor adapters), apply any
  discharge holds. Returns a list of human-readable action lines
  (`"[APPLIED] ..."`, `"[SIMULATE] would ..."`, `"[FAILED] ...: ..."`,
  `"[BLOCKED] ..."`) — these are logged and surfaced to the user, not just
  for debugging.
- **`supports_live_rate`** — `True` if the inverter exposes a settable charge
  rate the planner can read back and throttle in real time; `False` if the
  inverter only takes a window + target SOC and self-regulates the rate
  internally. See "Rate tier" below — this flag gates the live supply guard.
- **`set_charge_rate(watts)`** — set the live charge rate in **watts**
  (convert to the inverter's native unit, e.g. DC amps, internally). Used by
  the supply guard to throttle mid-window. Adapters that don't support a live
  rate should return a `"[SKIP] ..."` line and do nothing.
- **`read_charge_rate()`** — read back the current charge rate in **watts**.
  Does not catch exceptions itself — callers (the supply guard) isolate read
  failures and skip that tick rather than crash the loop.
- **`planned_rate_w(intent)`** — the rate (W) the adapter *intends* to charge
  at for this intent, before any live throttling. Floor adapters that don't
  control rate return `0.0` (the inverter self-regulates to the SOC target).

All rates that cross the `Charger` boundary are in **watts**, even though an
adapter's native control might be DC amps (Solis) or a power switch (no rate
at all). Converting at the adapter boundary keeps the supply guard and planner
unit-agnostic — see `ha_spark/config.py` (`battery_voltage_v`) for the
DC-amps-to-watts conversion Solis uses; don't compare a battery's DC current
directly against an AC supply limit without converting through voltage.

`ChargeIntent` is the inverter-agnostic command the planner emits — "reach
`target_soc_pct` by `window_end`, currently at `soc_now`" — defined in
`ha_spark/energy/models.py`:

```python
ChargeIntent(target_soc_pct, soc_now, window_start, window_end, holds)
```

`holds` is a list of `(start, end)` dispatch windows during which the adapter
should stop discharge (e.g. Octopus Intelligent dispatch slots). Adapters
realize the intent however their hardware/integration needs to; the planner
never reaches past the `Charger` interface into entity IDs or services.

## Registering a new adapter

1. Implement the `Charger` protocol as a class (constructor takes
   `(settings: Settings, rest: HomeAssistantRest)`, matching `SolisCharger`
   and `AlphaESSCharger`).
2. Add a new `Literal` value to `Settings.inverter` in `ha_spark/config.py`
   (currently `Literal["solis", "alphaess"]`).
3. Add any new config fields the adapter needs (entity IDs, service params,
   serials) — blank-string defaults, documented with a comment, matching the
   pattern of `charge_current_entity`, `charge_window_start_entity`,
   `alphaess_serial`.
4. Register the class in `charger_for`'s dict dispatch
   (`ha_spark/energy/chargers.py`):

   ```python
   def charger_for(settings: Settings, rest: HomeAssistantRest) -> Charger:
       chargers: dict[str, Callable[[Settings, HomeAssistantRest], Charger]] = {
           "solis": SolisCharger,
           "alphaess": AlphaESSCharger,
           "your_inverter": YourCharger,
       }
       return chargers[settings.inverter](settings, rest)
   ```

5. Add a preset (next section) so onboarding can offer a complete entity map
   for your hardware.
6. Write a characterization test exercising `apply()` against a faked
   `HomeAssistantRest` (mock the service calls + state reads) the way the
   Solis adapter's tests do, covering: applied happy path, read-back
   mismatch warning, write failure, and the SoC-unreadable guard.

## The preset pattern

`ha_spark/presets.py` holds one `config_field -> entity_id` dict per known
hardware combination (`SOLIS`, `ALPHAESS`). The onboarding wizard
(`ha-spark onboard`) uses presets to fill fields its keyword-based
auto-discovery can't confidently match, so a user on a supported setup gets a
complete proposal even when entity names don't carry an obvious keyword.

Two shapes show up in the existing presets:

- **Entity-controlled** (Solis): the preset maps straight to entity IDs —
  `number.solisac_timed_charge_current`, `select.solisac_power_switch`, etc.
  — that the adapter reads/writes directly via `rest.call_service` /
  `rest.get_state`.
- **Service-controlled** (AlphaESS): control is a single service call
  (`alphaess.setbatterycharge`) carrying the window and stop-SOC as
  parameters, not entities. The preset still needs to map the *sensor*
  entities (SoC, battery voltage) the planner reads, but the *write* path is
  a service call with a config field (`alphaess_serial`) rather than an
  entity ID.

Add a new `dict[str, str]` constant for your inverter, register it in
`PRESETS`, and document in a comment where its sensors come from (the
integration name) and whether control is entity- or service-based.

## Rate tier: who gets the live supply guard

The supply guard throttles battery charging in real time when whole-house AC
draw approaches the supply limit. It only runs for adapters where
`supports_live_rate = True` — i.e. the inverter exposes a charge rate the
adapter can read back and re-set mid-window via `set_charge_rate` /
`read_charge_rate`, both in watts.

- **`SolisCharger`** (`supports_live_rate = True`): the charge rate is a
  settable DC current (`number.solisac_timed_charge_current`), converted
  to/from watts via `battery_voltage_v`. The supply guard can read the
  current rate, compare whole-house draw against `supply_max_current_a`, and
  throttle by calling `set_charge_rate` with a lower wattage.
- **`AlphaESSCharger`** (`supports_live_rate = False`): control is window +
  stop-SOC only (`alphaess.setbatterycharge`); the inverter's own firmware
  decides the rate. There is nothing to throttle, so `set_charge_rate` is a
  no-op (`"[SKIP] AlphaESS has no settable charge rate"`) and
  `planned_rate_w` returns `0.0`. The supply guard stays dormant for this
  inverter.

When adding an adapter, ask: **can a user set a numeric charge rate (current
or power) on this inverter from Home Assistant, and read it back?** If yes,
it's a rate-tier adapter like Solis — implement `set_charge_rate` /
`read_charge_rate` for real and set `supports_live_rate = True`. If the
inverter only takes a window/target-SOC and self-regulates, it's a floor
adapter like AlphaESS — `supports_live_rate = False`, and don't pretend to
support a rate you can't actually set.

## Worked sketches (NOT shipped — stubs only)

These two are **not implemented**. They sketch what a rate-tier adapter would
look like for two inverter families that are current/power-tier like Solis,
to save the next contributor the discovery work. Each needs a real device to
test against (entity names, service schemas, and unit conversions vary by
integration version) before it ships — treat every entity ID and service name
below as a placeholder to verify, not a confirmed mapping.

### Sunsynk / Deye (current-based, rate tier)

Sunsynk and Deye inverters are commonly exposed to Home Assistant via the
[`kellerza/sunsynk`](https://github.com/kellerza/sunsynk) integration (MQTT or
the official cloud API), or via the
[Solar Assistant](https://solar-assistant.io/) integration for a wider range
of Deye/Sunsynk/Growatt hybrids. Both expose battery charge current as a
settable `number` entity, similar in shape to Solis:

- Likely fields: a SoC sensor, a battery-voltage sensor, a settable
  **max battery charge current** `number` entity (A, DC — same
  voltage-conversion concern as Solis), and a grid-charge enable/time-window
  control (either entities or a service, depending on integration version).
- Adapter shape: `SunsynkCharger` with `supports_live_rate = True`, mirroring
  `SolisCharger._set_current` / `_write_window` — convert watts to DC amps
  using the adapter's own `battery_voltage_v` read (or sensor), write the
  current `number`, write the window entities/service, read back to confirm.
- Config additions to sketch: `inverter: Literal[..., "sunsynk"]`,
  `sunsynk_charge_current_entity`, plus whatever window control the chosen
  integration exposes.
- A `SUNSYNK` preset in `presets.py` mapping the integration's actual entity
  IDs once confirmed against a real install.

### Victron (power/DVCC, rate tier)

Victron systems are exposed via **Venus OS** — either the native
[`victron`](https://github.com/home-assistant/core) / community Victron MQTT
integrations (Venus OS publishes over MQTT, consumed by an MQTT-based HA
integration), or the BLE-only [`victron_ble`](https://www.home-assistant.io/integrations/victron_ble/)
integration for systems without a GX device. DVCC (Distributed Voltage and
Current Control) is Victron's mechanism for capping charge current/power
system-wide:

- Likely fields: a SoC sensor, a battery-voltage sensor, and a **DVCC max
  charge current** (A) or **max charge power** (W) control — Victron's native
  unit varies by entity (some integrations expose power directly in W, which
  would skip the voltage conversion Solis/Sunsynk need).
- Adapter shape: `VictronCharger` with `supports_live_rate = True`. If the
  exposed entity is already in watts, `set_charge_rate`/`read_charge_rate`
  are a near-identity pass-through instead of an amps conversion — check the
  integration's entity carefully before assuming amps.
- Config additions to sketch: `inverter: Literal[..., "victron"]`,
  `victron_max_charge_current_entity` (or `_power_entity`), and window control
  (Venus OS scheduled charging, if used, vs. always-on DVCC cap).
- A `VICTRON` preset in `presets.py` once the real entity IDs are confirmed.

## Clean-room note

Derive every entity/service mapping for a new adapter **from that inverter's
own Home Assistant integration** (its `services.yaml`, entity registry, or
integration source/docs) — never from Predbat. Predbat ships under a
proprietary, non-commercial licence; do not copy its code, its `apps.yaml`
templates, or its inverter-specific config snippets when building an
ha-spark adapter, even as a reference for field names. If you've used Predbat
with a given inverter, treat that experience only as a hint about which HA
integration to look up — go read that integration's own documentation/source
for the actual mapping.
