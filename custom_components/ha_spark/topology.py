"""Pure, bounded topology contract and DC/AC limit maths, mirrored in the add-on.

No authority or I/O lives here. Callers supply time and charge efficiency. Numeric
bounds are per field, sized for a UK home: AC voltage 200–260 V, supply current
0–200 A, DC voltage 12–1000 V, DC current 0–500 A, capacity up to 200 kWh, DNO
permission up to 100 kW and load demand up to 50 kW. Missing device limits are zero, never a guessed
hardware capability.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from math import isclose, isfinite
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SafeId = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")]
EntityId = Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$", max_length=255)]
Current = Annotated[float, Field(strict=True, ge=0, le=500, allow_inf_nan=False)]
SupplyCurrent = Annotated[float, Field(strict=True, ge=0, le=200, allow_inf_nan=False)]
Demand = Annotated[float, Field(strict=True, gt=0, le=50, allow_inf_nan=False)]
Percent = Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)]
DriverKind = Literal["solis", "alphaess"]


class FrozenModel(BaseModel):
    """Reject unknown fields, non-finite numbers and coercion of control flags."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class GridConnection(FrozenModel):
    """AC supply constraints; DNO permission may disable export entirely."""

    id: SafeId
    supply_voltage_v: Annotated[float, Field(strict=True, ge=200, le=260, allow_inf_nan=False)]
    import_limit_a: SupplyCurrent = 0
    export_limit_a: SupplyCurrent = 0
    dno_export_limit_kw: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)] = 0
    grid_power_entity: EntityId | None = None


class BatteryBank(FrozenModel):
    """One energy store, counted once even when several inverters share it."""

    id: SafeId
    connection_id: SafeId
    usable_capacity_kwh: Annotated[float, Field(strict=True, gt=0, le=200, allow_inf_nan=False)]
    nominal_voltage_v: Annotated[float, Field(strict=True, ge=12, le=1000, allow_inf_nan=False)]
    one_way_charge_efficiency: Annotated[
        float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)
    ] = 0.949
    min_soc: Percent
    target_cap: Percent
    charge_limit_a: Current = 0
    discharge_limit_a: Current = 0
    served_load_share_pct: Percent | None = None
    load_source_entity: EntityId | None = None

    @model_validator(mode="after")
    def valid_bank(self) -> Self:
        """A bank has exactly one load attribution and a non-inverted SoC range."""
        if self.min_soc > self.target_cap:
            raise ValueError("invalid_soc_range")
        if (self.served_load_share_pct is None) == (self.load_source_entity is None):
            raise ValueError("invalid_load_attribution")
        return self


class Inverter(FrozenModel):
    """Owner-entered DC limits, with explicit confirmation before use."""

    id: SafeId
    bank_id: SafeId
    driver_kind: DriverKind
    charge_limit_a: Current = 0
    discharge_limit_a: Current = 0
    confirmed: Annotated[bool, Field(strict=True)] = False
    grid_port_power_entity: EntityId | None = None
    battery_power_entity: EntityId | None = None
    power_sign: Literal["positive_import", "negative_import"] = "positive_import"
    transport: Literal["legacy", "native"] = "legacy"
    gateway_host: Annotated[
        str, Field(strict=True, min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")
    ] | None = None
    gateway_port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 502
    gateway_unit_id: Annotated[int, Field(strict=True, ge=1, le=247)] = 1

    @model_validator(mode="after")
    def valid_transport(self) -> Self:
        """Only Solis has a native transport; saving never validates a device."""
        if self.transport == "native" and (
            self.driver_kind != "solis" or self.gateway_host is None
        ):
            raise ValueError("invalid_native_gateway")
        return self


class V2LCharger(FrozenModel):
    """DC source into a bank, never a site-import load."""

    id: SafeId
    bank_id: SafeId
    max_current_a: Current = 0


class MajorLoad(FrozenModel):
    """Declared long-running demand; heat pumps cover heating and cooling."""

    id: SafeId
    connection_id: SafeId
    kind: Literal["ev_charger", "heat_pump"]
    demand_kw: Demand | None = None
    heating_demand_kw: Demand | None = None
    cooling_demand_kw: Demand | None = None
    power_entity: EntityId | None = None
    dispatch_driven: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def valid_demands(self) -> Self:
        """No implicit demand defaults or dispatch-driven heat pumps."""
        if self.kind == "ev_charger":
            if (
                self.demand_kw is None
                or self.heating_demand_kw is not None
                or (self.cooling_demand_kw is not None)
            ):
                raise ValueError("invalid_load_demand")
        elif (
            self.heating_demand_kw is None
            or self.cooling_demand_kw is None
            or (self.demand_kw is not None or self.dispatch_driven)
        ):
            raise ValueError("invalid_load_demand")
        return self

    @property
    def effective_demand_kw(self) -> float:
        """AC demand used by both reserve callers."""
        if self.kind == "ev_charger":
            assert self.demand_kw is not None
            return self.demand_kw
        assert self.heating_demand_kw is not None and self.cooling_demand_kw is not None
        return max(self.heating_demand_kw, self.cooling_demand_kw)


class Topology(FrozenModel):
    """Whole-document validation prevents duplicate or dangling allocations."""

    connections: Annotated[tuple[GridConnection, ...], Field(min_length=1, max_length=4)]
    banks: Annotated[tuple[BatteryBank, ...], Field(min_length=1, max_length=6)]
    inverters: Annotated[tuple[Inverter, ...], Field(min_length=1, max_length=8)]
    v2l_chargers: Annotated[tuple[V2LCharger, ...], Field(max_length=6)] = ()
    major_loads: Annotated[tuple[MajorLoad, ...], Field(max_length=12)] = ()

    @model_validator(mode="after")
    def valid_graph(self) -> Self:
        """Ids are globally unique, membership single-valued and shares complete."""
        ids = [
            item.id
            for group in (
                self.connections,
                self.banks,
                self.inverters,
                self.v2l_chargers,
                self.major_loads,
            )
            for item in group
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_id")
        connections = {c.id for c in self.connections}
        banks = {b.id for b in self.banks}
        if (
            any(b.connection_id not in connections for b in self.banks)
            or any(load.connection_id not in connections for load in self.major_loads)
            or any(i.bank_id not in banks for i in self.inverters)
            or any(v.bank_id not in banks for v in self.v2l_chargers)
        ):
            raise ValueError("invalid_membership")
        if len({v.bank_id for v in self.v2l_chargers}) != len(self.v2l_chargers):
            raise ValueError("duplicate_v2l_bank")
        if sum(load.dispatch_driven for load in self.major_loads) > 1:
            raise ValueError("multiple_dispatch_loads")
        for connection in self.connections:
            shares = [
                b.served_load_share_pct
                for b in self.banks
                if b.connection_id == connection.id and b.load_source_entity is None
            ]
            if shares and not isclose(
                sum(s for s in shares if s is not None), 100, abs_tol=1e-6, rel_tol=0
            ):
                raise ValueError("invalid_load_shares")
        return self


def validate_topology(raw: object) -> Topology | Literal["invalid_topology"]:
    """Untrusted-input seam: never expose validation errors or rejected values."""
    try:
        return Topology.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        return "invalid_topology"


class DriverCapabilities(FrozenModel):
    """None is no known driver ceiling, not an owner-limit default."""

    charge_ceiling_a: float | None
    discharge_ceiling_a: float | None
    setpoint_step_a: float | None


def driver_capabilities(kind: DriverKind) -> DriverCapabilities:
    """AlphaESS has no settable charge rate but retains owner planning limits."""
    if kind == "solis":
        return DriverCapabilities(
            charge_ceiling_a=62.5, discharge_ceiling_a=62.5, setpoint_step_a=1
        )
    if kind == "alphaess":
        return DriverCapabilities(
            charge_ceiling_a=None, discharge_ceiling_a=None, setpoint_step_a=None
        )
    raise ValueError("invalid_driver_kind")


class InverterLimits(FrozenModel):
    """DC amps after confirmation and capability ceilings."""

    id: str
    bank_id: str
    charge_a: float
    discharge_a: float


class BankLimits(FrozenModel):
    """DC power capped by the bank and sum of its inverters."""

    id: str
    connection_id: str
    charge_a: float
    charge_kw: float
    bank_charge_kw: float
    discharge_kw: float


class ConnectionLimits(FrozenModel):
    """Shared AC budgets, not multiplied by the number of attached banks."""

    id: str
    import_kw: float
    site_export_kw: float


class EffectiveLimits(FrozenModel):
    """Stable, serialisable result, in topology order."""

    inverters: tuple[InverterLimits, ...]
    banks: tuple[BankLimits, ...]
    connections: tuple[ConnectionLimits, ...]


def effective_limits(topology: Topology) -> EffectiveLimits:
    """Compute only from validated owner limits and known driver capabilities."""
    inverters = []
    for inverter in topology.inverters:
        caps = driver_capabilities(inverter.driver_kind)
        inverters.append(
            InverterLimits(
                id=inverter.id,
                bank_id=inverter.bank_id,
                charge_a=min(
                    inverter.charge_limit_a,
                    caps.charge_ceiling_a
                    if caps.charge_ceiling_a is not None
                    else inverter.charge_limit_a,
                )
                if inverter.confirmed
                else 0,
                discharge_a=min(
                    inverter.discharge_limit_a,
                    caps.discharge_ceiling_a
                    if caps.discharge_ceiling_a is not None
                    else inverter.discharge_limit_a,
                )
                if inverter.confirmed
                else 0,
            )
        )
    banks = []
    for bank in topology.banks:
        charge_a = min(
            bank.charge_limit_a, sum(i.charge_a for i in inverters if i.bank_id == bank.id)
        )
        discharge_a = min(
            bank.discharge_limit_a, sum(i.discharge_a for i in inverters if i.bank_id == bank.id)
        )
        banks.append(
            BankLimits(
                id=bank.id,
                connection_id=bank.connection_id,
                charge_a=charge_a,
                charge_kw=charge_a * bank.nominal_voltage_v / 1000,
                bank_charge_kw=bank.charge_limit_a * bank.nominal_voltage_v / 1000,
                discharge_kw=discharge_a * bank.nominal_voltage_v / 1000,
            )
        )
    return EffectiveLimits(
        inverters=tuple(inverters),
        banks=tuple(banks),
        connections=tuple(
            ConnectionLimits(
                id=c.id,
                import_kw=c.import_limit_a * c.supply_voltage_v / 1000,
                site_export_kw=min(
                    c.dno_export_limit_kw, c.export_limit_a * c.supply_voltage_v / 1000
                ),
            )
            for c in topology.connections
        ),
    )


def setpoint_a(requested_a: float, limit_a: float, step_a: float) -> float:
    """Round down, using decimal arithmetic to avoid stepping above a ceiling."""
    if not all(isfinite(v) and v >= 0 for v in (requested_a, limit_a, step_a)) or step_a == 0:
        raise ValueError("invalid_setpoint_input")
    ceiling = min(requested_a, limit_a)
    step = Decimal(str(step_a))
    result = float((Decimal(str(ceiling)) / step).to_integral_value(rounding=ROUND_FLOOR) * step)
    return min(result, ceiling)


def charge_setpoint_a(
    requested_a: float, inverter: Inverter, limits: InverterLimits
) -> float | None:
    """No charge allocation command exists for a non-settable driver."""
    if inverter.id != limits.id or inverter.bank_id != limits.bank_id:
        raise ValueError("invalid_inverter_limits")
    caps = driver_capabilities(inverter.driver_kind)
    if caps.setpoint_step_a is None:
        return None
    ceiling = min(limits.charge_a, inverter.charge_limit_a) if inverter.confirmed else 0
    if caps.charge_ceiling_a is not None:
        ceiling = min(ceiling, caps.charge_ceiling_a)
    return setpoint_a(requested_a, ceiling, caps.setpoint_step_a)


def charge_capacity_kw(
    bank: BankLimits,
    connection: ConnectionLimits,
    *,
    charge_efficiency: float,
    base_load_kw: float = 0,
    active_major_loads_kw: float = 0,
    reserve_kw: float = 0,
    planned_v2l_dc_kw: float = 0,
) -> float:
    """Per-slot stored DC power; V2L consumes bank room, not AC site import.

    For shared connections the caller passes only the remaining connection budget
    to each allocation: this primitive must not grant the same AC budget twice.
    """
    values = (base_load_kw, active_major_loads_kw, reserve_kw, planned_v2l_dc_kw)
    if (
        bank.connection_id != connection.id
        or not all(isfinite(v) and v >= 0 for v in values)
        or (not isfinite(charge_efficiency) or not 0 < charge_efficiency <= 1)
    ):
        raise ValueError("invalid_capacity_input")
    return max(
        0,
        min(
            bank.charge_kw,
            bank.bank_charge_kw - planned_v2l_dc_kw,
            (connection.import_kw - base_load_kw - active_major_loads_kw - reserve_kw)
            * charge_efficiency,
        ),
    )


def major_load_reserve_kw(
    loads: Iterable[MajorLoad],
    exclude: Callable[[MajorLoad], bool],
) -> float:
    """Same primitive for planner (running or expected) and guard (running)."""
    demands = [load.effective_demand_kw for load in loads if not exclude(load)]
    return max(max(demands, default=0), sum(demands) * 0.5)


def is_running(
    load: MajorLoad,
    reading_kw: float | None,
    reported_at: datetime | None,
    now: datetime,
) -> bool:
    """Only fresh, aware, non-future evidence at 10% of demand counts as running."""
    if reading_kw is None or not isfinite(reading_kw) or reading_kw < 0 or reported_at is None:
        return False
    if now.utcoffset() is None or reported_at.utcoffset() is None:
        return False
    age = now.timestamp() - reported_at.timestamp()
    return 0 <= age <= 60 and reading_kw >= load.effective_demand_kw / 10
