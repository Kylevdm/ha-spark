"""Build a Lovelace dashboard dict from the entity-id fields set in Settings.

Pure-ish: takes an already-open HomeAssistantRest session (cli.py owns the
session lifecycle, matching energy/sources.py:gather_inputs). Never raises —
an unreachable HA degrades to static labels rather than failing the command.
"""

from __future__ import annotations

from typing import Any

import httpx

from ha_spark.config import Settings
from ha_spark.ha.rest import HomeAssistantRest

# (category title, [(Settings field name, static fallback label), ...])
_CATEGORIES: list[tuple[str, list[tuple[str, str]]]] = [
    ("Battery", [
        ("soc_entity", "Battery SoC"),
        ("battery_voltage_entity", "Battery Voltage"),
    ]),
    ("Solar", [
        ("solar_tomorrow_entity", "Solar Forecast (Tomorrow)"),
    ]),
    ("EV / Charger", [
        ("ev_plug_entity", "EV Plug"),
        ("ev_status_entity", "EV Status"),
        ("charge_current_entity", "Charge Current"),
        ("charge_window_start_entity", "Charge Window Start"),
        ("charge_window_end_entity", "Charge Window End"),
        ("ha_template_charge_needed_entity", "Charge Needed"),
    ]),
    ("Grid & Tariff", [
        ("octopus_rate_entity", "Octopus Rate"),
        ("dispatch_entity", "Dispatch"),
        ("grid_power_entity", "Grid Power"),
    ]),
    ("Other", [
        ("consumption_energy_entity", "House Consumption"),
        ("inverter_power_switch_entity", "Inverter Power Switch"),
        ("heatpump_energy_entity", "Heat Pump Energy"),
        ("outdoor_weather_entity", "Outdoor Weather"),
        ("backfill_source_entity", "Backfill Source"),
    ]),
]


def _entity_card(title: str, entities: list[dict[str, str]]) -> dict[str, Any]:
    return {"type": "entities", "title": title, "entities": entities}


async def build_dashboard(settings: Settings, rest: HomeAssistantRest) -> dict[str, Any]:
    """Render a single-view Lovelace dashboard from configured entity fields."""
    names: dict[str, str] = {}
    try:
        states = await rest.get_states()
        names = {s.entity_id: s.friendly_name for s in states}
    except httpx.HTTPError:
        pass  # HA unreachable: fall back to static labels below.

    cards: list[dict[str, Any]] = []
    for title, fields in _CATEGORIES:
        entities = [
            {"entity": entity_id, "name": names.get(entity_id, label)}
            for field, label in fields
            if (entity_id := getattr(settings, field))
        ]
        if entities:
            cards.append(_entity_card(title, entities))

    people = [p.strip() for p in settings.person_entities.split(",") if p.strip()]
    if people:
        cards.append(
            _entity_card("People", [{"entity": p, "name": names.get(p, p)} for p in people])
        )

    return {"title": "ha-spark", "views": [{"title": "Energy", "cards": cards}]}
