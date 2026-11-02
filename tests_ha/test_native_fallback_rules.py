"""Pure native fallback schedule rules: no default, no widening, fixed reasons."""
from copy import deepcopy
from datetime import time
from typing import Any

from custom_components.ha_spark.native_fallback import (
    FallbackOptions,
    FallbackSchedule,
    validate_fallback,
)
from custom_components.ha_spark.topology import Topology, effective_limits, validate_topology
from tests_ha.test_topology_flow import STORED_TOPOLOGY


def topology(**inverter_changes: object) -> Topology:
    raw = deepcopy(STORED_TOPOLOGY)
    raw["inverters"][0].update(transport="native", gateway_host="gateway.invalid",
                               **inverter_changes)
    parsed = validate_topology(raw)
    assert isinstance(parsed, Topology)
    return parsed


def options(**changes: object) -> FallbackOptions:
    raw: dict[str, object] = {
        "window_start": "23:30", "window_end": "05:30", "currents": {"inv1": 20},
    }
    raw.update(changes)
    return FallbackOptions.model_validate(raw)


def reason(site: Topology, document: object, **kwargs: Any) -> str:
    result = validate_fallback(document, site, effective_limits(site), **kwargs)
    assert isinstance(result, str), f"expected a reason, got {result!r}"
    return result


def test_valid_schedule_carries_whole_ampere_currents_and_household_window() -> None:
    site = topology()
    assert validate_fallback(options(currents={"inv1": 20.9}), site,
                             effective_limits(site)) == FallbackSchedule(
        time(23, 30), time(5, 30), {"inv1": 20})


def test_missing_extra_or_below_minimum_current_is_invalid_not_defaulted() -> None:
    site = topology()
    assert reason(site, options(currents={})) == "fallback_incomplete"
    assert reason(site, options(currents={"inv1": 20, "gone": 5})) == "fallback_incomplete"
    assert reason(site, options(currents={"inv1": 0.5})) == "fallback_current_invalid"
    assert reason(site, options(currents={"inv1": 0})) == "fallback_current_invalid"
    assert reason(site, options(currents={"inv1": float("nan")})) == (
        "fallback_current_invalid")


def test_inverter_register_and_effective_limits_narrow_to_invalid() -> None:
    assert reason(topology(), options(currents={"inv1": 63})) == "fallback_inverter_limit"
    assert reason(topology(charge_limit_a=10), options(currents={"inv1": 20})) == (
        "fallback_inverter_limit")
    assert reason(topology(confirmed=False), options(currents={"inv1": 20})) == (
        "fallback_inverter_limit")


def test_bank_and_site_supply_totals_are_checked() -> None:
    site = topology()
    raw = site.model_dump(mode="json")
    raw["banks"][0]["charge_limit_a"] = 15
    narrow = validate_topology(raw)
    assert isinstance(narrow, Topology)
    assert reason(narrow, options(currents={"inv1": 20})) == "fallback_bank_limit"
    # 50 A at 50 V with eta 0.949 is above 10 A x 240 V.
    raw = site.model_dump(mode="json")
    raw["connections"][0]["import_limit_a"] = 10
    supply = validate_topology(raw)
    assert isinstance(supply, Topology)
    assert reason(supply, options(currents={"inv1": 50})) == "fallback_supply_limit"


def test_bank_efficiency_makes_the_ac_supply_check_stricter() -> None:
    site = topology()
    raw = site.model_dump(mode="json")
    raw["connections"][0]["import_limit_a"] = 5
    raw["banks"][0]["one_way_charge_efficiency"] = 0.5
    narrow = validate_topology(raw)
    assert isinstance(narrow, Topology)
    # 20 A x 50 V / 0.5 = 2000 W is above 5 A x 240 V = 1200 W.
    assert reason(narrow, options(currents={"inv1": 20})) == "fallback_supply_limit"


def test_window_must_be_two_distinct_whole_minutes_on_the_clock_face() -> None:
    site = topology()
    for start, end in (("23:30", "23:30"), ("24:00", "05:30"), ("0730", "05:30"),
                       ("23:30:15", "05:30"), ("23:30+01:00", "05:30")):
        assert reason(site, options(window_start=start, window_end=end)) == (
            "fallback_window_invalid")


def test_export_or_a_control_without_read_back_is_refused() -> None:
    site = topology()
    assert reason(site, options(), export=object()) == "fallback_export_enabled"
    assert reason(site, options(), controls=frozenset({"charge_window", "export"})) == (
        "fallback_control_unverified")


def test_non_native_or_invalid_topology_is_invalid() -> None:
    site = topology()
    raw = site.model_dump(mode="json")
    raw["inverters"][0] = {key: value for key, value in raw["inverters"][0].items()
                           if not key.startswith("gateway")}
    raw["inverters"][0]["transport"] = "legacy"
    legacy = validate_topology(raw)
    assert isinstance(legacy, Topology)
    assert reason(legacy, options()) == "fallback_topology_invalid"
    assert reason(site, None) == "fallback_not_configured"
    assert reason(site, {"window_start": "23:30"}) == "fallback_invalid"
