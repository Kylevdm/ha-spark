"""`get_statistics` agent tool: read-only, same figures as ``stats`` and ``/api/history``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from ha_spark import cli
from ha_spark.agent.mcp_server import build_mcp
from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings
from ha_spark.energy.axle_events import AxleEvent
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot
from ha_spark.energy.readiness_measurements import COMPONENTS
from tests.api_helpers import owner_client
from tests.authority_helpers import seed_settings
from tests.test_api_history import _frozen_server_clock
from tests.test_stats_ranges import LONDON, priced_row

CANARY = "SECRET-SWEEP-617-DO-NOT-ECHO"


def _state(tmp_path: Path, **settings_kw: object) -> AppState:
    settings_kw.setdefault("db_path", str(tmp_path / "db.sqlite"))
    settings_kw.setdefault("agent_surface", "on")
    settings_kw.setdefault("agent_exposure", "read")
    settings_kw.setdefault("ha_token", "t")
    service = seed_settings(
        tmp_path, settings=Settings(ha_url="http://ha.test", **settings_kw)
    )
    return AppState(service.settings, tmp_path / "options.json", settings_service=service)


def _export_row(start: datetime, exported: float) -> LedgerSlot:
    base = priced_row(start, 0)
    components = {name: ComponentFlow(kwh=0, provenance="boundary") for name in COMPONENTS}
    components["grid_export"] = ComponentFlow(kwh=exported, provenance="boundary")
    return base.model_copy(update={"components": components})


def _patch_ledger_and_axle(
    monkeypatch: pytest.MonkeyPatch,
    slot: datetime,
    exported: tuple[float, float],
    event: AxleEvent | None = None,
) -> list[tuple[str, datetime, datetime]]:
    calls: list[tuple[str, datetime, datetime]] = []
    rows = [_export_row(slot, exported[0]), _export_row(slot + timedelta(minutes=30), exported[1])]

    async def slots(self, start, end):
        calls.append(("slots", start, end))
        return rows

    async def evidence(db_path, start, end):
        calls.append(("evidence", start, end))
        if event is None:
            return [], []
        return [event], []

    monkeypatch.setattr(EnergyLedger, "slots", slots)
    monkeypatch.setattr("ha_spark.agent.tools.window_evidence", evidence)
    monkeypatch.setattr("ha_spark.api.server.window_evidence", evidence)
    monkeypatch.setattr("ha_spark.cli.window_evidence", evidence)
    return calls


def test_agent_statistics_matches_history_and_cli_for_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    frozen = datetime(2026, 9, 8, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    state = _state(tmp_path, timezone=LONDON.key)
    slot = datetime(2026, 9, 1, 9, tzinfo=LONDON).astimezone(UTC)
    event = AxleEvent.model_validate({
        "start": slot, "end": slot + timedelta(hours=1),
        "accepted_at": slot - timedelta(hours=1),
        "committed_kwh": 3, "rate_gbp_kwh": 1.5,
    })
    calls = _patch_ledger_and_axle(monkeypatch, slot, (2, 1), event=event)
    monkeypatch.setattr(cli, "load_settings", lambda **_: state.settings)

    with owner_client(state) as client:
        history = client.get("/api/history", params={
            "resolution": "day",
            "from": "2026-08-30T23:00:00+00:00",
            "to": "2026-09-06T23:00:00+00:00",
        })
    assert history.status_code == 200
    history_totals = history.json()["range_totals"]

    with owner_client(state) as client:
        agent = client.get("/agent/statistics", params={"range": "week", "at": "2026-09-01"})
    assert agent.status_code == 200
    agent_body = agent.json()
    assert agent_body["range"] == "week"
    assert agent_body["at"] == "2026-09-01"
    assert agent_body["resolution"] == "day"
    assert agent_body["timezone"] == LONDON.key
    assert agent_body["range_totals"] == history_totals
    assert agent_body["axle"]["events"][0]["outcome"] == "delivered"
    assert agent_body["axle"]["events"][0]["earnings_gbp"] == 4.5
    assert agent_body["axle"]["delivery_rate"] == 1.0
    week_start = datetime(2026, 8, 30, 23, tzinfo=UTC)
    week_end = datetime(2026, 9, 6, 23, tzinfo=UTC)
    assert agent_body["start"] == week_start.isoformat()
    assert agent_body["end"] == week_end.isoformat()
    assert calls[-2:] == [("slots", week_start - timedelta(minutes=30), week_end),
                          ("evidence", week_start, week_end)]

    assert cli.main(["stats", "--range", "week", "--at", "2026-09-01"]) == 0
    out = capsys.readouterr().out
    totals = history_totals
    assert f'Actual cost={totals["actual_cost"]:.2f} GBP' in out
    assert f'No-battery cost={totals["no_battery_cost"]:.2f} GBP' in out
    assert f'Battery savings={totals["battery_savings"]:.2f} GBP' in out
    assert f'Coverage: {totals["covered_slots"]}/{totals["total_slots"]} slots' in out
    assert "Axle delivery rate" in out
    assert "Axle earnings" in out


async def test_mcp_get_statistics_lists_at_read_and_above(tmp_path: Path) -> None:
    for exposure in ("read", "read_act", "read_write"):
        mcp = build_mcp(_state(tmp_path, agent_exposure=exposure))
        assert "get_statistics" in {t.name for t in await mcp.list_tools()}


async def test_mcp_get_statistics_absent_when_surface_off(tmp_path: Path) -> None:
    mcp = build_mcp(_state(tmp_path, agent_exposure="read_write", agent_surface="off"))
    assert "get_statistics" not in {t.name for t in await mcp.list_tools()}
    with pytest.raises(ToolError):
        await mcp.call_tool("get_statistics", {"range": "week"})


@pytest.mark.parametrize(("params", "text"), [
    ({"range": "bad"}, "invalid statistics range"),
    ({"range": "week", "at": "not-a-date"}, "at must be a valid YYYY-MM-DD date"),
    ({"range": "week", "at": "2026-02-29"}, "at must be a valid YYYY-MM-DD date"),
    ({"range": {"x": CANARY}}, "invalid statistics range"),
    ({"range": "week", "at": [CANARY]}, "at must be a valid YYYY-MM-DD date"),
])
async def test_mcp_get_statistics_rejects_invalid_input_without_echo(
    tmp_path: Path, params: dict[str, object], text: str,
) -> None:
    mcp = build_mcp(_state(tmp_path, agent_exposure="read"))
    with pytest.raises(ToolError) as excinfo:
        await mcp.call_tool("get_statistics", params)
    assert text in str(excinfo.value)
    assert CANARY not in str(excinfo.value)
    for value in params.values():
        if isinstance(value, str) and value != "week":
            assert value not in str(excinfo.value)


def test_route_get_statistics_200_and_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 9, 8, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    state = _state(tmp_path, timezone=LONDON.key)
    slot = datetime(2026, 9, 1, 9, tzinfo=LONDON).astimezone(UTC)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[priced_row(slot, 2)]))

    with owner_client(state) as client:
        ok = client.get("/agent/statistics", params={"range": "week", "at": "2026-09-01"})
    assert ok.status_code == 200
    assert ok.json()["range_totals"]["components_kwh"]["grid_import"] == 2

    with owner_client(state) as client:
        bad_range = client.get("/agent/statistics", params={"range": "bad"})
    assert bad_range.status_code == 400
    assert bad_range.json()["detail"] == "invalid statistics range"

    with owner_client(state) as client:
        bad_date = client.get("/agent/statistics", params={"range": "week", "at": "not-a-date"})
    assert bad_date.status_code == 400
    assert bad_date.json()["detail"] == "at must be a valid YYYY-MM-DD date"
    assert "not-a-date" not in bad_date.text


def test_route_get_statistics_404_when_surface_off(tmp_path: Path) -> None:
    state = _state(tmp_path, agent_surface="off")
    with TestClient(build_app(state)) as client:
        assert client.get("/agent/statistics", params={"range": "week"}).status_code == 404


async def test_statistics_response_contains_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 9, 8, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    state = _state(
        tmp_path,
        timezone=LONDON.key,
        octopus_api_key=CANARY,
        agent_api_token=CANARY,
        ha_token=CANARY,
    )
    slot = datetime(2026, 9, 1, 9, tzinfo=LONDON).astimezone(UTC)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[priced_row(slot, 2)]))

    with owner_client(state) as client:
        route = client.get("/agent/statistics", params={"range": "week", "at": "2026-09-01"})
    assert route.status_code == 200
    assert CANARY not in route.text

    mcp = build_mcp(state)
    _content, payload = await mcp.call_tool("get_statistics", {"range": "week", "at": "2026-09-01"})
    assert CANARY not in str(payload)


def test_route_fails_closed_on_unreadable_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    frozen = datetime(2026, 9, 8, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    state = _state(tmp_path, timezone=LONDON.key)

    async def unreadable(*args):
        raise OSError("private-path")

    monkeypatch.setattr(EnergyLedger, "slots", unreadable)
    caplog.set_level("WARNING")
    with owner_client(state) as client:
        resp = client.get("/agent/statistics", params={"range": "week", "at": "2026-09-01"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "statistics unavailable"
    assert "private-path" not in resp.text
    assert any("Statistics read failed" in rec.message for rec in caplog.records)


async def test_mcp_fails_closed_on_unreadable_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    frozen = datetime(2026, 9, 8, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    state = _state(tmp_path, timezone=LONDON.key)

    async def unreadable(*args):
        raise OSError("private-path")

    monkeypatch.setattr(EnergyLedger, "slots", unreadable)
    caplog.set_level("WARNING")
    mcp = build_mcp(state)
    with pytest.raises(ToolError) as excinfo:
        await mcp.call_tool("get_statistics", {"range": "week", "at": "2026-09-01"})
    assert "statistics unavailable" in str(excinfo.value)
    assert "private-path" not in str(excinfo.value)
    assert any("Statistics read failed" in rec.message for rec in caplog.records)


TOP_LEVEL_KEYS = {"version", "range", "at", "start", "end", "resolution", "timezone", "buckets",
                  "range_totals", "axle"}


def test_output_contract_is_version_1_and_matches_history_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phases 13 and 14 build on this shape: changing it needs a new version."""
    frozen = datetime(2026, 9, 8, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    state = _state(tmp_path, timezone=LONDON.key)
    slot = datetime(2026, 9, 1, 9, tzinfo=LONDON).astimezone(UTC)
    event = AxleEvent.model_validate({
        "start": slot, "end": slot + timedelta(hours=1),
        "accepted_at": slot - timedelta(hours=1),
        "committed_kwh": 3, "rate_gbp_kwh": 1.5,
    })
    _patch_ledger_and_axle(monkeypatch, slot, (2, 1), event=event)

    with owner_client(state) as client:
        day = client.get("/agent/statistics", params={"range": "day", "at": "2026-09-01"})
        history = client.get("/api/history", params={
            "resolution": "slot",
            "from": "2026-08-31T23:00:00+00:00",
            "to": "2026-09-01T23:00:00+00:00",
        })
        week = client.get("/agent/statistics", params={"range": "week", "at": "2026-09-01"})
    assert day.status_code == history.status_code == week.status_code == 200
    body, hist = day.json(), history.json()
    assert set(body) == TOP_LEVEL_KEYS
    assert body["version"] == 1
    assert body["resolution"] == "slot"
    assert body["range_totals"] == hist["range_totals"]
    assert set(body["range_totals"]) == set(hist["range_totals"])
    assert set(body["axle"]) == {"delivery_rate", "earnings_by_day", "events"}
    assert set(body["axle"]["events"][0]) == {
        "start", "end", "outcome", "delivered_kwh", "earnings_gbp"}
    assert body["axle"]["earnings_by_day"] == [{"date": "2026-09-01", "earnings_gbp": 4.5}]
    assert len(body["buckets"]) == len(hist["buckets"]) == 48
    shared = ("flows", "negative_clamped", "cost_inputs")
    for ours, theirs in zip(body["buckets"], hist["buckets"], strict=True):
        assert set(ours) - {"cost_reason"} == (
            set(theirs) & (set(hist["range_totals"]) | {"start", *shared}))
        for key in shared:
            assert ours.get(key) == theirs.get(key)
    priced = [b for b in body["buckets"] if "cost_inputs" in b]
    assert priced and set(priced[0]["cost_inputs"]) == set(
        next(b for b in hist["buckets"] if "cost_inputs" in b)["cost_inputs"])
    week_body = week.json()
    assert set(week_body) == TOP_LEVEL_KEYS
    assert week_body["version"] == 1
    assert len(week_body["buckets"]) == 7
    assert set(week_body["buckets"][0]) == {"start", *hist["range_totals"]}


async def test_mcp_fails_closed_on_unreadable_axle_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state(tmp_path, timezone=LONDON.key)
    slot = datetime(2026, 9, 1, 9, tzinfo=LONDON).astimezone(UTC)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[priced_row(slot, 2)]))

    async def unreadable(*args: object) -> None:
        raise OSError("private-path")

    monkeypatch.setattr("ha_spark.agent.tools.window_evidence", unreadable)
    caplog.set_level("WARNING")
    mcp = build_mcp(state)
    with pytest.raises(ToolError) as excinfo:
        await mcp.call_tool("get_statistics", {"range": "week", "at": "2026-09-01"})
    assert str(excinfo.value).endswith(": statistics unavailable")
    assert "private-path" not in caplog.text


@pytest.mark.parametrize("failing", ["first_slot", "summarise"])
def test_any_later_failure_fails_closed_without_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    failing: str,
) -> None:
    state = _state(tmp_path, timezone=LONDON.key)

    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("private-path")

    async def aboom(*args: object) -> None:
        boom()

    if failing == "first_slot":
        monkeypatch.setattr(EnergyLedger, "first_slot", aboom)
    else:
        monkeypatch.setattr("ha_spark.agent.tools.summarise", boom)
    caplog.set_level("WARNING")
    with owner_client(state) as client:
        resp = client.get("/agent/statistics", params={"range": "all"})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "statistics unavailable"}
    assert "private-path" not in caplog.text


def test_all_range_ignores_at_except_for_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like ``stats --range all``: first ledger slot to now, whatever ``at`` says."""
    frozen = datetime(2026, 9, 8, 12, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    monkeypatch.setattr("ha_spark.agent.tools.datetime", type(
        "_Clock", (datetime,), {"now": classmethod(lambda cls, tz=None: frozen.astimezone(tz))}))
    state = _state(tmp_path, timezone=LONDON.key)
    slot = datetime(2026, 9, 1, 9, tzinfo=LONDON).astimezone(UTC)
    _patch_ledger_and_axle(monkeypatch, slot, (2, 1))
    monkeypatch.setattr(EnergyLedger, "first_slot", AsyncMock(return_value=slot))
    with owner_client(state) as client:
        resp = client.get("/agent/statistics", params={"range": "all", "at": "2026-01-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["start"] == datetime(2026, 9, 1, tzinfo=LONDON).astimezone(UTC).isoformat()
    assert body["end"] == frozen.astimezone(UTC).isoformat()
    assert body["resolution"] == "month"
