# Phase 7 — Device-driver core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap and relocate the already-shipped multi-inverter charge contract into a `devices/` driver package with a registry, a `Capability` model, and a per-device `ControlAuthority` gate, plus structured `devices` config with a dual-read shim off the flat config — with zero behaviour change for the current Solis install.

**Architecture:** Adapters move from `energy/chargers.py` into `devices/inverters/*`. A single `effective_mode(control, proactive_mode)` chokepoint enforces the actuation invariant (real write ⇔ `control == ha_spark` **and** `PROACTIVE_MODE == on`). Config grows a canonical `devices:` list; when absent, a pydantic after-validator synthesizes one inverter device from the flat keys in memory (the file is never rewritten). `energy/chargers.py` becomes a re-export shim for one release.

**Tech Stack:** Python 3.11+, asyncio, pydantic v2 / pydantic-settings, httpx, respx (tests), pytest (`asyncio_mode = "auto"`), ruff, mypy `strict = true`.

**Spec:** `docs/superpowers/specs/2026-06-25-phase7-device-driver-core-design.md`

## Per-task model (token budget)

Most tasks are mechanical and run on **Sonnet**. **Task 3 is judgment-heavy (config shim + schema + test-parser fix) — run it on Opus.** Each task header repeats its model. Switch with `/model-switch` between tasks as noted. Final switch-back note at the end.

## Global Constraints

- **Security is top priority (CLAUDE.md).** Never log/echo secrets. No secret enters device config, logs, or agent output — `devices` holds only entity IDs + enum values.
- **Actuation invariant:** a real write requires `control == ha_spark` **and** `proactive_mode == on`; never on invalid SoC; always read-back verified; failures isolated per write. Do not weaken.
- **The LLM never controls hardware.** Planner stays the sole decider; the planner is unchanged by this phase.
- **Strict typing:** `mypy ha_spark` with `strict = true` must stay clean. pydantic mypy plugin enabled.
- **Tests required per module:** mock HTTP with `respx`; pytest `asyncio_mode = "auto"` (no `@pytest.mark.asyncio`). ruff lints `E,F,I,UP,B,ASYNC,W`, line length 100.
- **Quality gates (all green before each commit):** `ruff check .`, `mypy ha_spark`, `pytest -q`.
- **Import discipline (avoid cycles):** `config.py` imports **only** `ha_spark.devices.base` (for `ControlAuthority`), never `ha_spark.devices` (the package `__init__`). `devices/base.py` imports `ChargeIntent` under `TYPE_CHECKING` only. `energy/models.py` must not import `config`.
- **Zero behaviour change** for the existing flat-config Solis install: the relocated adapters must produce byte-identical actuation; the existing `test_chargers.py` assertion *values* are preserved (only import paths / constructor calls / capability checks change).
- Next shipped add-on version is **0.14.0**; every shipped `config.yaml` version needs a matching annotated `vX.Y.Z` tag.

## File Structure

- Create `ha_spark/devices/__init__.py` — public surface: `get_device`, `inverter_device`, re-exports `Capability`, `ControlAuthority`, `Device`.
- Create `ha_spark/devices/base.py` — `Capability`, `ControlAuthority`, `effective_mode`, `Device` Protocol.
- Create `ha_spark/devices/registry.py` — `register` decorator + `lookup`.
- Create `ha_spark/devices/inverters/__init__.py` — empty package marker.
- Create `ha_spark/devices/inverters/solis.py` — `SolisDevice` (+ `solis_current_a`), full capabilities.
- Create `ha_spark/devices/inverters/alphaess.py` — `AlphaESSDevice`, floor capability.
- Modify `ha_spark/config.py` — `DeviceConfig` model, `devices` field + synthesis validator, `_OPTION_KEYS += {"devices"}`.
- Modify `ha_spark/energy/chargers.py` — becomes a thin re-export shim.
- Modify `ha_spark/energy/scheduler.py` — resolve the inverter via `inverter_device()`; capability check replaces `supports_live_rate`.
- Modify `ha_spark/energy/supply_guard.py` — resolve via `inverter_device()`.
- Modify `ha_spark/agent/tools.py` — `get_state` reports each device's `control` (read-only).
- Modify `ha_spark_addon/config.yaml` — `version: 0.14.0`, `devices` option + schema.
- Modify `ha_spark_addon/CHANGELOG.md`, `ha_spark_addon/DOCS.md`.
- Tests: `tests/test_devices.py` (new), `tests/test_chargers.py` (update imports/ctors), `tests/test_config.py` (sync-parser fix + shim tests), `tests/test_supply_guard.py` (capability gating), `tests/test_agent_routes.py` (control reported).

---

### Task 1: Capability, ControlAuthority, and the effective_mode gate

**Model:** Sonnet. (Exact code below. **Security-critical — review the truth table carefully.**)

**Files:**
- Create: `ha_spark/devices/__init__.py` (empty for now)
- Create: `ha_spark/devices/base.py`
- Test: `tests/test_devices.py`

**Interfaces:**
- Produces: `Capability(StrEnum){CHARGE_WINDOW, CHARGE_RATE, STOP_DISCHARGE}`; `ControlAuthority(StrEnum){OBSERVE, HA_SPARK, SUPPLIER}`; `effective_mode(control: ControlAuthority, proactive_mode: str) -> str` returning one of `"off"|"simulate"|"on"|"observe"`; `Device` Protocol with `capabilities: frozenset[Capability]`, `async apply(intent) -> list[str]`, `async set_charge_rate(watts: float) -> str`, `async read_charge_rate() -> float`, `planned_rate_w(intent) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_devices.py
from ha_spark.devices.base import Capability, ControlAuthority, effective_mode


def test_effective_mode_only_ha_spark_passes_proactive_through():
    # ha_spark authority: proactive_mode passes through unchanged.
    assert effective_mode(ControlAuthority.HA_SPARK, "on") == "on"
    assert effective_mode(ControlAuthority.HA_SPARK, "simulate") == "simulate"
    assert effective_mode(ControlAuthority.HA_SPARK, "off") == "off"


def test_effective_mode_non_ha_spark_never_writes():
    # observe and supplier collapse to a no-write "observe", even with on.
    assert effective_mode(ControlAuthority.OBSERVE, "on") == "observe"
    assert effective_mode(ControlAuthority.SUPPLIER, "on") == "observe"
    assert effective_mode(ControlAuthority.OBSERVE, "simulate") == "observe"


def test_capability_and_authority_values():
    assert Capability.CHARGE_RATE == "charge_rate"
    assert set(ControlAuthority) == {
        ControlAuthority.OBSERVE,
        ControlAuthority.HA_SPARK,
        ControlAuthority.SUPPLIER,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_devices.py -v`
Expected: FAIL with `ModuleNotFoundError: ha_spark.devices.base`.

- [ ] **Step 3: Write minimal implementation**

```python
# ha_spark/devices/__init__.py
"""Device-driver core (Phase 7)."""
```

```python
# ha_spark/devices/base.py
"""Device-driver core: capabilities, control authority, and the actuation gate."""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid an import cycle: models never imports config/devices at runtime
    from ha_spark.energy.models import ChargeIntent


class Capability(StrEnum):
    CHARGE_WINDOW = "charge_window"   # write window + target SOC (floor)
    CHARGE_RATE = "charge_rate"       # settable live charge power (W) — rate tier
    STOP_DISCHARGE = "stop_discharge" # hold/stop-discharge during a dispatch


class ControlAuthority(StrEnum):
    OBSERVE = "observe"     # never write; read & plan around the device
    HA_SPARK = "ha_spark"   # ha-spark may write, still PROACTIVE_MODE-gated
    SUPPLIER = "supplier"   # reserved; behaves like OBSERVE this phase


def effective_mode(control: ControlAuthority, proactive_mode: str) -> str:
    """Collapse (authority, proactive_mode) -> off|simulate|on|observe.

    The CLAUDE.md actuation invariant in one place: a real write ("on") requires
    control == ha_spark AND proactive_mode == on. Any other authority returns
    "observe" (compute/log only, never actuate), regardless of proactive_mode.
    "observe" is kept distinct from the user's "off" so logs show *why* a write
    was suppressed.
    """
    if control != ControlAuthority.HA_SPARK:
        return "observe"
    return proactive_mode


@runtime_checkable
class Device(Protocol):
    """Realizes a ChargeIntent via a specific inverter; returns action lines."""

    capabilities: frozenset[Capability]

    async def apply(self, intent: ChargeIntent) -> list[str]: ...
    async def set_charge_rate(self, watts: float) -> str: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_devices.py -v && ruff check ha_spark/devices && mypy ha_spark`
Expected: tests PASS; ruff/mypy clean.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/devices/__init__.py ha_spark/devices/base.py tests/test_devices.py
git commit -m "feat(devices): Capability, ControlAuthority, and the effective_mode gate"
```

---

### Task 2: Driver registry

**Model:** Sonnet.

**Files:**
- Create: `ha_spark/devices/registry.py`
- Test: `tests/test_devices.py` (append)

**Interfaces:**
- Produces: `register(name: str) -> Callable[[type[T]], type[T]]` (decorator; raises `ValueError` on duplicate name); `lookup(driver: str) -> type` (raises `ValueError` on unknown driver).

- [ ] **Step 1: Write the failing test (append to tests/test_devices.py)**

```python
import pytest

from ha_spark.devices import registry


def test_registry_register_and_lookup():
    registry._REGISTRY.clear()

    @registry.register("dummy")
    class Dummy:
        pass

    assert registry.lookup("dummy") is Dummy


def test_registry_unknown_driver_raises():
    registry._REGISTRY.clear()
    with pytest.raises(ValueError, match="unknown driver"):
        registry.lookup("nope")


def test_registry_duplicate_name_raises():
    registry._REGISTRY.clear()

    @registry.register("dup")
    class A:
        pass

    with pytest.raises(ValueError, match="already registered"):

        @registry.register("dup")
        class B:
            pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_devices.py -k registry -v`
Expected: FAIL with `ModuleNotFoundError: ha_spark.devices.registry`.

- [ ] **Step 3: Write minimal implementation**

```python
# ha_spark/devices/registry.py
"""Driver registry: driver name -> Device class, populated by @register."""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

_REGISTRY: dict[str, type] = {}
T = TypeVar("T")


def register(name: str) -> Callable[[type[T]], type[T]]:
    def deco(cls: type[T]) -> type[T]:
        if name in _REGISTRY:
            raise ValueError(f"driver {name!r} already registered")
        _REGISTRY[name] = cls
        return cls

    return deco


def lookup(driver: str) -> type:
    try:
        return _REGISTRY[driver]
    except KeyError:
        raise ValueError(
            f"unknown driver {driver!r}; registered: {sorted(_REGISTRY)}"
        ) from None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_devices.py -v && mypy ha_spark`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/devices/registry.py tests/test_devices.py
git commit -m "feat(devices): driver registry (register decorator + lookup)"
```

---

### Task 3: Structured devices config + dual-read shim

**Model:** **Opus.** (Judgment-heavy: pydantic synthesis validator, HA schema nesting, and the sync-test parser fix.)

**Files:**
- Modify: `ha_spark/config.py` (add `DeviceConfig`, `devices` field + validator, `_OPTION_KEYS`)
- Modify: `ha_spark_addon/config.yaml` (`devices` option + schema)
- Test: `tests/test_config.py` (fix sync parser; add shim tests)

**Interfaces:**
- Consumes: `ControlAuthority` from `ha_spark.devices.base` (Task 1).
- Produces: `DeviceConfig` (pydantic) with `id: str`, `type: Literal["inverter"] = "inverter"`, `driver: str`, `control: ControlAuthority = HA_SPARK`, `entities: dict[str, str]`; `Settings.devices: list[DeviceConfig]` — always non-empty after construction (synthesized from flat keys when not supplied).

- [ ] **Step 1: Write the failing tests (append to tests/test_config.py)**

```python
from ha_spark.config import DeviceConfig
from ha_spark.devices.base import ControlAuthority


def test_devices_synthesized_from_flat_keys_when_absent():
    s = Settings(
        supervisor_token="sup",
        inverter="solis",
        charge_current_entity="number.cc",
        charge_window_start_entity="time.ws",
        charge_window_end_entity="time.we",
        inverter_power_switch_entity="select.pw",
    )
    assert len(s.devices) == 1
    d = s.devices[0]
    assert d.id == "main_inverter"
    assert d.type == "inverter"
    assert d.driver == "solis"
    assert d.control == ControlAuthority.HA_SPARK
    assert d.entities["charge_current"] == "number.cc"
    assert d.entities["window_start"] == "time.ws"


def test_explicit_devices_list_parses_through():
    s = Settings(
        supervisor_token="sup",
        devices=[
            {
                "id": "main_inverter",
                "type": "inverter",
                "driver": "alphaess",
                "control": "observe",
                "entities": {"charge_current": "number.x"},
            }
        ],
    )
    assert len(s.devices) == 1
    assert s.devices[0].driver == "alphaess"
    assert s.devices[0].control == ControlAuthority.OBSERVE


def test_devices_in_option_keys():
    assert "devices" in _OPTION_KEYS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -k "devices or synthesiz" -v`
Expected: FAIL — `ImportError: DeviceConfig` / `devices` not a field.

- [ ] **Step 3: Implement DeviceConfig, the field, and the synthesis validator**

In `ha_spark/config.py`, add near the imports (respecting import discipline — only `devices.base`):

```python
from pydantic import BaseModel, Field, field_validator, model_validator

from ha_spark.devices.base import ControlAuthority
```

Add the model above `class Settings`:

```python
class DeviceConfig(BaseModel):
    """One controllable device. Phase 7 ships type == "inverter" only."""

    id: str
    type: Literal["inverter"] = "inverter"
    driver: str
    control: ControlAuthority = ControlAuthority.HA_SPARK
    entities: dict[str, str] = Field(default_factory=dict)
```

Add the field to `Settings` (near the inverter fields, ~line 234):

```python
    devices: list[DeviceConfig] = Field(default_factory=list)
```

Add an after-validator method on `Settings` (synthesizes from flat keys; idempotent):

```python
    @model_validator(mode="after")
    def _synthesize_devices(self) -> "Settings":
        """Dual-read shim: if no structured `devices`, build one inverter device
        from the flat entity keys in memory. Never rewrites options.json."""
        if not self.devices:
            self.devices = [
                DeviceConfig(
                    id="main_inverter",
                    type="inverter",
                    driver=self.inverter,
                    control=ControlAuthority.HA_SPARK,
                    entities={
                        "charge_current": self.charge_current_entity,
                        "window_start": self.charge_window_start_entity,
                        "window_end": self.charge_window_end_entity,
                        "power_switch": self.inverter_power_switch_entity,
                    },
                )
            ]
        return self
```

Add `"devices"` to the `_OPTION_KEYS` frozenset (in the inverter-selector section, ~line 96):

```python
        "alphaess_serial",
        "devices",
```

- [ ] **Step 4: Add the add-on schema and fix the sync-test parser**

In `ha_spark_addon/config.yaml`, under `options:` add:

```yaml
  devices: []
```

Under `schema:` add (HA can't express a free-form dict, so `entities` lists the known inverter keys):

```yaml
  devices:
    - id: str
      type: list(inverter)
      driver: list(solis|alphaess)
      control: list(observe|ha_spark|supplier)?
      entities:
        charge_current: str?
        window_start: str?
        window_end: str?
        power_switch: str?
```

The existing `test_addon_schema_covers_all_option_keys` collects *every* 2-space-indented `schema:` line and would now pick up the nested `id`/`type`/`driver` keys. Restrict it to **exactly** 2-space indentation. In `tests/test_config.py`, replace the loop body of that test:

```python
def test_addon_schema_covers_all_option_keys() -> None:
    """Every honoured option key appears in the add-on schema, and vice versa.

    Only top-level (exactly 2-space-indented) schema keys count; nested keys
    under `devices:` (deeper indentation) are skipped.
    """
    in_schema = False
    schema_keys: set[str] = set()
    for line in ADDON_CONFIG.read_text(encoding="utf-8").splitlines():
        if line == "schema:":
            in_schema = True
            continue
        if in_schema:
            if not line.startswith("  "):
                break
            if line.startswith("   "):  # 3+ spaces => nested; skip
                continue
            schema_keys.add(line.split(":", 1)[0].strip())
    assert schema_keys == set(_OPTION_KEYS)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v && mypy ha_spark && ruff check .`
Expected: all PASS (including `test_addon_schema_covers_all_option_keys`); mypy/ruff clean.

- [ ] **Step 6: Commit**

```bash
git add ha_spark/config.py ha_spark_addon/config.yaml tests/test_config.py
git commit -m "feat(config): structured devices list + dual-read shim off flat keys"
```

---

### Task 4: Relocate SolisDevice into devices/inverters, gated + capability-aware

**Model:** Sonnet. (Mechanical move; the existing characterization test is the regression guard.)

**Files:**
- Create: `ha_spark/devices/inverters/__init__.py` (empty)
- Create: `ha_spark/devices/inverters/solis.py`
- Modify: `ha_spark/energy/chargers.py` (re-export `SolisDevice`/`solis_current_a`; alias `SolisCharger = SolisDevice`)
- Test: `tests/test_chargers.py` (update Solis ctors/imports; add authority-gate test)

**Interfaces:**
- Consumes: `Capability`, `ControlAuthority`, `effective_mode`, `Device` (Task 1); `register` (Task 2); `DeviceConfig` (Task 3); `ChargeIntent`, `window_hours` (`energy/models.py`).
- Produces: `SolisDevice(config: DeviceConfig, settings: Settings, rest: HomeAssistantRest)` with `capabilities = frozenset({CHARGE_WINDOW, CHARGE_RATE, STOP_DISCHARGE})`; module-level `solis_current_a(intent, settings) -> float` (unchanged signature).

- [ ] **Step 1: Write the failing/updated tests**

In `tests/test_chargers.py`, change the import line and the Solis constructor calls. Replace:

```python
from ha_spark.energy.chargers import AlphaESSCharger, SolisCharger, charger_for, solis_current_a
```
with:
```python
from ha_spark.config import DeviceConfig
from ha_spark.devices.base import Capability, ControlAuthority
from ha_spark.devices.inverters.solis import SolisDevice, solis_current_a


def _solis_cfg(control="ha_spark", **entities):
    base = {
        "charge_current": "number.charge_current",
        "window_start": "time.ws",
        "window_end": "time.we",
        "power_switch": "select.power",
    }
    base.update(entities)
    return DeviceConfig(id="main_inverter", driver="solis", control=control, entities=base)
```

Update every `SolisCharger(s, rest)` to `SolisDevice(_solis_cfg(), s, rest)` and make each device read its entity IDs from the config (the test settings `s` should still carry `charge_current_entity` etc. so the synthesized values match, OR pass them via `_solis_cfg`). Replace the `supports_live_rate` test:

```python
def test_solis_capabilities_include_rate():
    s = _settings()
    rest = HomeAssistantRest("http://ha.test", "tok")
    assert Capability.CHARGE_RATE in SolisDevice(_solis_cfg(), s, rest).capabilities
```

Add the security-critical authority-gate test:

```python
async def test_solis_observe_authority_never_writes_even_when_on():
    s = _settings(proactive_mode="on")
    async with respx_mock() as mock:  # match the file's existing respx pattern
        cc = mock.post("http://ha.test/api/services/number/set_value")
        lines = await SolisDevice(_solis_cfg(control="observe"), s, rest).apply(_intent())
    assert not cc.called  # observe authority suppresses the write despite on
    assert any("[OBSERVE]" in ln for ln in lines)
```

(Use the existing helpers/fixtures in `test_chargers.py` — `_settings`, `_intent`, the respx setup — adapting names to match the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chargers.py -v`
Expected: FAIL — `ModuleNotFoundError: ha_spark.devices.inverters.solis`.

- [ ] **Step 3: Create devices/inverters/solis.py**

Create `ha_spark/devices/inverters/__init__.py` (empty). Then create `solis.py` by **moving the `solis_current_a` function and the `SolisCharger` class verbatim** from `energy/chargers.py`, with these changes:

1. Class renamed `SolisDevice`, decorated `@register("solis")`.
2. Constructor takes `config: DeviceConfig` first; store `self._config = config`.
3. Add `capabilities = frozenset({Capability.CHARGE_WINDOW, Capability.CHARGE_RATE, Capability.STOP_DISCHARGE})`.
4. Replace every `self._settings.charge_current_entity` with `self._config.entities["charge_current"]`; `inverter_power_switch_entity` → `entities["power_switch"]`; `charge_window_start_entity` → `entities.get("window_start", "")`; `charge_window_end_entity` → `entities.get("window_end", "")`.
5. Replace each `mode = self._settings.proactive_mode` with `mode = effective_mode(self._config.control, self._settings.proactive_mode)`.
6. Extend the no-write branches to handle `"observe"`:

```python
        if mode == "simulate":
            return f"[SIMULATE] would {desc}"
        if mode in ("off", "observe"):
            return f"[{mode.upper()}] computed: {desc}"
        # mode == "on": real write below
```

(The SoC-valid guard stays `if mode == "on" and not intent.soc_valid:` — with `effective_mode`, `"on"` already implies `control == ha_spark`.) `planned_rate_w`, `set_charge_rate`, `read_charge_rate`, the read-back helpers, and `solis_current_a` move unchanged except `set_charge_rate`/`read_charge_rate` now read `self._config.entities["charge_current"]`.

Header imports for `solis.py`:

```python
from __future__ import annotations

from datetime import time

from ha_spark.config import DeviceConfig, Settings
from ha_spark.devices.base import Capability, effective_mode
from ha_spark.devices.registry import register
from ha_spark.energy.models import ChargeIntent, window_hours
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger
```

- [ ] **Step 4: Make energy/chargers.py re-export Solis (keep imports valid)**

At the top of `ha_spark/energy/chargers.py`, add re-exports so existing importers keep working this release:

```python
from ha_spark.devices.inverters.solis import SolisDevice, solis_current_a

SolisCharger = SolisDevice  # back-compat alias; removed when chargers.py is deleted
```

Remove the now-relocated `SolisCharger` class body and `solis_current_a` from `chargers.py` (AlphaESS handled in Task 5).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_chargers.py -v && mypy ha_spark && ruff check .`
Expected: all PASS — characterization values unchanged; new gate test green.

- [ ] **Step 6: Commit**

```bash
git add ha_spark/devices/inverters/ ha_spark/energy/chargers.py tests/test_chargers.py
git commit -m "refactor(devices): relocate SolisDevice with capabilities + authority gate"
```

---

### Task 5: Relocate AlphaESSDevice into devices/inverters

**Model:** Sonnet.

**Files:**
- Create: `ha_spark/devices/inverters/alphaess.py`
- Modify: `ha_spark/energy/chargers.py` (re-export `AlphaESSDevice`; alias `AlphaESSCharger`)
- Test: `tests/test_chargers.py` (update AlphaESS ctors)

**Interfaces:**
- Consumes: same as Task 4.
- Produces: `AlphaESSDevice(config, settings, rest)` with `capabilities = frozenset({Capability.CHARGE_WINDOW})`; `alphaess_serial` read from `settings.alphaess_serial` (top-level scalar, not an entity).

- [ ] **Step 1: Update the AlphaESS tests**

In `tests/test_chargers.py`, change AlphaESS constructions `AlphaESSCharger(s, rest)` → `AlphaESSDevice(_alpha_cfg(), s, rest)` with:

```python
from ha_spark.devices.inverters.alphaess import AlphaESSDevice


def _alpha_cfg(control="ha_spark"):
    return DeviceConfig(id="main_inverter", driver="alphaess", control=control, entities={})
```

Replace `test_alphaess_does_not_support_live_rate` with:

```python
def test_alphaess_capabilities_exclude_rate():
    s = _settings()
    assert Capability.CHARGE_RATE not in AlphaESSDevice(_alpha_cfg(), s, rest).capabilities
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chargers.py -k alphaess -v`
Expected: FAIL — `ModuleNotFoundError: ha_spark.devices.inverters.alphaess`.

- [ ] **Step 3: Create devices/inverters/alphaess.py**

**Move the `AlphaESSCharger` class verbatim** from `energy/chargers.py` into `alphaess.py`, renamed `AlphaESSDevice`, decorated `@register("alphaess")`, with: constructor `(config, settings, rest)`; `capabilities = frozenset({Capability.CHARGE_WINDOW})`; `mode = effective_mode(self._config.control, self._settings.proactive_mode)` and the `if mode in ("off", "observe")` branch as in Task 4; `serial` read from `self._settings.alphaess_serial`. Same import header as `solis.py` (minus `window_hours` if unused; keep `_fmt_hhmm` — move it to a shared spot or duplicate the 2-line helper in each module).

> Note: `_fmt_hhmm` is used by both devices. Put it in `devices/base.py` and import it in both, to stay DRY.

- [ ] **Step 4: Re-export from chargers.py**

In `ha_spark/energy/chargers.py` add:

```python
from ha_spark.devices.inverters.alphaess import AlphaESSDevice

AlphaESSCharger = AlphaESSDevice  # back-compat alias
```

Remove the relocated `AlphaESSCharger` class body and the now-unused `_fmt_hhmm`/`Charger` Protocol from `chargers.py` (the Protocol moved to `Device` in `base.py`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_chargers.py -v && mypy ha_spark && ruff check .`
Expected: PASS; clean.

- [ ] **Step 6: Commit**

```bash
git add ha_spark/devices/inverters/alphaess.py ha_spark/devices/base.py ha_spark/energy/chargers.py tests/test_chargers.py
git commit -m "refactor(devices): relocate AlphaESSDevice; share _fmt_hhmm in base"
```

---

### Task 6: get_device/inverter_device factory + rewire scheduler & supply_guard

**Model:** Sonnet.

**Files:**
- Modify: `ha_spark/devices/__init__.py` (add `get_device`, `inverter_device`, re-exports)
- Modify: `ha_spark/energy/scheduler.py` (4 `charger_for` sites; capability check)
- Modify: `ha_spark/energy/supply_guard.py` (`charger_for` → `inverter_device`)
- Modify: `ha_spark/energy/chargers.py` (re-export `get_device`; drop `charger_for` body)
- Test: `tests/test_chargers.py` (`charger_for` selection test → `get_device`); `tests/test_supply_guard.py` (capability gating)

**Interfaces:**
- Consumes: `lookup` (Task 2), `DeviceConfig`/`Settings.devices` (Task 3), `SolisDevice`/`AlphaESSDevice` (Tasks 4–5).
- Produces: `get_device(config: DeviceConfig, settings: Settings, rest) -> Device`; `inverter_device(settings: Settings, rest) -> Device` (first `type == "inverter"` device).

- [ ] **Step 1: Write the failing tests**

In `tests/test_chargers.py`, replace `test_charger_for_selects_by_inverter`:

```python
from ha_spark.devices import get_device, inverter_device


def test_get_device_selects_by_driver():
    rest = HomeAssistantRest("http://ha.test", "tok")
    s = _settings()
    assert isinstance(get_device(_solis_cfg(), s, rest), SolisDevice)
    assert isinstance(get_device(_alpha_cfg(), s, rest), AlphaESSDevice)


def test_inverter_device_picks_inverter_type():
    rest = HomeAssistantRest("http://ha.test", "tok")
    s = _settings(inverter="solis")  # synthesizes a main_inverter device
    assert isinstance(inverter_device(s, rest), SolisDevice)
```

In `tests/test_supply_guard.py`, add a capability-gating assertion (adapt to the file's fixtures):

```python
from ha_spark.devices.base import Capability
from ha_spark.devices import inverter_device


def test_guard_dormant_for_inverter_without_rate(_settings_factory):
    s = _settings_factory(inverter="alphaess", grid_power_entity="sensor.grid")
    rest = HomeAssistantRest("http://ha.test", "tok")
    assert Capability.CHARGE_RATE not in inverter_device(s, rest).capabilities
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chargers.py -k "get_device or inverter_device" tests/test_supply_guard.py -v`
Expected: FAIL — `cannot import name 'get_device'`.

- [ ] **Step 3: Implement the factory in devices/__init__.py**

```python
# ha_spark/devices/__init__.py
"""Device-driver core (Phase 7)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ha_spark.devices.base import Capability, ControlAuthority, Device, effective_mode
from ha_spark.devices.registry import lookup

# Import driver modules so their @register side effects run.
from ha_spark.devices.inverters import alphaess as _alphaess  # noqa: F401
from ha_spark.devices.inverters import solis as _solis  # noqa: F401

if TYPE_CHECKING:
    from ha_spark.config import DeviceConfig, Settings
    from ha_spark.ha.rest import HomeAssistantRest

__all__ = [
    "Capability", "ControlAuthority", "Device", "effective_mode",
    "get_device", "inverter_device",
]


def get_device(config: "DeviceConfig", settings: "Settings", rest: "HomeAssistantRest") -> Device:
    return lookup(config.driver)(config, settings, rest)


def inverter_device(settings: "Settings", rest: "HomeAssistantRest") -> Device:
    config = next(d for d in settings.devices if d.type == "inverter")
    return get_device(config, settings, rest)
```

- [ ] **Step 4: Rewire scheduler.py and supply_guard.py**

In `ha_spark/energy/scheduler.py` replace `from ha_spark.energy.chargers import charger_for` with `from ha_spark.devices import inverter_device` and update the four sites:
- `await charger_for(settings, rest).apply(intent)` → `await inverter_device(settings, rest).apply(intent)`
- `charger = charger_for(settings, rest)` → `charger = inverter_device(settings, rest)`
- `charger_for(settings, rest).planned_rate_w(plan.charge_intent)` → `inverter_device(settings, rest).planned_rate_w(plan.charge_intent)`
- `charger_for(settings, rest).supports_live_rate` → `Capability.CHARGE_RATE in inverter_device(settings, rest).capabilities` (import `Capability` from `ha_spark.devices`)

In `ha_spark/energy/supply_guard.py` replace `from ha_spark.energy.chargers import charger_for` with `from ha_spark.devices import inverter_device` and `self._charger = charger_for(settings, rest)` → `self._charger = inverter_device(settings, rest)`.

In `ha_spark/energy/chargers.py` add `from ha_spark.devices import get_device` and `charger_for = ...`? No — drop `charger_for`; its only callers are now rewired. Leave `chargers.py` as a slim re-export of `SolisDevice`/`AlphaESSDevice`/`solis_current_a` plus the aliases (back-compat for any stray importer), with a module docstring noting it is deprecated and removed next release.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q && mypy ha_spark && ruff check .`
Expected: all PASS; clean. (Confirms scheduler/supply_guard behaviour unchanged.)

- [ ] **Step 6: Commit**

```bash
git add ha_spark/devices/__init__.py ha_spark/energy/scheduler.py ha_spark/energy/supply_guard.py ha_spark/energy/chargers.py tests/test_chargers.py tests/test_supply_guard.py
git commit -m "refactor(devices): get_device/inverter_device factory; rewire scheduler + supply guard"
```

---

### Task 7: Agent surface reports device control (read-only)

**Model:** Sonnet.

**Files:**
- Modify: `ha_spark/agent/tools.py` (`StateResult` + `get_state`)
- Test: `tests/test_agent_routes.py` (control appears; no write tool)

**Interfaces:**
- Consumes: `Settings.devices` (Task 3).
- Produces: `StateResult.devices: list[dict]` of `{id, type, driver, control}` — entity IDs and secrets excluded.

- [ ] **Step 1: Write the failing test (append to tests/test_agent_routes.py)**

```python
async def test_get_state_reports_device_control(monkeypatch):
    from ha_spark.agent import tools
    from ha_spark.config import Settings

    s = Settings(supervisor_token="sup", inverter="solis")
    # stub the HA read so get_state doesn't hit the network (match file's pattern)
    ...
    result = await tools.get_state(s)
    assert result.devices == [
        {"id": "main_inverter", "type": "inverter", "driver": "solis", "control": "ha_spark"}
    ]
```

(Use the existing respx/monkeypatch fixtures already in `test_agent_routes.py` to stub `gather_inputs`/REST.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent_routes.py -k device_control -v`
Expected: FAIL — `StateResult` has no `devices`.

- [ ] **Step 3: Implement**

In `ha_spark/agent/tools.py`, add to `StateResult`:

```python
    devices: list[dict[str, str]] = []
```

In `get_state`, build the device summary (read-only, no entity IDs/secrets):

```python
    devices = [
        {"id": d.id, "type": d.type, "driver": d.driver, "control": d.control.value}
        for d in settings.devices
    ]
    return StateResult(inputs=data, devices=devices)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent_routes.py -v && mypy ha_spark && ruff check .`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/agent/tools.py tests/test_agent_routes.py
git commit -m "feat(agent): get_state reports each device's control authority (read-only)"
```

---

### Task 8: Packaging — version, changelog, docs

**Model:** Sonnet.

**Files:**
- Modify: `ha_spark_addon/config.yaml` (`version: "0.14.0"`)
- Modify: `ha_spark_addon/CHANGELOG.md`
- Modify: `ha_spark_addon/DOCS.md`

**Interfaces:** none (docs/packaging only).

- [ ] **Step 1: Bump the version**

In `ha_spark_addon/config.yaml`: `version: "0.14.0"`.

- [ ] **Step 2: Add the CHANGELOG entry (top of file)**

```markdown
## 0.14.0

- Device-driver core (Phase 7): inverter adapters now live in a `devices/`
  driver package behind a registry, each advertising a `Capability` set, and
  every controllable device carries a `control` authority
  (`observe | ha_spark | supplier`). A real write now requires BOTH
  `control: ha_spark` and `proactive_mode: on` — `observe`/`supplier` compute
  and log (`[OBSERVE]`) but never actuate. Config gains a structured `devices:`
  list; existing flat setups are migrated in memory automatically (no config
  change needed, options.json is never rewritten). The agent surface's
  `get_state` now reports each device's control authority. No behaviour change
  for the existing Solis install.
- (Back-note) The multi-inverter charge contract (`ChargeIntent`, the
  inverter selector, the AlphaESS adapter) landed in an earlier build without a
  changelog entry; it is the foundation this phase formalises.
```

- [ ] **Step 3: Update DOCS.md**

Add a "Multiple inverters / device control" section documenting: the `devices:`
list shape (mirror the spec's example), the `control` authority values and the
"real write needs `ha_spark` + `proactive_mode: on`" rule, and that single-
inverter installs need no change (flat keys still work).

- [ ] **Step 4: Verify gates and the config-sync test**

Run: `pytest -q && mypy ha_spark && ruff check .`
Expected: all PASS (notably `test_addon_schema_covers_all_option_keys`).

- [ ] **Step 5: Commit**

```bash
git add ha_spark_addon/config.yaml ha_spark_addon/CHANGELOG.md ha_spark_addon/DOCS.md
git commit -m "chore(addon): ship device-driver core in 0.14.0 (changelog, docs, version)"
```

> **Release (do NOT do automatically — confirm with the user):** per CLAUDE.md, after merge to `master` the add-on needs an annotated `v0.14.0` tag pushed (`git tag -a v0.14.0 -m ... && git push origin v0.14.0`) and a GitHub release, or the image won't build. Sequence: bump on master → tag → push branch + tag.

---

## Self-Review

**Spec coverage:**
- `devices/` package + registry → Tasks 1, 2, 6. ✓
- `Capability` model → Task 1; consumed in 4/5/6. ✓
- `ControlAuthority` + single-chokepoint gate → Task 1 (`effective_mode`), enforced in Tasks 4/5. ✓
- `supplier` reserved/observe-like → Task 1 (`effective_mode` collapses non-`ha_spark` to `observe`). ✓
- Structured config + dual-read shim → Task 3. ✓
- `entities` entity-IDs only; `alphaess_serial` top-level → Tasks 3, 5. ✓
- Drop `supports_live_rate`, use capability membership → Tasks 4, 5, 6. ✓
- Resolve inverter by `type`, not index → Task 6 (`inverter_device`). ✓
- chargers.py re-export shim for one release → Tasks 4–6. ✓
- Agent reports `control` read-only, no write tool → Task 7. ✓
- Characterization preserved / zero behaviour change → Task 4 (existing assertions kept). ✓
- Packaging 0.14.0 + contract back-note → Task 8. ✓

**Placeholder scan:** Test bodies in Tasks 4/6/7 say "adapt to the file's existing fixtures" rather than reprinting unseen respx/fixture scaffolding — intentional, because those helpers already exist in the target test files; the *assertions* are concrete. No TBD/TODO in implementation code.

**Type consistency:** `effective_mode` returns `off|simulate|on|observe` (Task 1) and Tasks 4/5 branch on exactly those. `get_device(config, settings, rest)` / `inverter_device(settings, rest)` names match across Tasks 6, 7, and the scheduler/guard rewire. `DeviceConfig` fields (`id/type/driver/control/entities`) consistent across Tasks 3–7. `capabilities: frozenset[Capability]` consistent (base Protocol ↔ devices ↔ checks).

---

## Model-switch summary (token budget)

- **Tasks 1, 2** — Sonnet (`/model-switch sonnet`).
- **Task 3** — **Opus** (`/model-switch opus`) — config shim + schema + test-parser judgment.
- **Tasks 4–8** — Sonnet (`/model-switch sonnet`).

After the plan completes, return the parent session to **Opus** (`/model-switch opus`) for review/merge decisions.
