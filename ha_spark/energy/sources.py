"""Gather live planner inputs from Home Assistant (REST reads).

Slot horizons start at the current half-hour and end at the window following
the running-or-next charge window. Daily load profiles use household clock time;
solar uses each slot's calendar date. The planner prorates slot 0's energy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from ha_spark.config import Settings
from ha_spark.energy.axle import AxleApiError, read_axle_event
from ha_spark.energy.dispatch_evidence import (
    DispatchEvidence,
    partition_dispatches,
)
from ha_spark.energy.evidence_freshness import (
    EvidenceFreshness,
    battery_direction,
    check_report_time,
)
from ha_spark.energy.forecast import load_timezone, predict_home_load
from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import (
    SLOTS_PER_DAY,
    DispatchSlot,
    LoadForecast,
    PlannerConfig,
    PlannerInputs,
    PricePoint,
)
from ha_spark.energy.octopus import (
    OctopusApiError,
    fetch_planned_dispatches,
    fetch_standard_unit_rates,
)
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.energy.soc_monitor import observe_soc
from ha_spark.energy.solar import distribute_solar
from ha_spark.energy.tariff import (
    AxleTariffProvider,
    DynamicTariffProvider,
    FixedTariffProvider,
    OctopusIntelligentProvider,
    TariffProvider,
    TariffSchedule,
    _in_overnight_window,
)
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# myenergi zappi states that mean the EV is actively drawing power.
_EV_ACTIVE = {"charging", "delivering", "boosting", "diverting"}
# States that stop the house battery discharging while the EV is charging.
# Eco+ diversion is solar-led and intentionally does not trigger an EV hold.
_EV_HOLD_ACTIVE = {"charging", "boosting", "delivering"}


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _parse_dispatches(raw: Any) -> tuple[DispatchSlot, ...]:
    slots: list[DispatchSlot] = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        try:
            start = datetime.fromisoformat(str(d["start"]))
            end = datetime.fromisoformat(str(d["end"]))
        except (KeyError, ValueError, TypeError):
            continue
        slots.append(
            DispatchSlot(
                start,
                end,
                _to_float(d.get("charge_in_kwh"), 0.0),
                str(d.get("source", "")),
            )
        )
    return tuple(slots)


def _parse_price_points(raw: Any) -> tuple[PricePoint, ...]:
    """Tolerantly parse a rates-array attribute: [{start, end, value_inc_vat|value}, ...]."""
    points: list[PricePoint] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        try:
            start = datetime.fromisoformat(str(r["start"]))
            end = datetime.fromisoformat(str(r["end"]))
        except (KeyError, ValueError, TypeError):
            continue
        price = _opt_float(r.get("value_inc_vat", r.get("value")))
        if price is None:
            continue
        points.append(PricePoint(start, end, price))
    return tuple(points)


def _parse_detailed_forecast(
    raw: Any, percentile: int = 50
) -> list[tuple[datetime, float]] | None:
    """Parse Solcast's ``detailedForecast`` attribute (shape varies).

    ``percentile`` selects ``pv_estimate10``/``pv_estimate90`` when present,
    falling back to the median ``pv_estimate``. One malformed entry — a
    non-dict, an unparseable ``period_start``, a missing or non-numeric value
    — makes the whole list unusable (``None``): that date's solar then counts
    as zero instead of being sized on a shape the payload can't be trusted
    for (#398).
    """
    if not isinstance(raw, list):
        return None
    key = "pv_estimate" if percentile == 50 else f"pv_estimate{percentile}"
    entries: list[tuple[datetime, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            start = datetime.fromisoformat(str(item["period_start"]))
            value = item.get(key)
            if value is None:
                value = item["pv_estimate"]
            entries.append((start, float(value)))
        except (KeyError, ValueError, TypeError):
            return None
    return entries or None


class _SolarDayReading(NamedTuple):
    """One calendar date's validated solar forecast."""

    total_kwh: float
    # Solcast detailed-forecast weights, or None to use the fallback shape.
    detailed: list[tuple[datetime, float]] | None


def _solar_reading(
    state: EntityState | None, percentile: int
) -> _SolarDayReading | None:
    """One date's solar reading, or ``None`` to count that date as zero.

    The total is the percentile attribute when it is a finite number, else the
    sensor state (as before #398, with a warning). The reading is then used
    only when that total is finite and >= 0 and every ``detailedForecast``
    entry is finite and >= 0 — a non-numeric, NaN, infinite, negative or
    malformed payload makes the date's solar zero, so a bad sensor degrades
    the plan instead of crashing it.
    """
    if state is None:
        return None
    estimate_key = "estimate" if percentile == 50 else f"estimate{percentile}"
    percentile_total = _opt_float(state.attributes.get(estimate_key))
    total: float
    if percentile_total is not None and isfinite(percentile_total):
        total = percentile_total
    else:
        log.warning(
            "Solcast %s day-total attribute missing or invalid; "
            "using sensor state with unknown percentile",
            estimate_key,
        )
        total = _to_float(state.state, 0.0)
    if not isfinite(total) or total < 0:
        log.warning(
            "Solcast solar day total %s is negative or not a number; "
            "counting that day's solar as zero",
            total,
        )
        return None
    detailed: list[tuple[datetime, float]] | None = None
    raw_detailed = state.attributes.get("detailedForecast")
    if raw_detailed is not None:
        detailed = _parse_detailed_forecast(raw_detailed, percentile)
        if detailed is None or any(not isfinite(v) or v < 0 for _, v in detailed):
            log.warning(
                "Solcast detailedForecast is malformed or holds a negative "
                "value; counting that day's solar as zero",
            )
            return None
    return _SolarDayReading(total, detailed)


def _solar_slots(
    readings: dict[date, _SolarDayReading | None],
    slot_starts: tuple[datetime, ...],
    tz: ZoneInfo,
) -> tuple[float, ...]:
    """Lay each date's daily solar profile onto real half-hours by household clock.

    Today's entity covers today, the tomorrow entity tomorrow, and any other
    calendar date is zero. Daily profiles stay in 48-value local clock order,
    including when the horizon crosses a DST transition.
    """
    profiles: dict[date, tuple[float, ...]] = {}
    for day, reading in readings.items():
        if reading is None:
            profiles[day] = (0.0,) * SLOTS_PER_DAY
        else:
            profiles[day] = distribute_solar(reading.total_kwh, reading.detailed, tz, day)
    values: list[float] = []
    for start in slot_starts:
        local = start.astimezone(tz)
        profile = profiles.get(local.date())
        values.append(profile[local.hour * 2 + local.minute // 30] if profile else 0.0)
    return tuple(values)


def pre_window_drain(
    forecast: LoadForecast, now: datetime, window_start: time, window_end: time
) -> float:
    """Forecast battery drain (kWh) from ``now`` until the charge window opens.

    Used by the daily-balance fallback; slot horizons account for this load
    directly. 0 when already inside the window, or when the window is more
    than 12 h away (a daytime manual run shouldn't dock half a day of load).
    """
    if _in_overnight_window(now.time(), window_start, window_end):
        return 0.0
    open_today = now.replace(
        hour=window_start.hour, minute=window_start.minute, second=0, microsecond=0
    )
    opens = open_today if open_today > now else open_today + timedelta(days=1)
    gap_hours = (opens - now).total_seconds() / 3600.0
    if gap_hours > 12:
        return 0.0
    if forecast.slots is None:
        return forecast.total_kwh / 24.0 * gap_hours
    # Sum slot-of-day values between now and the window, prorating partials.
    # (Uses the served day's day-type profile for tonight — same approximation
    # as the slot horizon.)
    total = 0.0
    t = now
    while t < opens:
        slot_floor = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)
        segment_end = min(slot_floor + timedelta(minutes=30), opens)
        fraction = (segment_end - t).total_seconds() / 1800.0
        total += forecast.slots[(t.hour * 2 + t.minute // 30) % SLOTS_PER_DAY] * fraction
        t = segment_end
    return total


def build_config(
    settings: Settings, voltage_v: float, *, buffer_pct: float | None = None
) -> PlannerConfig:
    if buffer_pct is None:
        buffer_pct = settings.charge_buffer_pct
    return PlannerConfig(
        capacity_kwh=settings.battery_capacity_kwh,
        voltage_v=voltage_v,
        min_soc=settings.min_soc,
        target_cap=settings.target_soc_cap,
        max_current_a=settings.max_charge_current_a,
        battery_discharge_ceiling_kw=settings.battery_discharge_ceiling_kw,
        dno_export_limit_kw=settings.dno_export_limit_kw,
        supply_max_current_a=settings.supply_max_current_a,
        supply_voltage_v=settings.supply_voltage_v,
        solar_haircut_k=settings.solar_haircut_k,
        window_start=parse_time(settings.charge_window_start),
        window_end=parse_time(settings.charge_window_end),
        rate_offpeak=settings.rate_offpeak_gbp_kwh,
        rate_peak=settings.rate_peak_gbp_kwh,
        rate_export=settings.rate_export_gbp_kwh,
        buffer_pct=buffer_pct,
        charge_efficiency=settings.charge_efficiency,
        strategy=settings.charge_strategy,
        v2l_budget_kwh=settings.v2l_budget_kwh,
        v2l_round_trip_efficiency=settings.v2l_round_trip_efficiency,
        v2l_offpeak_rate_gbp=settings.v2l_offpeak_rate_gbp,
        v2l_max_rate_kw=settings.v2l_max_rate_kw,
        v2l_preferred_rate_kw=settings.v2l_preferred_rate_kw,
    )


def build_schedule(
    settings: Settings, inputs: PlannerInputs, cfg: PlannerConfig
) -> TariffSchedule:
    """Build the configured tariff provider's schedule for this plan."""
    fixed = FixedTariffProvider(
        cheap_rate=cfg.rate_offpeak, standard_rate=cfg.rate_peak, export_rate=cfg.rate_export
    )
    provider: TariffProvider = fixed
    if settings.tariff_provider == "dynamic":
        provider = DynamicTariffProvider(fallback=fixed)
    elif settings.tariff_provider == "octopus_intelligent":
        provider = OctopusIntelligentProvider(fallback=fixed)
    if settings.export_event_provider == "axle":
        provider = AxleTariffProvider(
            fallback=provider, event_rate_gbp_kwh=settings.axle_event_rate_gbp_kwh
        )
    return provider.schedule(inputs, cfg)


async def read_dispatches(
    settings: Settings, rest: HomeAssistantRest
) -> tuple[tuple[DispatchSlot, ...], bool]:
    """Read the HA dispatch entity: ``(dispatches, trusted)`` (#143 §3).

    The one definition of hold trust on the HA-entity path, shared by
    :func:`gather_inputs` and the per-minute reconcile. Untrusted only when
    ``dispatch_entity`` is set and the read failed or reports ``unavailable``/
    ``unknown``: then an empty result means "unreadable", not "no dispatch". An
    unset entity is a household with no dispatch source — no holds, not
    unreadable ones, and no read. Every provider folds dispatches into holds, so
    distrusting it would refuse every export for ever.

    A failure is logged at debug only: the per-minute caller would otherwise put
    1440 identical warnings a day into the add-on log, so each caller reports an
    untrusted result at its own cadence.
    """
    dispatches, trusted, _live = await read_dispatches_and_live(settings, rest)
    return dispatches, trusted


async def read_dispatches_and_live(
    settings: Settings, rest: HomeAssistantRest,
    *, report_times: dict[str, datetime | None] | None = None,
) -> tuple[tuple[DispatchSlot, ...], bool, bool | None]:
    """Read planned and live dispatch state from the same HA entity request.

    ``live`` is ``None`` when the source is unset or unreadable, and otherwise
    is true only while the dispatch entity is ``on``.
    """
    if report_times is not None:
        report_times["live_dispatch"] = None
    if not settings.dispatch_entity:
        return (), True, None
    try:
        dispatch = await rest.get_state(settings.dispatch_entity)
    except Exception as exc:  # noqa: BLE001 - a missing entity must not crash the plan
        log.debug("Could not read %s (%s)", settings.dispatch_entity, exc)
        return (), False, None
    if report_times is not None:
        report_times["live_dispatch"] = dispatch.last_reported
    state_value = str(dispatch.state).strip().lower()
    if state_value in ("unavailable", "unknown"):
        return (), False, None
    return (
        _parse_dispatches(dispatch.attributes.get("planned_dispatches")),
        True,
        state_value == "on",
    )


@dataclass(frozen=True)
class DispatchEvidenceRead:
    """Evidence snapshot plus EV status read reused by the separate EV hold."""

    evidence: DispatchEvidence
    ev_status: EntityState | None
    ev_status_trusted: bool
    freshness: dict[str, EvidenceFreshness] = field(default_factory=dict)
    battery_direction: str = "unknown"


async def _read_evidence_entity(
    entity_id: str, rest: HomeAssistantRest
) -> EntityState | None:
    """Read an optional corroborating entity without making it a plan failure."""
    if not entity_id:
        return None
    try:
        state = await rest.get_state(entity_id)
    except Exception as exc:  # noqa: BLE001 - corroboration must never block planning
        log.debug("Could not read %s (%s)", entity_id, exc)
        return None
    if str(state.state).strip().lower() in ("unavailable", "unknown"):
        return None
    return state


async def read_dispatch_evidence(
    settings: Settings,
    rest: HomeAssistantRest,
    planned_dispatches: tuple[DispatchSlot, ...],
    *,
    live_dispatch: bool | None = None,
    forecast_house_kw: float | None = None,
    report_times: dict[str, datetime | None] | None = None,
    now: datetime | None = None,
    read_all: bool = False,
) -> DispatchEvidenceRead:
    """Read the EV status once and plug/rate only when slots need rating.

    Dispatch state is supplied from the same entity GET that returned its
    planned slots. Failures of plug/rate corroboration degrade to ``None`` and
    never affect dispatch-source trust. EV status is also returned for the
    existing active-EV hold, avoiding a second request.
    """
    reports = dict(report_times or {})
    ev_status, ev_status_trusted = await read_ev_status(settings, rest)
    reports["ev_status"] = ev_status.last_reported if ev_status else None
    ev_sibling = await _read_evidence_entity(settings.ev_liveness_entity, rest)
    dispatch_sibling = await _read_evidence_entity(settings.dispatch_liveness_entity, rest)
    battery = await _read_evidence_entity(settings.battery_power_entity, rest)
    reports["battery_power"] = battery.last_reported if battery else None
    plug: EntityState | None = None
    rate: EntityState | None = None
    if planned_dispatches or read_all:
        plug = await _read_evidence_entity(settings.ev_plug_entity, rest)
        rate = await _read_evidence_entity(settings.octopus_rate_entity, rest)
    reports["ev_plug"] = plug.last_reported if plug else None
    reports["adjusted_rate"] = rate.last_reported if rate else None
    car_power: float | None = None
    grid_power: float | None = None
    if planned_dispatches or read_all:

        async def power(entity: str, source: str) -> float | None:
            reading = await _read_evidence_entity(entity, rest)
            reports[source] = reading.last_reported if reading else None
            if reading is None:
                return None
            value = _opt_float(reading.state)
            unit = reading.attributes.get("unit_of_measurement", "W")
            if value is None or not isfinite(value) or unit not in ("W", "kW"):
                return None
            return value / 1000 if unit == "W" else value

        car_power = await power(settings.ev_power_entity, "ev_power")
        grid_power = await power(settings.dispatch_grid_power_entity, "dispatch_grid_power")
        if grid_power is not None and settings.dispatch_grid_power_invert:
            grid_power = -grid_power
    adjusted = None
    if rate is not None:
        raw_adjusted = rate.attributes.get("is_intelligent_adjusted")
        if raw_adjusted is not None:
            adjusted = raw_adjusted is True or (
                isinstance(raw_adjusted, str) and raw_adjusted.strip().lower() == "true"
            )
    # Additive metadata only: do not filter or reinterpret legacy evidence here.
    source_entities = {
        "ev_status": settings.ev_status_entity,
        "ev_plug": settings.ev_plug_entity,
        "ev_power": settings.ev_power_entity,
        "live_dispatch": settings.dispatch_entity,
        "adjusted_rate": settings.octopus_rate_entity,
        "dispatch_grid_power": settings.dispatch_grid_power_entity,
        "battery_power": settings.battery_power_entity,
        "octopus_dispatch": settings.tariff_provider == "octopus_intelligent",
    }
    observed_at = now if now is not None else datetime.now(UTC)
    freshness: dict[str, EvidenceFreshness] = {}
    for source, configured in source_entities.items():
        if not configured:
            continue
        sibling = (
            ev_sibling if source in ("ev_status", "ev_plug", "ev_power")
            else dispatch_sibling if source in ("live_dispatch", "adjusted_rate")
            else None
        )
        option = f"{source}_max_age_seconds"
        freshness[source] = check_report_time(
            reports.get(source), now=observed_at,
            max_age_s=getattr(settings, option), option=option,
            sibling_reported_at=sibling.last_reported if sibling else None,
        )
    return DispatchEvidenceRead(
        freshness=freshness,
        battery_direction=battery_direction(
            battery.state if battery else None,
            unit=battery.attributes.get("unit_of_measurement") if battery else None,
            discharge_sign=settings.battery_power_discharge_sign,
        ),
        evidence=DispatchEvidence(
            live_dispatch=live_dispatch,
            rate_adjusted=adjusted,
            car_power_kw=car_power,
            grid_import_kw=grid_power,
            forecast_house_kw=forecast_house_kw,
            plug_value=plug.state if plug is not None else None,
            ev_status_value=(
                ev_status.state if ev_status is not None and ev_status_trusted else None
            ),
        ),
        ev_status=ev_status,
        ev_status_trusted=ev_status_trusted,
    )


async def read_ev_status(
    settings: Settings, rest: HomeAssistantRest
) -> tuple[EntityState | None, bool]:
    """Read the configured EV status entity, separating no source from bad data."""
    if not settings.ev_status_entity:
        return None, True
    try:
        state = await rest.get_state(settings.ev_status_entity)
    except Exception as exc:  # noqa: BLE001 - EV evidence must not stop reconciliation
        log.debug("Could not read %s (%s)", settings.ev_status_entity, exc)
        return None, False
    if str(state.state).strip().lower() in ("unavailable", "unknown"):
        return None, False
    return state, True


async def read_ev_hold_status(settings: Settings, rest: HomeAssistantRest) -> bool | None:
    """Return charging evidence for the hold, or ``None`` when it cannot be read.

    ``None`` means no configured EV source or an unreadable configured entity.
    The hold intentionally excludes ``diverting`` (eco+ solar diversion).
    """
    state, trusted = await read_ev_status(settings, rest)
    return ev_hold_status_from_read(state, trusted)


def ev_hold_status_from_read(state: EntityState | None, trusted: bool) -> bool | None:
    """Apply the separate active-EV hold rule to one already-read state."""
    if state is None or not trusted:
        return None
    return str(state.state).strip().lower() in _EV_HOLD_ACTIVE


async def gather_inputs(
    settings: Settings, rest: HomeAssistantRest, *, soc: SocMeasurement | None = None
) -> tuple[PlannerInputs, PlannerConfig, LoadForecast]:
    """Read live HA state and build (inputs, config, load forecast).

    ``soc`` is the daemon tick's already-checked measurement: when supplied
    it is used as-is, so the plan is sized from the exact observation the
    loop made (no independent reread). Otherwise one observation is made
    here through the same shared :func:`observe_soc` path.
    """

    async def state(entity_id: str) -> EntityState | None:
        if not entity_id:
            return None
        try:
            return await rest.get_state(entity_id)
        except Exception as exc:  # noqa: BLE001 - a missing entity must not crash the plan
            log.warning("Could not read %s (%s)", entity_id, exc)
            return None

    soc_measurement = soc if soc is not None else await observe_soc(settings, rest)
    if not soc_measurement.ok:
        log.warning("SoC measurement failed integrity check: %s", soc_measurement.reason)
    voltage = await state(settings.battery_voltage_entity)
    solar = await state(settings.solar_tomorrow_entity)
    solar_today = await state(settings.solar_today_entity)
    ha_needed = await state(settings.ha_template_charge_needed_entity)

    voltage_v = _to_float(voltage.state if voltage else None, settings.battery_voltage_v)

    flexibility_event = None
    flexibility_event_trusted = True
    if settings.export_event_provider == "axle":
        try:
            flexibility_event = await read_axle_event(settings, rest)
        except AxleApiError as exc:
            log.warning("Could not read Axle event: %s", exc)
            flexibility_event_trusted = False

    report_times: dict[str, datetime | None] = {}
    dispatches: tuple[DispatchSlot, ...]
    live_dispatch: bool | None = None
    # Whether an empty `dispatches` means "none" or "unreadable" (#143 §3).
    dispatches_trusted = True
    if settings.tariff_provider == "octopus_intelligent":
        # Planned dispatches come from Kraken. A configured HA dispatch entity
        # still contributes live-state evidence, but never replaces Kraken's
        # planned windows or changes Kraken read trust.
        try:
            dispatches = await fetch_planned_dispatches(settings)
            report_times["octopus_dispatch"] = datetime.now(UTC)
        except OctopusApiError as exc:
            log.warning("Could not read Octopus planned dispatches (%s)", exc)
            dispatches = ()
            dispatches_trusted = False
        if settings.dispatch_entity:
            _ha_dispatches, _ha_trusted, live_dispatch = await read_dispatches_and_live(
                settings, rest, report_times=report_times
            )
    else:
        dispatches, dispatches_trusted, live_dispatch = await read_dispatches_and_live(
            settings, rest, report_times=report_times
        )
        if not dispatches_trusted:
            log.warning("Dispatch entity %s unreadable; holds untrusted", settings.dispatch_entity)
    evidence_read = await read_dispatch_evidence(
        settings, rest, dispatches, live_dispatch=live_dispatch, report_times=report_times
    )
    if settings.ev_status_entity and not evidence_read.ev_status_trusted:
        log.warning("EV status entity %s unreadable", settings.ev_status_entity)
    ev_status = evidence_read.ev_status
    ev_status_value = (
        str(ev_status.state).strip().lower()
        if ev_status is not None and evidence_read.ev_status_trusted
        else None
    )
    ev_charging = ev_status_value in _EV_ACTIVE
    ev_hold_charging = (
        ev_status_value in _EV_HOLD_ACTIVE if ev_status_value is not None else None
    )

    dynamic_prices: tuple[PricePoint, ...] = ()
    if settings.tariff_provider == "dynamic" and settings.dynamic_rates_entity:
        today = await state(settings.dynamic_rates_entity)
        points = list(_parse_price_points(today.attributes.get("rates") if today else None))
        if settings.dynamic_rates_entity_tomorrow:
            rates_tomorrow = await state(settings.dynamic_rates_entity_tomorrow)
            points.extend(
                _parse_price_points(
                    rates_tomorrow.attributes.get("rates") if rates_tomorrow else None
                )
            )
        dynamic_prices = tuple(sorted(points, key=lambda p: p.start))
    elif settings.tariff_provider == "octopus_intelligent":
        try:
            dynamic_prices = await fetch_standard_unit_rates(
                settings,
                period_from=datetime.now(UTC),
                period_to=datetime.now(UTC) + timedelta(hours=48),
            )
        except OctopusApiError as exc:
            log.warning("Could not read Octopus standard unit rates (%s)", exc)

    lat, lon = settings.latitude, settings.longitude
    if lat is None or lon is None:
        # HA knows the site location; the explicit config fields override it.
        try:
            ha_cfg = await rest.get_config()
            lat = lat if lat is not None else _opt_float(ha_cfg.get("latitude"))
            lon = lon if lon is not None else _opt_float(ha_cfg.get("longitude"))
        except Exception as exc:  # noqa: BLE001 - location only gates the ML model
            log.warning("Could not read HA config for site location (%s)", exc)

    tz = load_timezone(settings.timezone)
    window_start = parse_time(settings.charge_window_start)
    window_end = parse_time(settings.charge_window_end)
    now = datetime.now(tz)
    geometry = compute_horizon_geometry(now, window_start, window_end, tz)
    served_day = geometry.served_day
    forecast = await predict_home_load(settings, lat=lat, lon=lon, target=served_day)
    # Solar is forecast per calendar date (#398): today's from the optional
    # today's-solar entity, tomorrow's from the existing tomorrow entity. A
    # date with no entity, or a payload that fails the checks in
    # :func:`_solar_reading`, counts as zero that day.
    today_date = now.date()
    tomorrow_date = (now + timedelta(days=1)).date()
    readings: dict[date, _SolarDayReading | None] = {
        today_date: _solar_reading(solar_today, settings.solar_percentile),
        tomorrow_date: _solar_reading(solar, settings.solar_percentile),
    }
    # The plan's day-total solar is the served day's: today's while its charge
    # window is still running, otherwise the day the next window serves.
    served_solar = readings.get(served_day)
    solar_kwh = served_solar.total_kwh if served_solar is not None else 0.0

    unrated_dispatches = dispatches
    dispatches, dropped_dispatches = partition_dispatches(
        dispatches, replace(evidence_read.evidence, forecast_house_kw=forecast.total_kwh / 24), now
    )
    for dropped in dropped_dispatches:
        plug_value = (
            " ".join(evidence_read.evidence.plug_value.split())
            if evidence_read.evidence.plug_value
            else ""
        )
        log.info(
            "Dropped contradicted dispatch %s to %s (EV plug: %s)",
            dropped.start.isoformat(),
            dropped.end.isoformat(),
            plug_value or "unreadable",
        )
    drain = 0.0

    load_slots: tuple[float, ...] | None = None
    solar_slots: tuple[float, ...] | None = None
    horizon_start: datetime | None = None
    if forecast.slots is not None:
        horizon_start = geometry.horizon_start
        load_slots = tuple(
            forecast.slots[local.hour * 2 + local.minute // 30]
            for start in geometry.slot_starts
            for local in (start.astimezone(tz),)
        )
        solar_slots = _solar_slots(readings, geometry.slot_starts, tz)
    else:
        drain = pre_window_drain(forecast, now, window_start, window_end)

    inputs = PlannerInputs(
        soc=soc_measurement,
        pre_window_drain_kwh=drain,
        solar_tomorrow_kwh=solar_kwh,
        predicted_home_load_kwh=forecast.total_kwh,
        dispatches=dispatches,
        dispatches_trusted=dispatches_trusted,
        flexibility_event=flexibility_event,
        flexibility_event_trusted=flexibility_event_trusted,
        ev_charging=ev_charging,
        ev_hold_charging=ev_hold_charging,
        ha_template_needed=_opt_float(ha_needed.state) if ha_needed else None,
        load_slots=load_slots,
        solar_slots=solar_slots,
        horizon_start=horizon_start,
        geometry=geometry if load_slots is not None else None,
        now=now,
        dynamic_prices=dynamic_prices,
        unrated_dispatches=unrated_dispatches,
        evidence_freshness=evidence_read.freshness,
        battery_direction=evidence_read.battery_direction,
    )
    # Dynamic buffer: when the quantile ML forecast drives the plan, replace the
    # fixed margin with the model's own uncertainty, (P90 - P50) / P50.
    buffer_override: float | None = None
    if (
        settings.buffer_mode == "quantile"
        and forecast.p90_total_kwh is not None
        and forecast.total_kwh > 0
    ):
        buffer_override = max(
            0.0, (forecast.p90_total_kwh / forecast.total_kwh - 1.0) * 100.0
        )
    return inputs, build_config(settings, voltage_v, buffer_pct=buffer_override), forecast
