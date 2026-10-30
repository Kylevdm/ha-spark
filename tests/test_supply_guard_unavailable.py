"""#555: the daemon guard on an unavailable topology record.

With the accepted record unreadable the limit is the legacy ``supply_max_current_a``
at ``supply_voltage_v``. The guard writes 0 A only while a fresh grid reading (a
finite W value reported no more than 60 s ago and no more than 5 s ahead) exceeds
it; anything else writes nothing. The zero write keeps the authority gate and the
read-back. Documented gap: a lower native connection limit isn't enforced here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ha_spark.devices.execution import execution_context
from ha_spark.energy.supply_guard import SupplyGuard
from ha_spark.energy.topology_record import LoadedTopology
from ha_spark.ha.models import EntityState
from tests.authority_helpers import legacy_settings as Settings
from tests.test_solis_export import FakeRest

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
_GRID = "sensor.grid_power"
# 20 A x 240 V = 4800 W legacy supply limit.
_LIMIT_W = 4800.0


@pytest.fixture(autouse=True)
def _unavailable_record(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    context = execution_context()
    context.limits_record = LoadedTopology("unavailable")
    context.planned_limits.set(context.limits_key())
    yield
    context.limits_record = None
    context.planned_limits.set(("legacy", None))


class _GridRest(FakeRest):
    """The Solis overlay plus a grid sensor with a unit and a report time."""

    def __init__(
        self,
        state: str | None,
        *,
        reported: datetime | None = _NOW,
        unit: str | None = "W",
        setpoint_a: float = 20.0,
        takes_write: bool = True,
    ) -> None:
        super().__init__()
        self.grid_state = state
        self.reported = reported
        self.unit = unit
        self.takes_write = takes_write
        self.states["sensor.solis_control_timed_charge_current"] = str(setpoint_a)

    async def get_state(self, entity_id: str) -> EntityState:
        if entity_id != _GRID:
            return await super().get_state(entity_id)
        if self.grid_state is None:
            raise RuntimeError("404")
        attributes = {} if self.unit is None else {"unit_of_measurement": self.unit}
        return EntityState(
            entity_id=_GRID, state=self.grid_state, attributes=attributes,
            last_reported=self.reported,
        )

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[EntityState]:
        if not self.takes_write:
            self.calls.append((domain, service, data or {}))
            return []
        return await super().call_service(domain, service, data)


def _settings(tmp_path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "authority_mode": "optimise",
        "db_path": str(tmp_path / "events.db"),
        "grid_power_entity": _GRID,
        "supply_max_current_a": 20.0,
        "supply_voltage_v": 240.0,
        "battery_voltage_v": 51.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _current_writes(rest: FakeRest) -> list[object]:
    return [
        call[2]["value"]
        for call in rest.calls
        if call[:2] == ("modbus", "write_register") and call[2]["address"] == 43141
    ]


async def _tick(settings: Settings, rest: FakeRest):  # type: ignore[no-untyped-def]
    return await SupplyGuard(settings, rest).tick(2040.0, now=_NOW)  # type: ignore[arg-type]


async def test_fresh_over_import_writes_zero_and_verifies_it(tmp_path) -> None:
    rest = _GridRest(str(_LIMIT_W + 1))

    line = await _tick(_settings(tmp_path), rest)

    assert _current_writes(rest) == [0]
    assert line is not None and line.startswith("[APPLIED]")
    assert rest.states["sensor.solis_control_timed_charge_current"] == "0.0"


@pytest.mark.parametrize(
    ("age", "label"),
    [(timedelta(seconds=60), "60 s old"), (timedelta(seconds=-5), "5 s ahead")],
)
async def test_freshness_bounds_are_inclusive(tmp_path, age: timedelta, label: str) -> None:
    rest = _GridRest(str(_LIMIT_W + 1), reported=_NOW - age)

    await _tick(_settings(tmp_path), rest)

    assert _current_writes(rest) == [0], label


async def test_fresh_reading_at_or_under_the_limit_writes_nothing(tmp_path) -> None:
    rest = _GridRest(str(_LIMIT_W))

    assert await _tick(_settings(tmp_path), rest) is None
    assert rest.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reported": _NOW - timedelta(seconds=61)},
        {"reported": _NOW + timedelta(seconds=6)},
        {"reported": None},
        {"reported": datetime(2026, 10, 30, 12, 0)},  # naive: no usable report time
        {"unit": None},
        {"unit": "kW"},
    ],
    ids=["stale", "future", "no-report-time", "naive-report-time", "no-unit", "kw"],
)
async def test_unfresh_over_import_writes_nothing(tmp_path, kwargs: dict[str, object]) -> None:
    rest = _GridRest(str(_LIMIT_W * 2), **kwargs)  # type: ignore[arg-type]

    assert await _tick(_settings(tmp_path), rest) is None
    assert rest.calls == []


@pytest.mark.parametrize("state", [None, "unavailable", "nan", "inf", "-inf"],
                         ids=["missing", "unavailable", "nan", "inf", "-inf"])
async def test_missing_or_non_finite_reading_writes_nothing(tmp_path, state: str | None) -> None:
    rest = _GridRest(state)

    assert await _tick(_settings(tmp_path), rest) is None
    assert rest.calls == []


@pytest.mark.parametrize(
    "update",
    [
        {"supply_max_current_a": 0.0},
        {"supply_max_current_a": -20.0},
        {"supply_max_current_a": float("nan")},
        {"supply_max_current_a": float("inf")},
        {"supply_voltage_v": 0.0},
        {"supply_voltage_v": float("nan")},
    ],
    ids=["zero", "negative", "nan", "inf", "zero-volts", "nan-volts"],
)
async def test_invalid_legacy_setting_writes_nothing(tmp_path, update: dict[str, object]) -> None:
    settings = _settings(tmp_path).model_copy(update=update)
    rest = _GridRest(str(_LIMIT_W * 10))

    assert await _tick(settings, rest) is None
    assert rest.calls == []


async def test_already_zero_setpoint_is_not_rewritten(tmp_path) -> None:
    rest = _GridRest(str(_LIMIT_W + 1), setpoint_a=0.0)

    assert await _tick(_settings(tmp_path), rest) is None
    assert rest.calls == []


async def test_zero_write_needs_authority(tmp_path) -> None:
    rest = _GridRest(str(_LIMIT_W + 1))

    line = await _tick(_settings(tmp_path, authority_mode="observe"), rest)

    assert line is not None and not line.startswith("[APPLIED]")
    assert _current_writes(rest) == []


async def test_zero_write_refused_on_stale_planned_limits(tmp_path) -> None:
    execution_context().planned_limits.set(("legacy", None))
    rest = _GridRest(str(_LIMIT_W + 1))

    line = await _tick(_settings(tmp_path), rest)

    assert line is not None and line.startswith("[REFUSED]")
    assert _current_writes(rest) == []


async def test_zero_write_that_does_not_read_back_is_unverified(tmp_path) -> None:
    rest = _GridRest(str(_LIMIT_W + 1), takes_write=False)

    line = await _tick(_settings(tmp_path), rest)

    assert _current_writes(rest) == [0]
    assert line is not None and not line.startswith("[APPLIED]")
    assert rest.states["sensor.solis_control_timed_charge_current"] == "20.0"
