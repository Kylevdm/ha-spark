"""Tests for the per-vendor entity presets."""

from __future__ import annotations

from ha_spark.presets import ALPHAESS, PRESETS, SOLIS, get_preset, preset_names


def test_preset_names_includes_solis_and_alphaess() -> None:
    assert preset_names() == ["alphaess", "solis"]


def test_get_preset_returns_the_registered_map() -> None:
    assert get_preset("solis") == SOLIS
    assert get_preset("alphaess") == ALPHAESS


def test_alphaess_preset_has_no_charge_control_entities() -> None:
    # AlphaESS control is the setbatterycharge service, not entities.
    assert "charge_current_entity" not in ALPHAESS
    assert "inverter_power_switch_entity" not in ALPHAESS
    assert ALPHAESS["soc_entity"] == "sensor.alphaess_battery_soc"


def test_presets_registry_keys_match_preset_names() -> None:
    assert set(PRESETS) == set(preset_names())
