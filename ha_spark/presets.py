"""Per-vendor entity presets for onboarding (Phase 4).

A preset is a baseline ``config_field -> entity_id`` map for a known hardware
combination. The wizard uses it to fill fields auto-discovery couldn't match,
so a user on a supported setup gets a complete proposal even for entities whose
names don't carry an obvious keyword. ``solis`` is the reference setup
(Solis inverter + Solcast + Octopus Intelligent + myenergi zappi) the code
defaults already target; more can be added without touching the discovery code.
"""

from __future__ import annotations

# Reference setup: matches the built-in Settings defaults.
SOLIS: dict[str, str] = {
    "soc_entity": "sensor.solisac_battery_soc",
    "battery_voltage_entity": "sensor.solisac_battery_voltage",
    "solar_tomorrow_entity": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "octopus_rate_entity": (
        "sensor.octopus_energy_electricity_22l4386358_2200012282082_current_rate"
    ),
    "dispatch_entity": (
        "binary_sensor.octopus_energy_00000000_0009_4000_8020_000000068b29"
        "_intelligent_dispatching"
    ),
    "ev_plug_entity": "sensor.myenergi_zappi_22300254_plug_status",
    "ev_status_entity": "sensor.myenergi_zappi_22300254_status",
    "consumption_energy_entity": "sensor.historic_household_usage",
    "charge_current_entity": "number.solisac_timed_charge_current",
    "inverter_power_switch_entity": "select.solisac_power_switch",
}

PRESETS: dict[str, dict[str, str]] = {"solis": SOLIS}


def preset_names() -> list[str]:
    """The available preset names."""
    return sorted(PRESETS)


def get_preset(name: str) -> dict[str, str]:
    """The field map for ``name``; raises KeyError if unknown."""
    return PRESETS[name]
