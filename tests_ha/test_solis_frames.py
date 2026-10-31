"""Register decoding and the site-wide polling budget, without HA I/O."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ha_spark.solis_frames import FRAMES, POINTS, Schedule, decode


def test_energy_word_order_scaling_and_signed_power() -> None:
    def value(address: int, words: tuple[int, ...]) -> object:
        point = next(p for p in POINTS if p.address == address)
        return decode(point, words, ZoneInfo("UTC"))

    assert value(33126, (1, 2)) == 65.538
    assert value(33029, (1, 2)) == 65538
    assert value(33130, (65535, 65526)) == -10
    assert value(33134, (65526,)) == -1
    assert value(33035, (123,)) == 12.3


def test_battery_direction_bits_clock_and_bad_words() -> None:
    signed = next(p for p in POINTS if p.key == "input_33149_signed")
    words = [0] * 21
    words[3], words[17], words[18] = 1, 0, 123
    assert decode(signed, tuple(words), None, frame=True) == -123
    words[3] = 0
    assert decode(signed, tuple(words), None, frame=True) == 123
    words[3] = 2
    assert decode(signed, tuple(words), None, frame=True) is None
    bit = next(p for p in POINTS if p.key == "input_33132_b5")
    assert decode(bit, (32,), None) is True
    clock = next(p for p in POINTS if p.address == 33022)
    zone = ZoneInfo("Europe/London")
    assert decode(clock, (26, 7, 1, 12, 30, 0), zone) == datetime(2026, 7, 1, 12, 30, tzinfo=zone)
    assert decode(clock, (26, 2, 30, 0, 0, 0), zone) is None
    assert decode(clock, (26, 7, 1, 12, 30, 0), None) is None
    assert decode(bit, (True,), None) is None
    assert decode(signed, (), None) is None


@pytest.mark.parametrize(("address", "count", "expected"), [
    (33029, 2, 65538), (33031, 2, 65538), (33033, 2, 65538),
    (33035, 1, 0.1), (33036, 1, 0.1), (33037, 2, 65538), (33039, 2, 65538),
    (33126, 2, 65.538),
    (33161, 2, 65538), (33163, 1, 0.1), (33164, 1, 0.1),
    (33165, 2, 65538), (33167, 1, 0.1), (33168, 1, 0.1),
    (33169, 2, 65538), (33171, 1, 0.1), (33172, 1, 0.1),
    (33173, 2, 65538), (33175, 1, 0.1), (33176, 1, 0.1),
    (33177, 2, 65538), (33179, 1, 0.1), (33180, 1, 0.1),
    (33186, 2, 65538), (33188, 2, 65538),
])
def test_energy_tier_table(address: int, count: int, expected: float) -> None:
    point = next(p for p in POINTS if p.address == address)
    assert point.count == count
    assert decode(point, (1, 2)[:count], None) == expected
    assert (point.unit, point.device_class, point.state_class) == (
        "kWh", "energy", "total_increasing")


def test_schedule_round_robin_and_one_request_per_second() -> None:
    schedule = Schedule(("a", "b"))
    selected = []
    for tick in range(100):
        now = tick / 10
        item = schedule.next(now)
        if item:
            selected.append((now, item))
    assert len(selected) == 10
    assert all(b[0] - a[0] >= 1 for a, b in zip(selected, selected[1:], strict=False))
    assert [item[0] for _, item in selected[:4]] == ["a", "b", "a", "b"]
    assert len({item for _, item in selected}) == 10
    assert len(FRAMES) == 9


def test_frame_table_is_pinned_and_never_touches_the_serial_block() -> None:
    assert {(f.bank, f.address, f.count, f.interval) for f in FRAMES} == {
        ("input", 33132, 21, 10), ("input", 33022, 6, 30), ("holding", 43141, 30, 30),
        ("holding", 43007, 1, 30), ("input", 33161, 29, 30), ("input", 33029, 12, 30),
        ("input", 33049, 10, 10), ("input", 33126, 6, 10), ("holding", 43073, 2, 30),
    }
    serial = set(range(33000, 33020))
    assert all(not serial & set(range(f.address, f.address + f.count)) for f in FRAMES)


def test_percent_out_of_range_and_export_limit() -> None:
    soc = next(p for p in POINTS if p.address == 33139)
    assert decode(soc, (100,), None) == 100
    assert decode(soc, (101,), None) is None
    assert decode(soc, (65535,), None) is None
    enabled = next(p for p in POINTS if p.key == "holding_43073_b4")
    limit = next(p for p in POINTS if p.key == "holding_43074")
    assert decode(enabled, (16, 37), None, frame=True) is True
    assert decode(enabled, (0, 37), None, frame=True) is False
    assert decode(limit, (16, 37), None, frame=True) == 3700
