"""Pure ingress timeline serialisation of the daemon's stored plan run.

No planning, clock reads or input gathering belongs here. All displayed plan
numbers and wording come from the planner view; only timestamps are formatted.
"""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from ha_spark.energy.plan_run import StoredPlanRun
from ha_spark.energy.plan_view import ev_hold_line


def timeline_payload(record: StoredPlanRun | None, operating_mode: str) -> dict[str, Any] | None:
    """Return no timeline when a legacy/bare plan has no planner view."""
    if record is None or record.run.plan.view is None:
        return None
    run = record.run
    view = run.plan.view
    assert view is not None

    def instant(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).astimezone(run.tz or UTC).isoformat()

    lines = view.lines + ((ev_hold_line(run.inputs.now),) if record.ev_hold_active else ())
    geometry = run.inputs.geometry
    horizon_start = geometry.horizon_start if geometry is not None else run.inputs.horizon_start
    return {
        "model": run.plan.model,
        "operating_mode": operating_mode,
        "strategy": run.plan.strategy,
        "headline": asdict(view.headline),
        "costs": asdict(view.costs),
        "lines": [dict(asdict(line), start=instant(line.start), end=instant(line.end))
                  for line in lines],
        "horizon_start": instant(horizon_start),
        "slot_minutes": 30,
        "now": instant(run.inputs.now),
        "slots": [dict(asdict(slot), start=instant(slot.start)) for slot in view.slots]
                 if view.slots is not None else None,
        "soc": [{"at": instant(point.at), "soc_pct": point.soc_pct} for point in view.soc]
               if view.soc is not None else None,
    }
