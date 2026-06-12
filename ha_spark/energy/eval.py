"""Forecast-vs-actual accuracy: the referee later ML phases must beat.

Joins forecasts recorded by the daemon (:mod:`ha_spark.energy.ledger`) against
actual daily consumption from HA long-term statistics, and reports MAE/MAPE
per recorded model. A model only gets to drive plans (``load_model: auto``,
Phase 6B) once it beats the ``median`` baseline here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ha_spark.energy.models import ForecastRecord


@dataclass(frozen=True)
class ModelEval:
    """Accuracy of one recorded model against matched actuals."""

    model: str
    n: int
    mae_kwh: float
    mape_pct: float


def actual_kwh_by_date(rows: list[dict[str, Any]]) -> dict[date, float]:
    """Map UTC date -> actual daily kWh from 'day'-period statistics rows."""
    out: dict[date, float] = {}
    for row in rows:
        raw_start = row.get("start")
        change = row.get("change")
        if raw_start is None or change is None or float(change) < 0:
            continue
        day = datetime.fromtimestamp(float(raw_start) / 1000, UTC).date()
        out[day] = float(change)
    return out


def evaluate(forecasts: list[ForecastRecord], actuals: dict[date, float]) -> list[ModelEval]:
    """Group forecasts by model and compute MAE/MAPE against matched actuals."""
    by_model: dict[str, list[tuple[float, float]]] = {}
    for f in forecasts:
        actual = actuals.get(f.target_date)
        if actual is None:
            continue
        by_model.setdefault(f.model, []).append((f.total_kwh, actual))

    results: list[ModelEval] = []
    for model, pairs in sorted(by_model.items()):
        n = len(pairs)
        mae = sum(abs(forecast - actual) for forecast, actual in pairs) / n
        ape_pairs = [(forecast, actual) for forecast, actual in pairs if actual]
        mape = (
            sum(abs(forecast - actual) / actual for forecast, actual in ape_pairs)
            / len(ape_pairs)
            * 100
            if ape_pairs
            else 0.0
        )
        results.append(ModelEval(model=model, n=n, mae_kwh=mae, mape_pct=mape))
    return results


def format_eval(results: list[ModelEval], days: int) -> str:
    """Render the per-model accuracy table."""
    if not results:
        return f"No recorded forecasts with matching actuals in the last {days} days."
    lines = [f"Forecast accuracy (last {days} days, vs actual daily consumption):"]
    for r in results:
        lines.append(
            f"  {r.model:<10} n={r.n:<3}  MAE {r.mae_kwh:5.2f} kWh   MAPE {r.mape_pct:5.1f}%"
        )
    return "\n".join(lines)
