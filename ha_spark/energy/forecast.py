"""ha-spark's own home-load forecast — owns the pipeline, retiring HA templates.

`predict_home_load` is the seam consumed by the planner. Forecast chain:

0. Weather-aware ML quantile model (Phase 6B, optional): gradient boosting
   over the same hourly history plus Open-Meteo temperatures and recorded
   signals. Gated by `load_model` — "auto" uses it only once the forecast
   ledger shows it beating the median over the trailing 14 days.
1. v2 slot profile — medians per local half-hour slot, built from hourly HA
   long-term statistics of the house-consumption sensor (true load: excludes
   battery charging and EV). Gives a 48-slot forecast.
2. v1 daily median of recent house-consumption totals from HA long-term statistics.
3. The configured baseline (`expected_load_kwh`).

Whenever the ML model runs, both its forecast and the median forecast are
shadow-recorded in the ledger regardless of which one drives the plan, so
`forecast-eval` accumulates a fair comparison from day one.

The Octopus consumption store deliberately does NOT feed this model: Octopus
meter data is grid *import*, which the battery/solar have shaped for the whole
history — training on it teaches the planner "what the battery already did".
The store is kept for cost backtesting instead.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from ha_spark.config import Settings
from ha_spark.energy import habits, ml, weather
from ha_spark.energy.context import ContextStore, combined_factor
from ha_spark.energy.eval import evaluate
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ConsumptionInterval, LoadForecast
from ha_spark.energy.profile import build_slot_profile, predict_day_slots
from ha_spark.ha.statistics import statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)

# `auto` needs at least this many scored ML days before trusting the model.
_AUTO_MIN_DAYS = 7
_AUTO_EVAL_DAYS = 14


def forecast_model_tag(source: str) -> str:
    """Short ledger model tag derived from ``LoadForecast.source``."""
    if source.startswith("ml"):
        return "ml"
    if source.startswith("slot profile"):
        return "slots"
    if source.startswith("median of"):
        return "median"
    return "baseline"


def load_timezone(name: str) -> ZoneInfo:
    """The configured local tz; UTC if tzdata is missing (degraded but functional)."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - e.g. no tzdata in a minimal container
        log.warning("Timezone %r unavailable; falling back to UTC", name)
        return ZoneInfo("UTC")


def intervals_from_hourly_stats(rows: list[dict[str, Any]]) -> list[ConsumptionInterval]:
    """Split hourly stats rows into half-hour intervals (kWh halved, assumed uniform)."""
    intervals: list[ConsumptionInterval] = []
    for row in rows:
        change = row.get("change")
        raw_start = row.get("start")
        if change is None or raw_start is None or float(change) < 0:
            continue
        start = datetime.fromtimestamp(float(raw_start) / 1000, UTC)
        half_kwh = float(change) / 2
        for i in range(2):
            slot_start = start + timedelta(minutes=30 * i)
            intervals.append(
                ConsumptionInterval(
                    start=slot_start, end=slot_start + timedelta(minutes=30), kwh=half_kwh
                )
            )
    return intervals


async def predict_home_load(
    settings: Settings, *, lat: float | None = None, lon: float | None = None
) -> LoadForecast:
    """Predict tomorrow's home load, per-slot when enough local history exists.

    ``lat``/``lon`` enable the weather-aware ML model (Phase 6B); when None the
    chain starts at the median slot profile as before.
    """
    tz = load_timezone(settings.timezone)
    intervals: list[ConsumptionInterval] = []
    median_forecast: LoadForecast | None = None
    try:
        start = datetime.now(UTC) - timedelta(days=settings.profile_history_days)
        rows = await statistics_during_period(
            settings.ha_websocket_url,
            settings.auth_token,
            settings.consumption_energy_entity,
            start,
            period="hour",
            timeout=settings.ha_timeout,
        )
        intervals = intervals_from_hourly_stats(rows)
        profile = build_slot_profile(intervals, tz, min_days=settings.profile_min_days)
        if profile is not None:
            tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
            slots = predict_day_slots(profile, tomorrow)
            median_forecast = LoadForecast(
                total_kwh=sum(slots),
                slots=slots,
                source=f"slot profile ({profile.days_used}d hourly house stats)",
            )
    except Exception as exc:  # noqa: BLE001 - the forecast must never crash the plan
        log.warning("Slot-profile forecast unavailable (%s); falling back", exc)
    if median_forecast is None:
        total, source = await predict_home_load_kwh(settings)
        median_forecast = LoadForecast(total_kwh=total, slots=None, source=source)

    # A dated context fact (away/guests/...) scales both candidate forecasts by
    # the same factor, so the ledger comparison and the P90/P50 buffer ratio are
    # unaffected; the report surfaces it via the forecast source. The away
    # factor is learned from past away periods (6E) when enough history exists.
    ctx_scale, ctx_lines = await _context_scale(settings, tz, intervals)
    median_forecast = _apply_context(median_forecast, ctx_scale, ctx_lines)
    if settings.load_model == "median":
        return median_forecast
    try:
        ml_forecast = await _ml_forecast(settings, intervals, tz, lat, lon)
    except Exception:  # noqa: BLE001 - the ML path must never crash the plan
        log.exception("ML forecast failed; using %s", median_forecast.source)
        return median_forecast
    if ml_forecast is None:
        return median_forecast
    ml_forecast = _apply_context(ml_forecast, ctx_scale, ctx_lines)
    await _record_shadow_forecasts(settings, tz, median_forecast, ml_forecast)
    if settings.load_model == "ml":
        return ml_forecast
    if await _ml_beats_median(settings, intervals, tz):
        return ml_forecast
    log.info(
        "load_model=auto: ML not yet beating the median over the trailing "
        "%dd; using %s", _AUTO_EVAL_DAYS, median_forecast.source,
    )
    return median_forecast


async def _context_scale(
    settings: Settings, tz: ZoneInfo, intervals: list[ConsumptionInterval]
) -> tuple[float, list[str]]:
    """Combined load multiplier (and report lines) for context active tomorrow."""
    target = (datetime.now(tz) + timedelta(days=1)).date()
    try:
        async with ContextStore(settings.db_path) as store:
            active = await store.active_on(target)
            override = None
            if any(e.kind == "away" for e in active):
                override = await _learned_away_factor(store, intervals, tz)
        return combined_factor(active, settings, away_factor_override=override)
    except Exception as exc:  # noqa: BLE001 - context is an optional adjustment
        log.warning("Context store unavailable (%s); ignoring context", exc)
        return 1.0, []


async def _learned_away_factor(
    store: ContextStore, intervals: list[ConsumptionInterval], tz: ZoneInfo
) -> float | None:
    """The away factor learned from past away periods, or None when too thin."""
    if not intervals:
        return None
    away_dates: set[date] = set()
    for e in await store.list_all():
        if e.kind != "away":
            continue
        d = e.start_date
        while d <= e.end_date:
            away_dates.add(d)
            d += timedelta(days=1)
    factor, n = habits.learn_away_factor(_daily_actuals(intervals, tz), away_dates)
    if factor is not None:
        log.info("Using learned away factor %.2f from %d past away days", factor, n)
    return factor


def _apply_context(
    forecast: LoadForecast, scale: float, lines: list[str]
) -> LoadForecast:
    """Scale a forecast by an active-context factor, noting it in the source."""
    if scale == 1.0 or not lines:
        return forecast
    note = "; ".join(lines)
    return LoadForecast(
        total_kwh=forecast.total_kwh * scale,
        slots=tuple(s * scale for s in forecast.slots) if forecast.slots else None,
        source=f"{forecast.source} ×{scale:.2f} [context: {note}]",
        p90_total_kwh=(
            forecast.p90_total_kwh * scale if forecast.p90_total_kwh is not None else None
        ),
    )


async def _ml_forecast(
    settings: Settings,
    intervals: list[ConsumptionInterval],
    tz: ZoneInfo,
    lat: float | None,
    lon: float | None,
) -> LoadForecast | None:
    """Run the quantile ML model; None whenever it cannot responsibly run."""
    if lat is None or lon is None or not intervals or not ml.ml_available():
        return None
    now = datetime.now(UTC)
    since = now - timedelta(days=settings.profile_history_days)
    temps: dict[datetime, float] = {}
    try:
        temps = await weather.hourly_temps(
            lat, lon, past_days=settings.profile_history_days
        )
    except Exception as exc:  # noqa: BLE001 - degrade to recorded signal temps
        log.warning("Open-Meteo unavailable (%s); using recorded temperatures", exc)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    occupancy: dict[date, float] = {}
    try:
        async with ForecastLedger(settings.db_path) as ledger:
            if temps:
                await ledger.record_signals(
                    (ts, "temp_out_c", v) for ts, v in temps.items() if ts <= now
                )
            else:
                temps = dict(await ledger.signal_history("temp_out_c", since))
            occ_samples = await ledger.signal_history("occupancy_home_frac", since)
        occupancy = _mean_by_local_date(occ_samples, tz)
        # Predict tomorrow's occupancy from the day-type pattern so the model
        # has a real value for the target day, not just the history mean (6E).
        predicted = habits.predict_occupancy(occ_samples, tomorrow, tz)
        if predicted is not None:
            occupancy[tomorrow] = predicted
    except Exception as exc:  # noqa: BLE001 - the ledger is an optional enrichment here
        log.warning("Signal ledger unavailable for ML features (%s)", exc)
    prediction = ml.train_and_predict(
        intervals, temps, occupancy, tomorrow, tz,
        min_days=max(14, settings.profile_min_days),
    )
    if prediction is None:
        return None
    return LoadForecast(
        total_kwh=sum(prediction.p50),
        slots=prediction.p50,
        source=f"ml quantile gbr ({prediction.days_used}d, weather-aware)",
        p90_total_kwh=sum(prediction.p90),
    )


def _mean_by_local_date(
    samples: list[tuple[datetime, float]], tz: ZoneInfo
) -> dict[date, float]:
    sums: dict[date, list[float]] = {}
    for ts, value in samples:
        sums.setdefault(ts.astimezone(tz).date(), []).append(value)
    return {d: sum(vs) / len(vs) for d, vs in sums.items()}


def _daily_actuals(intervals: list[ConsumptionInterval], tz: ZoneInfo) -> dict[date, float]:
    """Actual kWh per complete local day (today and sparse days excluded)."""
    by_day: dict[date, list[float]] = {}
    for iv in intervals:
        by_day.setdefault(iv.start.astimezone(tz).date(), []).append(iv.kwh)
    today = datetime.now(tz).date()
    return {d: sum(vs) for d, vs in by_day.items() if d < today and len(vs) >= 40}


async def _record_shadow_forecasts(
    settings: Settings, tz: ZoneInfo, *forecasts: LoadForecast
) -> None:
    """Record every candidate forecast so `forecast-eval` compares them fairly."""
    target = (datetime.now(tz) + timedelta(days=1)).date()
    try:
        async with ForecastLedger(settings.db_path) as ledger:
            for f in forecasts:
                await ledger.record_forecast(
                    datetime.now(UTC), target, forecast_model_tag(f.source),
                    f.total_kwh, f.slots, f.source,
                )
    except Exception:  # noqa: BLE001 - recording must never block the plan
        log.exception("Shadow forecast recording failed")


async def _ml_beats_median(
    settings: Settings, intervals: list[ConsumptionInterval], tz: ZoneInfo
) -> bool:
    """True when ledger history shows ML beating the median baseline on MAE."""
    try:
        since = (datetime.now(tz) - timedelta(days=_AUTO_EVAL_DAYS)).date()
        async with ForecastLedger(settings.db_path) as ledger:
            records = await ledger.forecasts_since(since)
        results = {r.model: r for r in evaluate(records, _daily_actuals(intervals, tz))}
        ml_eval = results.get("ml")
        baseline = results.get("slots") or results.get("median") or results.get("baseline")
        return (
            ml_eval is not None
            and baseline is not None
            and ml_eval.n >= _AUTO_MIN_DAYS
            and ml_eval.mae_kwh < baseline.mae_kwh
        )
    except Exception:  # noqa: BLE001 - gating must never crash the plan
        log.exception("ML-vs-median gate failed; staying on the median")
        return False


def _daily_totals(rows: list[dict[str, Any]]) -> list[float]:
    """Daily kWh from stats rows: prefer per-period ``change``, else diff ``sum``."""
    changes = [float(r["change"]) for r in rows if r.get("change") is not None]
    if changes:
        return [c for c in changes if c >= 0]
    cumulative = [float(r["sum"]) for r in rows if r.get("sum") is not None]
    if len(cumulative) >= 2:
        return [b - a for a, b in zip(cumulative, cumulative[1:], strict=False) if b - a >= 0]
    return []


async def predict_home_load_kwh(settings: Settings) -> tuple[float, str]:
    """Return (predicted tomorrow home-load kWh, human source description)."""
    start = datetime.now(UTC) - timedelta(days=settings.forecast_days + 1)
    try:
        rows = await statistics_during_period(
            settings.ha_websocket_url,
            settings.auth_token,
            settings.consumption_energy_entity,
            start,
            period="day",
            timeout=settings.ha_timeout,
        )
        totals = _daily_totals(rows)
        if totals:
            return float(median(totals)), f"median of {len(totals)}d house consumption (stats)"
        log.warning(
            "No usable statistics rows for %s; using baseline",
            settings.consumption_energy_entity,
        )
    except Exception as exc:  # noqa: BLE001 - the forecast must never crash the plan
        log.warning("Load statistics unavailable (%s); using baseline", exc)
    return settings.expected_load_kwh, "configured baseline (stats unavailable)"
