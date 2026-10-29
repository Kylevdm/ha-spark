"""The options flow stores a whole validated topology document atomically."""

from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_spark.const import ADDON_SLUG, DOMAIN
from custom_components.ha_spark.topology import Topology, validate_topology
from tests_ha.test_pairing import discovery

# A minimal valid document, as it is stored under entry.options["topology"].
STORED_TOPOLOGY: dict[str, Any] = {
    "connections": [{
        "id": "grid", "supply_voltage_v": 240, "import_limit_a": 75,
        "export_limit_a": 16, "dno_export_limit_kw": 3.6,
    }],
    "banks": [{
        "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
        "charge_limit_a": 125, "discharge_limit_a": 125, "served_load_share_pct": 100,
    }],
    "inverters": [{
        "id": "inv1", "bank_id": "bank", "driver_kind": "solis",
        "charge_limit_a": 62.5, "discharge_limit_a": 62.5, "confirmed": True,
    }],
    "v2l_chargers": [],
    "major_loads": [],
}


def entry_for(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """An entry whose options are the only varying part."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="site", data={
        **discovery().config, "slug": ADDON_SLUG, "site_id": "site",
    }, options=options)
    entry.add_to_hass(hass)
    return entry


async def choose(hass: HomeAssistant, flow_id: str, next_step_id: str) -> Any:
    """Follow one menu choice."""
    return await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": next_step_id}
    )


async def open_topology(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Start the options flow and navigate to the topology menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["solcast", "topology"]
    result = await choose(hass, result["flow_id"], "topology")
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "topology"
    return result["flow_id"]


async def add_item(
    hass: HomeAssistant, flow_id: str, key: str, payload: dict[str, Any],
) -> None:
    """Add one collection entry through its form, then leave the collection."""
    result = await choose(hass, flow_id, key)
    assert result["type"] is FlowResultType.MENU
    result = await choose(hass, flow_id, f"{key}_add")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == f"{key}_item"
    result = await hass.config_entries.options.async_configure(flow_id, payload)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == key
    result = await choose(hass, flow_id, f"{key}_done")
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "topology"


async def test_full_document_stored_and_read_back_with_revision(
    hass: HomeAssistant,
) -> None:
    """One connection, bank, two inverters, a V2L charger and two major loads."""
    entry = entry_for(hass)
    flow_id = await open_topology(hass, entry)

    await add_item(hass, flow_id, "connections", {
        "id": "grid", "supply_voltage_v": 240, "import_limit_a": 75,
        "export_limit_a": 16, "dno_export_limit_kw": 3.6,
    })
    await add_item(hass, flow_id, "banks", {
        "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
        "charge_limit_a": 125, "discharge_limit_a": 125, "served_load_share_pct": 100,
    })
    await add_item(hass, flow_id, "inverters", {
        "id": "inv1", "bank_id": "bank", "charge_limit_a": 62.5,
        "discharge_limit_a": 62.5, "confirmed": True,
    })
    await add_item(hass, flow_id, "inverters", {
        "id": "inv2", "bank_id": "bank", "driver_kind": "solis",
        "charge_limit_a": 62.5, "discharge_limit_a": 62.5, "confirmed": True,
    })
    await add_item(hass, flow_id, "v2l_chargers", {
        "id": "car", "bank_id": "bank", "max_current_a": 8,
    })

    # An EV charger defaults to 7.4 kW and can be marked dispatch-driven.
    result = await choose(hass, flow_id, "major_loads")
    result = await choose(hass, flow_id, "major_loads_add")
    assert result["step_id"] == "major_load_kind"
    result = await hass.config_entries.options.async_configure(
        flow_id, {"kind": "ev_charger"}
    )
    assert result["step_id"] == "major_load_ev"
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "ev", "connection_id": "grid", "dispatch_driven": True,
    })
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "major_loads"
    # A heat pump asks for both seasonal demands, with no default.
    result = await choose(hass, flow_id, "major_loads_add")
    result = await hass.config_entries.options.async_configure(
        flow_id, {"kind": "heat_pump"}
    )
    assert result["step_id"] == "major_load_heat"
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "hp", "connection_id": "grid",
        "heating_demand_kw": 3.0, "cooling_demand_kw": 2.0,
    })
    assert result["type"] is FlowResultType.MENU
    result = await choose(hass, flow_id, "major_loads_done")
    assert result["step_id"] == "topology"

    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await choose(hass, flow_id, "save")
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["topology_revision"] == 1

    topology = validate_topology(entry.options["topology"])
    assert isinstance(topology, Topology)
    assert [connection.id for connection in topology.connections] == ["grid"]
    assert topology.connections[0].import_limit_a == 75
    assert [bank.id for bank in topology.banks] == ["bank"]
    assert [inverter.id for inverter in topology.inverters] == ["inv1", "inv2"]
    assert all(inverter.confirmed for inverter in topology.inverters)
    assert [charger.id for charger in topology.v2l_chargers] == ["car"]
    assert [load.id for load in topology.major_loads] == ["ev", "hp"]
    ev, heat = topology.major_loads
    assert ev.kind == "ev_charger" and ev.demand_kw == 7.4 and ev.dispatch_driven
    assert heat.kind == "heat_pump"
    assert (heat.heating_demand_kw, heat.cooling_demand_kw) == (3.0, 2.0)
    assert not heat.dispatch_driven

    # A second save of the same document increments the revision.
    flow_id = await open_topology(hass, entry)
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await choose(hass, flow_id, "save")
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["topology_revision"] == 2


async def build_base(hass: HomeAssistant, flow_id: str) -> None:
    """Add one connection, bank and inverter to the working draft."""
    await add_item(hass, flow_id, "connections", {
        "id": "grid", "supply_voltage_v": 240, "import_limit_a": 75,
    })
    await add_item(hass, flow_id, "banks", {
        "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
        "served_load_share_pct": 100,
    })
    await add_item(hass, flow_id, "inverters", {
        "id": "inv1", "bank_id": "bank", "charge_limit_a": 50, "confirmed": True,
    })


async def test_non_finite_and_out_of_range_refused(hass: HomeAssistant) -> None:
    """Bad numbers show field errors and never reach the stored options."""
    entry = entry_for(hass)
    flow_id = await open_topology(hass, entry)
    await choose(hass, flow_id, "connections")
    await choose(hass, flow_id, "connections_add")

    result = await hass.config_entries.options.async_configure(
        flow_id, {"id": "grid", "supply_voltage_v": float("inf")}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"supply_voltage_v": "invalid_number"}

    # The refused values stay in the form; only the newly bad field errors.
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "grid", "supply_voltage_v": 240, "import_limit_a": 300,
    })
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"import_limit_a": "out_of_range"}

    hass.config_entries.options.async_abort(flow_id)
    assert "topology" not in entry.options


async def test_unknown_bank_refused_and_corrected(hass: HomeAssistant) -> None:
    """An inverter on an unknown bank is refused and can be corrected."""
    entry = entry_for(hass)
    flow_id = await open_topology(hass, entry)
    await add_item(hass, flow_id, "connections", {
        "id": "grid", "supply_voltage_v": 240, "import_limit_a": 75,
    })
    await add_item(hass, flow_id, "banks", {
        "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
        "served_load_share_pct": 100,
    })
    await choose(hass, flow_id, "inverters")
    await choose(hass, flow_id, "inverters_add")
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "inv1", "bank_id": "ghost", "charge_limit_a": 50, "confirmed": True,
    })
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"bank_id": "unknown_bank"}
    # The refused form can be corrected in place.
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "inv1", "bank_id": "bank", "charge_limit_a": 50, "confirmed": True,
    })
    assert result["type"] is FlowResultType.MENU
    hass.config_entries.options.async_abort(flow_id)
    assert "topology" not in entry.options


async def test_dependent_deletes_refused(hass: HomeAssistant) -> None:
    """Banks and connections with members, and the last inverter, stay."""
    entry = entry_for(hass)
    for key, item_id, error in (
        ("banks", "bank", "bank_in_use"),
        ("connections", "grid", "connection_in_use"),
        ("inverters", "inv1", "remove_last"),
    ):
        flow_id = await open_topology(hass, entry)
        await build_base(hass, flow_id)
        result = await choose(hass, flow_id, key)
        result = await choose(hass, flow_id, f"{key}_remove")
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            flow_id, {"id": item_id}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}
        hass.config_entries.options.async_abort(flow_id)
    assert "topology" not in entry.options


async def test_draft_level_refusals(hass: HomeAssistant) -> None:
    """Ids are unique site-wide and only one EV charger dispatches drives."""
    entry = entry_for(hass)
    flow_id = await open_topology(hass, entry)
    await add_item(hass, flow_id, "connections", {
        "id": "grid", "supply_voltage_v": 240, "import_limit_a": 75,
    })

    await choose(hass, flow_id, "banks")
    await choose(hass, flow_id, "banks_add")
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "grid", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
    })
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"id": "duplicate_id"}
    # A bank needs exactly one load attribution.
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
        "served_load_share_pct": 50, "load_source_entity": "sensor.load",
    })
    assert result["errors"] == {"base": "invalid_load_attribution"}
    # An inverted SoC range is named.
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 90, "target_cap": 50,
    })
    assert result["errors"] == {"base": "invalid_soc_range"}
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 14,
        "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
        "served_load_share_pct": 100,
    })
    assert result["type"] is FlowResultType.MENU
    await choose(hass, flow_id, "banks_done")

    # Only one EV charger can be marked as the one dispatches drive.
    result = await choose(hass, flow_id, "major_loads")
    for load_id in ("ev1", "ev2"):
        result = await choose(hass, flow_id, "major_loads_add")
        result = await hass.config_entries.options.async_configure(
            flow_id, {"kind": "ev_charger"}
        )
        result = await hass.config_entries.options.async_configure(flow_id, {
            "id": load_id, "connection_id": "grid", "dispatch_driven": True,
        })
        if load_id == "ev1":
            assert result["type"] is FlowResultType.MENU
        else:
            assert result["type"] is FlowResultType.FORM
            assert result["errors"] == {"dispatch_driven": "multiple_dispatch_loads"}

    hass.config_entries.options.async_abort(flow_id)
    assert "topology" not in entry.options


async def test_failed_edit_leaves_previous_document_live(
    hass: HomeAssistant,
) -> None:
    """A refused edit never touches the stored document or its revision."""
    entry = entry_for(
        hass, topology=STORED_TOPOLOGY, topology_revision=3,
    )
    flow_id = await open_topology(hass, entry)
    result = await choose(hass, flow_id, "connections")
    result = await choose(hass, flow_id, "connections_edit")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connections_edit"
    result = await hass.config_entries.options.async_configure(flow_id, {"id": "grid"})
    assert result["step_id"] == "connections_item"
    result = await hass.config_entries.options.async_configure(
        flow_id, {"id": "grid", "import_limit_a": 500}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"import_limit_a": "out_of_range"}
    hass.config_entries.options.async_abort(flow_id)
    assert entry.options["topology"] == STORED_TOPOLOGY
    assert entry.options["topology_revision"] == 3

    # A valid edit saves the whole document with the next revision.
    flow_id = await open_topology(hass, entry)
    await choose(hass, flow_id, "connections")
    await choose(hass, flow_id, "connections_edit")
    await hass.config_entries.options.async_configure(flow_id, {"id": "grid"})
    result = await hass.config_entries.options.async_configure(
        flow_id, {"id": "grid", "import_limit_a": 60}
    )
    assert result["type"] is FlowResultType.MENU
    await choose(hass, flow_id, "connections_done")
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await choose(hass, flow_id, "save")
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["topology_revision"] == 4
    topology = validate_topology(entry.options["topology"])
    assert isinstance(topology, Topology)
    assert topology.connections[0].import_limit_a == 60
    assert topology.connections[0].supply_voltage_v == 240
    assert [inverter.id for inverter in topology.inverters] == ["inv1"]


async def test_edit_form_suggests_stored_optional_sensors(hass: HomeAssistant) -> None:
    """Editing an inverter keeps its measurement sensors unless the owner clears them."""
    inverter = {
        **STORED_TOPOLOGY["inverters"][0],
        "grid_port_power_entity": "sensor.grid_port",
        "power_sign": "positive_import",
    }
    entry = entry_for(hass, topology={**STORED_TOPOLOGY, "inverters": [inverter]})
    flow_id = await open_topology(hass, entry)
    await choose(hass, flow_id, "inverters")
    await choose(hass, flow_id, "inverters_edit")
    result = await hass.config_entries.options.async_configure(flow_id, {"id": "inv1"})
    assert result["step_id"] == "inverters_item"
    schema = result["data_schema"]
    assert schema is not None
    suggested = {
        str(marker): (marker.description or {}).get("suggested_value")
        for marker in schema.schema
    }
    assert suggested["grid_port_power_entity"] == "sensor.grid_port"
    assert suggested["battery_power_entity"] is None
    hass.config_entries.options.async_abort(flow_id)


async def test_v2l_charger_one_per_bank_and_removable(hass: HomeAssistant) -> None:
    """A second V2L charger on one bank is refused; the first can be removed."""
    entry = entry_for(hass, topology=STORED_TOPOLOGY, topology_revision=1)
    flow_id = await open_topology(hass, entry)
    result = await choose(hass, flow_id, "v2l_chargers")
    result = await choose(hass, flow_id, "v2l_chargers_add")
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "car", "bank_id": "bank", "max_current_a": 8,
    })
    assert result["type"] is FlowResultType.MENU
    result = await choose(hass, flow_id, "v2l_chargers_add")
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "van", "bank_id": "bank", "max_current_a": 5,
    })
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"bank_id": "duplicate_v2l_bank"}
    hass.config_entries.options.async_abort(flow_id)
    assert entry.options["topology"] == STORED_TOPOLOGY

    # Unlike required collections, a V2L charger can be deleted outright.
    flow_id = await open_topology(hass, entry)
    result = await choose(hass, flow_id, "v2l_chargers")
    result = await choose(hass, flow_id, "v2l_chargers_add")
    result = await hass.config_entries.options.async_configure(flow_id, {
        "id": "car", "bank_id": "bank", "max_current_a": 8,
    })
    assert result["type"] is FlowResultType.MENU
    result = await choose(hass, flow_id, "v2l_chargers_remove")
    result = await hass.config_entries.options.async_configure(flow_id, {"id": "car"})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "v2l_chargers"
    hass.config_entries.options.async_abort(flow_id)
    assert entry.options["topology"] == STORED_TOPOLOGY


async def test_topology_flow_translations(hass: HomeAssistant) -> None:
    """Every new step, menu option, field and error has a translation."""
    translations = await async_get_translations(hass, "en", "options", [DOMAIN])
    prefix = f"component.{DOMAIN}.options."
    for step in (
        "init", "solcast", "topology", "connections", "connections_item",
        "connections_edit", "connections_remove", "banks", "banks_item",
        "banks_edit", "banks_remove", "inverters", "inverters_item",
        "inverters_edit", "inverters_remove", "v2l_chargers", "v2l_chargers_item",
        "v2l_chargers_edit", "v2l_chargers_remove", "major_loads",
        "major_load_kind", "major_load_ev", "major_load_heat", "major_loads_edit",
        "major_loads_remove", "save",
    ):
        assert translations[f"{prefix}step.{step}.title"], step
    menu_options = {
        "init": ("solcast", "topology"),
        "topology": (
            "connections", "banks", "inverters", "v2l_chargers", "major_loads", "save",
        ),
        "connections": (
            "connections_add", "connections_edit", "connections_remove", "connections_done",
        ),
        "banks": ("banks_add", "banks_edit", "banks_remove", "banks_done"),
        "inverters": (
            "inverters_add", "inverters_edit", "inverters_remove", "inverters_done",
        ),
        "v2l_chargers": (
            "v2l_chargers_add", "v2l_chargers_edit",
            "v2l_chargers_remove", "v2l_chargers_done",
        ),
        "major_loads": (
            "major_loads_add", "major_loads_edit", "major_loads_remove", "major_loads_done",
        ),
    }
    for step, options in menu_options.items():
        for option in options:
            assert translations[f"{prefix}step.{step}.menu_options.{option}"], option
    data_fields = {
        "solcast": ("calls_limit",),
        "connections_item": (
            "id", "supply_voltage_v", "import_limit_a", "export_limit_a",
            "dno_export_limit_kw", "grid_power_entity",
        ),
        "connections_edit": ("id",),
        "connections_remove": ("id",),
        "banks_item": (
            "id", "connection_id", "usable_capacity_kwh", "nominal_voltage_v",
            "min_soc", "target_cap", "charge_limit_a", "discharge_limit_a",
            "served_load_share_pct", "load_source_entity",
        ),
        "banks_edit": ("id",),
        "banks_remove": ("id",),
        "inverters_item": (
            "id", "bank_id", "driver_kind", "charge_limit_a", "discharge_limit_a",
            "confirmed", "grid_port_power_entity", "battery_power_entity", "power_sign",
        ),
        "inverters_edit": ("id",),
        "inverters_remove": ("id",),
        "v2l_chargers_item": ("id", "bank_id", "max_current_a"),
        "v2l_chargers_edit": ("id",),
        "v2l_chargers_remove": ("id",),
        "major_load_kind": ("kind",),
        "major_load_ev": ("id", "connection_id", "demand_kw", "power_entity", "dispatch_driven"),
        "major_load_heat": (
            "id", "connection_id", "heating_demand_kw", "cooling_demand_kw", "power_entity",
        ),
        "major_loads_edit": ("id",),
        "major_loads_remove": ("id",),
    }
    for step, fields in data_fields.items():
        for field in fields:
            assert translations[f"{prefix}step.{step}.data.{field}"], field
    for error in (
        "invalid_number", "out_of_range", "invalid_id", "duplicate_id", "invalid_entity",
        "unknown_bank", "unknown_connection", "duplicate_v2l_bank", "invalid_soc_range",
        "invalid_load_attribution", "invalid_load_demand", "multiple_dispatch_loads",
        "invalid_load_shares", "incomplete_topology", "invalid_topology", "invalid_item",
        "remove_last", "connection_in_use", "bank_in_use",
    ):
        assert translations[f"{prefix}error.{error}"], error
