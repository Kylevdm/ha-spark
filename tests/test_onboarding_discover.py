"""Tests for entity auto-discovery (Phase 4)."""

from __future__ import annotations

from typing import Any

from ha_spark.config import Settings
from ha_spark.ha.models import EntityState
from ha_spark.onboarding_discover import discover, propose


def _state(entity_id: str, **attrs: Any) -> EntityState:
    return EntityState(entity_id=entity_id, state="1", attributes=attrs)


def _realistic_dump() -> list[EntityState]:
    return [
        _state("sensor.solisac_battery_soc", device_class="battery", unit_of_measurement="%"),
        _state(
            "sensor.solisac_battery_voltage", device_class="voltage", unit_of_measurement="V"
        ),
        _state(
            "sensor.solcast_pv_forecast_forecast_tomorrow",
            detailedForecast=[{"pv_estimate": 1.0}],
        ),
        _state("sensor.octopus_energy_xxx_current_rate", device_class="monetary"),
        _state("binary_sensor.octopus_energy_xxx_intelligent_dispatching"),
        _state("sensor.myenergi_zappi_123_plug_status"),
        _state("sensor.myenergi_zappi_123_status"),
        _state(
            "sensor.historic_household_usage", device_class="energy", unit_of_measurement="kWh"
        ),
        _state("number.solisac_timed_charge_current", unit_of_measurement="A"),
        _state("select.solisac_power_switch"),
        # Distractors that must not win.
        _state("sensor.bedroom_temperature", device_class="temperature", unit_of_measurement="°C"),
        _state("sensor.phone_battery", device_class="battery", unit_of_measurement="%"),
    ]


def test_discover_ranks_expected_entities_first() -> None:
    ranked = discover(_realistic_dump())
    assert ranked["soc_entity"][0].entity_id == "sensor.solisac_battery_soc"
    assert ranked["battery_voltage_entity"][0].entity_id == "sensor.solisac_battery_voltage"
    assert (
        ranked["solar_tomorrow_entity"][0].entity_id
        == "sensor.solcast_pv_forecast_forecast_tomorrow"
    )
    assert ranked["dispatch_entity"][0].entity_id.endswith("intelligent_dispatching")
    assert ranked["charge_current_entity"][0].entity_id == "number.solisac_timed_charge_current"
    assert ranked["consumption_energy_entity"][0].entity_id == "sensor.historic_household_usage"


def test_soc_beats_other_battery_sensor_on_name() -> None:
    # Both are battery %, but only the SoC entity's name carries "soc".
    ranked = discover(_realistic_dump())
    soc = ranked["soc_entity"]
    assert soc[0].entity_id == "sensor.solisac_battery_soc"
    assert soc[0].score > soc[1].score  # phone_battery scores lower (no keyword)


def test_domain_is_a_hard_filter() -> None:
    # A number entity must never be proposed for a sensor field, even if named well.
    states = [_state("number.battery_soc_thing", unit_of_measurement="%")]
    ranked = discover(states)
    assert ranked["soc_entity"] == []


def test_solar_requires_no_device_class_but_uses_attribute() -> None:
    states = [
        _state("sensor.solcast_pv_forecast_forecast_tomorrow", detailedForecast=[]),
        _state("sensor.random_forecast"),  # keyword only, weaker
    ]
    ranked = discover(states)
    best = ranked["solar_tomorrow_entity"][0]
    assert best.entity_id == "sensor.solcast_pv_forecast_forecast_tomorrow"
    assert any("detailedForecast" in r for r in best.reasons)


def test_no_match_yields_empty() -> None:
    ranked = discover([_state("light.kitchen")])
    assert ranked["soc_entity"] == []


def test_propose_marks_status_against_current_config() -> None:
    settings = Settings(soc_entity="sensor.solisac_battery_soc")
    proposals = {p.config_field: p for p in propose(_realistic_dump(), settings)}

    soc = proposals["soc_entity"]
    assert soc.status == "match"  # configured soc_entity == discovered

    # A field whose configured value differs from the discovered best.
    settings2 = Settings(soc_entity="sensor.something_else")
    soc2 = next(p for p in propose(_realistic_dump(), settings2) if p.config_field == "soc_entity")
    assert soc2.status == "differs"


def test_propose_missing_when_no_candidate() -> None:
    proposals = {p.config_field: p for p in propose([], Settings())}
    assert proposals["soc_entity"].status == "missing"
    assert proposals["grid_power_entity"].optional is True
