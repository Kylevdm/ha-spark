# Generate Lovelace Dashboard CLI Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ha-spark generate-dashboard --output PATH`, which renders a Lovelace YAML dashboard from the entity-id fields already set in `Settings`, labelling each entity with its live HA `friendly_name` where reachable.

**Architecture:** A new pure-ish builder module (`ha_spark/dashboard.py`) takes `Settings` plus an already-open `HomeAssistantRest` session (same pattern as `energy/sources.py:gather_inputs`), groups configured entity fields into category cards, and returns a plain dict. `cli.py` owns the HTTP session lifecycle and YAML serialization/file write, matching every other `_cmd_*` function.

**Tech Stack:** Python 3.11+, existing `HomeAssistantRest` client, new `PyYAML` runtime dependency.

## Global Constraints

- mypy `strict = true`, `disallow_untyped_defs = true` — every new function fully typed.
- ruff lints `E,F,I,UP,B,ASYNC,W`, line length 100.
- Async I/O only; no `asyncio.run` inside library code — only in `cli.py` dispatch (existing pattern).
- A bad/unreachable HA REST call must degrade (static labels), never crash or raise out of `build_dashboard`.
- No new fuzzy/name-based entity resolution — only the raw `entity_id` fields already in `Settings`.
- Tests mock HTTP with `respx`; pytest runs in `asyncio_mode = "auto"`.

---

### Task 1: Add PyYAML dependency

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `yaml` module importable as a runtime dependency; `types-PyYAML` available for mypy strict.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, under `[project]` `dependencies`, add `"pyyaml>=6.0"`:

```toml
dependencies = [
    "httpx>=0.27",
    "websockets>=12.0",
    "aiosqlite>=0.20",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "aiohttp>=3.9",
    "pyyaml>=6.0",
]
```

Under `[project.optional-dependencies]` `dev`, add `"types-PyYAML>=6.0"` (mypy strict needs stubs for PyYAML, which ships none):

```toml
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.5",
    "mypy>=1.10",
    "types-PyYAML>=6.0",
]
```

- [ ] **Step 2: Install**

Run: `pip install -e ".[dev]"`
Expected: installs `pyyaml` and `types-pyyaml` with no errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: add PyYAML dependency for dashboard generation"
```

---

### Task 2: `build_dashboard` in `ha_spark/dashboard.py`

**Files:**
- Create: `ha_spark/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `ha_spark.config.Settings` (string entity-id fields, all default `""` except `outdoor_weather_entity` default `"weather.home"`); `ha_spark.ha.rest.HomeAssistantRest.get_states() -> list[EntityState]` where `EntityState.friendly_name -> str` falls back to `entity_id` itself; `httpx.HTTPError` is the exception base raised by `get_states()` on network/HTTP failure.
- Produces: `async def build_dashboard(settings: Settings, rest: HomeAssistantRest) -> dict[str, Any]` — a dict with shape `{"title": "ha-spark", "views": [{"title": "Energy", "cards": [...]}]}`, where each card is `{"type": "entities", "title": <category>, "entities": [{"entity": <entity_id>, "name": <label>}, ...]}`. Used by Task 3's `_cmd_generate_dashboard`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dashboard.py`:

```python
"""Tests for Lovelace dashboard generation from configured Settings."""

from __future__ import annotations

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.dashboard import build_dashboard
from ha_spark.ha.rest import HomeAssistantRest


def _states_json() -> list[dict[str, object]]:
    return [
        {
            "entity_id": "sensor.solisac_battery_soc",
            "state": "82",
            "attributes": {"friendly_name": "Battery SoC (Live)"},
        },
        {
            "entity_id": "sensor.solcast_forecast",
            "state": "12.3",
            "attributes": {"friendly_name": "Solar Forecast Tomorrow"},
        },
    ]


@respx.mock
async def test_build_dashboard_groups_configured_fields_with_live_names() -> None:
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=_states_json())
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        soc_entity="sensor.solisac_battery_soc",
        solar_tomorrow_entity="sensor.solcast_forecast",
    )
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    cards = dashboard["views"][0]["cards"]
    titles = [c["title"] for c in cards]
    assert "Battery" in titles
    assert "Solar" in titles

    battery = next(c for c in cards if c["title"] == "Battery")
    assert battery["entities"] == [
        {"entity": "sensor.solisac_battery_soc", "name": "Battery SoC (Live)"}
    ]
    solar = next(c for c in cards if c["title"] == "Solar")
    assert solar["entities"] == [
        {"entity": "sensor.solcast_forecast", "name": "Solar Forecast Tomorrow"}
    ]


@respx.mock
async def test_build_dashboard_skips_unconfigured_categories() -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    settings = Settings(ha_url="http://ha.test", ha_token="t", soc_entity="sensor.soc")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    cards = dashboard["views"][0]["cards"]
    titles = [c["title"] for c in cards]
    # outdoor_weather_entity defaults to weather.home, so "Other" is always present.
    assert titles == ["Battery", "Other"]


@respx.mock
async def test_build_dashboard_falls_back_to_static_labels_when_ha_unreachable() -> None:
    respx.get("http://ha.test/api/states").mock(side_effect=httpx.ConnectError("refused"))
    settings = Settings(ha_url="http://ha.test", ha_token="t", soc_entity="sensor.soc")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    battery = next(
        c for c in dashboard["views"][0]["cards"] if c["title"] == "Battery"
    )
    assert battery["entities"] == [{"entity": "sensor.soc", "name": "Battery SoC"}]


@respx.mock
async def test_build_dashboard_adds_people_card_from_csv_field() -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        person_entities="person.alice, person.bob",
    )
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    people = next(c for c in dashboard["views"][0]["cards"] if c["title"] == "People")
    assert people["entities"] == [
        {"entity": "person.alice", "name": "person.alice"},
        {"entity": "person.bob", "name": "person.bob"},
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dashboard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ha_spark.dashboard'`

- [ ] **Step 3: Implement `ha_spark/dashboard.py`**

```python
"""Build a Lovelace dashboard dict from the entity-id fields set in Settings.

Pure-ish: takes an already-open HomeAssistantRest session (cli.py owns the
session lifecycle, matching energy/sources.py:gather_inputs). Never raises —
an unreachable HA degrades to static labels rather than failing the command.
"""

from __future__ import annotations

from typing import Any

import httpx

from ha_spark.config import Settings
from ha_spark.ha.rest import HomeAssistantRest

# (category title, [(Settings field name, static fallback label), ...])
_CATEGORIES: list[tuple[str, list[tuple[str, str]]]] = [
    ("Battery", [
        ("soc_entity", "Battery SoC"),
        ("battery_voltage_entity", "Battery Voltage"),
    ]),
    ("Solar", [
        ("solar_tomorrow_entity", "Solar Forecast (Tomorrow)"),
    ]),
    ("EV / Charger", [
        ("ev_plug_entity", "EV Plug"),
        ("ev_status_entity", "EV Status"),
        ("charge_current_entity", "Charge Current"),
        ("charge_window_start_entity", "Charge Window Start"),
        ("charge_window_end_entity", "Charge Window End"),
        ("ha_template_charge_needed_entity", "Charge Needed"),
    ]),
    ("Grid & Tariff", [
        ("octopus_rate_entity", "Octopus Rate"),
        ("dispatch_entity", "Dispatch"),
        ("grid_power_entity", "Grid Power"),
    ]),
    ("Other", [
        ("consumption_energy_entity", "House Consumption"),
        ("inverter_power_switch_entity", "Inverter Power Switch"),
        ("heatpump_energy_entity", "Heat Pump Energy"),
        ("outdoor_weather_entity", "Outdoor Weather"),
        ("backfill_source_entity", "Backfill Source"),
    ]),
]


def _entity_card(title: str, entities: list[dict[str, str]]) -> dict[str, Any]:
    return {"type": "entities", "title": title, "entities": entities}


async def build_dashboard(settings: Settings, rest: HomeAssistantRest) -> dict[str, Any]:
    """Render a single-view Lovelace dashboard from configured entity fields."""
    names: dict[str, str] = {}
    try:
        states = await rest.get_states()
        names = {s.entity_id: s.friendly_name for s in states}
    except httpx.HTTPError:
        pass  # HA unreachable: fall back to static labels below.

    cards: list[dict[str, Any]] = []
    for title, fields in _CATEGORIES:
        entities = [
            {"entity": entity_id, "name": names.get(entity_id, label)}
            for field, label in fields
            if (entity_id := getattr(settings, field))
        ]
        if entities:
            cards.append(_entity_card(title, entities))

    people = [p.strip() for p in settings.person_entities.split(",") if p.strip()]
    if people:
        cards.append(
            _entity_card("People", [{"entity": p, "name": names.get(p, p)} for p in people])
        )

    return {"title": "ha-spark", "views": [{"title": "Energy", "cards": cards}]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dashboard.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint and type-check**

Run: `ruff check ha_spark/dashboard.py tests/test_dashboard.py && mypy ha_spark/dashboard.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add ha_spark/dashboard.py tests/test_dashboard.py
git commit -m "feat: build Lovelace dashboard dict from configured entity fields"
```

---

### Task 3: Wire `generate-dashboard` into the CLI

**Files:**
- Modify: `ha_spark/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `ha_spark.dashboard.build_dashboard(settings, rest) -> dict[str, Any]` from Task 2; `ha_spark.ha.rest.HomeAssistantRest` (existing); `yaml.safe_dump` from PyYAML (Task 1).
- Produces: `async def _cmd_generate_dashboard(settings: Settings, *, output: str) -> int`; CLI subcommand `generate-dashboard --output PATH`.

- [ ] **Step 1: Write the failing CLI test**

In `tests/test_cli.py`, add to the imports from `ha_spark.cli`:

```python
from ha_spark.cli import (
    _cmd_ask,
    _cmd_backfill_load,
    _cmd_backtest,
    _cmd_context,
    _cmd_forecast_eval,
    _cmd_generate_dashboard,
    _cmd_import_csv,
    _cmd_learn_factors,
    _cmd_onboard,
    _cmd_run,
    build_parser,
)
```

And add a new import at the top alongside the others:

```python
import yaml
```

Then append a new test (anywhere after the existing `import-csv` tests, before `test_help_mentions_every_command_and_flags`):

```python
@respx.mock
async def test_generate_dashboard_writes_yaml_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "sensor.soc",
                    "state": "80",
                    "attributes": {"friendly_name": "Battery SoC (Live)"},
                }
            ],
        )
    )
    settings = Settings(ha_url="http://ha.test", ha_token="t", soc_entity="sensor.soc")
    out_path = tmp_path / "dash.yaml"

    rc = await _cmd_generate_dashboard(settings, output=str(out_path))

    assert rc == 0
    assert f"Wrote dashboard to {out_path}" in capsys.readouterr().out
    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    cards = written["views"][0]["cards"]
    battery = next(c for c in cards if c["title"] == "Battery")
    assert battery["entities"] == [
        {"entity": "sensor.soc", "name": "Battery SoC (Live)"}
    ]
```

Also update `test_help_mentions_every_command_and_flags` to include the new command in the checked tuple:

```python
    for command in (
        "states", "health", "onboard", "plan", "ask", "run",
        "backfill-load", "import-csv", "pull-consumption", "backtest", "forecast-eval",
        "context", "learn-factors", "generate-dashboard",
    ):
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -k generate_dashboard -v`
Expected: FAIL — `ImportError: cannot import name '_cmd_generate_dashboard'`

- [ ] **Step 3: Implement the command in `ha_spark/cli.py`**

Edit the imports at the top of `ha_spark/cli.py`. There's currently no third-party import group (only stdlib, then a blank line, then `ha_spark.*`). Add a new third-party group with `import yaml` between them:

```python
import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from ha_spark.config import ConfigError, Settings, load_settings
```

Add `from ha_spark.dashboard import build_dashboard` into the existing alphabetically-sorted `from ha_spark...` block, right after the `from ha_spark.config import ...` line:

```python
from ha_spark.config import ConfigError, Settings, load_settings
from ha_spark.dashboard import build_dashboard
from ha_spark.energy import habits
```

Add the command function near `_cmd_onboard` (after it, before `_cmd_backfill_load`):

```python
async def _cmd_generate_dashboard(settings: Settings, *, output: str) -> int:
    """Render a Lovelace dashboard from configured entity fields and write it to disk."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        dashboard = await build_dashboard(settings, rest)
    Path(output).write_text(yaml.safe_dump(dashboard, sort_keys=False), encoding="utf-8")
    print(f"Wrote dashboard to {output}")
    return 0
```

In `build_parser()`, add a subparser after the `onboard` block (after line 526's closing of `p_onboard`, before `p_plan = sub.add_parser("plan", ...)`):

```python
    p_dash = sub.add_parser(
        "generate-dashboard",
        help="Render a Lovelace dashboard YAML file from configured entity fields",
        description="Build a Lovelace dashboard from whichever entity-id fields are "
        "already set in config (battery SoC, solar, EV/charger, grid/tariff, ...), "
        "labelling each with its live HA friendly_name where reachable. Re-run any "
        "time config changes — no onboarding re-run needed.",
    )
    p_dash.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="File path to write the Lovelace YAML to",
    )
```

In `main()`, add dispatch after the `onboard` block (after the `if args.command == "onboard":` block, before `if args.command == "backfill-load":`):

```python
    if args.command == "generate-dashboard":
        return asyncio.run(_cmd_generate_dashboard(settings, output=args.output))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (all tests, including the new one and the updated help-text test)

- [ ] **Step 5: Full quality gate**

Run: `ruff check . && mypy ha_spark && pytest -q`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add ha_spark/cli.py tests/test_cli.py
git commit -m "feat(cli): add generate-dashboard command"
```

---

## Verification

1. In standalone dev mode (`HA_URL`/`HA_TOKEN` set in `.env`, with at least one `*_entity` field configured):
   ```bash
   ha-spark generate-dashboard --output /tmp/dash.yaml
   cat /tmp/dash.yaml
   ```
   Confirm it's valid YAML with a `views[0].cards` list grouping your configured entities.
2. Paste the file contents into Home Assistant's Lovelace "Edit Dashboard" → "Raw configuration editor" and confirm it renders without errors.
3. Stop your HA instance (or point `HA_URL` at an unreachable address) and re-run — confirm the command still exits 0 and writes a file with static labels (e.g. "Battery SoC") instead of crashing.
4. `ruff check . && mypy ha_spark && pytest -q` all green.
