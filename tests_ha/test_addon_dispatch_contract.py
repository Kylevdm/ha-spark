"""Validate the v7 Kraken schedule independently of the rest of the summary."""

from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.ha_spark.native_hold import dispatch_corroborated
from custom_components.ha_spark.wire import parse_summary
from tests_ha.test_ownership import peer_body
from tests_ha.test_polling import fixture


def test_addon_dispatches_parsed_at_v7_only() -> None:
    block = fixture("addon_dispatches")
    raw = peer_body(version=7) | {"addon_dispatches": block}
    summary = parse_summary(raw)
    assert summary.addon_dispatches is not None
    assert summary.addon_dispatches.fetched_at == datetime.fromisoformat(block["fetched_at"])
    assert summary.addon_dispatches.windows == tuple(
        (datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]))
        for w in block["windows"])
    assert parse_summary(raw | {"api_version": 6}).addon_dispatches is None


@pytest.mark.parametrize("bad", [None, [], {}, {"fetched_at": 0, "windows": []},
    {"fetched_at": "2026-11-02T11:59:00", "windows": []},
    {"fetched_at": "0001-01-01T00:00:00+14:00", "windows": []},
    {"windows": {}}, {"windows": [False]}, {"windows": [{}]},
    {"windows": [{"start": 0, "end": 10}]},
    {"windows": [{"start": "2026-11-02T12:00:00", "end": "2026-11-02T14:30:00Z"}]},
    {"windows": [{"start": "2026-11-02T14:30:00Z", "end": "2026-11-02T14:30:00Z"}]},
    {"windows": [{"start": "2026-11-02T14:30:00Z", "end": "2026-11-02T12:00:00Z"}]},
    {"windows": [{"start": "2026-11-02T12:00:00Z", "end": "2026-11-03T00:00:01Z"}]},
    {"windows": fixture("addon_dispatches")["windows"] * 17},
])
def test_bad_dispatch_block_is_unknown_without_breaking_the_summary(bad: Any) -> None:
    raw = peer_body(version=7)
    result = parse_summary(raw | {"addon_dispatches": (
        fixture("addon_dispatches") | bad if isinstance(bad, dict) and bad else bad)})
    assert result.session_id == raw["session_id"]
    assert result.addon_dispatches is None


@pytest.mark.parametrize(("status", "plug", "car_kw", "active", "expected"), [
    (" charging ", None, None, True, True), ("Boosting", None, None, True, True),
    ("delivering", None, None, True, True), (None, None, 1.4, True, True),
    (None, "EV Connected", None, True, True), (None, "charging", None, True, True),
    (None, "Waiting for EV", None, True, True), (None, None, None, True, False),
    ("stopped", "EV Disconnected", 1.399, True, False),
    (None, None, float("inf"), True, False),
    ("charging", "EV Connected", 7, False, False),
])
def test_addon_dispatch_needs_active_window_and_positive_car_evidence(
    status: str | None, plug: str | None, car_kw: float | None, active: bool, expected: bool,
) -> None:
    block = fixture("addon_dispatches")["windows"][0]
    start, end = (datetime.fromisoformat(block[key]) for key in ("start", "end"))
    now = start + timedelta(minutes=1) if active else end
    assert dispatch_corroborated(start, end, now, status=status, plug=plug,
                                 car_kw=car_kw) is expected
