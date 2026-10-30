"""Config flow for the ha-spark companion integration.

Supervisor discovery links one add-on to a generated site identity. Confirmation
never probes the add-on; subsequent boots refresh the connection silently.
Manual host/port/token waits for phase 10. Solcast credentials are entry-owned;
reconfigure and reauth check them through the same durable per-entry quota gate.
The options flow edits the Solcast allowance and, as one atomic document, the
site topology (#535's contract) that later phases send on the heartbeat.
"""

from collections.abc import Callable
from math import isclose
from typing import Any, TypeGuard, cast
from uuid import uuid4

import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pydantic import BaseModel, ValidationError

from .const import ADDON_SLUG, DOMAIN
from .solcast import Outcome, async_list_sites
from .solcast_budget import async_get_budget
from .topology import (
    BatteryBank,
    GridConnection,
    Inverter,
    MajorLoad,
    Topology,
    V2LCharger,
    validate_topology,
)


class HaSparkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ha-spark config flow."""

    VERSION = 1

    _connection: dict[str, Any]

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Accept only our Supervisor add-on and validated connection fields."""
        config = discovery_info.config
        if discovery_info.slug != ADDON_SLUG or not isinstance(config, dict) or not all(
            isinstance(config.get(key), str) and bool(config[key].strip())
            for key in ("host", "auth_token", "session_id")
        ) or type(config.get("port")) is not int or not 1 <= config["port"] <= 65535:
            return self.async_abort(reason="invalid_discovery")
        self._connection = {
            key: config[key] for key in ("host", "port", "auth_token", "session_id")
        }
        self._connection["slug"] = ADDON_SLUG
        for flow in self._async_in_progress():
            self.hass.config_entries.flow.async_abort(flow["flow_id"])
        if entries := self._async_current_entries():
            entry = entries[0]
            if entry.data.get("slug") != ADDON_SLUG:
                return self.async_abort(reason="already_configured")
            return self.async_update_reload_and_abort(
                entry, data_updates=self._connection, reason="already_configured"
            )
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the site once, even if confirmations race."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is None:
            return self.async_show_form(step_id="confirm")
        site_id = str(uuid4())
        await self.async_set_unique_id(site_id)
        # No await between this final check and creation. Never use discovery's
        # uuid: Supervisor withdrawal removes entries keyed on that uuid.
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(
            title="ha-spark", data={**self._connection, "site_id": site_id}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "HaSparkOptionsFlow":
        """Quota and topology are owner configuration, not credentials."""
        return HaSparkOptionsFlow()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Add or replace a key; an empty key requires explicit confirmation."""
        return await self._async_solcast_key(user_input, "reconfigure")

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Ask for a replacement without exposing the rejected key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Replace a rejected Solcast credential."""
        return await self._async_solcast_key(user_input, "reauth_confirm")

    async def _async_solcast_key(
        self, user_input: dict[str, Any] | None, step_id: str,
    ) -> ConfigFlowResult:
        entry = (
            self._get_reauth_entry() if self.source == SOURCE_REAUTH
            else self._get_reconfigure_entry()
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input.get("solcast_api_key", "")
            if not key and self.source != SOURCE_REAUTH:
                return await self.async_step_clear_solcast()
            if not isinstance(key, str) or not key.strip():
                errors["base"] = "auth_failed"
            else:
                gate = async_get_budget(self.hass, entry.entry_id)
                result = await gate.async_request(
                    lambda: async_list_sites(async_get_clientsession(self.hass), key),
                    calls_limit=entry.options.get("calls_limit", 10),
                )
                if result.outcome == Outcome.OK:
                    if await gate.async_set_first_fetch_pending(True):
                        return self.async_update_reload_and_abort(entry, data_updates={
                            "solcast_api_key": key,
                            "solcast_resource_ids": list(result.resource_ids),
                            "solcast_cache_generation": uuid4().hex,
                        })
                    errors["base"] = "stale"
                else:
                    errors["base"] = result.flow_error or result.outcome.value
        return self.async_show_form(step_id=step_id, errors=errors, data_schema=probatio.Schema({
            probatio.Required("solcast_api_key"): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD),
            ),
        }))

    async def async_step_clear_solcast(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Only the confirmation step can remove a saved key."""
        if user_input is None:
            return self.async_show_form(step_id="clear_solcast", data_schema=probatio.Schema({}))
        entry = self._get_reconfigure_entry()
        gate = async_get_budget(self.hass, entry.entry_id)
        # Removing credentials must remain possible even when quota storage fails.
        await gate.async_set_first_fetch_pending(False)
        data = dict(entry.data)
        data.pop("solcast_api_key", None)
        data.pop("solcast_resource_ids", None)
        data.pop("solcast_cache_generation", None)
        result = self.async_update_reload_and_abort(entry, data=data)
        registry = dr.async_get(self.hass)
        identifier = (DOMAIN, f"{entry.data['site_id']}_solcast")
        if device := registry.async_get_device_by_identifier(identifier, entry.entry_id):
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
        return result

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a user-initiated setup to the add-on discovery path."""
        return self.async_abort(reason="addon_required")


_TEXT = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_NUMBER = NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any"))
_ENTITY = EntitySelector()
_BOOLEAN = BooleanSelector()
_DRIVER_KINDS = SelectSelector(SelectSelectorConfig(options=[
    SelectOptionDict(value="solis", label="Solis"),
    SelectOptionDict(value="alphaess", label="AlphaESS"),
]))
_POWER_SIGNS = SelectSelector(SelectSelectorConfig(options=[
    SelectOptionDict(value="positive_import", label="Positive import"),
    SelectOptionDict(value="negative_import", label="Negative import"),
]))
_LOAD_KINDS = SelectSelector(SelectSelectorConfig(options=[
    SelectOptionDict(value="ev_charger", label="EV charger"),
    SelectOptionDict(value="heat_pump", label="Heat pump"),
]))

# Draft keys in topology order; the document is saved or refused as a whole.
_COLLECTIONS = ("connections", "banks", "inverters", "v2l_chargers", "major_loads")

# Pydantic error types mapped to fixed outcome codes; never values or error text.
_NON_FINITE = {"finite_number", "float_type", "int_type", "string_type", "bool_type"}
_BOUNDS = {"greater_than_equal", "less_than_equal", "greater_than", "less_than"}
_TEXT_PATTERN = {"string_pattern_mismatch", "too_long", "string_too_long"}


def _is_number(value: object) -> TypeGuard[float]:
    """Strict numbers only: booleans are not currents, voltages or shares."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _field_errors(exc: ValidationError) -> dict[str, str]:
    """Field-keyed fixed codes for one model's rejected input."""
    errors: dict[str, str] = {}
    for error in exc.errors():
        if error["type"] == "value_error":
            continue  # cross-field rules get their own named codes below
        loc = error["loc"]
        if not loc or not isinstance(loc[0], str):
            continue
        if error["type"] in _NON_FINITE:
            errors[loc[0]] = "invalid_number"
        elif error["type"] in _BOUNDS:
            errors[loc[0]] = "out_of_range"
        elif error["type"] in _TEXT_PATTERN:
            errors[loc[0]] = "invalid_id" if loc[0] == "id" else "invalid_entity"
    return errors


def _condition_errors(model: type[BaseModel], clean: dict[str, Any]) -> dict[str, str]:
    """Named codes for the models' cross-field rules, checked directly."""
    if model is BatteryBank:
        min_soc, target_cap = clean.get("min_soc"), clean.get("target_cap")
        if _is_number(min_soc) and _is_number(target_cap) and min_soc > target_cap:
            return {"base": "invalid_soc_range"}
        share, source = clean.get("served_load_share_pct"), clean.get("load_source_entity")
        if (share is None) == (source is None):
            return {"base": "invalid_load_attribution"}
    elif model is MajorLoad:
        if clean.get("kind") == "ev_charger":
            if (
                clean.get("demand_kw") is None
                or "heating_demand_kw" in clean
                or "cooling_demand_kw" in clean
            ):
                return {"base": "invalid_load_demand"}
        elif clean.get("kind") == "heat_pump":
            if (
                clean.get("heating_demand_kw") is None
                or clean.get("cooling_demand_kw") is None
                or "demand_kw" in clean
                or clean.get("dispatch_driven")
            ):
                return {"base": "invalid_load_demand"}
    return {}


def _clean_item(model: type[BaseModel], user_input: dict[str, Any]) -> dict[str, Any]:
    """Keep only model fields; blank optionals fall back to their defaults."""
    clean: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if name not in user_input:
            continue
        value = user_input[name]
        if not field.is_required() and (value is None or value == ""):
            continue
        clean[name] = value
    return clean


def _id_select_schema(ids: list[str]) -> probatio.Schema:
    """Pick one existing entry of a collection by its identifier."""
    return probatio.Schema({
        probatio.Required("id"): SelectSelector(SelectSelectorConfig(
            options=[
                SelectOptionDict(value=identifier, label=identifier) for identifier in ids
            ],
        )),
    })


class HaSparkOptionsFlow(OptionsFlowWithReload):
    """Solcast allowance and the site topology; each save reloads the entry."""

    _draft: dict[str, list[dict[str, Any]]] | None = None
    _edit_index: int | None = None
    _load_kind: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose between the Solcast allowance and the topology editor."""
        return self.async_show_menu(step_id="init", menu_options=["solcast", "topology"])

    async def async_step_solcast(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose the home account's 10-call or legacy 50-call allowance."""
        if user_input is not None:
            return self.async_create_entry(
                title="", data={**self.config_entry.options, **user_input},
            )
        return self.async_show_form(step_id="solcast", data_schema=probatio.Schema({
            probatio.Required(
                "calls_limit", default=self.config_entry.options.get("calls_limit", 10),
            ): probatio.In([10, 50]),
        }))

    async def async_step_topology(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit the topology collections; save applies the whole document."""
        return self.async_show_menu(step_id="topology", menu_options=[
            "connections", "banks", "inverters", "v2l_chargers", "major_loads", "save",
        ])

    @property
    def _topology_draft(self) -> dict[str, list[dict[str, Any]]]:
        """Working copy; a stored document that fails validation starts empty."""
        if self._draft is None:
            draft: dict[str, list[dict[str, Any]]] = {key: [] for key in _COLLECTIONS}
            stored = self.config_entry.options.get("topology")
            if isinstance(stored, dict):
                parsed = validate_topology(stored)
                if isinstance(parsed, Topology):
                    loaded = cast(
                        "dict[str, list[dict[str, Any]]]", parsed.model_dump(mode="json")
                    )
                    draft = {key: list(loaded[key]) for key in _COLLECTIONS}
            self._draft = draft
        return self._draft

    def _collection_menu(self, key: str) -> ConfigFlowResult:
        """One menu per collection: add, plus edit and remove when items exist."""
        options = [f"{key}_add"]
        if self._topology_draft[key]:
            options += [f"{key}_edit", f"{key}_remove"]
        options.append(f"{key}_done")
        return self.async_show_menu(step_id=key, menu_options=options)

    def _select_over(self, key: str) -> SelectSelector:
        """Existing entries of one collection, with typed ids also accepted."""
        identifiers = [str(item["id"]) for item in self._topology_draft[key]]
        return SelectSelector(SelectSelectorConfig(
            options=[
                SelectOptionDict(value=identifier, label=identifier)
                for identifier in identifiers
            ],
            custom_value=True,
        ))

    def _connection_schema(self, defaults: dict[str, Any]) -> probatio.Schema:
        """AC supply limits for one grid connection."""
        return probatio.Schema({
            probatio.Required("id", default=defaults.get("id", "")): _TEXT,
            probatio.Required(
                "supply_voltage_v", default=defaults.get("supply_voltage_v")
            ): _NUMBER,
            probatio.Required("import_limit_a", default=defaults.get("import_limit_a", 0)): _NUMBER,
            probatio.Required("export_limit_a", default=defaults.get("export_limit_a", 0)): _NUMBER,
            probatio.Required(
                "dno_export_limit_kw", default=defaults.get("dno_export_limit_kw", 0)
            ): _NUMBER,
            # Optional fields carry no default: an absent key stays unset, so a
            # cleared sensor or share is never resurrected by the schema.
            probatio.Optional("grid_power_entity"): _ENTITY,
        })

    def _bank_schema(self, defaults: dict[str, Any]) -> probatio.Schema:
        """One energy store, its DC limits and its load attribution."""
        return probatio.Schema({
            probatio.Required("id", default=defaults.get("id", "")): _TEXT,
            probatio.Required(
                "connection_id", default=defaults.get("connection_id")
            ): self._select_over("connections"),
            probatio.Required(
                "usable_capacity_kwh", default=defaults.get("usable_capacity_kwh")
            ): _NUMBER,
            probatio.Required(
                "nominal_voltage_v", default=defaults.get("nominal_voltage_v")
            ): _NUMBER,
            probatio.Required(
                "one_way_charge_efficiency",
                default=defaults.get("one_way_charge_efficiency", 0.949),
            ): _NUMBER,
            probatio.Required("min_soc", default=defaults.get("min_soc")): _NUMBER,
            probatio.Required("target_cap", default=defaults.get("target_cap")): _NUMBER,
            probatio.Required("charge_limit_a", default=defaults.get("charge_limit_a", 0)): _NUMBER,
            probatio.Required(
                "discharge_limit_a", default=defaults.get("discharge_limit_a", 0)
            ): _NUMBER,
            probatio.Optional("served_load_share_pct"): _NUMBER,
            probatio.Optional("load_source_entity"): _ENTITY,
        })

    def _inverter_schema(self, defaults: dict[str, Any]) -> probatio.Schema:
        """Owner DC limits for one inverter, with explicit confirmation."""
        return probatio.Schema({
            probatio.Required("id", default=defaults.get("id", "")): _TEXT,
            probatio.Required(
                "bank_id", default=defaults.get("bank_id")
            ): self._select_over("banks"),
            probatio.Required(
                "driver_kind", default=defaults.get("driver_kind", "solis")
            ): _DRIVER_KINDS,
            probatio.Required("charge_limit_a", default=defaults.get("charge_limit_a", 0)): _NUMBER,
            probatio.Required(
                "discharge_limit_a", default=defaults.get("discharge_limit_a", 0)
            ): _NUMBER,
            probatio.Required("confirmed", default=defaults.get("confirmed", False)): _BOOLEAN,
            probatio.Optional("grid_port_power_entity"): _ENTITY,
            probatio.Optional("battery_power_entity"): _ENTITY,
            probatio.Required(
                "power_sign", default=defaults.get("power_sign", "positive_import")
            ): _POWER_SIGNS,
        })

    def _v2l_schema(self, defaults: dict[str, Any]) -> probatio.Schema:
        """One V2L charger as a DC source into a bank."""
        return probatio.Schema({
            probatio.Required("id", default=defaults.get("id", "")): _TEXT,
            probatio.Required(
                "bank_id", default=defaults.get("bank_id")
            ): self._select_over("banks"),
            probatio.Required("max_current_a", default=defaults.get("max_current_a", 0)): _NUMBER,
        })

    def _ev_schema(self, defaults: dict[str, Any]) -> probatio.Schema:
        """One EV charger; the dispatch-driven flag is exclusive site-wide."""
        return probatio.Schema({
            probatio.Required("id", default=defaults.get("id", "")): _TEXT,
            probatio.Required(
                "connection_id", default=defaults.get("connection_id")
            ): self._select_over("connections"),
            probatio.Required("demand_kw", default=defaults.get("demand_kw", 7.4)): _NUMBER,
            probatio.Optional("power_entity"): _ENTITY,
            probatio.Required(
                "dispatch_driven", default=defaults.get("dispatch_driven", False)
            ): _BOOLEAN,
        })

    def _heat_schema(self, defaults: dict[str, Any]) -> probatio.Schema:
        """One heat pump; both seasonal demands are required, with no default."""
        return probatio.Schema({
            probatio.Required("id", default=defaults.get("id", "")): _TEXT,
            probatio.Required(
                "connection_id", default=defaults.get("connection_id")
            ): self._select_over("connections"),
            probatio.Required(
                "heating_demand_kw", default=defaults.get("heating_demand_kw")
            ): _NUMBER,
            probatio.Required(
                "cooling_demand_kw", default=defaults.get("cooling_demand_kw")
            ): _NUMBER,
            probatio.Optional("power_entity"): _ENTITY,
        })

    def _prefilled(
        self,
        schema_for: Callable[[dict[str, Any]], probatio.Schema],
        values: dict[str, Any],
    ) -> probatio.Schema:
        """Suggest optional sensors too, so an edit keeps them unless cleared."""
        return self.add_suggested_values_to_schema(
            schema_for(values),
            {name: value for name, value in values.items() if value is not None},
        )

    def _draft_errors(self, key: str, clean: dict[str, Any]) -> dict[str, str]:
        """Structural checks against the working draft, with fixed codes."""
        errors: dict[str, str] = {}
        draft = self._topology_draft
        item_id = clean.get("id")
        if isinstance(item_id, str):
            for other_key, items in draft.items():
                for index, other in enumerate(items):
                    if (
                        other.get("id") == item_id
                        and not (other_key == key and index == self._edit_index)
                    ):
                        errors["id"] = "duplicate_id"
        bank_ids = {str(item["id"]) for item in draft["banks"]}
        if "bank_id" in clean and clean["bank_id"] not in bank_ids:
            errors["bank_id"] = "unknown_bank"
        connection_ids = {str(item["id"]) for item in draft["connections"]}
        if "connection_id" in clean and clean["connection_id"] not in connection_ids:
            errors["connection_id"] = "unknown_connection"
        if (
            key == "v2l_chargers"
            and "bank_id" not in errors
            and clean.get("bank_id") in {
                item["bank_id"]
                for index, item in enumerate(draft["v2l_chargers"])
                if index != self._edit_index
            }
        ):
            errors["bank_id"] = "duplicate_v2l_bank"
        if clean.get("dispatch_driven") and any(
            other.get("dispatch_driven")
            for index, other in enumerate(draft["major_loads"])
            if not (key == "major_loads" and index == self._edit_index)
        ):
            errors["dispatch_driven"] = "multiple_dispatch_loads"
        return errors

    def _store_item(self, key: str, item: dict[str, Any]) -> None:
        """Upsert at the edited position, or append a new entry."""
        items = self._topology_draft[key]
        if self._edit_index is None or not 0 <= self._edit_index < len(items):
            items.append(item)
        else:
            items[self._edit_index] = item
        self._edit_index = None

    async def _async_item_step(
        self,
        form_id: str,
        key: str,
        model: type[BaseModel],
        user_input: dict[str, Any] | None,
        schema_for: Callable[[dict[str, Any]], probatio.Schema],
        extra: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """One item form: model validation plus draft-level structural checks."""
        if user_input is None:
            return self.async_show_form(step_id=form_id, data_schema=schema_for({}))
        clean = _clean_item(model, user_input)
        if extra:
            clean.update(extra)
        errors = self._draft_errors(key, clean)
        if not errors:
            try:
                item = model.model_validate(clean)
            except ValidationError as exc:
                errors = (
                    _condition_errors(model, clean)
                    or _field_errors(exc)
                    or {"base": "invalid_item"}
                )
            else:
                self._store_item(key, item.model_dump(mode="json"))
                return self._collection_menu(key)
        return self.async_show_form(
            step_id=form_id, errors=errors, data_schema=self._prefilled(schema_for, clean)
        )

    async def _async_edit_step(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        schema_for: Callable[[dict[str, Any]], probatio.Schema],
        form_id: str,
    ) -> ConfigFlowResult:
        """Pick one entry of a collection, then prefill its item form."""
        items = self._topology_draft[key]
        identifiers = [str(item["id"]) for item in items]
        if user_input is not None and user_input.get("id") in identifiers:
            index = identifiers.index(str(user_input["id"]))
            self._edit_index = index
            return self.async_show_form(
                step_id=form_id, data_schema=self._prefilled(schema_for, items[index])
            )
        return self.async_show_form(
            step_id=f"{key}_edit", data_schema=_id_select_schema(identifiers)
        )

    async def _async_remove_step(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        minimum: int,
        in_use_error: str,
        in_use: Callable[[str], bool],
    ) -> ConfigFlowResult:
        """Remove one entry unless the site still needs it or uses it."""
        items = self._topology_draft[key]
        identifiers = [str(item["id"]) for item in items]
        schema = _id_select_schema(identifiers)
        step_id = f"{key}_remove"
        if user_input is None or user_input.get("id") not in identifiers:
            return self.async_show_form(step_id=step_id, data_schema=schema)
        item_id = str(user_input["id"])
        errors: dict[str, str] = {}
        if in_use(item_id):
            errors["base"] = in_use_error
        elif len(identifiers) <= minimum:
            errors["base"] = "remove_last"
        if errors:
            return self.async_show_form(step_id=step_id, errors=errors, data_schema=schema)
        self._topology_draft[key] = [
            item for item in items if item["id"] != item_id
        ]
        return self._collection_menu(key)

    def _load_form(self, defaults: dict[str, Any]) -> ConfigFlowResult:
        """The EV charger or heat pump form for the chosen kind."""
        if self._load_kind == "heat_pump":
            return self.async_show_form(
                step_id="major_load_heat",
                data_schema=self._prefilled(self._heat_schema, defaults),
            )
        return self.async_show_form(
            step_id="major_load_ev", data_schema=self._prefilled(self._ev_schema, defaults)
        )

    async def async_step_connections(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the site's grid connections."""
        return self._collection_menu("connections")

    async def async_step_connections_done(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Return to the topology menu."""
        return await self.async_step_topology()

    async def async_step_connections_add(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start a new grid connection."""
        self._edit_index = None
        return self.async_show_form(
            step_id="connections_item", data_schema=self._connection_schema({})
        )

    async def async_step_connections_edit(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pick the grid connection to edit."""
        return await self._async_edit_step(
            "connections", user_input, self._connection_schema, "connections_item"
        )

    async def async_step_connections_item(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store one grid connection."""
        return await self._async_item_step(
            "connections_item", "connections", GridConnection, user_input,
            self._connection_schema,
        )

    async def async_step_connections_remove(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Refuse while banks or major loads still use the connection."""
        draft = self._topology_draft
        return await self._async_remove_step(
            "connections", user_input,
            minimum=1,
            in_use_error="connection_in_use",
            in_use=lambda item_id: (
                any(item["connection_id"] == item_id for item in draft["banks"])
                or any(item["connection_id"] == item_id for item in draft["major_loads"])
            ),
        )

    async def async_step_banks(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the site's battery banks."""
        return self._collection_menu("banks")

    async def async_step_banks_done(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Return to the topology menu."""
        return await self.async_step_topology()

    async def async_step_banks_add(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start a new battery bank."""
        self._edit_index = None
        return self.async_show_form(step_id="banks_item", data_schema=self._bank_schema({}))

    async def async_step_banks_edit(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pick the battery bank to edit."""
        return await self._async_edit_step(
            "banks", user_input, self._bank_schema, "banks_item"
        )

    async def async_step_banks_item(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store one battery bank."""
        return await self._async_item_step(
            "banks_item", "banks", BatteryBank, user_input, self._bank_schema
        )

    async def async_step_banks_remove(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Refuse while inverters or a V2L charger still use the bank."""
        draft = self._topology_draft
        return await self._async_remove_step(
            "banks", user_input,
            minimum=1,
            in_use_error="bank_in_use",
            in_use=lambda item_id: (
                any(item["bank_id"] == item_id for item in draft["inverters"])
                or any(item["bank_id"] == item_id for item in draft["v2l_chargers"])
            ),
        )

    async def async_step_inverters(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the site's inverters."""
        return self._collection_menu("inverters")

    async def async_step_inverters_done(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Return to the topology menu."""
        return await self.async_step_topology()

    async def async_step_inverters_add(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start a new inverter."""
        self._edit_index = None
        return self.async_show_form(
            step_id="inverters_item", data_schema=self._inverter_schema({})
        )

    async def async_step_inverters_edit(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pick the inverter to edit."""
        return await self._async_edit_step(
            "inverters", user_input, self._inverter_schema, "inverters_item"
        )

    async def async_step_inverters_item(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store one inverter."""
        return await self._async_item_step(
            "inverters_item", "inverters", Inverter, user_input, self._inverter_schema
        )

    async def async_step_inverters_remove(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """The site always needs at least one inverter."""
        return await self._async_remove_step(
            "inverters", user_input,
            minimum=1,
            in_use_error="inverters_in_use",
            in_use=lambda _item_id: False,
        )

    async def async_step_v2l_chargers(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the optional V2L charger (one per bank)."""
        return self._collection_menu("v2l_chargers")

    async def async_step_v2l_chargers_done(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Return to the topology menu."""
        return await self.async_step_topology()

    async def async_step_v2l_chargers_add(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start a new V2L charger."""
        self._edit_index = None
        return self.async_show_form(
            step_id="v2l_chargers_item", data_schema=self._v2l_schema({})
        )

    async def async_step_v2l_chargers_edit(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pick the V2L charger to edit."""
        return await self._async_edit_step(
            "v2l_chargers", user_input, self._v2l_schema, "v2l_chargers_item"
        )

    async def async_step_v2l_chargers_item(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store one V2L charger."""
        return await self._async_item_step(
            "v2l_chargers_item", "v2l_chargers", V2LCharger, user_input, self._v2l_schema
        )

    async def async_step_v2l_chargers_remove(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """A V2L charger can always be removed."""
        return await self._async_remove_step(
            "v2l_chargers", user_input,
            minimum=0,
            in_use_error="v2l_chargers_in_use",
            in_use=lambda _item_id: False,
        )

    async def async_step_major_loads(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the site's major loads."""
        return self._collection_menu("major_loads")

    async def async_step_major_loads_done(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Return to the topology menu."""
        return await self.async_step_topology()

    async def async_step_major_loads_add(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pick the kind of major load before its form."""
        return self.async_show_form(step_id="major_load_kind", data_schema=probatio.Schema({
            probatio.Required("kind"): _LOAD_KINDS,
        }))

    async def async_step_major_load_kind(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Route to the EV charger or heat pump form for a new load."""
        if user_input is not None and user_input.get("kind") in ("ev_charger", "heat_pump"):
            self._load_kind = str(user_input["kind"])
            self._edit_index = None
            return self._load_form({})
        return self.async_show_form(step_id="major_load_kind", data_schema=probatio.Schema({
            probatio.Required("kind"): _LOAD_KINDS,
        }))

    async def async_step_major_loads_edit(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pick the major load to edit, then prefill its kind's form."""
        items = self._topology_draft["major_loads"]
        identifiers = [str(item["id"]) for item in items]
        if user_input is not None and user_input.get("id") in identifiers:
            index = identifiers.index(str(user_input["id"]))
            self._edit_index = index
            self._load_kind = str(items[index]["kind"])
            return self._load_form(items[index])
        return self.async_show_form(
            step_id="major_loads_edit", data_schema=_id_select_schema(identifiers)
        )

    async def async_step_major_loads_remove(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """A major load can always be removed."""
        return await self._async_remove_step(
            "major_loads", user_input,
            minimum=0,
            in_use_error="major_loads_in_use",
            in_use=lambda _item_id: False,
        )

    async def async_step_major_load_ev(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store one EV charger."""
        return await self._async_item_step(
            "major_load_ev", "major_loads", MajorLoad, user_input, self._ev_schema,
            extra={"kind": "ev_charger"},
        )

    async def async_step_major_load_heat(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store one heat pump."""
        return await self._async_item_step(
            "major_load_heat", "major_loads", MajorLoad, user_input, self._heat_schema,
            extra={"kind": "heat_pump"},
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Apply the whole document at once, or show why it is refused.

        Submitting the refusal form returns to the topology menu with the draft.
        """
        if user_input is not None:
            return await self.async_step_topology()
        draft = self._topology_draft
        document = {key: list(items) for key, items in draft.items()}
        errors: dict[str, str] = {}
        if not (document["connections"] and document["banks"] and document["inverters"]):
            errors["base"] = "incomplete_topology"
        else:
            for identifier in (str(item["id"]) for item in document["connections"]):
                shares = [
                    item["served_load_share_pct"]
                    for item in document["banks"]
                    if item["connection_id"] == identifier and not item.get("load_source_entity")
                ]
                if shares and not isclose(sum(shares), 100, abs_tol=1e-6, rel_tol=0):
                    errors["base"] = "invalid_load_shares"
                    break
        if errors:
            return self.async_show_form(
                step_id="save", errors=errors, data_schema=probatio.Schema({})
            )
        topology = validate_topology(document)
        if not isinstance(topology, Topology):
            return self.async_show_form(
                step_id="save", errors={"base": "invalid_topology"},
                data_schema=probatio.Schema({}),
            )
        revision = self.config_entry.options.get("topology_revision")
        return self.async_create_entry(title="", data={
            **self.config_entry.options,
            "topology": topology.model_dump(mode="json"),
            "topology_revision": revision + 1 if type(revision) is int else 1,
        })

