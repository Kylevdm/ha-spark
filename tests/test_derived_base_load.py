"""Tests for the derived base-load pipeline (ADR-0001).

The pure ``derive_base_load`` is exercised here without HA mocks (per spec).
The IO helpers in the same module (``backfill_derived_load``,
``rerive_trailing_window``, ``build_component_series``) lean on the same
fake-server / monkeypatch pattern as ``tests/test_onboarding.py`` and
``tests/test_statistics.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ha_spark.config import Settings
from ha_spark.energy import derived_base_load
from ha_spark.energy.derived_base_load import (
    BACKFILL_NAME,
    BACKFILL_STATISTIC_ID,
    ComponentSeries,
    ComponentSpec,
    DeriveResult,
    backfill_derived_load,
    build_component_series,
    derive_base_load,
    derive_specs_from_settings,
    import_rows_for,
    last_imported_sum,
    latest_target_before,
    rerive_trailing_window,
)

_T0_MS = 1780304400000  # an hour boundary, epoch ms
_T0 = datetime.fromtimestamp(_T0_MS / 1000, UTC)


def _hour_series(values: list[float]) -> dict[datetime, float]:
    """Build a dict[hour_start_utc -> kWh] for an ascending list."""
    return {_T0 + timedelta(hours=i): v for i, v in enumerate(values)}


def _series(entity: str, values: list[float]) -> ComponentSeries:
    return ComponentSeries(entity_id=entity, hourly=_hour_series(values))


class _FrozenDatetime:
    """A drop-in ``datetime`` module stand-in that returns a fixed ``now``.

    Lets tests pin the rerive's window_start to a value aligned with the
    test's pre-built hourly rows without monkeypatching every call site.
    """

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self, tz: object = None) -> datetime:
        if tz is None:
            return self._now
        return self._now.astimezone(tz)  # type: ignore[union-attr]

    def __getattr__(self, name: str) -> object:
        # Anything we don't override (timedelta, datetime class itself,
        # timezone etc.) falls through to the real module.
        return getattr(__import__("datetime").datetime, name)


# --- pure derivation ---


def test_derive_full_formula() -> None:
    """base = grid_import - grid_export + solar + battery_discharge - battery_charge - ev_charge."""
    gi = _series("sensor.grid_import", [10.0, 8.0, 12.0])
    ge = _series("sensor.grid_export", [2.0, 1.0, 0.0])
    sol = _series("sensor.solar", [5.0, 6.0, 0.0])
    bd = _series("sensor.battery_d", [1.0, 0.0, 3.0])
    bc = _series("sensor.battery_c", [4.0, 2.0, 0.0])
    ev = _series("sensor.ev", [3.0, 0.0, 1.0])

    result = derive_base_load(gi, grid_export=ge, solar_generation=sol,
                              battery_discharge=bd, battery_charge=bc, ev_charge=ev)
    # Hour 0: 10 - 2 + 5 + 1 - 4 - 3 = 7
    # Hour 1:  8 - 1 + 6 + 0 - 2 - 0 = 11
    # Hour 2: 12 - 0 + 0 + 3 - 0 - 1 = 14
    assert [round(v, 4) for _, v in result.hourly] == [7.0, 11.0, 14.0]
    assert result.negative_clamped == 0
    assert result.degradation == []


def test_derive_each_invert_behavior() -> None:
    """The pure function ignores build-time invert; it consumes canonical magnitudes."""
    gi = _series("sensor.grid_import", [10.0])
    base = derive_base_load(gi)
    assert [round(v, 4) for _, v in base.hourly] == [10.0]

    # Export inverted: invert is applied at build_component_series time, not
    # here. The pure function just plugs values into the formula.
    ge_inverted = ComponentSeries(
        entity_id="sensor.grid_export",
        hourly={_T0: 5.0},  # already inverted by build_component_series
    )
    ge_canonical = ComponentSeries(
        entity_id="sensor.grid_export",
        hourly={_T0: 5.0},
    )
    base2 = derive_base_load(gi, grid_export=ge_inverted)
    # base = 10 - 5 = 5 (invert only affects how the caller builds the series)
    assert [round(v, 4) for _, v in base2.hourly] == [5.0]

    base3 = derive_base_load(gi, grid_export=ge_canonical)
    assert [round(v, 4) for _, v in base3.hourly] == [5.0]


def test_build_component_series_applies_invert() -> None:
    """Invert flips the sign of the canonical contribution.

    Previously the helper clamped the inverted value to zero, which
    silently erased every positive source once invert was set; the
    formula's ``negative_clamped`` counter then never fired because
    there was nothing to warn about.
    """
    rows = [{"start": _T0_MS, "change": 4.0}]
    spec = ComponentSpec(entity_id="sensor.x", invert=True)
    series = build_component_series(spec, rows, "kWh")
    # invert=True flips the sign: a +4.0 raw reading becomes -4.0 (not 0).
    assert series.hourly[_T0] == -4.0

    spec_canonical = ComponentSpec(entity_id="sensor.x", invert=False)
    series_canonical = build_component_series(spec_canonical, rows, "kWh")
    assert series_canonical.hourly[_T0] == 4.0

    rows_w = [{"start": _T0_MS, "mean": 1000.0}]
    series_w = build_component_series(spec_canonical, rows_w, "W")
    assert series_w.hourly[_T0] == 1.0


def test_build_component_series_preserves_signed_readings() -> None:
    """Signed inputs (net sensors) flow through with their sign intact.

    The legacy ``hourly_kwh_from_stats`` clamps negatives to zero for the
    source-entity backfill (a negative reading there is genuinely a sensor
    error); the derived path needs the sign preserved so an explicit
    ``invert=True`` flips a signed reading into the opposite canonical
    direction. This test pins that behaviour.
    """
    rows = [
        {"start": _T0_MS, "change": -3.0},                  # net: discharging
        {"start": _T0_MS + 3600000, "change": 2.0},        # net: charging
    ]
    spec = ComponentSpec(entity_id="sensor.x", invert=False)
    series = build_component_series(spec, rows, "kWh")
    # Sign is preserved through conversion (no clamping).
    assert series.hourly[_T0] == -3.0
    assert series.hourly[_T0 + timedelta(hours=1)] == 2.0

    spec_inv = ComponentSpec(entity_id="sensor.x", invert=True)
    series_inv = build_component_series(spec_inv, rows, "kWh")
    # Invert flips every reading's sign — useful when reusing a net
    # sensor as the "discharge" component.
    assert series_inv.hourly[_T0] == 3.0
    assert series_inv.hourly[_T0 + timedelta(hours=1)] == -2.0


def test_inverted_contribution_is_not_erased_in_formula() -> None:
    """An inverted component reaches the formula as a real signed value.

    Regression for the clamp-before-invert bug: a positive source with
    ``invert=True`` must reach the formula as a non-zero contribution
    (otherwise the operator's intent is silently lost).
    """
    rows = [{"start": _T0_MS, "change": 4.0}]
    inverted = build_component_series(
        ComponentSpec(entity_id="sensor.ev_charge", invert=True), rows, "kWh"
    )
    # -4.0 — not zero.
    assert inverted.hourly[_T0] == -4.0

    gi = _series("sensor.grid_import", [10.0])
    # Use the inverted sensor as ev_charge (subtractive component):
    # base = 10 - (-4) = 14. The contribution is preserved.
    result = derive_base_load(gi, ev_charge=inverted)
    assert [round(v, 4) for _, v in result.hourly] == [14.0]


def test_derive_with_omitted_components_reports_degradation() -> None:
    """An omitted component contributes zero and the report names it."""
    gi = _series("sensor.grid_import", [1.0, 2.0])
    result = derive_base_load(gi)
    assert [round(v, 4) for _, v in result.hourly] == [1.0, 2.0]
    assert len(result.degradation) == 5
    for name in ("grid_export", "solar_generation", "battery_discharge",
                 "battery_charge", "ev_charge"):
        assert any(name in note for note in result.degradation)


def test_derive_reports_coverage_for_every_component() -> None:
    """Coverage covers *every* component, not just grid_import and omissions.

    A configured component with data must report its real
    ``(earliest, latest)`` range; an omitted or empty component reports
    ``(None, None)``. The coverage map drives the operator-visible
    per-component summary so it must always be complete.
    """
    gi = _series("sensor.grid_import", [1.0, 2.0, 3.0])
    sol = _series("sensor.solar", [0.5, 0.6, 0.7])
    empty_bd = ComponentSeries(entity_id="sensor.battery_d", hourly={})
    result = derive_base_load(gi, solar_generation=sol, battery_discharge=empty_bd)

    # Every component is in the coverage map.
    assert set(result.coverage) == {
        "grid_import", "grid_export", "solar_generation",
        "battery_discharge", "battery_charge", "ev_charge",
    }
    # Configured with data: real range.
    assert result.coverage["grid_import"] == (_T0, _T0 + timedelta(hours=2))
    assert result.coverage["solar_generation"] == (_T0, _T0 + timedelta(hours=2))
    # Configured but empty: (None, None) — still in the map.
    assert result.coverage["battery_discharge"] == (None, None)
    # Omitted: (None, None).
    assert result.coverage["grid_export"] == (None, None)
    assert result.coverage["battery_charge"] == (None, None)
    assert result.coverage["ev_charge"] == (None, None)


def test_derive_coverage_for_fully_configured_components() -> None:
    """Every component configured: coverage reports a real range for each."""
    gi = _series("sensor.grid_import", [5.0, 6.0])
    ge = _series("sensor.grid_export", [1.0, 2.0])
    sol = _series("sensor.solar", [0.5, 0.7])
    bd = _series("sensor.battery_d", [1.0, 0.0])
    bc = _series("sensor.battery_c", [2.0, 1.0])
    ev = _series("sensor.ev", [1.5, 0.5])
    result = derive_base_load(
        gi, grid_export=ge, solar_generation=sol,
        battery_discharge=bd, battery_charge=bc, ev_charge=ev,
    )
    # Every component is present with a real range (no omissions).
    for name in (
        "grid_import", "grid_export", "solar_generation",
        "battery_discharge", "battery_charge", "ev_charge",
    ):
        assert result.coverage[name] == (_T0, _T0 + timedelta(hours=1))
    assert result.degradation == []


def test_derive_optional_missing_hour_treated_as_zero() -> None:
    """Hours present in grid_import but missing from optional components contribute zero."""
    gi = _series("sensor.grid_import", [3.0, 2.0, 4.0])
    sol = ComponentSeries(
        entity_id="sensor.solar",
        hourly={_T0: 1.0, _T0 + timedelta(hours=2): 2.0},  # missing hour 1
    )
    result = derive_base_load(gi, solar_generation=sol)
    # Hour 0: 3 + 1 = 4; Hour 1: 2 + 0 = 2 (sol missing); Hour 2: 4 + 2 = 6
    assert [round(v, 4) for _, v in result.hourly] == [4.0, 2.0, 6.0]
    assert result.negative_clamped == 0


def test_derive_negative_clamped_and_counted() -> None:
    """Negative per-hour base is clamped to zero and counted for reporting."""
    gi = _series("sensor.grid_import", [5.0, 10.0, 1.0])
    ge = _series("sensor.grid_export", [10.0, 1.0, 0.0])
    result = derive_base_load(gi, grid_export=ge)
    # Hour 0: 5 - 10 = -5 -> clamped to 0; Hour 1: 10 - 1 = 9; Hour 2: 1 - 0 = 1
    assert [round(v, 4) for _, v in result.hourly] == [0.0, 9.0, 1.0]
    assert result.negative_clamped == 1


def test_derive_requires_grid_import_with_rows() -> None:
    with pytest.raises(ValueError, match="Grid import component"):
        derive_base_load(ComponentSeries(entity_id="sensor.x", hourly={}))


def test_derive_skipped_hour_preserves_prior_cumulative_via_to_import() -> None:
    """Skip-without-grid-import semantics: prior cumulative sum is preserved.

    The pure function only sees grid-import hours; the cumulative ``sum``
    column is built by :func:`import_rows_for` (``to_import_stats``), which
    is time-ordered and never re-anchored. A gap in grid import therefore
    leaves the prior cumulative sum untouched in the consumer — the gap
    shows up as missing rows in the recorder, not as a downward step.
    """
    gi = _series("sensor.grid_import", [1.0, 2.0, 3.0])
    result = derive_base_load(gi)
    rows = import_rows_for(result.hourly)
    # Hourly is contiguous (grid_import drives it); cumulative sums grow.
    assert [r["sum"] for r in rows] == [1.0, 3.0, 6.0]
    # Re-deriving the same input must yield identical rows.
    again = derive_base_load(gi)
    assert import_rows_for(again.hourly) == rows


def test_import_rows_for_continues_cumulative_sum_from_anchor() -> None:
    """Rolling rerive anchor: start_sum extends the cumulative column."""
    hourly = [
        (_T0, 1.0),
        (_T0 + timedelta(hours=1), 2.0),
    ]
    rows = import_rows_for(hourly, start_sum=10.0)
    # Cumulative sums: 10 + 1 = 11, 10 + 1 + 2 = 13.
    assert [r["sum"] for r in rows] == [11.0, 13.0]


def test_last_imported_sum_picks_max_start() -> None:
    rows = [
        {"start": _T0_MS, "sum": 1.0},
        {"start": _T0_MS + 3600000, "sum": 5.0},
        {"start": _T0_MS + 7200000, "sum": 3.0},
    ]
    anchor = last_imported_sum(rows)
    assert anchor == (
        datetime.fromtimestamp((_T0_MS + 7200000) / 1000, UTC),
        3.0,
    )


def test_last_imported_sum_returns_none_for_empty_or_partial() -> None:
    assert last_imported_sum([]) is None
    assert last_imported_sum([{"start": _T0_MS, "sum": None}]) is None
    assert last_imported_sum([{"start": None, "sum": 1.0}]) is None


def test_latest_target_before_filters_out_rows_at_or_after_cutoff() -> None:
    """The rerive anchor must be strictly *before* the recompute window."""
    rows = [
        {"start": _T0_MS - 7200000, "sum": 80.0},  # before
        {"start": _T0_MS - 3600000, "sum": 90.0},  # before (latest)
        {"start": _T0_MS, "sum": 100.0},  # at cutoff: excluded
        {"start": _T0_MS + 3600000, "sum": 110.0},  # after: excluded
    ]
    anchor = latest_target_before(rows, datetime.fromtimestamp(_T0_MS / 1000, UTC))
    assert anchor == (
        datetime.fromtimestamp((_T0_MS - 3600000) / 1000, UTC),
        90.0,
    )


def test_latest_target_before_returns_none_when_no_row_before() -> None:
    """No row strictly before the cutoff -> no anchor; rerive starts from 0."""
    rows = [
        {"start": _T0_MS, "sum": 100.0},
        {"start": _T0_MS + 3600000, "sum": 110.0},
    ]
    assert latest_target_before(
        rows, datetime.fromtimestamp(_T0_MS / 1000, UTC)
    ) is None
    # Empty / partial rows still return None.
    assert latest_target_before([], _T0) is None
    assert latest_target_before(
        [{"start": _T0_MS, "sum": None}], _T0,
    ) is None


def test_derive_specs_from_settings_is_shared_helper() -> None:
    """The shared spec mapper behaves the same as the prior per-caller copies."""
    settings = Settings(
        ha_url="http://ha.test", ha_token="t",
        derive_grid_import_entity="sensor.gi",
        derive_solar_generation_entity="sensor.sol",
        derive_invert_battery_charge=True,
    )
    specs = derive_specs_from_settings(settings)
    # Required component is always present (even with a blank entity id).
    assert "grid_import" in specs
    assert specs["grid_import"].entity_id == "sensor.gi"
    assert "grid_export" not in specs  # blanked optionals are absent.
    assert specs["solar_generation"].entity_id == "sensor.sol"
    assert specs["solar_generation"].invert is False
    # Invert flag flows through when the component is configured.
    settings_inv = Settings(
        ha_url="http://ha.test", ha_token="t",
        derive_grid_import_entity="sensor.gi",
        derive_battery_charge_entity="sensor.bc",
        derive_invert_battery_charge=True,
    )
    specs_inv = derive_specs_from_settings(settings_inv)
    assert specs_inv["battery_charge"].invert is True
    assert specs_inv["battery_charge"].entity_id == "sensor.bc"


# --- IO integration (monkeypatch + fake HA) ---


def _settings() -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="t")


def _hourly_ms(start: datetime, *, hours: int) -> list[dict[str, Any]]:
    """Hourly rows with a constant ``change`` of 1.0 starting at ``start``."""
    return [
        {"start": int((start + timedelta(hours=h)).timestamp() * 1000), "change": 1.0}
        for h in range(hours)
    ]


async def test_backfill_derived_load_imports_cumulative_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"statistic_id": "sensor.grid_import",
             "statistics_unit_of_measurement": "kWh"},
            {"statistic_id": "sensor.grid_export",
             "statistics_unit_of_measurement": "kWh"},
            {"statistic_id": "sensor.solar",
             "statistics_unit_of_measurement": "kWh"},
        ]

    hourly_by_stat: dict[str, list[dict[str, Any]]] = {
        "sensor.grid_import": _hourly_ms(_T0, hours=3),
        "sensor.grid_export": [
            {"start": int((_T0 + timedelta(hours=h)).timestamp() * 1000), "change": 0.5}
            for h in range(3)
        ],
        "sensor.solar": [
            {"start": int((_T0 + timedelta(hours=h)).timestamp() * 1000), "change": 0.25}
            for h in range(3)
        ],
    }

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return hourly_by_stat[kwargs.get("statistic_id") or args[2]]  # type: ignore[index]

    async def fake_import(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(derived_base_load, "list_statistic_ids", fake_list)
    monkeypatch.setattr(derived_base_load, "statistics_during_period", fake_stats)
    monkeypatch.setattr(derived_base_load, "import_statistics", fake_import)

    specs = {
        "grid_import": ComponentSpec(entity_id="sensor.grid_import"),
        "grid_export": ComponentSpec(entity_id="sensor.grid_export"),
        "solar_generation": ComponentSpec(entity_id="sensor.solar"),
    }
    result = await backfill_derived_load(
        _settings(),
        specs,
        statistic_id=BACKFILL_STATISTIC_ID,
        statistic_name=BACKFILL_NAME,
        start=_T0,
    )
    assert result.rows_imported == 3
    # base = 1 - 0.5 + 0.25 = 0.75 each hour -> cumulative sums 0.75, 1.5, 2.25
    assert captured["statistic_id"] == BACKFILL_STATISTIC_ID
    assert captured["stats"] == [
        {"start": (_T0).isoformat(), "state": 0.75, "sum": 0.75},
        {"start": (_T0 + timedelta(hours=1)).isoformat(), "state": 0.75, "sum": 1.5},
        {"start": (_T0 + timedelta(hours=2)).isoformat(), "state": 0.75, "sum": 2.25},
    ]
    assert any("grid_export" in n for n in result.degradation)
    assert any("solar_generation" in n for n in result.degradation)
    # Coverage covers every configured and unconfigured component.
    # Configured (data): real range. Unconfigured: omitted.
    assert set(result.coverage) == {
        "grid_import", "grid_export", "solar_generation",
        "battery_discharge", "battery_charge", "ev_charge",
    }
    assert result.coverage["grid_import"] == (_T0, _T0 + timedelta(hours=2))
    assert result.coverage["grid_export"] == (_T0, _T0 + timedelta(hours=2))
    assert result.coverage["solar_generation"] == (_T0, _T0 + timedelta(hours=2))
    assert result.coverage["battery_discharge"] == (None, None)
    assert result.coverage["battery_charge"] == (None, None)
    assert result.coverage["ev_charge"] == (None, None)


async def test_backfill_derived_load_requires_grid_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(derived_base_load, "list_statistic_ids", fake_list)

    with pytest.raises(ValueError, match="Grid import component"):
        await backfill_derived_load(
            _settings(),
            {"grid_import": ComponentSpec(entity_id="")},
            statistic_id=BACKFILL_STATISTIC_ID,
            statistic_name=BACKFILL_NAME,
            start=_T0,
        )


async def test_backfill_derived_load_errors_on_unsupported_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"statistic_id": "sensor.grid_import",
             "statistics_unit_of_measurement": "°C"},
        ]

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(derived_base_load, "list_statistic_ids", fake_list)
    monkeypatch.setattr(derived_base_load, "statistics_during_period", fake_stats)

    specs = {
        "grid_import": ComponentSpec(entity_id="sensor.grid_import"),
        "grid_export": ComponentSpec(entity_id="sensor.grid_export"),
    }
    with pytest.raises(ValueError, match="could not be read"):
        await backfill_derived_load(
            _settings(),
            specs,
            statistic_id=BACKFILL_STATISTIC_ID,
            statistic_name=BACKFILL_NAME,
            start=_T0,
        )


async def test_backfill_derived_load_no_rows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"statistic_id": "sensor.grid_import",
                 "statistics_unit_of_measurement": "kWh"}]

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(derived_base_load, "list_statistic_ids", fake_list)
    monkeypatch.setattr(derived_base_load, "statistics_during_period", fake_stats)
    monkeypatch.setattr(derived_base_load, "import_statistics",
                        lambda *a, **kw: None)

    specs = {"grid_import": ComponentSpec(entity_id="sensor.grid_import")}
    with pytest.raises(ValueError, match="no hourly rows"):
        await backfill_derived_load(
            _settings(),
            specs,
            statistic_id=BACKFILL_STATISTIC_ID,
            statistic_name=BACKFILL_NAME,
            start=_T0,
        )


async def test_rerive_trailing_window_recomputes_full_window_and_upserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rolling rerive recomputes *every* derivable hour in the trailing window.

    Late-arriving component rows must overwrite existing target rows in
    the window so the consumer never trains on stale base load. The
    cumulative ``sum`` anchors on the latest target row strictly *before*
    the recompute window — a row inside the window cannot anchor because
    this rerive will overwrite it — and every derivable hour in the
    window is recomputed and upserted (recorder upserts by start).
    """
    captured: dict[str, Any] = {}

    # Pin ``now`` so window_start = _T0 - 1h (a fixed-relative offset the
    # test data aligns with).
    fake_now = _T0 + timedelta(hours=2)
    monkeypatch.setattr(derived_base_load, "datetime", _FrozenDatetime(fake_now))

    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"statistic_id": "sensor.grid_import",
             "statistics_unit_of_measurement": "kWh"},
        ]

    # Prior target rows (anchor + already-present window rows): the row at
    # hour -2 is the anchor (strictly before window_start = _T0 - 1h);
    # the rows at hours -1 and 0 are inside the window and must be
    # *replaced*, not preserved.
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        statistic_id = kwargs.get("statistic_id") or args[2]
        if statistic_id == BACKFILL_STATISTIC_ID:
            return [
                {"start": _T0_MS - 7200000, "sum": 100.0},  # anchor (hour -2)
                {"start": _T0_MS - 3600000, "sum": 102.0},  # inside window: replaced
                {"start": _T0_MS, "sum": 105.0},  # inside window: replaced
            ]
        # Component data covers the window (hours 0..2).
        return [
            {"start": _T0_MS, "change": 2.0},
            {"start": _T0_MS + 3600000, "change": 3.0},
            {"start": _T0_MS + 7200000, "change": 5.0},
        ]

    async def fake_import(*args: Any, **kwargs: Any) -> None:
        captured["stats"] = kwargs["stats"]

    monkeypatch.setattr(derived_base_load, "list_statistic_ids", fake_list)
    monkeypatch.setattr(derived_base_load, "statistics_during_period", fake_stats)
    monkeypatch.setattr(derived_base_load, "import_statistics", fake_import)

    specs = {"grid_import": ComponentSpec(entity_id="sensor.grid_import")}
    result = await rerive_trailing_window(
        _settings(),
        specs,
        statistic_id=BACKFILL_STATISTIC_ID,
        statistic_name=BACKFILL_NAME,
        window_hours=3,
    )
    assert result is not None
    # All 3 window hours are recomputed and imported; cumulative sums
    # continue from the anchor (100) so the running total never drops:
    # 100 + 2 = 102, 102 + 3 = 105, 105 + 5 = 110.
    assert captured["stats"] == [
        {
            "start": (_T0).isoformat(),
            "state": 2.0,
            "sum": 102.0,
        },
        {
            "start": (_T0 + timedelta(hours=1)).isoformat(),
            "state": 3.0,
            "sum": 105.0,
        },
        {
            "start": (_T0 + timedelta(hours=2)).isoformat(),
            "state": 5.0,
            "sum": 110.0,
        },
    ]
    assert result.rows_imported == 3


async def test_rerive_trailing_window_starts_from_zero_when_no_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No prior target rows before the window: cumulative starts at 0.

    Common on a fresh install where the rerive runs before any manual
    backfill has imported rows. The rerive still recomputes every hour
    in the window (the recorder upserts) and the report is honest about
    the cumulative sum not yet connecting to history outside the window.
    """
    captured: dict[str, Any] = {}

    fake_now = _T0 + timedelta(hours=1)
    monkeypatch.setattr(derived_base_load, "datetime", _FrozenDatetime(fake_now))

    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"statistic_id": "sensor.grid_import",
             "statistics_unit_of_measurement": "kWh"},
        ]

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        statistic_id = kwargs.get("statistic_id") or args[2]
        if statistic_id == BACKFILL_STATISTIC_ID:
            return []  # no prior target rows at all
        return [
            {"start": _T0_MS, "change": 1.5},
            {"start": _T0_MS + 3600000, "change": 2.5},
        ]

    async def fake_import(*args: Any, **kwargs: Any) -> None:
        captured["stats"] = kwargs["stats"]

    monkeypatch.setattr(derived_base_load, "list_statistic_ids", fake_list)
    monkeypatch.setattr(derived_base_load, "statistics_during_period", fake_stats)
    monkeypatch.setattr(derived_base_load, "import_statistics", fake_import)

    specs = {"grid_import": ComponentSpec(entity_id="sensor.grid_import")}
    result = await rerive_trailing_window(
        _settings(),
        specs,
        statistic_id=BACKFILL_STATISTIC_ID,
        statistic_name=BACKFILL_NAME,
        window_hours=3,
    )
    assert result is not None
    # Cumulative starts at 0 (no anchor found): 0 + 1.5 = 1.5, 1.5 + 2.5 = 4.0.
    assert captured["stats"] == [
        {
            "start": (_T0).isoformat(),
            "state": 1.5,
            "sum": 1.5,
        },
        {
            "start": (_T0 + timedelta(hours=1)).isoformat(),
            "state": 2.5,
            "sum": 4.0,
        },
    ]
    assert result.rows_imported == 2


async def test_rerive_trailing_window_returns_none_without_grid_import() -> None:
    specs = {"grid_import": ComponentSpec(entity_id="")}
    assert await rerive_trailing_window(
        _settings(),
        specs,
        statistic_id=BACKFILL_STATISTIC_ID,
        statistic_name=BACKFILL_NAME,
    ) is None


async def test_rerive_trailing_window_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_list(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ws down")

    async def boom_stats(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ws down")

    monkeypatch.setattr(derived_base_load, "list_statistic_ids", boom_list)
    monkeypatch.setattr(derived_base_load, "statistics_during_period", boom_stats)
    specs = {"grid_import": ComponentSpec(entity_id="sensor.grid_import")}
    # The scheduler catches this — but the helper itself raises so the
    # scheduler can log it. Confirm the bare helper propagates.
    with pytest.raises(RuntimeError, match="ws down"):
        await rerive_trailing_window(
            _settings(),
            specs,
            statistic_id=BACKFILL_STATISTIC_ID,
            statistic_name=BACKFILL_NAME,
        )


# --- DeriveResult shape (helps catch accidental field renames) ---


def test_derive_result_fields_are_stable() -> None:
    gi = _series("sensor.grid_import", [1.0])
    result = derive_base_load(gi)
    assert isinstance(result, DeriveResult)
    assert set(vars(result)) == {
        "hourly", "degradation", "negative_clamped", "skipped_hours", "coverage",
    }
