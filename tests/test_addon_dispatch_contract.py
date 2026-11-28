"""The heartbeat projects only trusted Kraken windows from the current plan run."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ha_spark.api import server
from ha_spark.api.contract import plan_summary
from ha_spark.config import Settings
from ha_spark.energy.evidence_freshness import check_report_time
from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import DispatchSlot
from tests.test_integration_route import FIXTURES, SNAPSHOT_BODY, discovery, request, state
from tests.test_plan_timeline import stored_run

__all__ = ["discovery", "state"]

GOLDEN = json.loads((FIXTURES / "addon_dispatches.json").read_text())
NOW = datetime.fromisoformat(GOLDEN["windows"][0]["start"])
FETCHED = datetime.fromisoformat(GOLDEN["fetched_at"])
END = datetime.fromisoformat(GOLDEN["windows"][0]["end"])
WINDOWS = (DispatchSlot(NOW, END),)


def kraken_run(*, trusted=True, fetched=FETCHED, windows=WINDOWS):
    record = stored_run(slots=False)
    inputs = replace(record.run.inputs, now=NOW, unrated_dispatches=windows, dispatches=(),
                     dispatches_trusted=trusted, evidence_freshness={
                         "octopus_dispatch": check_report_time(
                             fetched, now=NOW, max_age_s=2700, option="octopus_dispatch"),
                     })
    return replace(record, run=replace(record.run, inputs=inputs))


def summary(*, version=7, trusted=True, fetched=FETCHED, provider="octopus_intelligent",
            has_run=True, windows=WINDOWS):
    record = kraken_run(trusted=trusted, fetched=fetched, windows=windows)
    return plan_summary(api_version=version, session_id="test", built_at=NOW, loop_tick=NOW,
                        settings=Settings(tariff_provider=provider),
                        plan_run=record.run if has_run else None)


async def test_heartbeat_uses_the_stored_kraken_plan_run(state, discovery, monkeypatch):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: NOW)
    state.settings = state.settings.model_copy(update={"tariff_provider": "octopus_intelligent"})
    record = kraken_run()
    state.set_plan(record.run.plan, record)
    snapshot = json.loads(SNAPSHOT_BODY) | {"api_version": 7, "sent_at": NOW.isoformat(),
                                           "authority_owner": "addon"}
    response = await request(state, discovery, content=json.dumps(snapshot).encode(), headers={
        "Authorization": f"Bearer {discovery.auth_token}",
    })
    assert response.status_code == 200
    assert response.json()["addon_dispatches"] == GOLDEN


def test_summary_carries_the_unrated_kraken_read_at_v7_only():
    assert summary()["addon_dispatches"] == GOLDEN
    assert "addon_dispatches" not in summary(version=6)
    assert summary(windows=())["addon_dispatches"] == {"fetched_at": GOLDEN["fetched_at"],
                                                        "windows": []}


@pytest.mark.parametrize("options", [dict(trusted=False), dict(fetched=None),
                                     dict(provider="fixed"), dict(has_run=False)])
def test_summary_marks_missing_or_untrusted_kraken_read_unknown(options):
    assert summary(**options)["addon_dispatches"] is None


def test_summary_clips_to_the_plan_horizon_and_keeps_the_earliest_16():
    cfg = stored_run(slots=False).run.cfg
    horizon = compute_horizon_geometry(NOW, cfg.window_start, cfg.window_end, NOW.tzinfo)
    crossing_start = DispatchSlot(NOW - timedelta(hours=2), END)
    crossing_end = DispatchSlot(horizon.horizon_end - timedelta(minutes=10),
                                horizon.horizon_end + timedelta(minutes=10))
    expired = DispatchSlot(NOW - timedelta(hours=2), NOW)
    assert summary(windows=(crossing_end, expired, crossing_start))["addon_dispatches"] == {
        "fetched_at": GOLDEN["fetched_at"], "windows": [GOLDEN["windows"][0], {
            "start": crossing_end.start.isoformat(), "end": horizon.horizon_end.isoformat(),
        }],
    }
    later = tuple(DispatchSlot(END + timedelta(minutes=n), END + timedelta(hours=1))
                  for n in range(17))
    sent = summary(windows=(*reversed(later), crossing_start))["addon_dispatches"]["windows"]
    assert len(sent) == 16
    assert sent[0] == GOLDEN["windows"][0]
    assert sent[-1]["start"] == later[14].start.isoformat()
