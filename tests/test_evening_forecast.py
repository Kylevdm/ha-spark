"""Evening-plan selection and persistence through the forecast store."""

import ast
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy import evening_forecast, scheduler
from ha_spark.energy.evening_forecast import EveningForecastStore
from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import DispatchSlot, FreeSession, PlannerConfig, PlannerInputs
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.solar_source import NativeForecast, SolarSource
from ha_spark.energy.sources import NATIVE_MAX_AGE

TZ = ZoneInfo("Europe/London")
HALF_HOUR = timedelta(minutes=30)
CFG = PlannerConfig(10, 50, 20, 95, 50, 1, time(23, 30), time(5, 30))
DAY = date(2026, 1, 2)


def inputs_at(issued: datetime, kwh: float, cfg: PlannerConfig = CFG) -> PlannerInputs:
    geometry = compute_horizon_geometry(issued, cfg.window_start, cfg.window_end, TZ)
    return PlannerInputs(
        SocMeasurement(SocStatus.OK, issued, 50), 8, 10,
        geometry=geometry, now=issued,
        solar_slots=(kwh,) * len(geometry.slot_starts),
        solar_slots_known=(True,) * len(geometry.slot_starts),
    )


async def test_last_plan_before_window_wins_for_whole_day(tmp_path):
    store = EveningForecastStore(str(tmp_path / "household.db"))
    before = datetime(2026, 1, 1, 23, tzinfo=TZ)
    first = inputs_at(before, 1)
    latest = inputs_at(before + timedelta(minutes=15), 2)
    assert latest.solar_slots_known and latest.geometry is not None
    # The latest revision is missing one slot; don't carry it over from the older revision.
    index = latest.geometry.slot_starts.index(datetime.combine(DAY, time.min, UTC))
    known = list(latest.solar_slots_known)
    known[index] = False
    latest = replace(latest, solar_slots_known=tuple(known))
    await store.record(first, CFG, TZ)
    await store.record(latest, CFG, TZ)
    await store.record(first, CFG, TZ)  # Older write completing out of order.
    await store.record(inputs_at(before + HALF_HOUR, 3), CFG, TZ)  # At the cutoff.
    await store.record(inputs_at(before + timedelta(hours=2), 4), CFG, TZ)
    await store.record(inputs_at(before + timedelta(days=30), 5), CFG, TZ)

    slots = await store.slots(DAY, DAY + timedelta(days=1))
    assert len(slots) == 48
    assert slots[0].p50_kwh is None
    assert (slots[1].p10_kwh, slots[1].p50_kwh, slots[1].p90_kwh) == (None, 2, None)
    assert slots[12].p50_kwh == 2


async def test_after_midnight_window_ignores_daytime_dispatch_and_purchase(tmp_path):
    store = EveningForecastStore(str(tmp_path / "household.db"))
    cfg = replace(CFG, window_start=time(0, 30))
    issued = datetime(2026, 1, 2, 0, 15, tzinfo=TZ)
    dispatch = DispatchSlot(issued, issued + timedelta(minutes=10), source="intelligent")
    await store.record(
        replace(inputs_at(issued, 1, cfg), dispatches=(dispatch,),
                dispatch_purchase_slots=(dispatch,)), cfg, TZ,
    )
    later = datetime(2026, 1, 2, 14, tzinfo=TZ)
    dispatch = DispatchSlot(later, later + HALF_HOUR, source="intelligent")
    free_session = FreeSession("free_electricity", "booking", later, later + HALF_HOUR)
    await store.record(replace(
        inputs_at(later, 2, cfg), dispatches=(dispatch,), dispatch_purchase_slots=(dispatch,),
        free_sessions=(free_session,),
    ), cfg, TZ)
    slots = await store.slots(DAY, DAY + timedelta(days=1))
    assert len(slots) == 48
    assert {slot.p50_kwh for slot in slots} == {1}
    tomorrow = await store.slots(DAY + timedelta(days=1), DAY + timedelta(days=2))
    assert len(tomorrow) == 48
    assert {slot.p50_kwh for slot in tomorrow} == {2}


@pytest.mark.parametrize(("day", "count"), [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
async def test_native_bands_and_dst_slot_instants(tmp_path, day, count):
    store = EveningForecastStore(str(tmp_path / "household.db"))
    issued = datetime.combine(day - timedelta(days=1), time(23), TZ)
    start = datetime.combine(day, time.min, TZ).astimezone(UTC)
    await store.record(inputs_at(issued, 2), CFG, TZ, native_slots=((start, 2, 4, 6),))
    slots = await store.slots(day, day + timedelta(days=1))
    assert [slot.start for slot in slots] == [start + HALF_HOUR * i for i in range(count)]
    assert (slots[0].p10_kwh, slots[0].p50_kwh, slots[0].p90_kwh) == (1, 2, 3)
    assert slots[1].p10_kwh is slots[1].p90_kwh is None


async def test_repeated_window_start_uses_first_occurrence(tmp_path):
    store = EveningForecastStore(str(tmp_path / "household.db"))
    cfg = replace(CFG, window_start=time(1, 30))
    day = date(2026, 10, 25)
    first = datetime.combine(day, time(1, 15), TZ)
    repeated = first.replace(fold=1)
    await store.record(inputs_at(first, 1, cfg), cfg, TZ)
    await store.record(inputs_at(repeated, 2, cfg), cfg, TZ)

    slots = await store.slots(day, day + timedelta(days=1))
    assert {slot.p50_kwh for slot in slots if slot.p50_kwh is not None} == {1}
    assert await store.slots(day + timedelta(days=1), day + timedelta(days=2))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
async def test_invalid_forecast_values_are_null(tmp_path, value):
    store = EveningForecastStore(str(tmp_path / "household.db"))
    await store.record(inputs_at(datetime(2026, 1, 1, 23, tzinfo=TZ), value), CFG, TZ)
    slots = await store.slots(DAY, DAY + timedelta(days=1))
    assert len(slots) == 48
    assert all(slot.p50_kwh is None for slot in slots)


async def test_missing_forecast_database_is_not_created(tmp_path):
    path = tmp_path / "household.db"
    assert await EveningForecastStore(str(path)).slots(DAY, DAY + timedelta(days=1)) == []
    assert not path.exists()


def test_evening_forecast_has_no_write_path():
    forbidden = (
        "ha_spark.devices", "ha_spark.authority", "ha_spark.transitions",
        "ha_spark.mode_preconditions", "ha_spark.daemon_channel", "ha_spark.api.commands",
        "ha_spark.api.native_apply", "ha_spark.energy.supply_guard", "ha_spark.energy.soc_monitor",
        "ha_spark.energy.v2l", "ha_spark.energy.scheduler", "ha_spark.energy.plan_run",
        "ha_spark.energy.sources", "ha_spark.ha",
    )
    for node in ast.walk(ast.parse(Path(evening_forecast.__file__).read_text())):
        if isinstance(node, ast.Import | ast.ImportFrom):
            parent = f"{node.module}." if isinstance(node, ast.ImportFrom) and node.module else ""
            assert not any((parent + alias.name).startswith(forbidden) for alias in node.names)
            assert not any(alias.name.split(".")[-1] == "call_service" for alias in node.names)
        assert not (isinstance(node, ast.Attribute) and node.attr == "call_service")
        assert not (isinstance(node, ast.Name) and node.id == "call_service")


def _run(issued: datetime) -> SimpleNamespace:
    return SimpleNamespace(inputs=inputs_at(issued, 2), cfg=CFG, tz=TZ)


def _native(issued: datetime, fetched_at: datetime | None, kind: str = "native") -> SolarSource:
    start = datetime.combine(DAY, time.min, TZ).astimezone(UTC)
    forecast = NativeForecast("ok", fetched_at, 1, 10, ((start, 2, 4, 6),))
    return SolarSource(kind, forecast, issued, issued)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "age", "bands"),
    [
        ("native", timedelta(minutes=5), (1, 3)),
        ("native", NATIVE_MAX_AGE + timedelta(minutes=1), (None, None)),
        ("entities", timedelta(minutes=5), (None, None)),
    ],
)
async def test_scheduler_hook_stores_bands_only_from_a_fresh_native_source(
    tmp_path, kind, age, bands,
):
    settings = SimpleNamespace(db_path=str(tmp_path / "household.db"), timezone="Europe/London")
    issued = datetime(2026, 1, 1, 23, tzinfo=TZ).astimezone(UTC)
    await scheduler._record_evening_forecast(
        settings, _run(issued), _native(issued, issued - age, kind),  # type: ignore[arg-type]
    )
    slots = await EveningForecastStore(settings.db_path).slots(DAY, DAY + timedelta(days=1))
    assert (slots[0].p10_kwh, slots[0].p50_kwh, slots[0].p90_kwh) == (bands[0], 2, bands[1])


async def test_scheduler_hook_failure_logs_fixed_text_and_never_raises(
    tmp_path, monkeypatch, caplog,
):
    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret-payload")

    monkeypatch.setattr(EveningForecastStore, "record", boom)
    settings = SimpleNamespace(db_path=str(tmp_path / "household.db"), timezone="Europe/London")
    issued = datetime(2026, 1, 1, 23, tzinfo=TZ).astimezone(UTC)
    await scheduler._record_evening_forecast(
        settings, _run(issued), _native(issued, issued),  # type: ignore[arg-type]
    )
    assert "Recording evening-plan solar forecast failed" in caplog.text
    assert "secret-payload" not in caplog.text
