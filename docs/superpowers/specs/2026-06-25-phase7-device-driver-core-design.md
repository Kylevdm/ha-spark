# Phase 7 — Device-driver core — design

> Status: design approved, pre-implementation.
> Date: 2026-06-25.
> Implements ROADMAP v1.0 **Phase 7 — Device-driver core** (add-on 1.0.0 line;
> next shipped version is **0.14.0**).

## Context

ROADMAP Phase 7 is the foundation of the v1.0 "modular ecosystem": a `devices/`
driver layer, a `Capability` model, per-device `ControlAuthority`
(`observe | ha_spark | supplier`), and structured per-device config with a
migration shim off the flat 0.9.0 config — with **zero behaviour change for the
current Solis install**.

### What already exists (do not rebuild)

The **multi-inverter charge contract** (design `2026-06-21-multi-inverter-contract-design.md`)
is already implemented and on `master` — but shipped with **no CHANGELOG entry**
(doc drift this phase corrects):

- `ChargeIntent(target_soc_pct, soc_now, soc_valid, window_start, window_end, holds)`
  in `energy/models.py` — the unit-agnostic planner output.
- `Charger` Protocol in `energy/chargers.py` with the floor (`apply(intent)`) and
  the optional rate tier (`set_charge_rate`/`read_charge_rate`/`planned_rate_w` +
  `supports_live_rate: bool`).
- `SolisCharger` (full: window + DC-amps rate + stop-discharge) and
  `AlphaESSCharger` (floor: window + stop-SOC via `alphaess.setbatterycharge`).
- `charger_for(settings, rest)` — a dict-dispatch factory keyed on
  `settings.inverter`.
- Config: `inverter: solis|alphaess` selector, `charge_window_start_entity` /
  `charge_window_end_entity`, `alphaess_serial`, the AlphaESS preset, and
  `test_chargers.py`.

So the **drivers** third of Phase 7 exists in spirit. This phase **wraps and
relocates** it, and adds the three genuinely-missing pieces: the `devices/`
package + registry, the `Capability` model, and the `ControlAuthority` gate —
plus the structured config that ties devices to authority.

### What is missing today (verified by grep)

- **`ControlAuthority`** — nothing in the codebase. The CLAUDE.md actuation
  invariant ("real writes need `control == ha_spark` **and**
  `PROACTIVE_MODE == on`") is only **half-enforced**: writes gate on
  `PROACTIVE_MODE` alone. This phase adds the missing half.
- **`devices/` package + registry + `Capability`** — adapters live in
  `energy/chargers.py`; no registry, no capability type (only a `bool`).
- **Structured per-device config** — config is flat (entity IDs hang directly
  off `Settings`).

## Decisions (from brainstorming)

1. **Scope: full Phase 7** — `devices/` package, registry, `Capability`,
   `ControlAuthority`, and structured config + migration shim. (Chosen over a
   narrower authority-gate-only scope.)
2. **Config shape: `devices` list, dual-read shim.** A structured `devices:`
   list is the new canonical shape; when it is absent the loader synthesizes one
   device from the existing flat keys **in memory**. Existing installs keep
   working untouched; `options.json` is **never rewritten**. Both shapes are
   supported indefinitely. (Chosen over a one-time file rewrite and over a
   device map keyed by id.)
3. **Authority enforcement: single chokepoint.** One `effective_mode(control,
   proactive_mode)` helper that every driver write consults — not a check
   duplicated per adapter — so a new driver physically cannot write without
   passing the gate.
4. **`supplier` is reserved, observe-like this phase.** All three authority
   values ship, but with no supplier/EV integration yet, `supplier` means
   ha-spark never writes the device and plans *around* it (same no-write
   behaviour as `observe`), documented as "a third party controls this device."
   Its teeth (detecting/yielding to a supplier schedule) come in Phase 8/9.
5. **New-device default `control = ha_spark`**, so `PROACTIVE_MODE` stays the
   single meaningful trust gate (identical to today). Migrated flat installs are
   always `ha_spark` (zero behaviour change). `observe`/`supplier` are set
   explicitly.
6. **Agent surface: report-only.** `get_state` reports each device's `control`
   (read-only); no write tool for authority is added. Setting authority stays
   config-only.

## Architecture

### Module layout

Relocate the adapters out of `energy/chargers.py` into a `devices/` package:

```
devices/
  __init__.py        # public: get_device(), Capability, ControlAuthority, Device
  base.py            # Device Protocol, Capability enum, ControlAuthority enum, effective_mode()
  registry.py        # @register("solis") decorator + lookup dict
  inverters/
    solis.py         # SolisDevice (was SolisCharger) — full caps
    alphaess.py      # AlphaESSDevice (was AlphaESSCharger) — floor caps
```

- `ChargeIntent` **stays in `energy/models.py`** — it is the planner's output
  type; moving it is needless cross-module churn. Devices consume it.
- `energy/chargers.py` becomes a thin re-export shim (`from ha_spark.devices
  import ...`) for one release so nothing breaks mid-refactor, then is deleted.

### Capability model (`devices/base.py`)

Replace the lone `supports_live_rate: bool` with a capability set each driver
advertises:

```python
class Capability(StrEnum):
    CHARGE_WINDOW  = "charge_window"   # write window + target SOC (floor)
    CHARGE_RATE    = "charge_rate"     # settable live charge power (W) — the rate tier
    STOP_DISCHARGE = "stop_discharge"  # hold/stop-discharge during a dispatch
```

- Solis advertises `{CHARGE_WINDOW, CHARGE_RATE, STOP_DISCHARGE}`; AlphaESS
  advertises `{CHARGE_WINDOW}`.
- `supply_guard.py` checks `Capability.CHARGE_RATE in device.capabilities`
  instead of `device.supports_live_rate` (`supports_live_rate` becomes a derived
  convenience or is dropped).
- The set is general enough that Phase 9/11 device types (EV charger, heat pump)
  declare their own capabilities without touching the inverter drivers.

### ControlAuthority + the gate (security core)

```python
class ControlAuthority(StrEnum):
    OBSERVE  = "observe"    # never write; read & plan around the device
    HA_SPARK = "ha_spark"   # ha-spark may write, still PROACTIVE_MODE-gated
    SUPPLIER = "supplier"   # reserved; behaves like OBSERVE this phase
```

Single chokepoint — one helper, consulted by every driver write:

```python
def effective_mode(control: ControlAuthority, proactive_mode: str) -> str:
    """Collapse (authority, proactive_mode) to off|simulate|on.

    The CLAUDE.md invariant in one place: a real write requires
    control == ha_spark AND proactive_mode == on. Anything else -> "off"
    (compute/log only, never actuate).
    """
    return proactive_mode if control == ControlAuthority.HA_SPARK else "off"
```

Adapters already branch on `simulate | off | on`; they now compute that mode via
`effective_mode(device.control, settings.proactive_mode)` instead of reading
`settings.proactive_mode` directly. Consequences:

- The invariant is enforced in **exactly one place**; a new driver cannot bypass
  it (it has no other route to a write mode).
- Logs distinguish `[OBSERVE]` (suppressed because authority isn't `ha_spark`)
  from `[OFF]`/`[SIMULATE]` (suppressed by `PROACTIVE_MODE`), so the *reason* a
  write was suppressed stays auditable.
- The existing SoC-validity block, read-back verification, and per-write failure
  isolation are unchanged — they live below the gate.

### Structured config + dual-read shim (`config.py`)

```python
class DeviceConfig(BaseModel):
    id: str
    type: Literal["inverter"]
    driver: str
    control: ControlAuthority = ControlAuthority.HA_SPARK
    entities: dict[str, str] = {}
```

`load_settings` performs the dual read:

- If `options.json` has `devices`, parse it into `list[DeviceConfig]`.
- Else **synthesize** in memory:
  `[DeviceConfig(id="main_inverter", type="inverter", driver=settings.inverter,
  control="ha_spark", entities={"charge_current": charge_current_entity,
  "window_start": charge_window_start_entity, "window_end":
  charge_window_end_entity, "power_switch": inverter_power_switch_entity,
  "alphaess_serial": alphaess_serial})]`.

The flat keys remain in `_OPTION_KEYS` (legacy, still honoured); `devices` is
**added** to `_OPTION_KEYS` and to a `devices:` block in `config.yaml`
`options`/`schema`, keeping the `test_config.py` sync test green. `options.json`
is never rewritten.

Battery/site params (`battery_capacity_kwh`, `battery_voltage_v`, tariff knobs,
`max_charge_current_a`, `charge_efficiency`, etc.) **stay top-level** — they are
not per-inverter and the planner reads them directly. Only the inverter's
`driver` / `control` / `entities` move into the device.

### Rewiring

- `charger_for(settings, rest)` → `get_device(device_config, settings, rest)`,
  resolved through the registry (`registry.lookup(device_config.driver)`).
- `scheduler.py` (`run_once`, `guard_tick`) and `supply_guard.py` resolve the
  inverter device from `settings.devices[0]` instead of hardcoding
  `SolisCharger`/`charger_for`.
- `planner.py` / `sources.py` are unchanged — the planner still emits
  `ChargeIntent`; it has no knowledge of drivers or authority.

### Data flow (unchanged shape)

REST seeds inputs → planner computes deficit → planner emits
`ChargeIntent(target_soc, window, holds)` → `get_device(devices[0])` applies it,
gated by `effective_mode(device.control, proactive_mode)` → supply-guard tick
(only if the device advertises `CHARGE_RATE`) throttles the rate within the
window.

## Error handling & security

- `effective_mode` is the one authority chokepoint; `control != ha_spark` can
  never reach a real `call_service`, independent of `PROACTIVE_MODE`.
- SoC-unreadable guard (`intent.soc_valid`) stays — a dead SoC sensor must never
  command a max charge.
- Per-write failure isolation + read-back verification carry over to every
  driver: a failed write logs `[FAILED]` and returns without aborting the rest
  of the plan.
- Supply-guard reads that fail skip the tick (never throttle on bad data).
- No secret enters any device config, log line, or agent output (CLAUDE.md). The
  `devices` config holds only entity IDs and enum values.
- All structured config is validated by pydantic before use; a bad `devices`
  payload degrades to a `ConfigError` (fail fast) — it never actuates.

## Testing

- **Characterization (relocate, do not rewrite):** `test_chargers.py` — the
  Solis charge-current setpoint output stays bit-identical after the move into
  `devices/inverters/solis.py`.
- **Authority gate (new, security-critical):** `control = observe` (and
  `supplier`) with `PROACTIVE_MODE = on` ⇒ **no** `call_service`; `control =
  ha_spark` with `on` ⇒ writes. Asserts the missing invariant half.
- **Dual-read shim:** a flat-only `options.json` synthesizes the expected
  `main_inverter` device; an explicit `devices:` list parses through; both reach
  the same actuation path.
- **Capability gating:** supply guard dormant (no writes) when `CHARGE_RATE` is
  absent (AlphaESS); active for Solis.
- **Config sync:** `test_config.py` stays green with `devices` added to
  `_OPTION_KEYS` and `config.yaml`.
- Quality gates: `ruff check .`, `mypy ha_spark`, `pytest -q` all green.

## Packaging (per repo conventions)

Next version is **0.14.0** (latest shipped tag `v0.12.0`; `0.13.0` agent surface
is in `config.yaml` but not yet tagged — release it independently). Bump
`config.yaml` `version`, add a `CHANGELOG.md` entry for Phase 7 **and a
back-note that the multi-inverter contract landed without one**, update
`DOCS.md` (the `devices:` block + `control` authority + how to add a second
inverter), keep `config.yaml` `options`/`schema` and `config.py` `_OPTION_KEYS`
in sync (enforced by `test_config.py`), and create a matching annotated
`v0.14.0` tag + GitHub release so the add-on image can build.

## Verification (end-to-end)

1. Start the add-on on the existing flat `options.json`; confirm the synthesized
   `main_inverter` device drives the Solis exactly as before (plan output and
   setpoints identical), with no config change required.
2. Set `proactive_mode: on` but `devices[0].control: observe`; confirm the plan
   computes and logs `[OBSERVE]` but performs **no** `call_service`.
3. Set `control: ha_spark` + `proactive_mode: on`; confirm real writes resume.
4. Add a second `devices:` entry (`driver: alphaess`); confirm it is selected and
   the supply guard stays dormant for it (no `CHARGE_RATE`).
5. `get_state` over the agent surface reports each device's `control` (read-only)
   and exposes no authority write tool.

## Out of scope (YAGNI / later phases)

- Dynamic window selection (planner choosing the cheapest tariff slot) — Phase 8;
  calls the same window seam.
- EV charger / heat-pump device **types** — framework is ready, drivers ship in
  Phases 9 / 11.
- Native Modbus control bypassing HA integrations — no concrete gap.
- `supplier` teeth (detecting/yielding to a supplier schedule) — Phase 8/9.
- Agent write-control of authority — config-only this phase.
