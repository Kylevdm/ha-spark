# CLAUDE.md

`ha-spark` — local-first energy autopilot for Home Assistant. A deterministic
planner decides battery setpoints; an LLM only explains. Ships as an HA add-on.
Python 3.11+ / asyncio. See `ROADMAP.md`, `README.md`, and the code (source of truth).

## Security (hard rules — this process holds HA credentials and actuates hardware)

- **Never log/echo secrets** (`SUPERVISOR_TOKEN`/`auth_token`, `HA_TOKEN`, `octopus_api_key`, any key). Mark secret options `password`; don't commit `.env`.
- **Everything from outside the process is untrusted** — coerce with `_to_float`/`_opt_float`, validate with pydantic before persisting/acting. A bad payload degrades, never crashes or actuates.
- **The LLM never controls hardware** — it explains or proposes reviewable facts; the planner is the sole decider. It never reaches `call_service`.
- **Actuation invariants:** real writes only when `PROACTIVE_MODE == on` (and `control == ha_spark`); never on invalid SoC; always with read-back; failures isolated per action. See `energy/chargers.py`, `energy/supply_guard.py`.
- **Parameterised SQL only**; no `eval`/`exec`/`shell=True`, no shell interpolation of external data. **Raw `entity_id` addressing only** — no fuzzy name matching on control paths.
- **Dependencies pinned and minimal**; the add-on installs ha-spark from a pinned `vX.Y.Z` tag, not a branch.

## Quality gates (all green before merge)

```bash
ruff check .   # E,F,I,UP,B,ASYNC,W, line length 100
mypy ha_spark  # strict = true
pytest -q      # every module has tests; asyncio_mode = "auto"
```

## Pointers

- Releasing (dual version+tag footgun, add-on base image): `docs/releasing.md`
- Issues: GitHub (`Kylevdm/ha-spark`) via `gh` — see `docs/agents/`
