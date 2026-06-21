# Multi-inverter charge contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Solis-only charge path with a small inverter contract so other inverters (first: AlphaESS) drive through the same planner, with Solis behaviour unchanged.

**Architecture:** The planner emits a unit-agnostic `ChargeIntent(target_soc, window, holds)`. A `Charger` adapter realizes it natively — `SolisCharger` re-derives DC amps internally; `AlphaESSCharger` writes window + stop-SOC. An optional rate tier (`supports_live_rate` + `set_charge_rate`/`read_charge_rate`, in watts) gates the supply guard. A one-line `charger_for(settings, rest)` factory selects the adapter from `settings.inverter`.

**Tech Stack:** Python 3.11+ asyncio, pydantic-settings, dataclasses, httpx, respx (tests), pytest (`asyncio_mode = "auto"`), ruff, mypy `strict`.

## Global Constraints

- mypy `strict = true` (`disallow_untyped_defs`) must stay clean; pydantic mypy plugin enabled.
- ruff `E,F,I,UP,B,ASYNC,W`, line length 100.
- All I/O is `async`; one shared `httpx.AsyncClient` per client.
- Every module has tests; mock HTTP with `respx`. No `@pytest.mark.asyncio` needed.
- **Solis output must not change.** A characterization test pins the Solis charge-current setpoint across the refactor.
- **Battery charge current is DC, not AC.** Never compare raw DC amps to the AC supply limit; the supply guard reasons in **watts** (power is the same magnitude DC/AC) and the fuse limit is `supply_max_current_a * supply_voltage_v`.
- **Clean-room re: Predbat (proprietary licence).** Do not copy Predbat code, `apps.yaml` templates, or register maps. Entity/service mappings come from each inverter's own HA integration.
- All quality gates green before each commit: `ruff check . && mypy ha_spark && pytest -q`.

---

### Task 1: `ChargeIntent` model + planner emits it (additive)

Add the new type and populate it on `ChargePlan` *without removing* `overnight_current_a`/`actions` yet, so every existing consumer stays green.

**Files:**
- Modify: `ha_spark/energy/models.py` (add `ChargeIntent`; add `window_hours` free function; add `charge_intent` field to `ChargePlan`)
- Modify: `ha_spark/energy/planner.py:172-234` (build the intent, set the field)
- Test: `tests/test_planner.py`

**Interfaces:**
- Produces: `ChargeIntent(target_soc_pct: float, soc_now: float, window_start: time, window_end: time, holds: tuple[tuple[datetime, datetime], ...] = ())`; `window_hours(start: time, end: time) -> float`; `ChargePlan.charge_intent: ChargeIntent`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_planner.py
def test_plan_emits_charge_intent() -> None:
    plan = _run(  # existing helper that builds inputs+cfg and calls compute_plan
        soc_now=50.0, solar=0.0, load=20.0,
    )
    intent = plan.charge_intent
    assert intent.target_soc_pct == plan.target_soc
    assert intent.soc_now == plan.soc_now
    # default window is 23:30 -> 05:30
    assert (intent.window_start.hour, intent.window_start.minute) == (23, 30)
    assert (intent.window_end.hour, intent.window_end.minute) == (5, 30)


def test_daytime_dispatch_becomes_a_hold() -> None:
    plan = _run_with_daytime_dispatch()  # existing fixture path; see test_daytime_dispatch_emits_stop_discharge
    assert len(plan.charge_intent.holds) == 1
```

(If `_run`/`_run_with_daytime_dispatch` helpers don't exist verbatim, reuse the input-building already in `test_daytime_dispatch_emits_stop_discharge` at `tests/test_planner.py:85`.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_planner.py::test_plan_emits_charge_intent -v`
Expected: FAIL — `ChargePlan` has no attribute `charge_intent`.

- [ ] **Step 3: Implement**

```python
# ha_spark/energy/models.py — add near the top-level helpers
def window_hours(start: time, end: time) -> float:
    """Length of the (possibly midnight-wrapping) charge window, in hours."""
    s = start.hour + start.minute / 60
    e = end.hour + end.minute / 60
    return (e - s) % 24 or 24.0


@dataclass(frozen=True)
class ChargeIntent:
    """Inverter-agnostic charge command: reach ``target_soc_pct`` by ``window_end``.

    ``soc_now`` is carried so a rate-based adapter (Solis) can re-derive the kWh
    to add without re-reading the sensor. ``holds`` are daytime dispatch windows
    during which the battery must stop discharging (hold for cheap grid).
    """

    target_soc_pct: float
    soc_now: float
    window_start: time
    window_end: time
    holds: tuple[tuple[datetime, datetime], ...] = ()
```

```python
# ha_spark/energy/models.py — add to ChargePlan (after `actions`, before soc_valid default block)
    charge_intent: ChargeIntent | None = None  # control contract (Task 5 makes it required)
```

```python
# ha_spark/energy/planner.py — in compute_plan, after `actions` is built (around line 201),
# before the `return ChargePlan(...)`:
    holds = tuple((d.start, d.end) for d in daytime)
    intent = ChargeIntent(
        target_soc_pct=target_soc,
        soc_now=inputs.soc_now,
        window_start=cfg.window_start,
        window_end=cfg.window_end,
        holds=holds,
    )
```

Add `charge_intent=intent,` to the `ChargePlan(...)` constructor call, and import `ChargeIntent` from `ha_spark.energy.models`. Also refactor `PlannerConfig.window_hours` (models.py:87-92) to `return window_hours(self.window_start, self.window_end)` (DRY).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_planner.py -v && ruff check . && mypy ha_spark`
Expected: PASS, lint+types clean.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/energy/models.py ha_spark/energy/planner.py tests/test_planner.py
git commit -m "feat(energy): add ChargeIntent and emit it from the planner

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Generalize `Charger` + reimplement `SolisCharger` on the intent

`SolisCharger` consumes the `ChargeIntent`, derives amps internally (no behaviour change), gains the rate tier, and handles `holds` for stop-discharge. A characterization test pins the amps.

**Files:**
- Modify: `ha_spark/energy/chargers.py` (whole file)
- Test: `tests/test_chargers.py`

**Interfaces:**
- Consumes: `ChargeIntent`, `window_hours` (Task 1); `Settings.battery_capacity_kwh`, `.charge_efficiency`, `.battery_voltage_v`, `.max_charge_current_a`, `.charge_current_entity`, `.inverter_power_switch_entity`, `.charge_window_start_entity`, `.charge_window_end_entity` (last two added in Task 3 — until then read via `getattr(settings, ..., "")`).
- Produces: `Charger` Protocol = `apply(intent) -> list[str]`, `supports_live_rate: bool`, `set_charge_rate(watts: float) -> str`, `read_charge_rate() -> float` (watts), `planned_rate_w(intent) -> float`; module fn `solis_current_a(intent, settings) -> float`.

- [ ] **Step 1: Write the failing characterization + rate tests**

```python
# tests/test_chargers.py
from datetime import time
from ha_spark.energy.models import ChargeIntent
from ha_spark.energy.chargers import SolisCharger, solis_current_a

def _intent(target_soc=77.0, soc_now=50.0):
    return ChargeIntent(target_soc, soc_now, time(23, 30), time(5, 30))

def test_solis_current_matches_legacy_sizing() -> None:
    # capacity 26.88 kWh, eff 0.90, voltage 51 V, 6.0 h window, max 62.5 A.
    # needed = (77-50)/100*26.88 = 7.2576 kWh; buy = 7.2576/0.9 = 8.064 kWh;
    # kwh_per_amp = 6.0*51/1000 = 0.306; amps = 8.064/0.306 = 26.35 A.
    s = _settings(battery_capacity_kwh=26.88, charge_efficiency=0.90,
                  battery_voltage_v=51.0, max_charge_current_a=62.5)
    assert solis_current_a(_intent(), s) == pytest.approx(26.35, abs=0.05)

def test_solis_current_clamps_to_max() -> None:
    s = _settings(battery_capacity_kwh=26.88, charge_efficiency=0.90,
                  battery_voltage_v=51.0, max_charge_current_a=10.0)
    assert solis_current_a(_intent(target_soc=90.0), s) == 10.0

async def test_apply_writes_charge_current(respx_mock) -> None:
    # mode "on": number.set_value to charge_current_entity with the derived amps,
    # then read-back. Assert the posted value rounds to solis_current_a.
    ...  # adapt from existing test_apply_executes_real_write at tests/test_chargers.py:~40

def test_supports_live_rate_true() -> None:
    assert SolisCharger(_settings(), _fake_rest()).supports_live_rate is True
```

Keep `_settings(...)` / `_fake_rest()` consistent with the existing helpers in `tests/test_chargers.py`.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_chargers.py::test_solis_current_matches_legacy_sizing -v`
Expected: FAIL — `solis_current_a` undefined.

- [ ] **Step 3: Implement `chargers.py`**

```python
"""Charger adapters: realize a ChargeIntent as (simulated or real) HA writes.

PROACTIVE_MODE gates side effects: ``simulate`` -> log intended writes only;
``on`` -> real ``call_service``; ``off`` -> compute only. Each adapter isolates
per-write failures and reads back each write to confirm the device took it.
"""
from __future__ import annotations

from datetime import time
from typing import Protocol

from ha_spark.config import Settings
from ha_spark.energy.models import ChargeIntent, window_hours
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)


class Charger(Protocol):
    """Realizes a :class:`ChargeIntent`; returns human-readable action lines."""

    supports_live_rate: bool

    async def apply(self, intent: ChargeIntent) -> list[str]: ...
    async def set_charge_rate(self, watts: float) -> str: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...


def solis_current_a(intent: ChargeIntent, settings: Settings) -> float:
    """DC charge current (A) for the intent — the legacy planner sizing, inverted."""
    needed_kwh = max(0.0, (intent.target_soc_pct - intent.soc_now) / 100.0
                     * settings.battery_capacity_kwh)
    eff = settings.charge_efficiency if settings.charge_efficiency > 0 else 1.0
    purchase = needed_kwh / eff
    kwh_per_amp = window_hours(intent.window_start, intent.window_end) * settings.battery_voltage_v / 1000.0
    if kwh_per_amp <= 0:
        return 0.0
    return min(settings.max_charge_current_a, purchase / kwh_per_amp)


def _fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


class SolisCharger:
    """Solis: timed charge current (number) + window (time/select) + power switch (select)."""

    supports_live_rate = True

    def __init__(self, settings: Settings, rest: HomeAssistantRest) -> None:
        self._settings = settings
        self._rest = rest

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return solis_current_a(intent, self._settings) * self._settings.battery_voltage_v

    async def apply(self, intent: ChargeIntent) -> list[str]:
        mode = self._settings.proactive_mode
        lines: list[str] = []
        current = round(solis_current_a(intent, self._settings))
        # SoC-unreadable guard: soc_now==0 from a dead sensor would size a max charge.
        if mode == "on" and intent.soc_now <= 0:
            line = f"[BLOCKED] SoC unreadable; not charging to {intent.target_soc_pct:.0f}%"
            log.warning(line)
            return [line]
        lines.append(await self._write_window(intent))
        lines.append(await self._set_current(current,
                     f"set timed charge current to {current} A for the "
                     f"{window_hours(intent.window_start, intent.window_end):.1f} h window"))
        for start, end in intent.holds:
            lines.append(await self._stop_discharge(
                f"turn inverter off (stop discharge) during dispatch "
                f"{start:%H:%M}-{end:%H:%M}"))
        return lines

    async def set_charge_rate(self, watts: float) -> str:
        amps = round(watts / self._settings.battery_voltage_v) if self._settings.battery_voltage_v > 0 else 0
        return await self._set_current(amps, f"set charge current to {amps} A ({watts:.0f} W)")

    async def read_charge_rate(self) -> float:
        state = await self._rest.get_state(self._settings.charge_current_entity)
        return float(state.state) * self._settings.battery_voltage_v

    # --- internal writes (PROACTIVE_MODE-gated, failure-isolated, read-back verified) ---

    async def _set_current(self, amps: float, desc: str) -> str:
        mode = self._settings.proactive_mode
        if mode == "simulate":
            log.info("[SIMULATE] would %s", desc); return f"[SIMULATE] would {desc}"
        if mode == "off":
            return f"[OFF] computed: {desc}"
        try:
            entity = self._settings.charge_current_entity
            await self._rest.call_service("number", "set_value",
                                          {"entity_id": entity, "value": amps})
            mismatch = await self._read_back_number(entity, amps)
        except Exception as exc:  # noqa: BLE001 - isolate per write
            log.error("[FAILED] %s: %r", desc, exc); return f"[FAILED] {desc}: {exc!r}"
        if mismatch:
            log.warning("[WARNING] %s, but %s", desc, mismatch); return f"[WARNING] {desc}, but {mismatch}"
        return f"[APPLIED] {desc}"

    async def _stop_discharge(self, desc: str) -> str:
        mode = self._settings.proactive_mode
        if mode == "simulate": return f"[SIMULATE] would {desc}"
        if mode == "off": return f"[OFF] computed: {desc}"
        try:
            entity = self._settings.inverter_power_switch_entity
            await self._rest.call_service("select", "select_option",
                                          {"entity_id": entity, "option": "Off"})
            mismatch = await self._read_back_option(entity, "Off")
        except Exception as exc:  # noqa: BLE001
            return f"[FAILED] {desc}: {exc!r}"
        return f"[WARNING] {desc}, but {mismatch}" if mismatch else f"[APPLIED] {desc}"

    async def _write_window(self, intent: ChargeIntent) -> str:
        start_e = getattr(self._settings, "charge_window_start_entity", "")
        end_e = getattr(self._settings, "charge_window_end_entity", "")
        if not (start_e and end_e):
            return "[SKIP] no window entities configured; window left as-is"
        desc = f"set charge window {_fmt_hhmm(intent.window_start)}-{_fmt_hhmm(intent.window_end)}"
        mode = self._settings.proactive_mode
        if mode == "simulate": return f"[SIMULATE] would {desc}"
        if mode == "off": return f"[OFF] computed: {desc}"
        try:
            await self._rest.call_service("time", "set_value",
                {"entity_id": start_e, "time": _fmt_hhmm(intent.window_start) + ":00"})
            await self._rest.call_service("time", "set_value",
                {"entity_id": end_e, "time": _fmt_hhmm(intent.window_end) + ":00"})
        except Exception as exc:  # noqa: BLE001
            return f"[FAILED] {desc}: {exc!r}"
        return f"[APPLIED] {desc}"

    async def _read_back_number(self, entity: str, wanted: float) -> str | None:
        try:
            got = float((await self._rest.get_state(entity)).state)
        except Exception as exc:  # noqa: BLE001
            return f"read-back failed: {exc!r}"
        return None if abs(got - wanted) <= 0.5 else f"read back {got:g} (wanted {wanted:g})"

    async def _read_back_option(self, entity: str, wanted: str) -> str | None:
        try:
            got = str((await self._rest.get_state(entity)).state)
        except Exception as exc:  # noqa: BLE001
            return f"read-back failed: {exc!r}"
        return None if got.lower() == wanted.lower() else f"read back {got!r} (wanted {wanted!r})"
```

> **Window service note:** `time.set_value` matches the solax-modbus integration's timed-charge `time.*` entities. If a given install exposes the window as `select`/`number` instead, that's a per-profile mapping change, not a contract change — `_write_window` no-ops cleanly when the entities are blank.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_chargers.py -v && ruff check . && mypy ha_spark`
Expected: PASS. (Update the old `apply(plan)` tests in this file to `apply(intent)`; delete the obsolete `apply_action`/`ChargeAction` cases — the supply guard rewires in Task 4.)

- [ ] **Step 5: Commit**

```bash
git add ha_spark/energy/chargers.py tests/test_chargers.py
git commit -m "refactor(energy): SolisCharger consumes ChargeIntent, adds rate tier

Characterization test pins Solis charge current across the refactor.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Config (`inverter` selector + window/AlphaESS fields), AlphaESS preset + adapter, `charger_for`

**Files:**
- Modify: `ha_spark/config.py` (new fields + `_USER_OPTION_KEYS` entries around line 88-91)
- Modify: `ha_spark/presets.py` (add `ALPHAESS`, register in `PRESETS`)
- Modify: `ha_spark/energy/chargers.py` (add `AlphaESSCharger` + `charger_for`)
- Test: `tests/test_chargers.py`, `tests/test_presets.py` (if present), `tests/test_config.py` (if present)

**Interfaces:**
- Consumes: `Charger` Protocol, `ChargeIntent`, `SolisCharger` (Task 2).
- Produces: `Settings.inverter: Literal["solis","alphaess"]`, `.charge_window_start_entity`, `.charge_window_end_entity`, `.alphaess_serial`; `AlphaESSCharger`; `charger_for(settings, rest) -> Charger`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chargers.py
from ha_spark.energy.chargers import charger_for, AlphaESSCharger, SolisCharger

def test_charger_for_selects_by_inverter() -> None:
    assert isinstance(charger_for(_settings(inverter="solis"), _fake_rest()), SolisCharger)
    assert isinstance(charger_for(_settings(inverter="alphaess"), _fake_rest()), AlphaESSCharger)

def test_alphaess_does_not_support_live_rate() -> None:
    assert AlphaESSCharger(_settings(inverter="alphaess"), _fake_rest()).supports_live_rate is False

async def test_alphaess_apply_writes_window_and_stop_soc(respx_mock) -> None:
    # mode "on": one alphaess.setbatterycharge call with the window + stop-SOC.
    route = respx_mock.post(url__regex=r".*/services/alphaess/setbatterycharge").respond(200, json=[])
    s = _settings(inverter="alphaess", proactive_mode="on", alphaess_serial="ABC123")
    async with HomeAssistantRest(...) as rest:
        lines = await AlphaESSCharger(s, rest).apply(_intent(target_soc=80.0))
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["chargeStopSOC"] == 80
    assert "[APPLIED]" in lines[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_chargers.py::test_charger_for_selects_by_inverter -v`
Expected: FAIL — `charger_for` undefined.

- [ ] **Step 3: Implement**

```python
# ha_spark/config.py — add to Settings (near the entity-id block, ~line 214)
    inverter: Literal["solis", "alphaess"] = Field(default="solis")
    charge_window_start_entity: str = Field(default="")
    charge_window_end_entity: str = Field(default="")
    alphaess_serial: str = Field(default="")  # AlphaESS system serial for setbatterycharge
```

Add `"inverter"`, `"charge_window_start_entity"`, `"charge_window_end_entity"`, `"alphaess_serial"` to `_USER_OPTION_KEYS` (the frozenset around line 44-104).

```python
# ha_spark/presets.py — add and register
ALPHAESS: dict[str, str] = {
    # Control is the alphaess.setbatterycharge service (window + stop-SOC), not entities.
    # Sensors below come from the CharlesGillanders integration (cloud or local).
    "soc_entity": "sensor.alphaess_battery_soc",
    "battery_voltage_entity": "sensor.alphaess_battery_voltage",
    "grid_power_entity": "sensor.alphaess_total_load",  # used only if a rate tier appears
}
PRESETS: dict[str, dict[str, str]] = {"solis": SOLIS, "alphaess": ALPHAESS}
```

```python
# ha_spark/energy/chargers.py — add
class AlphaESSCharger:
    """AlphaESS: charge window + stop-SOC via the alphaess.setbatterycharge service.

    No settable rate -> the supply guard stays dormant for this inverter.
    """

    supports_live_rate = False

    def __init__(self, settings: Settings, rest: HomeAssistantRest) -> None:
        self._settings = settings
        self._rest = rest

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return 0.0  # no rate control; the inverter self-regulates to the SOC target

    async def set_charge_rate(self, watts: float) -> str:
        return "[SKIP] AlphaESS has no settable charge rate"

    async def read_charge_rate(self) -> float:
        return 0.0

    async def apply(self, intent: ChargeIntent) -> list[str]:
        stop_soc = round(intent.target_soc_pct)
        desc = (f"charge to {stop_soc}% in window "
                f"{_fmt_hhmm(intent.window_start)}-{_fmt_hhmm(intent.window_end)}")
        mode = self._settings.proactive_mode
        if mode == "on" and intent.soc_now <= 0:
            return [f"[BLOCKED] SoC unreadable; not {desc}"]
        if mode == "simulate":
            return [f"[SIMULATE] would {desc}"]
        if mode == "off":
            return [f"[OFF] computed: {desc}"]
        try:
            await self._rest.call_service("alphaess", "setbatterycharge", {
                "serial": self._settings.alphaess_serial,
                "enabled": True,
                "cp1start": _fmt_hhmm(intent.window_start),
                "cp1end": _fmt_hhmm(intent.window_end),
                "chargeStopSOC": stop_soc,
            })
        except Exception as exc:  # noqa: BLE001
            return [f"[FAILED] {desc}: {exc!r}"]
        return [f"[APPLIED] {desc}"]


def charger_for(settings: Settings, rest: HomeAssistantRest) -> Charger:
    """Select the inverter adapter from ``settings.inverter`` (dict dispatch)."""
    chargers: dict[str, type] = {"solis": SolisCharger, "alphaess": AlphaESSCharger}
    return chargers[settings.inverter](settings, rest)  # type: ignore[no-any-return]
```

> **VERIFY before shipping the AlphaESS adapter:** confirm the `alphaess.setbatterycharge` field names (`serial`, `cp1start`, `cp1end`, `chargeStopSOC`, `enabled`) against the integration's `services.yaml` on the tester's box (`ha-spark states` won't show services — check Developer Tools → Services, or the integration repo). Adjust the dict keys if they differ; the contract is unaffected.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_chargers.py tests/test_presets.py -v && ruff check . && mypy ha_spark`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/config.py ha_spark/presets.py ha_spark/energy/chargers.py tests/
git commit -m "feat(energy): inverter selector, AlphaESS adapter + preset, charger_for factory

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Supply guard → watts + `supports_live_rate` gating; rewire scheduler + cli

**Files:**
- Modify: `ha_spark/energy/supply_guard.py` (whole file)
- Modify: `ha_spark/energy/scheduler.py:24,76,147-161,192,195-199`
- Modify: `ha_spark/cli.py:20,103`
- Test: `tests/test_supply_guard.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Charger`, `charger_for`, `SolisCharger.planned_rate_w/set_charge_rate/read_charge_rate` (Tasks 2-3).
- Produces: `throttled_rate_w(supply_w, setpoint_w, target_w, *, limit_a, supply_voltage_v) -> float`; `SupplyGuard.tick(target_w: float)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_supply_guard.py
from ha_spark.energy.supply_guard import throttled_rate_w

def test_throttle_subtracts_battery_from_supply() -> None:
    # 240 V, limit 75 A -> 18000 W. supply 16000 W incl. battery 4000 W ->
    # other load 12000 W; headroom 6000 W; capped at target 4000 W -> 4000.
    assert throttled_rate_w(16000, 4000, 4000, limit_a=75, supply_voltage_v=240) == 4000

def test_throttle_sheds_when_over_limit() -> None:
    # other load 16000 W, limit 18000 W -> headroom 2000 W < target 4000 -> 2000.
    assert throttled_rate_w(20000, 4000, 4000, limit_a=75, supply_voltage_v=240) == 2000

async def test_guard_dormant_when_no_live_rate(...) -> None:
    # AlphaESSCharger.supports_live_rate is False -> scheduler never calls the guard.
    ...
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_supply_guard.py::test_throttle_subtracts_battery_from_supply -v`
Expected: FAIL — `throttled_rate_w` undefined.

- [ ] **Step 3: Implement**

```python
# ha_spark/energy/supply_guard.py — replace throttled_current with:
_MIN_DELTA_W = 200.0  # ignore sub-200 W churn

def throttled_rate_w(supply_w: float, setpoint_w: float, target_w: float, *,
                     limit_a: float, supply_voltage_v: float) -> float:
    """Charge power (W) that fits the supply limit, capped at the target.

    Power balances directly DC<->AC, so we never mix DC amps with AC amps: the
    measured draw includes the battery's own charging (~= its DC charge power),
    so subtract it to get the other load, then grant the battery the remaining
    headroom under ``limit_a * supply_voltage_v``.
    """
    other_load_w = supply_w - setpoint_w
    limit_w = limit_a * supply_voltage_v
    return max(0.0, min(target_w, limit_w - other_load_w))
```

```python
# SupplyGuard.tick — now in watts, talking to the charger's rate methods
class SupplyGuard:
    def __init__(self, settings: Settings, rest: HomeAssistantRest) -> None:
        self._settings = settings
        self._rest = rest
        self._charger = charger_for(settings, rest)  # was SolisCharger(...)

    async def tick(self, target_w: float) -> str | None:
        s = self._settings
        try:
            supply_w = float((await self._rest.get_state(s.grid_power_entity)).state)
            setpoint_w = await self._charger.read_charge_rate()
        except Exception as exc:  # noqa: BLE001 - never throttle on bad data
            log.warning("Supply guard: read failed (%s); skipping", exc)
            return None
        wanted_w = throttled_rate_w(supply_w, setpoint_w, target_w,
                                    limit_a=s.supply_max_current_a, supply_voltage_v=s.supply_voltage_v)
        if abs(wanted_w - setpoint_w) < _MIN_DELTA_W:
            return None
        return await self._charger.set_charge_rate(wanted_w)
```

```python
# ha_spark/energy/scheduler.py
# line 24/76: from ha_spark.energy.chargers import charger_for
#   lines = await charger_for(settings, rest).apply(plan.charge_intent)
# guard_tick: take/return target_w; adopt via charger.read_charge_rate() when None;
#   skip entirely when not charger_for(settings, rest).supports_live_rate.
# run_forever line 192: target_w = charger_for(...).planned_rate_w(plan.charge_intent)
#   (compute once after run_once; guard only if grid_power_entity and supports_live_rate)
```

```python
# ha_spark/cli.py:20,103
# from ha_spark.energy.chargers import charger_for
#   lines = await charger_for(settings, rest).apply(plan.charge_intent)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_supply_guard.py tests/test_scheduler.py -v && ruff check . && mypy ha_spark`
Expected: PASS. Update `tests/test_scheduler.py:32,78` (`current_a` fixture) to drive `charge_intent`/`target_w`.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/energy/supply_guard.py ha_spark/energy/scheduler.py ha_spark/cli.py tests/
git commit -m "refactor(energy): supply guard reasons in watts, gated on supports_live_rate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Remove amps/actions from `ChargePlan`; rewire report + publish; make `charge_intent` required

**Files:**
- Modify: `ha_spark/energy/models.py` (drop `overnight_current_a`, `actions`, `ChargeAction`; make `charge_intent` non-optional)
- Modify: `ha_spark/energy/planner.py:180-201,216,220` (stop building `actions`/`current`)
- Modify: `ha_spark/energy/report.py:53`
- Modify: `ha_spark/energy/publish.py:63-71`
- Test: `tests/test_planner.py`, `tests/test_report.py`, `tests/test_publish.py`, `tests/test_copilot.py`, `tests/test_intent_parser.py` (fixtures referencing `overnight_current_a`/`actions`)

**Interfaces:**
- Consumes: `ChargeIntent`, `solis_current_a` (only inside Solis paths — report/publish stay inverter-agnostic and must NOT import it).
- Produces: `ChargePlan` without `overnight_current_a`/`actions`/`ChargeAction`; `charge_intent: ChargeIntent` required.

- [ ] **Step 1: Write/adjust the failing tests**

```python
# tests/test_planner.py — replace the amps assertions (lines 42-44, 51, 182, 200, 226)
def test_plan_targets_correct_soc() -> None:
    plan = _run(soc_now=50.0, solar=0.0, load=20.0)
    assert plan.target_soc > 50.0
    assert plan.charge_intent.target_soc_pct == plan.target_soc

def test_zero_need_targets_current_soc() -> None:
    plan = _run(soc_now=95.0, solar=30.0, load=5.0)
    assert plan.charge_intent.target_soc_pct == pytest.approx(plan.soc_now)

# tests/test_report.py — assert the window/target line replaces the amps line
def test_report_shows_target_and_window() -> None:
    out = format_plan(_plan(), "median")
    assert "Charge to" in out and "%" in out
    assert "Charge current" not in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_report.py::test_report_shows_target_and_window -v`
Expected: FAIL — output still says "Charge current".

- [ ] **Step 3: Implement**

- `models.py`: delete `ChargeAction`, and the `overnight_current_a: float` and `actions: tuple[ChargeAction, ...]` fields; change `charge_intent: ChargeIntent | None = None` to `charge_intent: ChargeIntent` (required — move it above the defaulted fields).
- `planner.py`: delete the `actions` list build (lines 180-201) and the `current`/`kwh_per_amp` lines (176-178); drop `overnight_current_a=current,` and `actions=tuple(actions),` from the constructor. Keep `holds = tuple((d.start, d.end) for d in daytime)` feeding the intent.
- `report.py:53`: replace with

```python
        f"  Charge to          {plan.target_soc:.0f}%  over the "
        f"{plan.window_hours:.1f} h window "
        f"({_fmt(plan.charge_intent.window_start)}-{_fmt(plan.charge_intent.window_end)})",
```
  (add a local `_fmt = lambda t: f"{t.hour:02d}:{t.minute:02d}"` or import `_fmt_hhmm`).
- `publish.py:63-71`: delete the `sensor.ha_spark_overnight_current` entity (target SOC is already published at lines 45-53). No replacement needed.

- [ ] **Step 4: Run full suite**

Run: `pytest -q && ruff check . && mypy ha_spark`
Expected: PASS. Fix any remaining fixtures in `tests/test_copilot.py:22`, `tests/test_intent_parser.py:22`, `tests/test_publish.py:31`, `tests/test_scheduler.py:32` that still pass `overnight_current_a=`/`actions=` — drop those kwargs, add `charge_intent=ChargeIntent(...)`.

- [ ] **Step 5: Commit**

```bash
git add ha_spark/energy/models.py ha_spark/energy/planner.py ha_spark/energy/report.py ha_spark/energy/publish.py tests/
git commit -m "refactor(energy): drop amps/actions from ChargePlan; control is ChargeIntent

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Sunsynk/Victron stubs + docs

Document how to add the next inverters (no shipping code), and update CLAUDE.md/README for the `inverter` option.

**Files:**
- Create: `docs/adding-an-inverter.md`
- Modify: `README.md` (mention `inverter: solis | alphaess`, link the doc)
- Modify: `CLAUDE.md` (one line under Architecture: chargers are per-inverter adapters behind `charger_for`)

- [ ] **Step 1: Write the doc**

`docs/adding-an-inverter.md` — the `Charger` contract (`apply(intent)`, `supports_live_rate`, rate methods), where to register in `charger_for`, the preset pattern, and the rate-tier rule (only inverters with a settable charge rate get the supply guard). Include Sunsynk (current/A — rate tier, like Solis) and Victron (W/DVCC — rate tier) as worked sketches naming their HA integrations, explicitly **not** shipped until a tester exists. Add the clean-room note: derive mappings from each integration, never from Predbat.

- [ ] **Step 2: Verify gates**

Run: `ruff check . && mypy ha_spark && pytest -q`
Expected: PASS (docs-only; no code change).

- [ ] **Step 3: Commit**

```bash
git add docs/adding-an-inverter.md README.md CLAUDE.md
git commit -m "docs: how to add an inverter adapter (Sunsynk/Victron stubs)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Charge-intent contract → Task 1 (type + planner) + Task 5 (made the sole control path). ✓
- Solis bit-for-bit (characterization test) → Task 2. ✓
- Optional rate tier + supply-guard gating → Task 2 (methods) + Task 4 (gating, watts). ✓
- AlphaESS floor adapter + preset + `inverter` selector + window entities → Task 3. ✓
- `charger_for` dict-dispatch factory → Task 3. ✓
- Window pass-through (no dynamic selection) → Task 1 (intent carries config window) + Task 2 (`_write_window`). ✓
- DC-vs-AC preserved (guard in watts) → Task 4 + Global Constraints. ✓
- Clean-room re: Predbat → Global Constraints + Task 6 doc. ✓
- Sunsynk/Victron documented stubs, not shipped → Task 6. ✓
- Error handling (failure isolation, read-back, PROACTIVE_MODE, SoC guard) → preserved in Task 2/3 adapter code. ✓

**Placeholder scan:** AlphaESS service field names carry an explicit VERIFY step (real code + a confirmation gate, not a placeholder); window service is `time.set_value` with a documented fallback. No "TBD"/"implement later".

**Type consistency:** `ChargeIntent` fields, `window_hours`, `solis_current_a`, `throttled_rate_w`, `charger_for`, `planned_rate_w` names are identical across the tasks that define and consume them. `charge_intent` is optional in Task 1 (additive) and made required in Task 5 — intentional, noted in both.
