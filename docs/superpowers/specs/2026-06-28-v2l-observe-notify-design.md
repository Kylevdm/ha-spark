# V2L observe + tally + notify — design spec

*Date: 2026-06-28. Status: approved shape, pending spec review.*

## Summary

A **tactical V2L spike**: make ha-spark aware of the car's Vehicle-to-Load (V2L)
discharge so it can *observe* the energy flowing out of the car, *tally* it into
kWh and an efficiency-discounted £ saving, and *notify* the user at the right
moments (unplug at the cheap-window cutoff, plug in to recharge when V2L stops,
and a predictive "you'll need to plug in soon" heads-up).

V2L is a **manual physical adapter** with no control API, so ha-spark only ever
reads it. The single signal available today is a **V2L discharge-power sensor
(W)** — there is no car SoC and no whole-house load sensor. This feature is
read/observe + notify only: **it does not touch the planner or `chargers.py`.**

## Goals

1. **Unplug nudge at the cutoff** (priority #1) — HA notification at the moment
   the cheap import window starts, so the user unplugs V2L and stops paying the
   peak rate to feed the house.
2. **Live tally + savings** (priority #2) — integrate the V2L power sensor into
   kWh delivered this session and an efficiency-discounted net £ benefit;
   surface it as `sensor.ha_spark_v2l_*` and via a `ha-spark v2l` CLI command.
3. **Plug-in notifications** (priority #3) — a predictive "you'll likely need to
   plug in soon" warning (against a user budget) and a reactive "plug in to
   recharge" when V2L stops.
4. Be **testable tonight** against the real car in dev/standalone mode
   (`HA_URL` + `HA_TOKEN`), with no add-on release required.

## Non-goals

- **No control of V2L.** It is manual; ha-spark never actuates it.
- **No planner or `chargers.py` changes.** The overnight charge plan already
  reads live SoC at plan time, so the V2L offset is captured implicitly. This
  keeps the diff small and avoids colliding with the in-flight Phase 7
  device-driver refactor (which rewrites `chargers.py`/`scheduler.py` wiring).
- **No TariffProvider integration.** Dedicated `v2l_peak/offpeak_rate` knobs are
  used deliberately; Phase 8's `TariffProvider` will supersede them later.
- **No car-SoC modelling.** Not available; the "budget" knob stands in for it.

## Relationship to the roadmap

This is **out of roadmap order** — a precursor to Phase 9 ("reads V2L
availability") and Phase 10 ("V2L-fed from the car" + NOTIFY action). It is a
standalone observe/tally/notify surface, so it integrates cleanly later: Phase 9
can lift the V2L reading into the EV driver, Phase 10 can lift the NOTIFY logic,
and Phase 8 replaces the rate knobs. Nothing here blocks or conflicts with the
Phase 7 work currently on `worktree-feat-agent-surface`.

## Configuration

New options on `Settings` (`config.py`). All three of `config.py`,
`ha_spark_addon/config.yaml` (`options` + `schema`), and the `_OPTION_KEYS` set
must stay in sync — a test enforces this.

| Option | Type / default | Meaning |
|---|---|---|
| `v2l_power_entity` | `str = ""` | V2L discharge-power sensor (W). **Empty ⇒ feature off.** |
| `v2l_round_trip_efficiency` | `float = 0.85` | Single calibration knob folding the AC→DC (charge) and DC→AC (discharge) conversion losses. |
| `v2l_peak_rate_gbp` | `float = 0.30` | £/kWh import rate being offset *now* (peak). |
| `v2l_offpeak_rate_gbp` | `float = 0.07` | £/kWh cheap rate used to *refill* the car later. |
| `v2l_cutoff_time` | `str = "01:00"` | Local time the cheap window starts (parsed by `sources.parse_time`). |
| `v2l_notify_service` | `str = ""` | HA `notify.<service>` target (e.g. `mobile_app_x`). **Empty ⇒ no notifications fire.** Not a secret. |
| `v2l_budget_kwh` | `float = 0` | Optional V2L budget (stand-in for car SoC). `0` ⇒ no predictive warning. |

Defaults are illustrative; the user tunes rates/efficiency for their tariff and
hardware (per the "leave the calibration knob" principle).

## Module: `ha_spark/energy/v2l.py`

Mostly pure functions plus thin IO, mirroring the existing
`sample_signals` (read) and `publish.plan_to_payload` (publish) patterns.

### Data model

```python
@dataclass
class V2LSession:
    day: str                 # local ISO date the session belongs to (for daily reset)
    kwh_delivered: float = 0.0
    last_power_w: float = 0.0
    peak_power_w: float = 0.0
    last_sample_ts: str | None = None   # ISO; None until first sample
    active: bool = False                # power above threshold this/last tick
    notified_unplug: bool = False       # N1 fired
    notified_plug_in: bool = False      # N2 fired
    notified_budget: bool = False       # N3 fired
```

Persisted to `/data/ha_spark_v2l_session.json` (same disk-cache pattern as
`ha_spark_published.json`), so a daemon restart never loses the tally.

Module constants (not config — values that rarely change, per YAGNI):
`_IDLE_W = 50.0` (power below this counts as "V2L idle/stopped"),
`_DT_CLAMP_S = 300.0` (integration gap ceiling),
`_PLUG_IN_LEAD_MIN = 20.0` (N3 predictive lead time).

### Functions

- `integrate(prev_kwh: float, power_w: float, dt_s: float) -> float`
  Rectangle rule with the last sampled power: `prev_kwh + power_w/1000 * dt_s/3600`.
  `dt_s` is clamped to `_DT_CLAMP_S` so a long downtime gap can't blow up the
  tally. `# ponytail:` comment names the rectangle/clamp ceiling.

- `savings(kwh, peak, offpeak, eff) -> tuple[float, float, float]`
  Returns `(avoided_gbp, refill_cost_gbp, net_gbp)`:
  - `avoided = kwh * peak`
  - `refill_cost = (kwh / eff) * offpeak`
  - `net = avoided - refill_cost`
  `net` may be negative and is reported honestly.

- `notifications(session, now, settings) -> list[Notice]`
  Pure decision returning which of N1/N2/N3 to fire (each fire-once via the
  session flags). Triggers below. `Notice` carries a title + message.

- `payload(session, settings) -> list[Entity]`
  Maps the session to `sensor.ha_spark_v2l_*` `(entity_id, state, attributes)`
  tuples, mirroring `publish.plan_to_payload`.

- IO helpers: `load_session(settings) -> V2LSession`,
  `save_session(settings, session)`, and
  `notify(rest, service, title, message)` — a thin wrapper over
  `rest.call_service("notify", service, {"title": ..., "message": ...})`.

### Efficiency math (the AC→DC→AC→DC ask)

The V2L power sensor reads **AC out of the car** (already past the car's DC→AC
inverter), so energy delivered to the house `E = ∫P dt` is measured directly —
that is what offsets peak import. The conversion losses bite on the **refill**:
to put `E` of usable energy back into the car you draw `E / η` from the grid,
where `η = η_charge × η_discharge` (the two DC↔AC conversions).

- Avoided peak import: `E × peak_rate`
- Cost to refill car at cheap rate: `(E / η) × offpeak_rate`
- **Net benefit: `E × peak_rate − (E / η) × offpeak_rate`**

**Assumption:** V2L is offsetting house *load*. If surplus V2L power is instead
charging the home battery there is a third conversion (AC→DC) not modelled; a
`# ponytail:` comment flags this ceiling.

## Daemon wiring (`energy/scheduler.py`, `run_forever`)

Each tick, when `v2l_power_entity` is set:

1. Read the V2L power sensor (best-effort; an unreadable entity logs a warning
   and skips, exactly like `sample_signals` — it never aborts the loop).
2. Compute `dt_s` from `now − last_sample_ts`; integrate into `kwh_delivered`.
3. Update `active`/`peak_power_w`; reset to a fresh session when
   `session.day != now.date()` **and** power is idle (≈0).
4. Publish `sensor.ha_spark_v2l_*` via the existing `_push` path.
5. Evaluate `notifications(...)` and fire any due ones via `notify(...)`.
6. Persist the session JSON.

All wrapped in try/except and isolated from the plan run, guard tick, and signal
sampler. Runs every tick (60 s); 60 s rectangle integration error is negligible.

## Published sensors

- `sensor.ha_spark_v2l_power_w` — current V2L discharge power (W), `device_class:
  power`.
- `sensor.ha_spark_v2l_energy_kwh` — session kWh delivered, `device_class:
  energy`.
- `sensor.ha_spark_v2l_net_saving_gbp` — efficiency-discounted net £ benefit,
  `device_class: monetary`; attributes carry `avoided_gbp`, `refill_cost_gbp`,
  `peak_power_w`.

## Notifications (three separate, timely, fire-once)

Fire only when `v2l_notify_service` is set. Each guarded by a session flag so it
fires at most once per session.

| ID | Trigger | Message (shape) |
|---|---|---|
| **N1 Unplug** | `now >= v2l_cutoff_time` and session active | "Cheap window starting — unplug V2L. Tonight: {kwh:.1f} kWh, net £{net:.2f}." |
| **N2 Plug in to recharge** | session was active and power drops below `_IDLE_W` (V2L stopped) | "V2L done — {kwh:.1f} kWh pulled. Plug the car in to recharge on the cheap rate." |
| **N3 Predictive plug-in** | `v2l_budget_kwh > 0` and minutes-to-budget `(budget − delivered) / (power/1000) * 60` ≤ `_PLUG_IN_LEAD_MIN`, or `delivered ≥ budget` | "Car will reach your V2L budget ({budget:.0f} kWh) in ~{mins} min — plan to plug in." |

N1 and N2 are distinct user actions (unplug the V2L adapter; plug into the
charger) and may both fire around the cutoff — that is the intended sequence.

## CLI: `ha-spark v2l`

Reads the persisted session and the live power sensor and prints: current W,
session kWh, peak W, avoided £, refill £, net £. Read-only; mirrors how other
CLI commands present computed numbers.

## Testing

Unit tests (`tests/test_v2l.py`, respx for HTTP, pytest `asyncio_mode = "auto"`):

- `integrate`: known power × duration ⇒ expected kWh; `dt_s` clamp caps a long gap.
- `savings`: the formula incl. the `/η` refill term; a case where `net` goes
  negative (peak < offpeak/η).
- `notifications`: N1 fires at/after cutoff once; N2 on active→idle once; N3 on
  budget projection once; nothing fires when `v2l_notify_service` is empty.
- session daily reset; persistence round-trip (`load`/`save`).
- `_OPTION_KEYS` ↔ `config.yaml` sync test stays green with the new keys.

**Tonight:** run the daemon in dev/standalone mode against live HA; watch
`ha-spark v2l` / the sensors; confirm notifications arrive through HA.

## Security

- No secrets involved: config holds entity IDs, enum/number knobs, and a notify
  service name (not a secret). Nothing logged that shouldn't be.
- All external reads (the V2L sensor) are coerced via the tolerant float helpers
  and never crash the loop on a bad payload.
- No SQL beyond the existing parameterised helpers (this feature adds none).
- The LLM is untouched; it never reaches this path.

## Versioning / release (deferred)

Tonight is dev-mode only — no release. When shipped, this bumps
`config.yaml` `version` (+ CHANGELOG, DOCS, schema) and needs a matching
annotated `vX.Y.Z` tag. **Coordinate the version with the in-flight Phase 7**,
which also targets `0.14.0`, to avoid a collision.

## Open questions

None outstanding — approach, rate-knob decoupling, and three-separate-messages
all confirmed.
