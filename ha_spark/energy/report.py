"""Human-readable rendering of a ChargePlan."""

from __future__ import annotations

from ha_spark.energy.models import ChargePlan


def format_plan(plan: ChargePlan, load_source: str) -> str:
    """Render the plan as an aligned, scannable block."""
    solar = f"{plan.solar_kwh:.2f} kWh"
    if abs(plan.effective_solar_kwh - plan.solar_kwh) > 1e-9:
        solar += f" (haircut -> {plan.effective_solar_kwh:.2f})"

    lines = [
        "Charge plan:",
        f"  SoC now            {plan.soc_now:.0f}%",
        f"  Solar tomorrow     {solar}",
        f"  Home load forecast {plan.load_kwh:.2f} kWh  ({load_source})",
    ]
    if plan.cheap_covered_kwh > 0:
        lines.append(f"  Cheap-covered load {plan.cheap_covered_kwh:.2f} kWh (daytime dispatch)")
    lines += [
        f"  Usable now         {plan.usable_now_kwh:.2f} kWh",
        f"  Required charge    {plan.required_kwh:.2f} kWh  ->  target {plan.target_soc:.0f}%",
        f"  Charge current     {plan.overnight_current_a:.0f} A over {plan.window_hours:.1f} h",
        f"  EV                 {'charging' if plan.ev_charging else 'not charging'}",
    ]
    if plan.ha_template_needed is not None:
        lines.append(f"  HA template needs  {plan.ha_template_needed:.2f} kWh  (for comparison)")
    return "\n".join(lines)
