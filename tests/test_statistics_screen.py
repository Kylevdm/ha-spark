"""The Statistics screen's Axle earnings list, run under Node when it is installed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCREEN = Path(__file__).parents[1] / "ha_spark" / "api" / "static" / "statistics.js"
NODE = shutil.which("node")


def _shown(axle):
    script = (
        f"const {{shownEarnings}} = await import({json.dumps(SCREEN.as_uri())});"
        f"console.log(JSON.stringify(shownEarnings({json.dumps(axle)})));"
    )
    done = subprocess.run(  # Fixed arguments, no shell.
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(done.stdout)


def _event(outcome, earnings, start="2026-09-01T16:00:00+01:00", end="2026-09-01T17:00:00+01:00"):
    return {"start": start, "end": end, "outcome": outcome, "delivered_kwh": None,
            "earnings_gbp": earnings}


def _day(date, earnings):
    return {"date": date, "earnings_gbp": earnings}


pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")


def test_a_priced_event_that_earned_nothing_shows_zero():
    axle = {"delivery_rate": 0, "events": [_event("missed", 0)],
            "earnings_by_day": [_day("2026-09-01", 0), _day("2026-09-02", 0)]}
    assert _shown(axle) == [_day("2026-09-01", 0)]


def test_days_without_events_hide_the_line():
    axle = {"delivery_rate": None, "events": [_event("cancelled", 0)],
            "earnings_by_day": [_day("2026-09-01", 0)]}
    assert _shown(axle) == []


@pytest.mark.parametrize("outcome", ["unknown", "delivered"])
def test_an_event_without_an_estimate_stays_unknown(outcome):
    axle = {"delivery_rate": None, "events": [_event(outcome, None)],
            "earnings_by_day": [_day("2026-09-01", None)]}
    assert _shown(axle) == [_day("2026-09-01", None)]


def test_an_event_crossing_midnight_shows_both_days_and_a_midnight_end_neither_after():
    axle = {"delivery_rate": 0, "events": [
        _event("missed", 0, "2026-09-01T23:30:00+01:00", "2026-09-02T00:30:00+01:00"),
        _event("missed", 0, "2026-09-03T23:30:00+01:00", "2026-09-04T00:00:00+01:00"),
    ], "earnings_by_day": [_day(f"2026-09-0{n}", 0) for n in range(1, 6)]}
    assert [day["date"] for day in _shown(axle)] == ["2026-09-01", "2026-09-02", "2026-09-03"]
