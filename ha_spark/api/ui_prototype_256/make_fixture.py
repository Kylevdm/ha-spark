"""PROTOTYPE (#256), throwaway: build plan fixtures for the ingress-app prototype.

Runs the real deterministic planner (``compute_plan``) and serialises the result
into a *proposed* ``timeline`` block -- today's ``/api/plan`` carries only nine
scalar sensors, not the per-slot data a plan visual needs. Writes ``fixtures.js``
(``window.PLAN_FIXTURES``) so ``index.html`` opens by double-click, no server.

    python ha_spark/api/ui_prototype_256/make_fixture.py          # synthetic (committed)
    python ha_spark/api/ui_prototype_256/make_fixture.py --live --options my_options.yaml

``--live`` (gitignored output) reaches HA with ``HA_URL``/``HA_TOKEN`` from .env
and takes the add-on options from ``--options``: the YAML from the add-on's
Configuration tab, "Edit in YAML" (JSON works too), filtered through the same
``_OPTION_KEYS`` allowlist as ``/data/options.json``. Read-only:
``current_plan`` never writes to HA. Writes ``fixtures_live.js``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from ha_spark.energy.models import (
    ChargePlan,
    DispatchSlot,
    FlexibilityEvent,
    PlannerConfig,
    PlannerInputs,
)
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.energy.report import format_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.tariff import AxleTariffProvider, FixedTariffProvider, TariffSchedule

HERE = Path(__file__).parent
LONDON = ZoneInfo("Europe/London")
SLOT = timedelta(minutes=30)


def _load_kwh(t: datetime) -> float:
    """A plausible October weekday: night base, morning bump, evening cooking peak."""
    h = t.hour + t.minute / 60
    if h < 6:
        return 0.22
    if h < 7:
        return 0.35
    if h < 9:
        return 0.65
    if h < 16:
        return 0.38
    if h < 17:
        return 0.55
    if h < 20:
        return 0.95
    if h < 22:
        return 0.6
    return 0.4


def _solar_kwh(t: datetime) -> float:
    """October bell between ~07:30 and ~18:00, peaking ~0.8 kWh/slot at midday."""
    import math

    h = t.hour + t.minute / 60 + 0.25
    if not 7.5 < h < 18.0:
        return 0.0
    bell: float = math.sin(math.pi * (h - 7.5) / 10.5) ** 1.5
    return round(0.8 * bell, 3)


# Synthetic grid carbon intensity (gCO2/kWh), standing in for the regional
# NESO Carbon Intensity forecast until the source is researched. Windy October
# night low, solar dip at midday, gas-heavy teatime peak.
CARBON_BANDS = ((50, "very low"), (120, "low"), (200, "moderate"), (280, "high"))


def _carbon_gco2(t: datetime) -> int:
    import math

    h = t.hour + t.minute / 60
    day = 0 if t.date() == datetime(2026, 10, 11).date() else 1
    base = 95 + 55 * math.exp(-((h - 8.0) ** 2) / 4) + 110 * math.exp(-((h - 18.0) ** 2) / 3)
    base -= 35 * math.exp(-((h - 13.0) ** 2) / 5)
    base += 18 * math.sin(h * 1.7 + day) - 25 * day
    return round(max(35.0, base))


def _carbon_index(g: int) -> str:
    return next((name for limit, name in CARBON_BANDS if g < limit), "very high")


def _solar_actual_kwh(t: datetime) -> float:
    """Today's measured PV: a bright morning, then cloud after 13:00."""
    f = _solar_kwh(t)
    return round(f * (1.12 if t.hour < 13 else 0.55), 3)


def synthetic(capacity_kwh: float = 20.0, label: str = "synthetic") -> dict[str, Any]:
    horizon = datetime(2026, 10, 11, 23, 30, tzinfo=LONDON)
    now = datetime(2026, 10, 11, 21, 10, tzinfo=LONDON)
    starts = [horizon + i * SLOT for i in range(48)]
    load = tuple(_load_kwh(t) for t in starts)
    solar = tuple(_solar_kwh(t) for t in starts)
    soc = SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=34.0,
        raw_state="34",
        reported_at=now,
        age_s=40.0,
        max_age_s=600.0,
    )
    event = FlexibilityEvent(
        start=datetime(2026, 10, 12, 16, 30, tzinfo=LONDON),
        end=datetime(2026, 10, 12, 19, 0, tzinfo=LONDON),
        direction="export",
        updated_at=datetime(2026, 10, 11, 15, 2, tzinfo=LONDON),
        rate_gbp_kwh=0.95,
    )
    dispatch = DispatchSlot(
        start=datetime(2026, 10, 12, 13, 0, tzinfo=LONDON),
        end=datetime(2026, 10, 12, 14, 30, tzinfo=LONDON),
        charge_in_kwh=6.5,
        source="smart-charge",
    )
    inputs = PlannerInputs(
        soc=soc,
        solar_tomorrow_kwh=sum(solar),
        predicted_home_load_kwh=sum(load),
        pre_window_drain_kwh=0.9,
        dispatches=(dispatch,),
        flexibility_event=event,
        car_plugged_in=True,
        load_slots=load,
        solar_slots=solar,
        horizon_start=horizon,
        now=now,
    )
    cfg = PlannerConfig(
        capacity_kwh=capacity_kwh,
        voltage_v=51.2,
        min_soc=15.0,
        target_cap=95.0,
        max_current_a=62.5,
        solar_haircut_k=0.85,
        window_start=time(23, 30),
        window_end=time(5, 30),
        battery_discharge_ceiling_kw=3.6,
        dno_export_limit_kw=3.68,
        rate_offpeak=0.07,
        rate_peak=0.2768,
        rate_export=0.15,
        buffer_pct=10.0,
        charge_efficiency=0.9,
        v2l_budget_kwh=6.0,
    )
    schedule = AxleTariffProvider(
        fallback=FixedTariffProvider(
            cheap_rate=cfg.rate_offpeak, standard_rate=cfg.rate_peak, export_rate=cfg.rate_export
        )
    ).schedule(inputs, cfg)
    plan = compute_plan(inputs, cfg, schedule)
    return build(plan, inputs, cfg, schedule, "slots", label, now)


async def live(raw: dict[str, Any]) -> dict[str, Any]:
    from ha_spark.config import _OPTION_KEYS, Settings
    from ha_spark.energy.plan_run import current_plan
    from ha_spark.ha.rest import HomeAssistantRest

    s = Settings(**{k: v for k, v in raw.items() if k in _OPTION_KEYS and v is not None})
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token, timeout=s.ha_timeout) as rest:
        run = await current_plan(s, rest)
    return build(
        run.plan, run.inputs, run.cfg, run.schedule, run.load_source, "live",
        datetime.now(LONDON), proactive_mode=s.proactive_mode,
    )


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(LONDON).isoformat() if t is not None else None


def build(
    plan: ChargePlan,
    inputs: PlannerInputs,
    cfg: PlannerConfig,
    schedule: TariffSchedule,
    load_source: str,
    label: str,
    now: datetime,
    proactive_mode: str = "simulate",
) -> dict[str, Any]:
    settings: Any = SimpleNamespace(proactive_mode=proactive_mode)
    api_plan = [
        {"entity_id": e, "state": v, "attributes": a} for e, v, a in plan_to_payload(plan, settings)
    ]
    out: dict[str, Any] = {
        "label": label,
        "generated_at": _iso(now),
        "api_plan": {"plan": api_plan, "generated_at": _iso(now)},
        "explain": format_plan(plan, load_source).splitlines(),
    }
    if inputs.load_slots is None or inputs.horizon_start is None:
        out["timeline"] = None  # daily model: nothing per-slot to draw
        return out

    n = len(inputs.load_slots)
    starts = [inputs.horizon_start + i * SLOT for i in range(n)]
    intent = plan.charge_intent
    window_slots = round(cfg.window_hours * 2)
    charging = plan.target_soc > plan.soc_now + 0.5
    soc_path = {t.astimezone(LONDON): pct for t, pct in plan.soc_path}
    export_slots = set(intent.export.selected_slots) if intent.export else set()
    skip_reasons = {s.start: s.reason for s in plan.export_skips}
    v2l = {r.slot: r for r in plan.v2l_refills}

    ramp_from = plan.soc_now - 100.0 * plan.pre_window_drain_kwh / cfg.capacity_kwh
    ramp_to = plan.soc_path[0][1] if plan.soc_path else plan.target_soc

    def overlaps(t: datetime, windows: tuple[tuple[datetime, datetime], ...]) -> bool:
        return any(a < t + SLOT and t < b for a, b in windows)

    slots = []
    for i, t in enumerate(starts):
        actions = []
        if i < window_slots and charging:
            actions.append("charge")
        if overlaps(t, intent.holds):
            actions.append("hold")
        if t in export_slots:
            actions.append("export")
        if t in skip_reasons:
            actions.append("skip")
        if i in v2l:
            actions.append("v2l")
        if t.astimezone(LONDON) in soc_path:
            soc, soc_kind = soc_path[t.astimezone(LONDON)], "path"
        elif i <= window_slots:
            # The planner projects SoC only from the window end; the prototype
            # ramps the window itself (flagged so the UI can draw it dashed),
            # from SoC-after-drain to the path's first point.
            soc = ramp_from + (ramp_to - ramp_from) * i / window_slots
            soc_kind = "ramp"
        else:
            soc, soc_kind = None, None
        slots.append(
            {
                "start": _iso(t),
                "import_price": round(schedule.prices[i], 4) if schedule.prices else None,
                "export_price": round(schedule.export_prices[i], 4)
                if schedule.export_prices
                else schedule.export_rate,
                "cheap_frac": round(schedule.cheap_fracs[i], 3) if schedule.cheap_fracs else 0,
                "load_kwh": round(inputs.load_slots[i], 3),
                "solar_kwh": round(inputs.solar_slots[i], 3) if inputs.solar_slots else 0.0,
                "carbon_gco2": _carbon_gco2(t),
                "carbon_index": _carbon_index(_carbon_gco2(t)),
                "soc_pct": round(soc, 1) if soc is not None else None,
                "soc_kind": soc_kind,
                "actions": actions,
            }
        )

    lines: list[dict[str, Any]] = []
    if charging:
        lines.append(
            {
                "kind": "charge",
                "start": _iso(starts[0]),
                "end": _iso(starts[0] + window_slots * SLOT),
                "text": f"Charge {plan.soc_now:.0f}% → {plan.target_soc:.0f}% "
                f"({plan.required_kwh:.1f} kWh) in the cheap window.",
                "source": "planner-numbers",
            }
        )
    for a, b in intent.holds:
        lines.append(
            {
                "kind": "hold",
                "start": _iso(a),
                "end": _iso(b),
                "text": "Hold the battery: a smart-charge dispatch makes grid power cheap, "
                "so the house runs from the grid.",
                "source": "prototype-wording",
            }
        )
    for r in plan.reservations:
        lines.append(
            {
                "kind": "reserve",
                "start": _iso(now),
                "end": _iso(r.target_time or starts[min(r.target_slot, n - 1)]),
                "kwh": round(r.energy_kwh, 2),
                "text": r.reason,
                "source": "planner",
            }
        )
    if intent.export:
        e = intent.export
        lines.append(
            {
                "kind": "export",
                "start": _iso(min(e.selected_slots, default=e.window_start)),
                "end": _iso(max(e.selected_slots) + SLOT if e.selected_slots else e.window_end),
                "text": f"Export {e.planned_export_kw:.1f} kW for the flexibility event"
                + (f" ({plan.export_funding})" if plan.export_funding else "."),
                "source": "planner-numbers",
            }
        )
    for s in plan.export_skips:
        lines.append(
            {"kind": "skip", "start": _iso(s.start), "end": _iso(s.start + SLOT),
             "text": s.reason, "source": "planner"}
        )
    for v in plan.v2l_refills:
        lines.append(
            {"kind": "v2l", "start": _iso(starts[v.slot]), "end": _iso(starts[v.slot] + SLOT),
             "kwh": round(v.kwh, 2), "text": v.reason, "source": "planner"}
        )
    if not plan.soc.ok:
        lines.append({"kind": "warning", "start": None, "end": None,
                      "text": plan.soc.reason, "source": "planner"})

    out["timeline"] = {
        "horizon_start": _iso(starts[0]),
        "slot_minutes": 30,
        "now": _iso(now),
        "soc_now": round(plan.soc_now, 1),
        "target_soc": round(plan.target_soc, 1),
        "min_soc": cfg.min_soc,
        "capacity_kwh": cfg.capacity_kwh,
        "proactive_mode": proactive_mode,
        "strategy": plan.strategy,
        "costs": {
            "planned": plan.planned_cost,
            "baseline": plan.baseline_cost,
            "export_revenue": plan.export_revenue,
        },
        "slots": slots,
        "carbon_source": "synthetic (prototype)",
        "history": _history(starts[0], now),
        "lines": _merge(sorted(lines, key=lambda x: x["start"] or "")),
    }
    return out


def _history(horizon: datetime, now: datetime) -> list[dict[str, Any]]:
    """Today from 07:00 up to the plan horizon: actuals where the slot has ended.

    Proposed payload, synthetic values: the planner has no history read yet
    (statistics fog). Solar actual is null for slots that haven't finished.
    """
    t = horizon.astimezone(LONDON).replace(hour=7, minute=0)
    out = []
    while t < horizon:
        done = t + SLOT <= now
        g = _carbon_gco2(t)
        out.append(
            {
                "start": _iso(t),
                "solar_forecast_kwh": _solar_kwh(t),
                "solar_actual_kwh": _solar_actual_kwh(t) if done else None,
                "carbon_gco2": g,
                "carbon_index": _carbon_index(g),
                "carbon_kind": "actual" if done else "forecast",
            }
        )
        t += SLOT
    return out


def _merge(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold back-to-back per-slot lines of one kind into a single span.

    The planner gives V2L refills and export skips one reason per slot; a
    reader wants one line per run. Formatting stays server-side (#254).
    """
    merged: list[dict[str, Any]] = []
    for line in lines:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev["kind"] == line["kind"] in ("v2l", "skip")
            and prev["end"] == line["start"]
            and (line["kind"] == "v2l" or prev["text"] == line["text"])
        ):
            prev["end"] = line["end"]
            prev["slots"] = prev.get("slots", 1) + 1
            if "kwh" in line:
                prev["kwh"] = round(prev["kwh"] + line["kwh"], 2)
                total = f"{prev['kwh']:.2f} kWh"
                prev["text"] = re.sub(r"[\d.]+ kWh", total, prev["text"], count=1)
            continue
        merged.append(dict(line))
    return merged


def main() -> None:
    if "--live" in sys.argv:
        import yaml

        options = Path(sys.argv[sys.argv.index("--options") + 1])
        raw = yaml.safe_load(options.read_text(encoding="utf-8")) or {}
        _write(HERE / "fixtures_live.js", {"live": asyncio.run(live(raw))})
        return
    # A 12 kWh battery can't fund the whole event: exercises export skips.
    _write(HERE / "fixtures.js", {"synthetic": synthetic(), "tight": synthetic(12.0, "tight")})


def _write(path: Path, datasets: dict[str, dict[str, Any]]) -> None:
    body = "window.PLAN_FIXTURES = window.PLAN_FIXTURES || {};\n"
    for key, data in datasets.items():
        body += f"window.PLAN_FIXTURES[{json.dumps(key)}] = {json.dumps(data, indent=1)};\n"
        tl = data.get("timeline") or {}
        acts: dict[str, int] = {}
        for s in tl.get("slots", []):
            for a in s["actions"]:
                acts[a] = acts.get(a, 0) + 1
        print(f"{key}: actions={acts} lines={[x['kind'] for x in tl.get('lines', [])]}")
    path.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    main()
