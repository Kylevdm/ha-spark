"""Human-readable rendering of a ChargePlan."""

from __future__ import annotations

from datetime import time

from ha_spark.energy.models import ChargePlan


def _fmt(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def format_plan(plan: ChargePlan, load_source: str) -> str:
    """Render the plan as an aligned, scannable block."""
    solar = f"{plan.solar_kwh:.2f} kWh"
    if abs(plan.effective_solar_kwh - plan.solar_kwh) > 1e-9:
        solar += f" (haircut -> {plan.effective_solar_kwh:.2f})"

    soc = f"{plan.soc_now:.0f}%"
    if not plan.soc_valid:
        soc += "  (SoC sensor unreadable!)"

    lines = [
        "Charge plan:",
        f"  SoC now            {soc}",
        f"  Solar tomorrow     {solar}",
        f"  Home load forecast {plan.load_kwh:.2f} kWh  ({load_source})",
    ]
    if plan.cheap_covered_kwh > 0:
        lines.append(f"  Cheap-covered load {plan.cheap_covered_kwh:.2f} kWh (daytime dispatch)")
    if plan.expensive_load_kwh is not None:
        lines.append(
            f"  Peak-slot load     {plan.expensive_load_kwh:.2f} kWh (after solar)"
        )
    if plan.slot_prices:
        lo, hi = min(plan.slot_prices), max(plan.slot_prices)
        cheap = sum(1 for p in plan.slot_prices if p <= lo + 1e-9)
        lines.append(
            f"  Slot import price  £{lo:.3f}–£{hi:.3f}/kWh  "
            f"({cheap}/{len(plan.slot_prices)} slots cheap)"
        )
    deficit = f"{plan.deficit_kwh:.2f} kWh"
    if plan.buffer_pct > 0 and plan.deficit_kwh > 0:
        buffered = plan.deficit_kwh * (1.0 + plan.buffer_pct / 100.0)
        deficit += f"  (+{plan.buffer_pct:.0f}% buffer -> {buffered:.2f})"

    required = f"{plan.required_kwh:.2f} kWh"
    if plan.strategy == "fill":
        required += f"  (fill to {plan.target_soc:.0f}%)"
    if plan.charge_efficiency < 1 and plan.required_kwh > 0:
        buy = plan.required_kwh / plan.charge_efficiency
        required += f"  (buy {buy:.2f} @ {plan.charge_efficiency:.0%} eff)"

    usable = f"{plan.usable_now_kwh:.2f} kWh"
    if plan.pre_window_drain_kwh > 0:
        at_window = plan.usable_now_kwh - plan.pre_window_drain_kwh
        usable += (
            f"  (-{plan.pre_window_drain_kwh:.2f} by window start -> {at_window:.2f})"
        )

    lines += [
        f"  Usable now         {usable}",
        f"  Energy deficit     {deficit}",
        f"  Required charge    {required}",
        f"  Charge to          {plan.target_soc:.0f}%  over the {plan.window_hours:.1f} h window "
        f"({_fmt(plan.charge_intent.window_start)}-{_fmt(plan.charge_intent.window_end)})",
        f"  EV                 {'charging' if plan.ev_charging else 'not charging'}",
    ]
    if plan.dispatch_ev_kwh is not None:
        lines.append(
            f"  EV dispatch energy {plan.dispatch_ev_kwh:.2f} kWh planned by Octopus"
        )
    if plan.baseline_cost is not None and plan.planned_cost is not None:
        saving = plan.baseline_cost - plan.planned_cost
        cost = (
            f"  Projected cost     £{plan.planned_cost:.2f}  "
            f"(vs £{plan.baseline_cost:.2f} without battery — saving £{saving:.2f})"
        )
        if plan.export_revenue is not None and plan.export_revenue > 0:
            cost += f"  (incl. export -£{plan.export_revenue:.2f})"
        lines.append(cost)
    if plan.ha_template_needed is not None:
        lines.append(f"  HA template needs  {plan.ha_template_needed:.2f} kWh  (for comparison)")
    return "\n".join(lines)
