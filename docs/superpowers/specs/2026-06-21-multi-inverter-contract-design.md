# Multi-inverter charge contract — design

**Date:** 2026-06-21
**Status:** approved (brainstorming), pending spec review
**Scope:** generalize ha-spark's Solis-only charge control into a small inverter
contract so other inverters (first: AlphaESS) can be driven through the same
planner, without changing Solis behavior.

## Problem

ha-spark controls exactly one inverter (Solis via the solax-modbus HA
integration). The coupling is concentrated but real:

- `ChargeAction.kind` is Solis-shaped: `set_charge_current` (DC **amps**) and
  `stop_discharge`.
- `SolisCharger._execute` maps amps → `number.set_value` on
  `charge_current_entity`, and stop → `select.select_option "Off"` on
  `inverter_power_switch_entity`.
- The planner itself emits **DC amps** (`overnight_current_a`), so a
  Solis-specific encoding leaks all the way up into planning.
- `supply_guard.py` hardcodes `SolisCharger`, reasons in DC amps, and assumes a
  continuously settable current setpoint exists.

Other inverters don't speak DC amps:

- **AlphaESS** (CharlesGillanders integration, cloud OpenAPI + optional local
  sensor polling): control is `alphaess.setbatterycharge` — charge **window(s)
  + "charging stop SOC"**. No settable amps/watts. Local mode adds *faster
  sensors*, not new control.
- **Sunsynk/Deye**: current-based (A), closest to Solis.
- **Victron**: power (W) / DVCC max-charge-current.

This matches how Predbat (and the category generally) works: the portable
control primitives are **(charge window) + (target SOC %)**, with a settable
**rate** as a less-common, more advanced capability.

### Net-new for Solis too

There are currently **no window-time entities** — ha-spark only modulates
current *within* a window the user configured manually on the Solis
(`charge_window_start/end` are config the planner reads, never written to the
inverter). Writing the window is new for every adapter, including Solis.

## Licensing note (clean-room)

Predbat (springfall2008/batpred) is under a **custom proprietary, non-commercial,
UK-only** license that forbids distribution outside its repo. ha-spark is public
and ships as an add-on. Therefore:

- **Do not copy** Predbat code, its `apps.yaml` per-inverter templates, or its
  register maps.
- We take only the **architecture idea** (window + target-SOC + optional rate),
  which is the category norm and not copyrightable expression.
- Entity mappings derive from **each inverter's own HA integration**
  (solax-modbus, AlphaESS), never from Predbat.

## Decisions (from brainstorming)

1. **Floor capability = `(window + target SOC %)`**, not settable rate. Rate is
   an optional tier. (Predbat-shaped; makes AlphaESS work today.)
2. **Window = pass-through**: the planner passes the user's configured
   `charge_window_start/end` to the adapter, which writes them to the inverter's
   window entities. Adapters without window entities no-op it. The planner does
   **not** dynamically choose the optimal window — that is a separate later phase
   that calls the same seam.
3. **Canonical rate unit = power (W)**, not amps. Physical quantity that every
   inverter maps to; Solis converts W↔A via `battery_voltage_v`. Amps stop
   leaking into the planner.
4. **Battery charge current is DC, not AC.** Charging at 62.5 A DC @ ~52 V ≈
   ~3.25 kW ≈ ~14 A AC at 230 V — not 62.5 A. The supply guard already converts
   (`battery_ac_a = setpoint_a * battery_voltage_v / supply_voltage_v`); the
   W-based contract must preserve that W→AC-amps conversion for the fuse limit.
5. **Solis stays bit-for-bit identical.** Planner emits `ChargeIntent`; the Solis
   adapter re-derives amps from it; a characterization test pins the current
   Solis setpoint output so any drift fails loudly.

## Architecture

### Charge-intent contract (the abstraction)

The planner stops emitting amps and emits a unit-agnostic intent:

```
ChargeIntent(target_soc_pct, window_start, window_end)
```

Each adapter realizes "reach `target_soc_pct` by `window_end`" via its native
mechanism:

- **Solis** → write window entities + size a DC charge current to deliver the
  required kWh over the window (today's behavior, driven from SOC→kWh).
- **AlphaESS** → write window + stop-SOC via `alphaess.setbatterycharge`.

Optional **rate tier** (for the supply guard only):

```
supports_live_rate: bool
set_charge_rate(watts)
read_charge_rate() -> watts
```

- Solis: `supports_live_rate = True`.
- AlphaESS-cloud: `False`.
- The supply guard engages **only** when `supports_live_rate`; otherwise it stays
  dormant (the inverter still charges, just isn't throttled against the fuse).

### Module changes

- **`chargers.py`** — `Charger` Protocol gains `apply(intent)` + the optional
  rate methods. `SolisCharger` implements all (rate tier included). New
  `AlphaESSCharger` implements the floor only. Add a tiny
  `charger_for(settings, rest) -> Charger` factory: one dict-dispatch keyed on
  `settings.inverter` (not a plugin registry — two inverters don't need it).
- **`models.py`** — add `ChargeIntent`. `ChargeAction`/amps become an internal
  detail of `SolisCharger`, no longer a cross-module type. `ChargePlan` carries
  a `ChargeIntent`.
- **`planner.py` / `sources.py`** — compute `target_soc_pct` from the kWh deficit
  and battery capacity (`target_soc = soc_now + needed_kwh/capacity_kwh`, clamped
  to `target_soc_cap`). Emit `ChargeIntent` instead of amps.
- **`config.py` / `presets.py`** — add `inverter: "solis" | "alphaess"` selector;
  add window-entity fields (`charge_window_start_entity`, `charge_window_end_entity`)
  to profiles; add the AlphaESS preset (control via `alphaess.setbatterycharge`;
  sensors local-or-cloud). No new battery field needed: `battery_capacity_kwh`,
  `target_soc_cap`, and `battery_voltage_v` already exist for the SOC↔kWh↔amps
  conversions.
- **`supply_guard.py`** — gate on `charger.supports_live_rate`; stop hardcoding
  `SolisCharger`; talk to the adapter in W; convert W→AC amps for the fuse limit.
- **`scheduler.py`** — use `charger_for(...)` instead of `SolisCharger(...)`
  directly in `run_once` and `guard_tick`.

### Data flow (unchanged shape)

REST seeds inputs → planner computes deficit → planner emits
`ChargeIntent(target_soc, window)` → `charger_for(settings)` applies it (real or
simulated per `PROACTIVE_MODE`) → supply guard tick (only if `supports_live_rate`)
throttles the rate within the window.

## Error handling

- Per-action failure isolation and read-back verification (already in
  `SolisCharger._execute`) carry over to every adapter: a failed write logs and
  returns a `[FAILED]` line without aborting the rest of the plan.
- `PROACTIVE_MODE` gating (`off`/`simulate`/`on`) stays in the adapter layer,
  unchanged.
- SoC-unreadable guard (`plan.soc_valid`) stays — a dead SoC sensor must never
  command a max charge.
- Supply guard reads that fail skip the tick (never throttle on bad data),
  unchanged.

## Testing

- **Characterization test** (write first): assert the Solis adapter produces the
  same charge-current setpoint as today's code for a representative
  deficit/SoC/voltage case. Locks current behavior before the refactor.
- **AlphaESS adapter test**: mock `alphaess.setbatterycharge` with respx; assert
  it writes the right window + stop-SOC for a given `ChargeIntent`.
- **Supply-guard gating test**: assert the guard stays dormant (no writes) when
  the active charger reports `supports_live_rate = False`, and still throttles
  for Solis.
- **Planner SOC test**: assert `target_soc_pct` is computed correctly from deficit
  + capacity and clamped to `target_soc_cap`.

## Shipping scope (this phase)

1. Generalize the contract + refactor Solis into an adapter — **zero behavior
   change**, characterization-tested. Verifiable on current hardware.
2. Add the AlphaESS adapter (floor: window + stop-SOC) + preset. Tester: a
   friend's AlphaESS. If their entity list reveals a settable rate, AlphaESS
   graduates to the rate tier and gains the supply guard.
3. Sunsynk/Victron: **documented stubs only**, not shipped, until a real tester
   appears. The contract makes them cheap to add later.

## Out of scope (YAGNI / later phases)

- Dynamic window selection (planner choosing the cheapest tariff slot) — separate
  phase, calls the same `set_charge_window` seam.
- Native Modbus control bypassing HA integrations — no concrete gap justifies it;
  rejected during brainstorming.
- A generic plugin/registry system for inverters — two adapters use a dict.
