# Generate Lovelace Dashboard CLI Command

## Context

Onboarding (`ha-spark onboard`) discovers and maps Home Assistant entities into
`Settings` fields (battery SoC, solar, EV charger, grid/tariff, etc.), but the
result lives only in config — there's no way to see those entities in HA's UI
without manually building a Lovelace dashboard. The user wants a CLI command
that turns the *already-configured* entity mapping into a ready-to-paste
Lovelace YAML dashboard, repeatable any time config changes, without having to
re-run the interactive onboarding discovery flow.

## Command

```
ha-spark generate-dashboard --output PATH
```

- `--output PATH` is required (e.g. `lovelace_dashboard.yaml`).
- Async command, dispatched via `asyncio.run` in `main()`, same pattern as
  `states`.

## Behavior

1. Load `Settings` via the existing `load_settings()` — no new discovery, no
   change to onboarding.
2. Open one `HomeAssistantRest` session and call `get_states()` once to build
   an `entity_id -> friendly_name` lookup, used purely to label cards nicely.
   If the call fails (HA unreachable, timeout, auth error), catch it and
   continue with no lookup — every label then falls back to a static
   per-category name. This must never raise; a dashboard with plain labels is
   strictly better than a crash.
3. Group configured entity-id fields into categories, building one
   `type: entities` card per non-empty category:
   - **Battery**: `soc_entity`, `battery_voltage_entity`
   - **Solar**: `solar_tomorrow_entity`
   - **EV / Charger**: `ev_plug_entity`, `ev_status_entity`, `charge_current_entity`,
     `charge_window_start_entity`, `charge_window_end_entity`,
     `ha_template_charge_needed_entity`
   - **Grid & Tariff**: `octopus_rate_entity`, `dispatch_entity`, `grid_power_entity`
   - **Other**: `consumption_energy_entity`, `inverter_power_switch_entity`,
     `heatpump_energy_entity`, `outdoor_weather_entity`, `backfill_source_entity`
   - `person_entities` (comma-separated) becomes its own **People** card if set.
   A category with zero configured fields produces no card at all.
4. Each card entry is `{"entity": entity_id, "name": friendly_name_or_fallback}`.
   Fallback name when no live lookup is available: a static title derived from
   the field (e.g. `soc_entity` → "Battery SoC") — same labels used in
   `onboarding_discover.py`'s existing field descriptions, reused rather than
   redefined.
5. Assemble into a single-view Lovelace dashboard dict:
   ```yaml
   title: ha-spark
   views:
     - title: Energy
       cards: [...]
   ```
6. Serialize with `yaml.safe_dump` and write to `--output`. Print the path on
   success.

## New module

`ha_spark/dashboard.py`:
- `async def build_dashboard(settings: Settings) -> dict[str, Any]` — pure
  builder (testable without writing files), returns the dashboard dict.
- CLI glue (`_cmd_generate_dashboard`) lives in `cli.py` alongside other
  `_cmd_*` functions, calls `build_dashboard` then writes YAML to disk.

## New dependency

`PyYAML` — not currently in `pyproject.toml`. Add as a runtime dependency
(`pyyaml`), pinned per project convention.

## Out of scope

- No custom/HACS cards (mini-graph, gauge) — plain `entities` cards only.
- No multi-view layout — one view, one card per category.
- No write-back to HA (this only ever produces a local YAML file).
- No changes to `onboarding_discover.py` or the `onboard` command.

## Testing

- Unit test `build_dashboard` with `respx`-mocked `get_states()`: configured
  settings + mocked friendly names → expected card grouping and labels.
- Unit test the HA-unreachable fallback path (mock raises) → static labels,
  no exception.
- Unit test that an unconfigured category produces no card.
- CLI-level test (existing pattern in `tests/`) asserting `--output` writes a
  parseable YAML file with the `views` key.

## Verification

- `ha-spark generate-dashboard --output /tmp/dash.yaml` against a configured
  `.env` (standalone dev mode) produces valid YAML; paste into HA's Lovelace
  YAML editor (or `ha dashboards` import) to confirm it renders without
  errors.
- `ruff check . && mypy ha_spark && pytest -q` green.
