"""Tests for the plan report annotations."""

from __future__ import annotations

from ha_spark.energy.models import ChargePlan
from ha_spark.energy.report import format_plan


def _plan(**kw: object) -> ChargePlan:
    base: dict[str, object] = dict(
        soc_now=69, capacity_kwh=26.88, solar_kwh=3.4, effective_solar_kwh=3.4,
        load_kwh=17.7, cheap_covered_kwh=0.0, usable_now_kwh=13.17,
        deficit_kwh=9.23, buffer_pct=20.0, required_kwh=0.0,
        target_soc=69, overnight_current_a=0, window_hours=6.0, ev_charging=False,
        ha_template_needed=None, actions=(),
    )
    base.update(kw)
    return ChargePlan(**base)  # type: ignore[arg-type]


def test_fill_strategy_annotates_required_line() -> None:
    out = format_plan(_plan(strategy="fill", required_kwh=5.64, target_soc=90), "test")
    assert "5.64 kWh  (fill to 90%)" in out


def test_pre_window_drain_annotates_usable_line() -> None:
    out = format_plan(_plan(pre_window_drain_kwh=0.42), "test")
    assert "13.17 kWh  (-0.42 by window start -> 12.75)" in out


def test_no_annotations_by_default() -> None:
    out = format_plan(_plan(), "test")
    assert "fill to" not in out
    assert "window start" not in out


def test_dispatch_ev_kwh_renders_when_present() -> None:
    out = format_plan(_plan(dispatch_ev_kwh=4.0), "test")
    assert "EV dispatch energy 4.00 kWh planned by Octopus" in out


def test_dispatch_ev_kwh_omitted_when_none() -> None:
    assert "EV dispatch energy" not in format_plan(_plan(), "test")
