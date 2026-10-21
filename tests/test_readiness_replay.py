"""The replay is pure and reproduces the owner-approved fixture to the penny."""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.readiness_ledger import SlotRecord
from ha_spark.energy.readiness_measurements import (
    DayRecord,
    MeasuredSlot,
    Measurement,
)
from ha_spark.energy.readiness_replay import replay_day

TZ = ZoneInfo("Europe/London")
MIDNIGHT = datetime(2026, 10, 14, 0, 0, tzinfo=TZ)  # BST: 48 slots in the day
PENNY = 0.005

CHARGE: dict[str, Any] = {"fraction": 1.0, "current": 40.0}
HOLD: dict[str, Any] = {"held": True}
SELF: dict[str, Any] = {}

# The owner-approved fixture day (ticket #434): blocks of half-hour slots, each
# with one intent and one measured load profile, and the actual per-block
# metered import/export spread evenly across the block's slots.
# (slots, intent kwargs, load, solar, ev, v2l, actual import, actual export,
#  import rate, event rate)
FIXTURE_BLOCKS: list[tuple[Any, ...]] = [
    (6, CHARGE, 0.3, 0.0, 0.0, 0.0, 8.0 / 6, 0.0, 0.07, None),  # B1 00:00-03:00
    (5, CHARGE, 0.3, 0.0, 0.0, 0.0, 1.5 / 5, 0.0, 0.07, None),  # B2 03:00-05:30
    (5, SELF, 0.6, 0.1, 0.0, 0.0, 0.4 / 5, 0.0, 0.30, None),  # B3 05:30-08:00
    (4, HOLD, 0.5, 0.3, 1.5, 0.0, 6.8 / 4, 0.0, 0.30, None),  # B4 08:00-10:00
    (10, SELF, 0.4, 1.4, 0.0, 0.0, 0.0, 6.0 / 10, 0.30, None),  # B5 10:00-15:00
    (2, SELF, 0.8, 0.3, 0.0, 0.5, 0.0, 0.0, 0.30, None),  # B6 15:00-16:00
    (2, {"export_kw": 3.0}, 0.7, 0.1, 0.0, 0.0, 0.0, 0.0, 0.30, 1.00),  # B7 16:00-17:00
    (4, SELF, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.30, None),  # B8 17:00-19:00
    (9, SELF, 0.6, 0.0, 0.0, 0.0, 6.0 / 9, 0.0, 0.30, None),  # B9 19:00-23:30
    (1, CHARGE, 0.25, 0.0, 0.0, 0.0, 1.5, 0.0, 0.07, None),  # B10 23:30-24:00
]

# Replay cost and battery energy after each block, from the owner's table.
BLOCK_RESULTS = [
    (0.616, 9.0), (0.105, 9.0), (0.0, 6.5), (2.040, 6.5),
    (-1.125, 9.0), (0.0, 9.0), (-3.000, 4.8), (0.0, 1.2),
    (1.560, 1.0), (0.105, 2.0),
]


def intent(index: int, *, fraction: float = 0.0, current: float | None = None,
           export_kw: float = 0.0, held: bool = False, v2l_available: bool = False,
           import_rate: float = 0.30, event_rate: float | None = None) -> SlotRecord:
    start = MIDNIGHT + timedelta(minutes=30 * index)
    return SlotRecord(
        slot_start=start, operating_mode="observe", revision_id="R", issued_at=start,
        charge_window_fraction=fraction, charge_current_a=current, export_kw=export_kw,
        hold_state="held" if held else "not_held", v2l_available=v2l_available,
        start_energy_kwh=None, capacity_kwh=10.0, min_soc=10.0, target_cap=90.0,
        voltage_v=50.0, charge_efficiency=0.8, max_current_a=100.0,
        battery_discharge_ceiling_kw=10.0, import_rate=import_rate, export_rate=0.15,
        event_rate=event_rate, forecast_model="median", config_generation=1,
    )


def measured(index: int, record: SlotRecord, *, load: float, solar: float = 0.0,
             ev: float = 0.0, v2l: float = 0.0, import_kwh: float = 0.0,
             export_kwh: float = 0.0) -> MeasuredSlot:
    kwh = {"grid_import": import_kwh, "grid_export": export_kwh,
           "solar_generation": solar, "battery_charge": 0.0, "battery_discharge": 0.0,
           "ev_charge": ev, "v2l": v2l}
    return MeasuredSlot(
        slot_start=record.slot_start, intent=record,
        components={name: Measurement(kwh=value, provenance="measured")
                    for name, value in kwh.items()},
        load_kwh=load, negative_clamped=False,
    )


def day_from_blocks(blocks: list[tuple[Any, ...]], *, reasons: tuple[str, ...] = (),
                    start: float | None = 3.4, end: float | None = 3.4,
                    cheap: float | None = 0.07) -> DayRecord:
    slots = []
    index = 0
    for count, kwargs, load, solar, ev, v2l, import_kwh, export_kwh, rate, event in blocks:
        for _ in range(count):
            record = intent(index, import_rate=rate, event_rate=event,
                            v2l_available=v2l > 0, **kwargs)
            slots.append(measured(index, record, load=load, solar=solar, ev=ev, v2l=v2l,
                                  import_kwh=import_kwh, export_kwh=export_kwh))
            index += 1
    return DayRecord(local_date=MIDNIGHT.date(), timezone="Europe/London",
                     slots=tuple(slots), reasons=reasons, start_energy_kwh=start,
                     end_energy_kwh=end, next_cheap_import_rate=cheap)


def single(**kwargs: Any) -> DayRecord:
    """A one-slot day for the rules the owner decided beyond the fixture."""
    start = kwargs.pop("start", 3.4)
    kwargs.setdefault("end", start)  # a day that ends where it started
    block = (1, {"fraction": kwargs.pop("fraction", 0.0),
                    "current": kwargs.pop("current", None),
                    "export_kw": kwargs.pop("export_kw", 0.0),
                    "held": kwargs.pop("held", False)},
             kwargs.pop("load", 0.0), kwargs.pop("solar", 0.0), kwargs.pop("ev", 0.0),
             kwargs.pop("v2l", 0.0), kwargs.pop("imp", 0.0), kwargs.pop("exp", 0.0),
             kwargs.pop("rate", 0.30), kwargs.pop("event", None))
    return day_from_blocks([block], start=start, **kwargs)


def test_owner_fixture_day_replays_to_the_penny():
    result = replay_day(day_from_blocks(FIXTURE_BLOCKS))
    assert abs(result.replay_cost - 0.4235) < PENNY
    assert abs(result.incumbent_cost - 3.8300) < PENNY
    assert abs(result.delta - (-3.4065)) < PENNY
    assert abs(result.ending_valuation - 0.1225) < PENNY
    assert len(result.slots) == 48
    first = 0
    for (count, _, _, _, _, _, _, _, rate, event), (cost, energy) in zip(
        FIXTURE_BLOCKS, BLOCK_RESULTS, strict=True
    ):
        block = result.slots[first:first + count]
        earned = event if event is not None else 0.15
        replayed = (sum(s.import_kwh for s in block) * rate
                    - sum(s.export_kwh for s in block) * earned)
        # Per-block costs and energies, so a compensating error cannot pass.
        assert abs(replayed - cost) < PENNY
        assert block[-1].energy_after_kwh == pytest.approx(energy, abs=1e-9)
        first += count


@pytest.mark.parametrize("problem, message", [
    ("excluded", "excluded day"),
    ("hold_unknown", "hold intent unknown"),
    ("window_current", "charge current unknown"),
    ("missing_intent", "slot record missing"),
    ("start", "actual start energy missing"),
    ("end", "actual end energy missing"),
    ("cheap", "next cheap-slot import rate missing"),
])
def test_excluded_or_unevidenced_days_are_refused_never_replayed(problem, message):
    day = day_from_blocks(FIXTURE_BLOCKS)
    slots = list(day.slots)
    if problem == "excluded":
        day = day.model_copy(update={"reasons": ("grid_import: substituted measurement gap",)})
    elif problem == "hold_unknown":
        record = slots[16].intent.model_copy(update={"hold_state": "unknown"})
        slots[16] = slots[16].model_copy(update={"intent": record})
        day = day.model_copy(update={"slots": tuple(slots)})
    elif problem == "window_current":
        record = slots[0].intent.model_copy(update={"charge_current_a": None})
        slots[0] = slots[0].model_copy(update={"intent": record})
        day = day.model_copy(update={"slots": tuple(slots)})
    elif problem == "missing_intent":
        slots[3] = slots[3].model_copy(update={"intent": None})
        day = day.model_copy(update={"slots": tuple(slots)})
    elif problem == "start":
        day = day.model_copy(update={"start_energy_kwh": None})
    elif problem == "end":
        day = day.model_copy(update={"end_energy_kwh": None})
    else:
        day = day.model_copy(update={"next_cheap_import_rate": None})
    with pytest.raises(ValueError, match=message):
        replay_day(day)


def test_partial_window_slot_splits_the_half_hour():
    # Half the slot charges 40 A (0.5 kWh stored, 0.625 kWh bought) with half
    # the 0.6 load imported; the other half self-serves from the battery.
    day = single(fraction=0.5, current=40.0, load=0.6, start=3.4, end=4.0, imp=1.0)
    result = replay_day(day)
    (slot,) = result.slots
    assert slot.import_kwh == pytest.approx(0.925)
    assert slot.export_kwh == pytest.approx(0.0)
    assert slot.energy_after_kwh == pytest.approx(3.6)
    # 0.925 kWh at £0.30, plus (4.0 - 3.6) × 0.07 / 0.8 bought back.
    assert result.ending_valuation == pytest.approx(0.035)
    assert result.replay_cost == pytest.approx(0.3125)
    assert result.incumbent_cost == pytest.approx(0.30)
    assert result.delta == pytest.approx(0.0125)


def test_a_held_battery_never_discharges_or_exports():
    # Hold outranks a recorded export intent: no forced export, no discharge.
    day = single(held=True, export_kw=3.0, load=0.6, start=5.0)
    result = replay_day(day)
    (slot,) = result.slots
    assert slot.import_kwh == pytest.approx(0.6)
    assert slot.export_kwh == pytest.approx(0.0)
    assert slot.energy_after_kwh == pytest.approx(5.0)
    assert result.replay_cost == pytest.approx(0.18)


@pytest.mark.parametrize("start, imported, exported, after", [
    (1.4, 0.1, 0.0, 1.0),  # shortfall kills the whole export, 0.1 kWh of net imported
    (2.0, 0.0, 0.5, 1.0),  # shortfall trims the export to 0.5 kWh
    (1.3, 0.2, 0.0, 1.0),
])
def test_export_shortfall_reduces_the_forced_export_before_importing(start, imported,
                                                                      exported, after):
    # net 0.5 plus 1.5 kWh forced export; the battery stops at the 1.0 floor.
    day = single(export_kw=3.0, load=0.9, solar=0.4, start=start)
    (slot,) = replay_day(day).slots
    assert slot.import_kwh == pytest.approx(imported)
    assert slot.export_kwh == pytest.approx(exported)
    assert slot.energy_after_kwh == pytest.approx(after)


def test_solar_surplus_in_an_export_slot_charges_then_exports_more():
    # net -1.9 with 1.5 kWh forced export: the 0.4 kWh surplus charges the battery.
    day = single(export_kw=3.0, load=0.1, solar=2.0, start=5.0)
    (slot,) = replay_day(day).slots
    assert slot.import_kwh == pytest.approx(0.0)
    assert slot.export_kwh == pytest.approx(1.5)
    assert slot.energy_after_kwh == pytest.approx(5.4)


def test_an_export_event_earns_its_rate_on_both_sides():
    # The incumbent's export in the event slot earns £1.00, not £0.15.
    day = single(load=0.4, imp=0.4, exp=2.0, event=1.00, start=5.0, end=4.6)
    result = replay_day(day)
    (slot,) = result.slots
    assert slot.import_kwh == pytest.approx(0.0)  # the battery self-serves
    assert result.incumbent_cost == pytest.approx(0.4 * 0.30 - 2.0 * 1.00)
    assert result.replay_cost == pytest.approx(0.0)
    assert result.delta == pytest.approx(1.88)


def test_a_non_positive_charge_efficiency_is_refused():
    day = single(load=0.5)
    slot = day.slots[0]
    bad = slot.model_copy(update={
        "intent": slot.intent.model_copy(update={"charge_efficiency": 0.0}),
    })
    with pytest.raises(ValueError, match="charge efficiency"):
        replay_day(day.model_copy(update={"slots": (bad,)}))
