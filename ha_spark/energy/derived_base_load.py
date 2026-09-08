"""Derived base-load history from HA component energy statistics.

ADR-0001: base load is what the house consumes with the plannable sources and
sinks stripped out (no battery charging, no EV charging). It is *derived* by
energy balance, never trusted from a single user-supplied sensor, because the
sensor cannot tell "the house is heavy" from "the battery is charging" — so
training on it teaches the planner to chase its own overnight setpoints.

Per hour, the formula is:

    base = grid_import - grid_export + solar_generation
           + battery_discharge - battery_charge - ev_charge

The function is pure over per-component hourly kWh series (already in
canonical direction). Grid import is the required component; the rest are
optional — an omitted component contributes zero and is reported as a
degradation note rather than failing the import. An hour without a grid
import row is skipped (any prior cumulative sum is preserved across the
gap). Negative per-hour results are clamped to zero and counted as a
sign-convention warning (per-component invert flags live in Settings, they
must be explicit). The output is monotonically cumulative, ready for
``recorder/import_statistics``; re-imports are idempotent (the recorder
upserts by (statistic_id, start)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ha_spark.config import Settings
from ha_spark.energy.onboarding import (
    hourly_kwh_from_stats,
    statistic_unit,
    to_import_stats,
)
from ha_spark.ha.statistics import (
    import_statistics,
    list_statistic_ids,
    statistics_during_period,
)

# Component canonical-direction names. Used as keys in derive_base_load and
# surfaced in the per-component coverage report.
COMPONENTS: tuple[str, ...] = (
    "grid_import",
    "grid_export",
    "solar_generation",
    "battery_charge",
    "battery_discharge",
    "ev_charge",
)
REQUIRED_COMPONENTS: frozenset[str] = frozenset({"grid_import"})

# Trailing window the scheduled run re-derives each cycle.
ROLLING_WINDOW_HOURS = 48

# Distinct external statistic id for the derived series (different from the
# source-entity path's ``ha_spark:house_load`` so a setup that already used
# the source-entity backfill can switch without colliding with stale rows;
# the two paths write to the same recorder upsert key, so this also keeps
# things clean if both ever run).
BACKFILL_DERIVED_STATISTIC_ID = "ha_spark:derived_house_load"
BACKFILL_DERIVED_NAME = "ha-spark house load (derived)"


@dataclass(frozen=True)
class ComponentSeries:
    """One component's hourly kWh, already in canonical direction (positive = canonical flow)."""

    entity_id: str
    hourly: dict[datetime, float]  # start_utc -> hourly kWh (positive canonical)

    def coverage(self) -> tuple[datetime | None, datetime | None]:
        if not self.hourly:
            return None, None
        starts = sorted(self.hourly)
        return starts[0], starts[-1]


@dataclass(frozen=True)
class DeriveResult:
    """Output of :func:`derive_base_load` — pure, no IO."""

    hourly: list[tuple[datetime, float]]  # base-load hourly kWh, sorted by start
    degradation: list[str] = field(default_factory=list)  # human-readable notes
    negative_clamped: int = 0  # count of hours where base was clamped to 0
    skipped_hours: int = 0  # count of hours skipped (no grid_import row)
    coverage: dict[str, tuple[datetime | None, datetime | None]] = field(default_factory=dict)


def derive_base_load(
    grid_import: ComponentSeries,
    *,
    grid_export: ComponentSeries | None = None,
    solar_generation: ComponentSeries | None = None,
    battery_discharge: ComponentSeries | None = None,
    battery_charge: ComponentSeries | None = None,
    ev_charge: ComponentSeries | None = None,
) -> DeriveResult:
    """Compute per-hour base load by the energy-balance formula.

    Pure: takes already-converted canonical-direction hourly kWh series,
    returns the derived hourly base-load plus a report. ``grid_import`` is
    required (the function raises ``ValueError`` when it has no rows at
    all). Hours present in optional components but missing from
    ``grid_import`` are skipped, preserving any prior cumulative sum.
    Hours present in ``grid_import`` but missing from an optional component
    contribute zero (important for pre-device history).
    """
    if not grid_import.hourly:
        raise ValueError(
            "Grid import component has no hourly rows; cannot derive base load"
        )
    degradation: list[str] = []
    coverage: dict[str, tuple[datetime | None, datetime | None]] = {
        "grid_import": grid_import.coverage()
    }

    components: dict[str, ComponentSeries | None] = {
        "grid_export": grid_export,
        "solar_generation": solar_generation,
        "battery_discharge": battery_discharge,
        "battery_charge": battery_charge,
        "ev_charge": ev_charge,
    }
    for name, comp in components.items():
        if comp is None:
            degradation.append(f"component '{name}' not configured — treated as zero")
            coverage[name] = (None, None)

    negative_clamped = 0
    sorted_starts = sorted(grid_import.hourly)
    hourly: list[tuple[datetime, float]] = []
    for start in sorted_starts:
        gi = grid_import.hourly[start]
        ge = grid_export.hourly.get(start, 0.0) if grid_export else 0.0
        sol = solar_generation.hourly.get(start, 0.0) if solar_generation else 0.0
        bd = battery_discharge.hourly.get(start, 0.0) if battery_discharge else 0.0
        bc = battery_charge.hourly.get(start, 0.0) if battery_charge else 0.0
        ev = ev_charge.hourly.get(start, 0.0) if ev_charge else 0.0
        raw = gi - ge + sol + bd - bc - ev
        if raw < 0:
            negative_clamped += 1
            raw = 0.0
        hourly.append((start, round(raw, 4)))
    return DeriveResult(
        hourly=hourly,
        degradation=degradation,
        negative_clamped=negative_clamped,
        skipped_hours=0,
        coverage=coverage,
    )


def import_rows_for(
    hourly: list[tuple[datetime, float]], *, start_sum: float = 0.0
) -> list[dict[str, object]]:
    """Build the cumulative-sum recorder rows for a derived hourly series.

    Re-export of ``onboarding.to_import_stats`` so the derived backfill path
    does not need to import the source-entity module. ``start_sum`` is
    forwarded so the rolling rerive can anchor the cumulative sum on the
    last imported row. Idempotent on re-run: the recorder upserts by
    (statistic_id, start).
    """
    return to_import_stats(hourly, start_sum=start_sum)


def last_imported_sum(rows: list[dict[str, object]]) -> tuple[datetime, float] | None:
    """Pick the last row's (start, cumulative sum) so the rolling rerive can anchor.

    HA does not expose a single-shot "max(sum)" for an external statistic;
    the smallest suitable helper is to ask the caller to pass the last row
    from a recent ``statistics_during_period`` pull (which the existing
    backfill already needs for hourly rows). Returns ``None`` when no rows.
    """
    if not rows:
        return None
    last = max(rows, key=lambda r: float(r.get("start") or 0.0))  # type: ignore[arg-type]
    raw_start: object = last.get("start")
    raw_sum: object = last.get("sum")
    if raw_start is None or raw_sum is None:
        return None
    return datetime.fromtimestamp(float(raw_start) / 1000, UTC), float(raw_sum)  # type: ignore[arg-type]


# --- integration with HA long-term statistics ---


@dataclass(frozen=True)
class ComponentSpec:
    """The configured statistic id + invert flag for one component."""

    entity_id: str
    invert: bool = False


def build_component_series(
    spec: ComponentSpec,
    raw_rows: list[dict[str, object]],
    unit: str,
) -> ComponentSeries:
    """Convert one component's raw hourly rows to a canonical-direction series.

    Uses the same ``hourly_kwh_from_stats`` helper as the source-entity
    backfill so unit handling stays in one place. The invert flag flips the
    sign after conversion, so the resulting ``hourly`` map always carries
    canonical-direction positive values for the formula.
    """
    hourly_pairs = hourly_kwh_from_stats(raw_rows, unit, spec.entity_id)
    sign = -1.0 if spec.invert else 1.0
    return ComponentSeries(
        entity_id=spec.entity_id,
        hourly={start: max(0.0, round(kwh * sign, 4)) for start, kwh in hourly_pairs},
    )


async def _fetch_component(
    settings: Settings, spec: ComponentSpec, start: datetime
) -> tuple[ComponentSeries, str] | None:
    """Fetch hourly rows for one component and convert them.

    Returns ``None`` when the entity has no long-term statistics metadata.
    Raises ``ValueError`` with a clear reason when the unit is unsupported
    (caller decides whether to disable derivation or fall back).
    """
    metas = await list_statistic_ids(
        settings.ha_websocket_url, settings.auth_token, timeout=settings.ha_timeout
    )
    meta = next((m for m in metas if m.get("statistic_id") == spec.entity_id), None)
    if meta is None:
        return None
    unit = statistic_unit(meta)
    rows = await statistics_during_period(
        settings.ha_websocket_url,
        settings.auth_token,
        spec.entity_id,
        start,
        period="hour",
        timeout=settings.ha_timeout,
    )
    return build_component_series(spec, rows, unit), unit


async def _gather_components(
    settings: Settings,
    specs: dict[str, ComponentSpec],
    start: datetime,
) -> tuple[dict[str, ComponentSeries], list[str]]:
    """Fetch and convert every configured component.

    Missing metadata → degradation note, no series returned for that
    component. Unsupported units → degradation note + that component treated
    as not-configured (per spec: an unsupported unit disables derivation
    with a clear reason, preserving old behaviour on a partial setup).
    """
    series: dict[str, ComponentSeries] = {}
    notes: list[str] = []
    for name, spec in specs.items():
        if not spec.entity_id:
            notes.append(f"component '{name}' not configured — treated as zero")
            continue
        try:
            result = await _fetch_component(settings, spec, start)
        except ValueError as exc:
            notes.append(f"component '{name}' disabled: {exc}")
            continue
        if result is None:
            notes.append(
                f"component '{name}' ({spec.entity_id}) has no long-term statistics — "
                "treated as zero"
            )
            continue
        comp, _unit = result
        series[name] = comp
    return series, notes


@dataclass(frozen=True)
class DerivedBackfillResult:
    """A derived backfill run: rows + degradation report + coverage ranges."""

    rows_imported: int
    span: str  # human date range
    degradation: list[str]  # per-component notes + sign warnings
    negative_clamped: int
    coverage: dict[str, tuple[datetime | None, datetime | None]]


def _coverage_line(
    coverage: dict[str, tuple[datetime | None, datetime | None]]
) -> str:
    """One-line per-component coverage summary (None when not configured)."""
    lines: list[str] = []
    for name in COMPONENTS:
        start, end = coverage.get(name, (None, None))
        if start is None and end is None:
            lines.append(f"{name}=none")
            continue
        lines.append(f"{name}={start:%Y-%m-%d %H:%M}..{end:%Y-%m-%d %H:%M}")
    return "; ".join(lines)


def _format_degradation(
    notes: list[str],
    *,
    negative_clamped: int,
    coverage: dict[str, tuple[datetime | None, datetime | None]],
) -> list[str]:
    """Format the backfill report lines: degradation notes + sign warning + coverage."""
    out = list(notes)
    if negative_clamped:
        out.append(
            f"{negative_clamped} hour(s) had a negative derived base; "
            "clamped to zero — check component invert flags"
        )
    out.append("coverage: " + _coverage_line(coverage))
    return out


async def backfill_derived_load(
    settings: Settings,
    specs: dict[str, ComponentSpec],
    *,
    statistic_id: str,
    statistic_name: str,
    start: datetime,
) -> DerivedBackfillResult:
    """Fetch each configured component and import the derived base-load series.

    ``specs`` is the full mapping (all six components; missing ones contribute
    zero). Grid import is required — the function raises ``ValueError``
    when it is unconfigured, has no statistics, or has no rows.
    """
    grid_spec = specs.get("grid_import")
    if grid_spec is None or not grid_spec.entity_id:
        raise ValueError(
            "Grid import component (derive_grid_import_entity) is required for "
            "derived base load; configure it or use --from for source-entity backfill"
        )

    series, notes = await _gather_components(settings, specs, start)
    grid_import = series.get("grid_import")
    if grid_import is None:
        raise ValueError(
            f"Grid import component {grid_spec.entity_id} could not be read; "
            "check long-term statistics availability and unit (W/kW/kWh/Wh)"
        )

    result = derive_base_load(
        grid_import,
        grid_export=series.get("grid_export"),
        solar_generation=series.get("solar_generation"),
        battery_discharge=series.get("battery_discharge"),
        battery_charge=series.get("battery_charge"),
        ev_charge=series.get("ev_charge"),
    )

    if not result.hourly:
        raise ValueError("Derived base load produced no hourly rows")

    rows = import_rows_for(result.hourly)
    await import_statistics(
        settings.ha_websocket_url,
        settings.auth_token,
        statistic_id=statistic_id,
        name=statistic_name,
        unit_of_measurement="kWh",
        stats=rows,
        timeout=max(settings.ha_timeout, 60.0),
    )
    first, _ = result.hourly[0]
    last, _ = result.hourly[-1]
    return DerivedBackfillResult(
        rows_imported=len(rows),
        span=f"{first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC",
        degradation=_format_degradation(
            [*notes, *result.degradation],
            negative_clamped=result.negative_clamped,
            coverage=result.coverage,
        ),
        negative_clamped=result.negative_clamped,
        coverage=result.coverage,
    )


async def rerive_trailing_window(
    settings: Settings,
    specs: dict[str, ComponentSpec],
    *,
    statistic_id: str,
    statistic_name: str,
    window_hours: int = ROLLING_WINDOW_HOURS,
) -> DerivedBackfillResult | None:
    """Re-derive the trailing window and continue the cumulative sum.

    Pulls the last imported row of ``statistic_id`` for the anchor point,
    fetches ``window_hours`` of each component, derives base load, and
    imports only the rows past the anchor so the cumulative ``sum`` never
    drops. Returns ``None`` when grid import is unconfigured (caller logs
    and continues). On any failure the caller catches and logs; this is
    best-effort and must never block the daily plan.
    """
    grid_spec = specs.get("grid_import")
    if grid_spec is None or not grid_spec.entity_id:
        return None

    start = datetime.now(UTC) - timedelta(hours=window_hours + 1)
    prior_rows = await statistics_during_period(
        settings.ha_websocket_url,
        settings.auth_token,
        statistic_id,
        start,
        period="hour",
        timeout=settings.ha_timeout,
    )
    anchor = last_imported_sum(prior_rows)

    series, notes = await _gather_components(settings, specs, start)
    grid_import = series.get("grid_import")
    if grid_import is None:
        return DerivedBackfillResult(
            rows_imported=0,
            span="",
            degradation=[
                f"grid import {grid_spec.entity_id} unreadable; skipping rolling rerive"
            ],
            negative_clamped=0,
            coverage={},
        )

    derived = derive_base_load(
        grid_import,
        grid_export=series.get("grid_export"),
        solar_generation=series.get("solar_generation"),
        battery_discharge=series.get("battery_discharge"),
        battery_charge=series.get("battery_charge"),
        ev_charge=series.get("ev_charge"),
    )
    hourly = derived.hourly
    start_sum = 0.0
    if anchor is not None:
        anchor_start, anchor_sum = anchor
        # Filter to strictly-after rows so the cumulative sum continues forward
        # from the anchor rather than double-counting it.
        hourly = [(s, k) for s, k in hourly if s > anchor_start]
        start_sum = anchor_sum
    rows = import_rows_for(hourly, start_sum=start_sum) if hourly else []
    if rows:
        await import_statistics(
            settings.ha_websocket_url,
            settings.auth_token,
            statistic_id=statistic_id,
            name=statistic_name,
            unit_of_measurement="kWh",
            stats=rows,
            timeout=max(settings.ha_timeout, 60.0),
        )
    if not hourly:
        return DerivedBackfillResult(
            rows_imported=0,
            span="",
            degradation=_format_degradation(
                notes,
                negative_clamped=derived.negative_clamped,
                coverage=derived.coverage,
            ),
            negative_clamped=derived.negative_clamped,
            coverage=derived.coverage,
        )
    first, _ = hourly[0]
    last, _ = hourly[-1]
    return DerivedBackfillResult(
        rows_imported=len(rows),
        span=f"{first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC",
        degradation=_format_degradation(
            [*notes, *derived.degradation],
            negative_clamped=derived.negative_clamped,
            coverage=derived.coverage,
        ),
        negative_clamped=derived.negative_clamped,
        coverage=derived.coverage,
    )
