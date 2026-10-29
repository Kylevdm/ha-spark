"""Shared topology contract and pure limit maths, with no add-on imports."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.ha_spark.topology import (
    MajorLoad,
    Topology,
    charge_capacity_kw,
    charge_setpoint_a,
    effective_limits,
    is_running,
    major_load_reserve_kw,
    setpoint_a,
    validate_topology,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/limits"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_limits_contract(path: Path) -> None:
    case = json.loads(path.read_text())
    topology = validate_topology(case["topology"])
    if "reason" in case:
        assert topology == case["reason"]
        return
    assert isinstance(topology, Topology)
    limits = effective_limits(topology)
    assert limits.model_dump(mode="json") == case["limits"]
    for slot in case.get("slots", []):
        assert charge_capacity_kw(limits.banks[0], limits.connections[0], **slot["inputs"]) == (
            pytest.approx(slot["expected_kw"])
        )
    for command in case.get("setpoints", []):
        inverter = next(i for i in topology.inverters if i.id == command["inverter"])
        limit = next(i for i in limits.inverters if i.id == inverter.id)
        assert charge_setpoint_a(command["requested_a"], inverter, limit) == command["expected_a"]
    for reserve in case.get("reserves", []):
        excluded = set(reserve["excluded"])
        def exclude(load: MajorLoad, excluded: set[str] = excluded) -> bool:
            return load.id in excluded

        assert major_load_reserve_kw(topology.major_loads, exclude) == reserve["expected_kw"]


@pytest.mark.parametrize("owner", [0, 0.1, 42.6, 62.5, 100])
@pytest.mark.parametrize("step", [0.1, 0.5, 1, 2.5])
def test_rounding_never_exceeds_ceiling(owner: float, step: float) -> None:
    for requested in [i / 10 for i in range(1101)]:
        ceiling = min(owner, 62.5)
        actual = setpoint_a(requested, ceiling, step)
        assert 0 <= actual <= min(requested, ceiling)
        assert actual / step == pytest.approx(round(actual / step))


@pytest.mark.parametrize(
    "reading,age,expected",
    [
        (0.74, 60, True),
        (0.739, 0, False),
        (2, 61, False),
        (2, -1, False),
        (-1, 0, False),
        (float("nan"), 0, False),
        (float("inf"), 0, False),
        (None, 0, False),
        (2, None, False),
        (0, 0, False),
    ],
)
def test_running_requires_fresh_valid_evidence(
    reading: float | None,
    age: int | None,
    expected: bool,
) -> None:
    load = MajorLoad(id="ev", connection_id="grid", kind="ev_charger", demand_kw=7.4)
    now = datetime(2026, 10, 29, tzinfo=UTC)
    report = None if age is None else now - timedelta(seconds=age)
    assert is_running(load, reading, report, now) is expected
    assert not is_running(load, reading, now.replace(tzinfo=None), now)


@pytest.mark.parametrize("efficiency", [0, -1, float("nan"), float("inf"), 1.01])
def test_invalid_efficiency_refused(efficiency: float) -> None:
    case = json.loads((FIXTURES / "two_inverters.json").read_text())
    topology = validate_topology(case["topology"])
    assert isinstance(topology, Topology)
    limits = effective_limits(topology)
    with pytest.raises(ValueError, match="^invalid_capacity_input$"):
        charge_capacity_kw(limits.banks[0], limits.connections[0], charge_efficiency=efficiency)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -1, 1001])
@pytest.mark.parametrize(
    "group,field",
    [
        ("connections", "import_limit_a"),
        ("connections", "export_limit_a"),
        ("connections", "supply_voltage_v"),
        ("connections", "dno_export_limit_kw"),
        ("banks", "charge_limit_a"),
        ("banks", "discharge_limit_a"),
        ("banks", "nominal_voltage_v"),
        ("banks", "usable_capacity_kwh"),
        ("banks", "min_soc"),
        ("banks", "target_cap"),
        ("inverters", "charge_limit_a"),
        ("inverters", "discharge_limit_a"),
    ],
)
def test_invalid_numbers_have_fixed_reason(group: str, field: str, value: float) -> None:
    raw = json.loads((FIXTURES / "two_inverters.json").read_text())["topology"]
    raw[group][0][field] = value
    assert validate_topology(raw) == "invalid_topology"


def test_reserve_planner_and_guard_predicates() -> None:
    now = datetime(2026, 10, 29, tzinfo=UTC)
    ev = MajorLoad(
        id="ev", connection_id="grid", kind="ev_charger", demand_kw=7.4, dispatch_driven=True
    )
    heat = MajorLoad(
        id="heat", connection_id="grid", kind="heat_pump", heating_demand_kw=4, cooling_demand_kw=6
    )
    unseen = MajorLoad(id="unseen", connection_id="grid", kind="ev_charger", demand_kw=3)
    loads = (ev, heat, unseen)

    def running(load: MajorLoad) -> bool:
        return is_running(load, 2 if load.id == "heat" else None, now, now)

    assert major_load_reserve_kw(loads, running) == 7.4
    assert major_load_reserve_kw(loads, lambda load: running(load) or load.dispatch_driven) == 3
    assert major_load_reserve_kw((), running) == 0


@pytest.mark.parametrize(
    "requested,limit,step",
    [
        (-1, 10, 1),
        (10, -1, 1),
        (10, 10, 0),
        (10, 10, -1),
        (float("nan"), 10, 1),
        (10, float("inf"), 1),
        (10, 10, float("nan")),
    ],
)
def test_invalid_setpoint_input_refused(requested: float, limit: float, step: float) -> None:
    with pytest.raises(ValueError, match="^invalid_setpoint_input$"):
        setpoint_a(requested, limit, step)


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("connections", "supply_voltage_v", 199),
        ("connections", "supply_voltage_v", 261),
        ("connections", "import_limit_a", 201),
        ("connections", "export_limit_a", 201),
        ("connections", "dno_export_limit_kw", 101),
        ("banks", "usable_capacity_kwh", 201),
        ("banks", "nominal_voltage_v", 11),
        ("banks", "charge_limit_a", 501),
        ("inverters", "discharge_limit_a", 501),
    ],
)
def test_uk_home_bounds(group: str, field: str, value: float) -> None:
    raw = json.loads((FIXTURES / "two_inverters.json").read_text())["topology"]
    raw[group][0][field] = value
    assert validate_topology(raw) == "invalid_topology"
