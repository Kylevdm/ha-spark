# V2L observe + tally + notify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ha-spark read the car's V2L discharge-power sensor, tally it into kWh + an efficiency-discounted net £ saving, publish `sensor.ha_spark_v2l_*`, and fire three timely HA notifications — observe/notify only, no planner or charger changes.

**Architecture:** A new `ha_spark/energy/v2l.py` holds a persisted `V2LSession` plus pure functions (`integrate`, `savings`, `apply_sample`, `notifications`, `payload`) and a thin IO layer (`load_session`/`save_session`, `notify`, `run_v2l_tick`). The daemon's `run_forever` calls `run_v2l_tick` once per 60 s tick when `v2l_power_entity` is set, mirroring the existing `sample_signals` pattern (each call opens its own REST client, best-effort, isolated). A `ha-spark v2l` CLI command prints the live tally.

**Tech Stack:** Python 3.11+, asyncio, pydantic v2 / pydantic-settings, httpx, dataclasses, respx (tests), pytest (`asyncio_mode = "auto"`), ruff, mypy `strict = true`.

**Spec:** `docs/superpowers/specs/2026-06-28-v2l-observe-notify-design.md`

## Global Constraints

- **Security is top priority (CLAUDE.md).** Never log/echo secrets. This feature touches none — config holds an entity ID, number knobs, a cutoff time, and a notify service name (not a secret). `rest.call_service` already logs its `data`; notify title/message carry only kWh/£ text, never secrets.
- **The LLM never controls hardware; planner stays the sole decider.** This feature does not touch the planner or `chargers.py`. V2L is manual — ha-spark only reads it.
- **Validate everything from outside the process.** The V2L sensor read goes through the tolerant `_to_float` helper; a bad payload degrades to 0.0, never crashes the loop.
- **Strict typing:** `mypy ha_spark` with `strict = true` must stay clean. pydantic mypy plugin enabled.
- **Tests required per module:** mock HTTP with `respx`; pytest `asyncio_mode = "auto"` (no `@pytest.mark.asyncio`). ruff lints `E,F,I,UP,B,ASYNC,W`, line length 100.
- **Quality gates (all green before each commit):** `ruff check .`, `mypy ha_spark`, `pytest -q`.
- **Config sync invariant:** `config.py` `_OPTION_KEYS`, `ha_spark_addon/config.yaml` `options`, and `config.yaml` `schema` must stay in sync — `tests/test_config.py` enforces it.
- **No `chargers.py`/`scheduler.py` semantic changes** beyond adding the single isolated `run_v2l_tick` call — avoids colliding with the in-flight Phase 7 device-driver refactor on `worktree-feat-agent-surface`.
- **Versioning deferred.** Tonight is dev-mode only; no add-on version bump in this plan. When shipped later, coordinate the `config.yaml` version with Phase 7 (also targeting `0.14.0`).

## Shared module constants (defined once in `v2l.py`, Task 2)

```python
_IDLE_W = 50.0            # power below this = V2L idle/stopped
_DT_CLAMP_S = 300.0       # integration gap ceiling (restart-safe)
_PLUG_IN_LEAD_MIN = 20.0  # N3 predictive lead time
_CUTOFF_WINDOW_MIN = 120.0  # N1 only fires within this many minutes after cutoff
```

## File Structure

- Modify `ha_spark/config.py` — 7 new `Settings` fields + 7 keys in `_OPTION_KEYS`.
- Modify `ha_spark_addon/config.yaml` — 7 `options` defaults + 7 `schema` entries.
- Create `ha_spark/energy/v2l.py` — session, pure math/decision, IO, daemon tick.
- Modify `ha_spark/energy/scheduler.py` — one guarded `run_v2l_tick` call in `run_forever`.
- Modify `ha_spark/cli.py` — `v2l` subparser, dispatch line, `_cmd_v2l` handler.
- Create `tests/test_v2l.py` — pure + IO + tick tests.
- Modify `tests/test_config.py` — assert the new keys load (sync test already covers parity).

---

### Task 1: Config options

**Files:**
- Modify: `ha_spark/config.py` (add fields + `_OPTION_KEYS` entries)
- Modify: `ha_spark_addon/config.yaml` (`options` + `schema`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.v2l_power_entity: str`, `Settings.v2l_round_trip_efficiency: float`, `Settings.v2l_peak_rate_gbp: float`, `Settings.v2l_offpeak_rate_gbp: float`, `Settings.v2l_cutoff_time: str`, `Settings.v2l_notify_service: str`, `Settings.v2l_budget_kwh: float`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_v2l_options_load_from_overlay() -> None:
    from ha_spark.config import Settings

    s = Settings(
        v2l_power_entity="sensor.car_v2l_power",
        v2l_round_trip_efficiency=0.8,
        v2l_peak_rate_gbp=0.32,
        v2l_offpeak_rate_gbp=0.07,
        v2l_cutoff_time="01:00",
        v2l_notify_service="mobile_app_phone",
        v2l_budget_kwh=5.0,
    )
    assert s.v2l_power_entity == "sensor.car_v2l_power"
    assert s.v2l_round_trip_efficiency == 0.8
    assert s.v2l_budget_kwh == 5.0
    # defaults
    assert Settings().v2l_power_entity == ""
    assert Settings().v2l_round_trip_efficiency == 0.85
    assert Settings().v2l_cutoff_time == "01:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_v2l_options_load_from_overlay -v`
Expected: FAIL — `Settings` rejects unknown kwargs / attribute missing.

- [ ] **Step 3: Add the fields to `Settings`**

In `ha_spark/config.py`, after the agent-surface fields block (the `agent_expose_port` field, ~line 276), add:

```python
    # --- V2L (Vehicle-to-Load) observe + tally + notify ---
    # The car's V2L discharge-power sensor (W). Empty disables the feature.
    v2l_power_entity: str = Field(default="")
    # Round-trip AC->DC->AC->DC efficiency, used to discount the cost of
    # refilling the car later. One calibration knob folding both DC<->AC stages.
    v2l_round_trip_efficiency: float = Field(default=0.85)
    # GBP/kWh import rate being offset now (peak) vs the cheap rate to refill.
    v2l_peak_rate_gbp: float = Field(default=0.30)
    v2l_offpeak_rate_gbp: float = Field(default=0.07)
    # Local time (HH:MM) the cheap window starts; the unplug nudge fires here.
    v2l_cutoff_time: str = Field(default="01:00")
    # HA notify.<service> target (e.g. mobile_app_x). Empty disables notifications.
    v2l_notify_service: str = Field(default="")
    # Optional V2L budget (kWh) standing in for car SoC; 0 disables the
    # predictive plug-in warning.
    v2l_budget_kwh: float = Field(default=0.0)
```

- [ ] **Step 4: Add the keys to `_OPTION_KEYS`**

In `ha_spark/config.py`, inside the `_OPTION_KEYS` frozenset, after the agent-surface keys (`"agent_expose_port"`), add:

```python
        # V2L observe + tally + notify.
        "v2l_power_entity",
        "v2l_round_trip_efficiency",
        "v2l_peak_rate_gbp",
        "v2l_offpeak_rate_gbp",
        "v2l_cutoff_time",
        "v2l_notify_service",
        "v2l_budget_kwh",
```

- [ ] **Step 5: Add `options` defaults to the add-on config**

In `ha_spark_addon/config.yaml`, in the `options:` block (near `agent_surface`), add:

```yaml
  v2l_power_entity: ""
  v2l_round_trip_efficiency: 0.85
  v2l_peak_rate_gbp: 0.30
  v2l_offpeak_rate_gbp: 0.07
  v2l_cutoff_time: "01:00"
  v2l_notify_service: ""
  v2l_budget_kwh: 0.0
```

- [ ] **Step 6: Add `schema` entries to the add-on config**

In `ha_spark_addon/config.yaml`, in the `schema:` block, add (mirroring `grid_power_entity: str?` and `plan_run_time: match(...)`):

```yaml
  v2l_power_entity: str?
  v2l_round_trip_efficiency: float
  v2l_peak_rate_gbp: float
  v2l_offpeak_rate_gbp: float
  v2l_cutoff_time: match(^\d{2}:\d{2}$)
  v2l_notify_service: str?
  v2l_budget_kwh: float
```

- [ ] **Step 7: Run the config tests**

Run: `pytest tests/test_config.py -q`
Expected: PASS — both the new test and the existing `_OPTION_KEYS` ↔ `config.yaml` sync test.

- [ ] **Step 8: Quality gates + commit**

```bash
ruff check . && mypy ha_spark && pytest tests/test_config.py -q
git add ha_spark/config.py ha_spark_addon/config.yaml tests/test_config.py
git commit -m "feat(config): V2L observe/notify options"
```

---

### Task 2: Pure core — session, integrate, savings, payload

**Files:**
- Create: `ha_spark/energy/v2l.py`
- Test: `tests/test_v2l.py`

**Interfaces:**
- Produces:
  - `V2LSession` dataclass with fields `day: str`, `kwh_delivered: float = 0.0`, `last_power_w: float = 0.0`, `peak_power_w: float = 0.0`, `last_sample_ts: str | None = None`, `active: bool = False`, `notified_unplug: bool = False`, `notified_plug_in: bool = False`, `notified_budget: bool = False`.
  - `Entity = tuple[str, str, dict[str, Any]]`
  - `integrate(prev_kwh: float, power_w: float, dt_s: float) -> float`
  - `savings(kwh: float, peak: float, offpeak: float, eff: float) -> tuple[float, float, float]` returning `(avoided, refill_cost, net)`.
  - `payload(session: V2LSession, settings: Settings) -> list[Entity]`
  - module constants `_IDLE_W`, `_DT_CLAMP_S`, `_PLUG_IN_LEAD_MIN`, `_CUTOFF_WINDOW_MIN`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_v2l.py`:

```python
"""Tests for the V2L observe + tally + notify surface."""

from __future__ import annotations

from ha_spark.config import Settings
from ha_spark.energy.v2l import V2LSession, integrate, payload, savings


def test_integrate_rectangle() -> None:
    # 2000 W for 1800 s (30 min) = 1.0 kWh
    assert integrate(0.0, 2000.0, 1800.0) == 1.0
    # accumulates onto the prior total
    assert integrate(1.0, 2000.0, 1800.0) == 2.0


def test_integrate_clamps_long_gap() -> None:
    # a 1-hour gap is clamped to _DT_CLAMP_S (300 s): 1000 W * 300/3600 = 0.0833 kWh
    got = integrate(0.0, 1000.0, 3600.0)
    assert abs(got - (1000.0 / 1000.0) * (300.0 / 3600.0)) < 1e-9


def test_savings_discounts_refill_by_efficiency() -> None:
    # 10 kWh, peak 0.30, offpeak 0.07, eff 0.85
    avoided, refill, net = savings(10.0, 0.30, 0.07, 0.85)
    assert avoided == 3.0
    assert abs(refill - (10.0 / 0.85) * 0.07) < 1e-9
    assert abs(net - (3.0 - (10.0 / 0.85) * 0.07)) < 1e-9


def test_savings_net_can_go_negative() -> None:
    # peak below offpeak/eff -> using V2L costs more than it saves
    _, _, net = savings(10.0, 0.05, 0.10, 0.85)
    assert net < 0


def test_savings_zero_efficiency_is_safe() -> None:
    avoided, refill, net = savings(10.0, 0.30, 0.07, 0.0)
    assert refill == 0.0
    assert net == avoided


def test_payload_maps_three_sensors() -> None:
    s = Settings(v2l_peak_rate_gbp=0.30, v2l_offpeak_rate_gbp=0.07, v2l_round_trip_efficiency=0.85)
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, last_power_w=1400.0, peak_power_w=1500.0)
    by_id = {eid: (state, attrs) for eid, state, attrs in payload(sess, s)}
    assert by_id["sensor.ha_spark_v2l_power_w"][0] == "1400"
    assert by_id["sensor.ha_spark_v2l_power_w"][1]["device_class"] == "power"
    assert by_id["sensor.ha_spark_v2l_energy_kwh"][0] == "2.00"
    assert by_id["sensor.ha_spark_v2l_net_saving_gbp"][1]["device_class"] == "monetary"
    assert "avoided_gbp" in by_id["sensor.ha_spark_v2l_net_saving_gbp"][1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2l.py -v`
Expected: FAIL — `ha_spark.energy.v2l` does not exist.

- [ ] **Step 3: Write the module**

Create `ha_spark/energy/v2l.py`:

```python
"""V2L (Vehicle-to-Load) observe + tally + notify.

ha-spark reads the car's V2L discharge-power sensor (W), integrates it into the
energy delivered this session, values it against the configured tariff (less a
round-trip efficiency), publishes sensor.ha_spark_v2l_*, and fires timely HA
notifications. V2L is a manual physical adapter with no control API: this is
read/observe + notify only. The planner and chargers are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ha_spark.config import Settings

# ponytail: rectangle integration + dt clamp; upgrade to trapezoid only if the
# 60 s tick proves too coarse (it won't for kWh-scale tallies).
_IDLE_W = 50.0            # power below this = V2L idle/stopped
_DT_CLAMP_S = 300.0       # integration gap ceiling (restart-safe)
_PLUG_IN_LEAD_MIN = 20.0  # N3 predictive lead time
_CUTOFF_WINDOW_MIN = 120.0  # N1 fires only within this many minutes after cutoff

Entity = tuple[str, str, dict[str, Any]]


@dataclass
class V2LSession:
    """The running tally for one V2L session, persisted across restarts."""

    day: str  # local ISO date the session belongs to (drives the daily reset)
    kwh_delivered: float = 0.0
    last_power_w: float = 0.0
    peak_power_w: float = 0.0
    last_sample_ts: str | None = None  # ISO of the last sample; None until first
    active: bool = False
    notified_unplug: bool = False
    notified_plug_in: bool = False
    notified_budget: bool = False


def integrate(prev_kwh: float, power_w: float, dt_s: float) -> float:
    """Add one rectangle of energy (kWh) to the running total.

    ``dt_s`` is clamped to ``_DT_CLAMP_S`` so a long downtime gap (e.g. a daemon
    restart) cannot inflate the tally with a single huge interval.
    """
    dt_s = min(dt_s, _DT_CLAMP_S)
    return prev_kwh + (power_w / 1000.0) * (dt_s / 3600.0)


def savings(kwh: float, peak: float, offpeak: float, eff: float) -> tuple[float, float, float]:
    """Return ``(avoided, refill_cost, net)`` GBP for ``kwh`` delivered via V2L.

    The V2L sensor reads AC out of the car, so ``kwh`` offsets peak import
    directly. The losses bite on the refill: putting ``kwh`` back into the car
    draws ``kwh / eff`` from the grid at the cheap rate. ``net`` may be negative.
    """
    avoided = kwh * peak
    refill = (kwh / eff) * offpeak if eff > 0 else 0.0
    return avoided, refill, avoided - refill


def payload(session: V2LSession, settings: Settings) -> list[Entity]:
    """Map the session to (entity_id, state, attributes) sensor tuples."""
    avoided, refill, net = savings(
        session.kwh_delivered,
        settings.v2l_peak_rate_gbp,
        settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency,
    )
    return [
        (
            "sensor.ha_spark_v2l_power_w",
            f"{session.last_power_w:.0f}",
            {
                "friendly_name": "ha-spark V2L power",
                "unit_of_measurement": "W",
                "device_class": "power",
            },
        ),
        (
            "sensor.ha_spark_v2l_energy_kwh",
            f"{session.kwh_delivered:.2f}",
            {
                "friendly_name": "ha-spark V2L energy",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
            },
        ),
        (
            "sensor.ha_spark_v2l_net_saving_gbp",
            f"{net:.2f}",
            {
                "friendly_name": "ha-spark V2L net saving",
                "unit_of_measurement": "GBP",
                "device_class": "monetary",
                "avoided_gbp": round(avoided, 2),
                "refill_cost_gbp": round(refill, 2),
                "peak_power_w": round(session.peak_power_w, 0),
            },
        ),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_v2l.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Quality gates + commit**

```bash
ruff check . && mypy ha_spark && pytest tests/test_v2l.py -q
git add ha_spark/energy/v2l.py tests/test_v2l.py
git commit -m "feat(v2l): session model, integrate, savings, payload"
```

---

### Task 3: Pure session evolution — `apply_sample`

**Files:**
- Modify: `ha_spark/energy/v2l.py`
- Test: `tests/test_v2l.py`

**Interfaces:**
- Consumes: `V2LSession`, `integrate`, `_IDLE_W`, `_DT_CLAMP_S` (Task 2).
- Produces: `apply_sample(session: V2LSession, power_w: float, now: datetime) -> V2LSession` — updates kwh (dt from `last_sample_ts`), `last_power_w`, `peak_power_w`, `active`, `last_sample_ts`; starts a fresh session when the day rolls over **and** V2L is idle.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_v2l.py` (add `from datetime import datetime` and `apply_sample` to imports):

```python
from datetime import datetime

from ha_spark.energy.v2l import apply_sample


def test_apply_sample_first_sample_sets_day_no_integration() -> None:
    now = datetime(2026, 6, 28, 19, 0, 0)
    s = apply_sample(V2LSession(day=""), 1400.0, now)
    assert s.day == "2026-06-28"
    assert s.kwh_delivered == 0.0  # no prior timestamp -> no interval
    assert s.last_power_w == 1400.0
    assert s.active is True
    assert s.last_sample_ts == now.isoformat()


def test_apply_sample_integrates_between_samples() -> None:
    t0 = datetime(2026, 6, 28, 19, 0, 0)
    s = apply_sample(V2LSession(day=""), 2000.0, t0)
    t1 = datetime(2026, 6, 28, 19, 30, 0)  # +1800 s
    s = apply_sample(s, 2000.0, t1)
    assert abs(s.kwh_delivered - 1.0) < 1e-9
    assert s.peak_power_w == 2000.0


def test_apply_sample_marks_idle() -> None:
    t0 = datetime(2026, 6, 28, 19, 0, 0)
    s = apply_sample(V2LSession(day=""), 2000.0, t0)
    s = apply_sample(s, 0.0, datetime(2026, 6, 28, 19, 1, 0))
    assert s.active is False
    assert s.peak_power_w == 2000.0  # peak retained


def test_apply_sample_resets_on_new_day_when_idle() -> None:
    s = V2LSession(day="2026-06-27", kwh_delivered=5.0, notified_unplug=True)
    s = apply_sample(s, 0.0, datetime(2026, 6, 28, 14, 0, 0))
    assert s.day == "2026-06-28"
    assert s.kwh_delivered == 0.0
    assert s.notified_unplug is False


def test_apply_sample_does_not_reset_mid_session_across_midnight() -> None:
    # active past midnight: keep accumulating under the original day
    s = V2LSession(day="2026-06-27", kwh_delivered=5.0, last_sample_ts="2026-06-28T00:59:00")
    s = apply_sample(s, 2000.0, datetime(2026, 6, 28, 1, 0, 0))
    assert s.day == "2026-06-27"
    assert s.kwh_delivered > 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2l.py -k apply_sample -v`
Expected: FAIL — `apply_sample` not defined.

- [ ] **Step 3: Implement `apply_sample`**

Add to `ha_spark/energy/v2l.py` (add `from datetime import datetime` to imports):

```python
def apply_sample(session: V2LSession, power_w: float, now: datetime) -> V2LSession:
    """Fold one power reading into the session and return it (mutates in place).

    Resets to a fresh session only when the calendar day has rolled over AND
    V2L is idle, so a session running across midnight is never cut mid-discharge.
    """
    today = now.date().isoformat()
    if session.day and session.day != today and power_w < _IDLE_W:
        session = V2LSession(day=today)
    if not session.day:
        session.day = today

    if session.last_sample_ts is not None:
        prev = datetime.fromisoformat(session.last_sample_ts)
        dt_s = max(0.0, (now - prev).total_seconds())
    else:
        dt_s = 0.0  # first sample: no interval to integrate

    session.kwh_delivered = integrate(session.kwh_delivered, power_w, dt_s)
    session.last_power_w = power_w
    session.peak_power_w = max(session.peak_power_w, power_w)
    session.active = power_w >= _IDLE_W
    session.last_sample_ts = now.isoformat()
    return session
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_v2l.py -k apply_sample -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Quality gates + commit**

```bash
ruff check . && mypy ha_spark && pytest tests/test_v2l.py -q
git add ha_spark/energy/v2l.py tests/test_v2l.py
git commit -m "feat(v2l): session evolution (apply_sample)"
```

---

### Task 4: Pure notification decision — `notifications`

**Files:**
- Modify: `ha_spark/energy/v2l.py`
- Test: `tests/test_v2l.py`

**Interfaces:**
- Consumes: `V2LSession`, `_IDLE_W`, `_PLUG_IN_LEAD_MIN`, `_CUTOFF_WINDOW_MIN` (Task 2); `Settings` fields `v2l_notify_service`, `v2l_cutoff_time`, `v2l_budget_kwh`, rate/eff knobs.
- Produces:
  - `@dataclass Notice` with `flag: str`, `title: str`, `message: str`.
  - `notifications(session: V2LSession, now: datetime, settings: Settings) -> list[Notice]` — fire-once decision; returns the notices whose trigger holds and whose `flag` is not yet set on the session. Returns `[]` when `v2l_notify_service` is empty. The caller fires each and sets `getattr/setattr(session, notice.flag, True)` only on success.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_v2l.py` (add `Notice`, `notifications` to imports):

```python
from ha_spark.energy.v2l import Notice, notifications


def _nsettings(**kw: object) -> Settings:
    base: dict[str, object] = dict(
        v2l_notify_service="mobile_app_x",
        v2l_cutoff_time="01:00",
        v2l_budget_kwh=0.0,
        v2l_peak_rate_gbp=0.30,
        v2l_offpeak_rate_gbp=0.07,
        v2l_round_trip_efficiency=0.85,
    )
    base.update(kw)
    return Settings(**base)


def test_no_notifications_without_service() -> None:
    s = _nsettings(v2l_notify_service="")
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True)
    assert notifications(sess, datetime(2026, 6, 28, 1, 5), s) == []


def test_n1_unplug_fires_within_cutoff_window_when_active() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 1, 5), s)}
    assert "notified_unplug" in flags


def test_n1_does_not_fire_in_afternoon() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 14, 0), s)}
    assert "notified_unplug" not in flags


def test_n1_fire_once() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.0, active=True, notified_unplug=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 1, 5), s)}
    assert "notified_unplug" not in flags


def test_n2_plug_in_fires_when_idle_after_delivering() -> None:
    s = _nsettings()
    sess = V2LSession(day="2026-06-28", kwh_delivered=3.0, active=False)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 22, 0), s)}
    assert "notified_plug_in" in flags


def test_n2_no_fire_while_active_or_zero() -> None:
    s = _nsettings()
    active = V2LSession(day="2026-06-28", kwh_delivered=3.0, active=True)
    empty = V2LSession(day="2026-06-28", kwh_delivered=0.0, active=False)
    assert "notified_plug_in" not in {n.flag for n in notifications(active, datetime(2026, 6, 28, 22, 0), s)}
    assert "notified_plug_in" not in {n.flag for n in notifications(empty, datetime(2026, 6, 28, 22, 0), s)}


def test_n3_predictive_fires_near_budget() -> None:
    s = _nsettings(v2l_budget_kwh=5.0)
    # 4.9 kWh delivered, 2000 W -> 0.1 kWh to go = 0.05 h = 3 min <= lead(20)
    sess = V2LSession(day="2026-06-28", kwh_delivered=4.9, last_power_w=2000.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 22, 0), s)}
    assert "notified_budget" in flags


def test_n3_disabled_without_budget() -> None:
    s = _nsettings(v2l_budget_kwh=0.0)
    sess = V2LSession(day="2026-06-28", kwh_delivered=4.9, last_power_w=2000.0, active=True)
    flags = {n.flag for n in notifications(sess, datetime(2026, 6, 28, 22, 0), s)}
    assert "notified_budget" not in flags
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2l.py -k "n1 or n2 or n3 or notifications" -v`
Expected: FAIL — `notifications`/`Notice` not defined.

- [ ] **Step 3: Implement `Notice` + `notifications`**

Add to `ha_spark/energy/v2l.py` (add `from ha_spark.energy.sources import parse_time` to imports):

```python
@dataclass
class Notice:
    """One pending HA notification; ``flag`` is the session attr set once fired."""

    flag: str
    title: str
    message: str


def _minutes_after(now: time, cutoff: time) -> float:
    """Minutes from ``cutoff`` to ``now`` within a day, wrapping at midnight."""
    now_m = now.hour * 60 + now.minute
    cut_m = cutoff.hour * 60 + cutoff.minute
    return float((now_m - cut_m) % (24 * 60))


def notifications(session: V2LSession, now: datetime, settings: Settings) -> list[Notice]:
    """Return the fire-once notices whose trigger holds (empty if notify off)."""
    if not settings.v2l_notify_service:
        return []

    out: list[Notice] = []
    _, _, net = savings(
        session.kwh_delivered,
        settings.v2l_peak_rate_gbp,
        settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency,
    )

    # N1 — unplug at cutoff: still discharging within the post-cutoff window.
    cutoff = parse_time(settings.v2l_cutoff_time)
    if (
        not session.notified_unplug
        and session.active
        and _minutes_after(now.time(), cutoff) <= _CUTOFF_WINDOW_MIN
    ):
        out.append(
            Notice(
                "notified_unplug",
                "Unplug V2L",
                f"Cheap window starting — unplug V2L. Tonight: "
                f"{session.kwh_delivered:.1f} kWh, net £{net:.2f}.",
            )
        )

    # N2 — plug in to recharge: delivered something and V2L has now stopped.
    if not session.notified_plug_in and not session.active and session.kwh_delivered > 0:
        out.append(
            Notice(
                "notified_plug_in",
                "Plug in to recharge",
                f"V2L done — {session.kwh_delivered:.1f} kWh pulled. "
                f"Plug the car in to recharge on the cheap rate.",
            )
        )

    # N3 — predictive plug-in: projected to hit the V2L budget within the lead.
    if not session.notified_budget and settings.v2l_budget_kwh > 0:
        remaining = settings.v2l_budget_kwh - session.kwh_delivered
        hit = remaining <= 0
        if not hit and session.last_power_w > 0:
            mins = (remaining / (session.last_power_w / 1000.0)) * 60.0
            hit = mins <= _PLUG_IN_LEAD_MIN
        if hit:
            out.append(
                Notice(
                    "notified_budget",
                    "Car nearing V2L budget",
                    f"Car will reach your V2L budget "
                    f"({settings.v2l_budget_kwh:.0f} kWh) soon — plan to plug in.",
                )
            )
    return out
```

Add `time` to the datetime import line: `from datetime import datetime, time`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_v2l.py -v`
Expected: PASS (all pure tests).

- [ ] **Step 5: Quality gates + commit**

```bash
ruff check . && mypy ha_spark && pytest tests/test_v2l.py -q
git add ha_spark/energy/v2l.py tests/test_v2l.py
git commit -m "feat(v2l): fire-once notification decision"
```

---

### Task 5: IO layer — persistence + notify wrapper

**Files:**
- Modify: `ha_spark/energy/v2l.py`
- Test: `tests/test_v2l.py`

**Interfaces:**
- Consumes: `V2LSession` (Task 2); `Settings.db_path`; `HomeAssistantRest.call_service`.
- Produces:
  - `load_session(settings: Settings) -> V2LSession` (fresh `V2LSession(day="")` if no/invalid file).
  - `save_session(settings: Settings, session: V2LSession) -> None`.
  - `async notify(rest: HomeAssistantRest, service: str, title: str, message: str) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_v2l.py` (add imports: `from pathlib import Path`, `import httpx`, `import respx`, `from ha_spark.ha.rest import HomeAssistantRest`, and `load_session, save_session, notify`):

```python
BASE = "http://ha.test/api"


def test_session_round_trip(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    assert load_session(s).day == ""  # no file yet
    sess = V2LSession(day="2026-06-28", kwh_delivered=2.5, notified_unplug=True)
    save_session(s, sess)
    back = load_session(s)
    assert back.day == "2026-06-28"
    assert back.kwh_delivered == 2.5
    assert back.notified_unplug is True


def test_load_session_tolerates_garbage(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    (tmp_path / "ha_spark_v2l_session.json").write_text("{not json", encoding="utf-8")
    assert load_session(s).day == ""


@respx.mock
async def test_notify_calls_notify_service() -> None:
    route = respx.post(f"{BASE}/services/notify/mobile_app_x").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with HomeAssistantRest(BASE, "token") as rest:
        await notify(rest, "mobile_app_x", "Title", "Body")
    assert route.called
    sent = route.calls.last.request
    assert b"Body" in sent.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2l.py -k "session_round_trip or garbage or notify_calls" -v`
Expected: FAIL — `load_session`/`save_session`/`notify` not defined.

- [ ] **Step 3: Implement the IO helpers**

Add to `ha_spark/energy/v2l.py` (add imports: `import json`, `from dataclasses import asdict`, `from pathlib import Path`, `from ha_spark.ha.rest import HomeAssistantRest`, `from ha_spark.logging import get_logger`, and `log = get_logger(__name__)`):

```python
def _session_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / "ha_spark_v2l_session.json"


def load_session(settings: Settings) -> V2LSession:
    """Load the persisted session, or a fresh one if absent/corrupt."""
    path = _session_path(settings)
    if not path.is_file():
        return V2LSession(day="")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return V2LSession(**data)
    except (OSError, ValueError, TypeError):
        log.warning("Reading V2L session failed; starting fresh", exc_info=True)
        return V2LSession(day="")


def save_session(settings: Settings, session: V2LSession) -> None:
    """Persist the session to /data (best-effort)."""
    path = _session_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(session)), encoding="utf-8")
    except OSError:
        log.warning("Caching V2L session failed", exc_info=True)


async def notify(rest: HomeAssistantRest, service: str, title: str, message: str) -> None:
    """Fire an HA notification via notify.<service>."""
    await rest.call_service("notify", service, {"title": title, "message": message})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_v2l.py -k "session_round_trip or garbage or notify_calls" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Quality gates + commit**

```bash
ruff check . && mypy ha_spark && pytest tests/test_v2l.py -q
git add ha_spark/energy/v2l.py tests/test_v2l.py
git commit -m "feat(v2l): session persistence + notify wrapper"
```

---

### Task 6: Daemon tick — `run_v2l_tick` + wire into `run_forever`

**Files:**
- Modify: `ha_spark/energy/v2l.py`
- Modify: `ha_spark/energy/scheduler.py`
- Test: `tests/test_v2l.py`

**Interfaces:**
- Consumes: `apply_sample`, `payload`, `notifications`, `notify`, `load_session`, `save_session` (Tasks 2–5); `_to_float`, `parse_time` patterns; `HomeAssistantRest`; `Settings`.
- Produces: `async run_v2l_tick(settings: Settings, now: datetime) -> None` — reads the V2L sensor, evolves+persists the session, publishes the sensors, and fires due notifications (setting each `flag` only on success). Best-effort: an unreadable sensor logs a warning and returns.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_v2l.py` (add `from ha_spark.energy.v2l import run_v2l_tick`):

```python
@respx.mock
async def test_run_v2l_tick_integrates_publishes_and_notifies(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test",
        ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
        v2l_notify_service="mobile_app_x",
        v2l_cutoff_time="01:00",
    )
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.car_v2l_power", "state": "2000", "attributes": {}}
        )
    )
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))

    # Seed a prior sample 30 min earlier so this tick integrates ~1 kWh, and an
    # active session past cutoff so N1 fires.
    prior = V2LSession(
        day="2026-06-28", kwh_delivered=0.0, active=True,
        last_sample_ts="2026-06-28T00:35:00",
    )
    save_session(s, prior)

    await run_v2l_tick(s, datetime(2026, 6, 28, 1, 5, 0))

    back = load_session(s)
    assert abs(back.kwh_delivered - 1.0) < 0.05  # ~2000 W * 0.5 h
    assert back.notified_unplug is True  # N1 fired and was flagged
    # both the sensor publish (POST /states/...) and the notify happened
    paths = [c.request.url.path for c in posts.calls]
    assert any(p.endswith("/services/notify/mobile_app_x") for p in paths)
    assert any("sensor.ha_spark_v2l_energy_kwh" in p for p in paths)


@respx.mock
async def test_run_v2l_tick_skips_on_unreadable_sensor(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
    )
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(return_value=httpx.Response(500))
    # must not raise
    await run_v2l_tick(s, datetime(2026, 6, 28, 19, 0, 0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2l.py -k run_v2l_tick -v`
Expected: FAIL — `run_v2l_tick` not defined.

- [ ] **Step 3: Implement `run_v2l_tick`**

Add to `ha_spark/energy/v2l.py` (add `from ha_spark.energy.sources import _to_float, parse_time` — `parse_time` is already imported from Task 4; extend that line to also import `_to_float`):

```python
async def run_v2l_tick(settings: Settings, now: datetime) -> None:
    """One V2L pass: read, integrate, publish sensors, notify, persist.

    Best-effort and self-contained (opens its own REST client), mirroring
    ``scheduler.sample_signals``. An unreadable sensor logs and returns; it
    never raises into the daemon loop.
    """
    session = load_session(settings)
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        try:
            state = await rest.get_state(settings.v2l_power_entity)
        except Exception as exc:  # noqa: BLE001 - never break the loop on bad data
            log.warning("V2L: %s unreadable (%s); skipping", settings.v2l_power_entity, exc)
            return
        power_w = _to_float(state.state, 0.0)
        session = apply_sample(session, power_w, now)

        for entity_id, value, attrs in payload(session, settings):
            try:
                await rest.set_state(entity_id, value, attrs)
            except Exception:  # noqa: BLE001 - publishing is best-effort
                log.warning("Publishing %s failed", entity_id, exc_info=True)

        for notice in notifications(session, now, settings):
            try:
                await notify(rest, settings.v2l_notify_service, notice.title, notice.message)
                setattr(session, notice.flag, True)  # flag only on success
            except Exception:  # noqa: BLE001 - a failed send retries next tick
                log.warning("V2L notify (%s) failed", notice.flag, exc_info=True)

    save_session(settings, session)
```

- [ ] **Step 4: Wire it into `run_forever`**

In `ha_spark/energy/scheduler.py`, add the import near the other energy imports:

```python
from ha_spark.energy.v2l import run_v2l_tick
```

Then in `run_forever`, inside the `while True:` loop, **after** the signal-sampling block and **before** `await asyncio.sleep(poll_seconds)`, add:

```python
            if settings.v2l_power_entity:
                try:
                    await run_v2l_tick(settings, now)
                except Exception:
                    log.exception("V2L tick failed; will retry next tick")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_v2l.py -k run_v2l_tick -v && pytest tests/test_scheduler.py -q`
Expected: PASS — the new tick tests pass and the scheduler suite is unbroken.

- [ ] **Step 6: Quality gates + commit**

```bash
ruff check . && mypy ha_spark && pytest -q
git add ha_spark/energy/v2l.py ha_spark/energy/scheduler.py tests/test_v2l.py
git commit -m "feat(v2l): daemon tick + run_forever wiring"
```

---

### Task 7: CLI — `ha-spark v2l`

**Files:**
- Modify: `ha_spark/cli.py`
- Test: `tests/test_v2l.py`

**Interfaces:**
- Consumes: `load_session`, `savings` (Tasks 2/5); `_to_float` (sources); `HomeAssistantRest`; `Settings`.
- Produces: `async _cmd_v2l(settings: Settings) -> int`; a `v2l` subcommand wired into `build_parser`/`main`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_v2l.py`:

```python
from ha_spark.cli import _cmd_v2l


@respx.mock
async def test_cmd_v2l_prints_tally(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    s = Settings(
        ha_url="http://ha.test", ha_token="token",
        db_path=str(tmp_path / "ha_spark.db"),
        v2l_power_entity="sensor.car_v2l_power",
        v2l_peak_rate_gbp=0.30, v2l_offpeak_rate_gbp=0.07, v2l_round_trip_efficiency=0.85,
    )
    save_session(s, V2LSession(day="2026-06-28", kwh_delivered=2.0, peak_power_w=1500.0))
    respx.get(f"{BASE}/states/sensor.car_v2l_power").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.car_v2l_power", "state": "1400", "attributes": {}}
        )
    )
    rc = await _cmd_v2l(s)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1400 W" in out
    assert "2.00 kWh" in out


async def test_cmd_v2l_unconfigured_returns_2(tmp_path: Path) -> None:
    s = Settings(db_path=str(tmp_path / "ha_spark.db"))
    assert await _cmd_v2l(s) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2l.py -k cmd_v2l -v`
Expected: FAIL — `_cmd_v2l` not importable.

- [ ] **Step 3: Add the handler**

In `ha_spark/cli.py`, add the imports (check existing import lines; add only what's missing):

```python
from ha_spark.energy.sources import _to_float
from ha_spark.energy.v2l import load_session, savings
```

Add the handler near the other `_cmd_*` functions:

```python
async def _cmd_v2l(settings: Settings) -> int:
    """Print the live V2L tally: current power, session energy, and savings."""
    if not settings.v2l_power_entity:
        print("V2L not configured (set v2l_power_entity).", file=sys.stderr)
        return 2
    session = load_session(settings)
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        try:
            power_w = _to_float((await rest.get_state(settings.v2l_power_entity)).state, 0.0)
        except Exception as exc:  # noqa: BLE001 - report and exit non-zero
            print(f"Could not read {settings.v2l_power_entity}: {exc}", file=sys.stderr)
            return 1
    avoided, refill, net = savings(
        session.kwh_delivered,
        settings.v2l_peak_rate_gbp,
        settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency,
    )
    print(f"V2L power now:  {power_w:.0f} W")
    print(f"Session energy: {session.kwh_delivered:.2f} kWh (peak {session.peak_power_w:.0f} W)")
    print(f"Avoided import: £{avoided:.2f}")
    print(f"Refill cost:    £{refill:.2f}")
    print(f"Net benefit:    £{net:.2f}")
    return 0
```

- [ ] **Step 4: Register the subcommand**

In `build_parser`, alongside the other `sub.add_parser(...)` calls, add:

```python
    sub.add_parser(
        "v2l",
        help="Show the live V2L tally (power, session energy, savings)",
        description="Read the V2L discharge-power sensor and print the current "
        "session's energy delivered and efficiency-discounted net saving.",
    )
```

In `main`, alongside the other `if args.command == ...` dispatch lines, add:

```python
    if args.command == "v2l":
        return asyncio.run(_cmd_v2l(settings))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_v2l.py -k cmd_v2l -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Full quality gates + commit**

```bash
ruff check . && mypy ha_spark && pytest -q
git add ha_spark/cli.py tests/test_v2l.py
git commit -m "feat(cli): ha-spark v2l live tally command"
```

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|---|---|
| Config: 7 V2L options + sync | Task 1 |
| `V2LSession`, persistence to `/data` | Task 2 (model), Task 5 (persistence) |
| `integrate` (rectangle + dt clamp) | Task 2 |
| `savings` (efficiency-discounted refill, net can be negative) | Task 2 |
| `payload` → 3 `sensor.ha_spark_v2l_*` | Task 2 |
| Session evolution + daily reset | Task 3 |
| N1 unplug / N2 plug-in / N3 predictive (fire-once, off when no service) | Task 4 |
| `notify` via `call_service("notify", …)` | Task 5 |
| Daemon tick each 60 s when configured, best-effort/isolated | Task 6 |
| `ha-spark v2l` CLI | Task 7 |
| No planner/chargers changes | All (verified — only scheduler gains one isolated call) |
| Security: no secrets, tolerant float coercion | Task 1 (knobs), Task 6 (`_to_float`) |
| Constants `_IDLE_W/_DT_CLAMP_S/_PLUG_IN_LEAD_MIN/_CUTOFF_WINDOW_MIN` | Task 2 (defined), Tasks 3–4 (used) |

Refinement vs spec: N1 uses a post-cutoff window (`_CUTOFF_WINDOW_MIN`) rather than a raw `now >= cutoff`, so it can't false-fire in the afternoon (cutoff is 01:00). This realizes the spec's intent ("at the cutoff") correctly.

**2. Placeholder scan:** None — every code/test step contains complete content.

**3. Type consistency:** `V2LSession`, `Entity`, `Notice.flag`, `savings(...) -> (avoided, refill, net)`, `apply_sample(session, power_w, now)`, `notifications(session, now, settings)`, `run_v2l_tick(settings, now)`, `load_session/save_session(settings[, session])`, `notify(rest, service, title, message)`, `_cmd_v2l(settings)` are used with identical signatures across all tasks. Notification flags (`notified_unplug/plug_in/budget`) match the `Notice.flag` strings set via `setattr` in Task 6.

## Tonight's live test (after the plan is built)

In dev mode (`.env` with `HA_URL` + `HA_TOKEN`), set `v2l_power_entity`, the rates, `v2l_cutoff_time`, and `v2l_notify_service`, then run the daemon (`python -m ha_spark run`) so it integrates each tick. Watch with `python -m ha_spark v2l` or the `sensor.ha_spark_v2l_*` entities in HA; confirm the notifications arrive. No add-on release needed.
