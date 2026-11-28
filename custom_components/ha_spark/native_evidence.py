"""Read native protection evidence from HA reports and successful bus reads.

HA states are untrusted; only last_reported establishes entity freshness. The
native direction default never reads an HA copy of the battery-power entity.
A current Kraken schedule can add active windows with local corroboration,
never supply clear evidence.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import isfinite
from typing import TYPE_CHECKING

from homeassistant.core import State
from homeassistant.helpers import issue_registry as ir
from pydantic import ValidationError

from .const import DOMAIN
from .native_hold import (
    DISPATCH_GROUP,
    EV_GROUP,
    SOURCES,
    DispatchWindow,
    EvidenceOptions,
    Freshness,
    charge_window,
    dispatch_corroborated,
    dispatch_kept,
    fresh,
    in_window,
    report_freshness,
)
from .solis_frames import POINTS, decode
from .topology import Inverter

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator
    from .wire import AddonDispatches

EVIDENCE_REPAIR_GRACE = timedelta(minutes=15)
KRAKEN_MAX_AGE = timedelta(minutes=45)
KRAKEN_CLOCK_SKEW = timedelta(seconds=60)


@dataclass(frozen=True)
class EvidenceRead:
    charging: bool | None
    ev_report: datetime | None
    dispatch_trusted: bool
    dispatches: tuple[DispatchWindow, ...]
    overnight: bool
    ev_via_sibling: bool = False
    corroborated: tuple[DispatchWindow, ...] = ()


class NativeEvidence:
    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator
        self._unavailable_since: dict[str, tuple[str, datetime]] = {}

    def options(self) -> EvidenceOptions | None:
        try:
            return EvidenceOptions.model_validate(
                self.coordinator.entry.options.get("evidence", {}))
        except ValidationError:
            return None

    def check_options(self) -> None:
        """Invalid stored evidence disables holds until the owner re-saves it."""
        c = self.coordinator
        issue_id = f"evidence_options_invalid_{c.entry.entry_id}"
        if self.options() is None:
            ir.async_create_issue(
                c.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key="evidence_options_invalid",
            )
        else:
            ir.async_delete_issue(c.hass, DOMAIN, issue_id)

    def check_health(self, now: datetime) -> None:
        """Warn about sustained evidence loss independently of any hold."""
        c = self.coordinator
        owned = c.ownership.verified and c.ownership.owner == "integration"
        options = self.options() if owned else None
        freshness = self.freshness(now) if options is not None else {}
        registry = ir.async_get(c.hass)
        for key in (*EV_GROUP, *DISPATCH_GROUP, "dispatch_grid_power"):
            issue_id = f"evidence_source_unavailable_{c.entry.entry_id}_{key}"
            source = getattr(options, key, None)
            entity_id = source.entity_id if source is not None else None
            reading = freshness.get(key)
            if entity_id is None or reading is None or reading.validity == "fresh":
                self._unavailable_since.pop(key, None)
                ir.async_delete_issue(c.hass, DOMAIN, issue_id)
                continue

            placeholders = {"source_key": key, "entity_id": entity_id}
            issue = registry.async_get_issue(DOMAIN, issue_id)
            if issue is not None and issue.translation_placeholders != placeholders:
                ir.async_delete_issue(c.hass, DOMAIN, issue_id)
            if reading.validity not in ("unknown", "stale"):
                self._unavailable_since.pop(key, None)
                continue

            previous_id, since = self._unavailable_since.get(key, (entity_id, now))
            if previous_id != entity_id or now < since:
                since = now
            self._unavailable_since[key] = (entity_id, since)
            if now - since >= EVIDENCE_REPAIR_GRACE:
                ir.async_create_issue(
                    c.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                    translation_key="evidence_source_unavailable",
                    translation_placeholders=placeholders,
                )

    @property
    def configured(self) -> bool:
        options = self.options()
        return options is not None and options.configured

    def _state(self, entity_id: str | None) -> State | None:
        state = self.coordinator.hass.states.get(entity_id) if entity_id else None
        return state if state is not None and state.state.strip().lower() not in (
            "unknown", "unavailable") else None

    def freshness(self, now: datetime) -> dict[str, Freshness]:
        """Freshness of every configured entity source, without exposing state values."""
        options = self.options()
        if options is None:
            return {}
        result: dict[str, Freshness] = {}
        for key in SOURCES:
            source = getattr(options, key)
            if source is None or source.entity_id is None:
                continue
            sibling_id = (options.ev_liveness_entity if key in EV_GROUP else
                          options.dispatch_liveness_entity if key in DISPATCH_GROUP else None)
            own = self._state(source.entity_id)
            sibling = self._state(sibling_id)
            result[key] = report_freshness(
                own.last_reported if own else None,
                sibling.last_reported if sibling else None, now, source.max_age_s)
        return result

    def read(self, now: datetime) -> EvidenceRead:
        c = self.coordinator
        options = self.options()
        if options is None:
            return EvidenceRead(None, None, False, (), False)
        freshness = self.freshness(now)
        states: dict[str, State | None] = {}
        for key in ("ev_status", "ev_plug", "ev_power", "live_dispatch", "adjusted_rate",
                    "dispatch_grid_power", "battery_direction"):
            source = getattr(options, key)
            state = self._state(source.entity_id) if source else None
            result = freshness.get(key)
            states[key] = state if result is not None and result.validity == "fresh" else None
        def power(key: str) -> float | None:
            reading = states[key]
            if reading is not None:
                try:
                    value = float(reading.state)
                    unit = reading.attributes.get("unit_of_measurement", "W")
                    if isfinite(value) and unit in ("W", "kW"):
                        return value / 1000 if unit == "W" else value
                except (ValueError, OverflowError):
                    pass
            return None

        ev = states["ev_status"]
        charging = (ev.state.strip().lower() in ("charging", "boosting", "delivering")
                    if ev is not None else None)
        plug = states["ev_plug"]
        plug_value = plug.state if plug else None
        ev_value = ev.state if ev else None
        car_kw = power("ev_power")
        dispatch = states["live_dispatch"]
        windows: list[DispatchWindow] = []
        # Only a fresh configured report carrying its dispatch list is trusted;
        # an absent source or attribute is unknown, never an empty clearance.
        trusted = dispatch is not None
        if dispatch is not None:
            raw = dispatch.attributes.get("planned_dispatches")
            if not isinstance(raw, (list, tuple)) or len(raw) > 128:
                trusted = False
            else:
                for item in raw:
                    if not isinstance(item, dict):
                        trusted = False
                        break
                    try:
                        window = DispatchWindow.model_validate({
                            "start": item.get("start"), "end": item.get("end")})
                    except ValidationError:
                        trusted = False
                        break
                    rate = states["adjusted_rate"]
                    adjusted = rate.attributes.get("is_intelligent_adjusted") if rate else None
                    grid = power("dispatch_grid_power")
                    if grid is not None and options.dispatch_grid_invert:
                        grid = -grid
                    if dispatch_kept(window, now, live=dispatch.state.strip().lower() == "on",
                        adjusted=adjusted is True or isinstance(adjusted, str)
                        and adjusted.strip().lower() == "true", status=ev_value,
                        plug=plug_value, car_kw=car_kw, grid_kw=grid):
                        windows.append(window)
        if not trusted:
            windows = []
        inverters = list(c.native.inverters.values())
        overnight = bool(inverters) and all(self._overnight(i, now, options) for i in inverters)
        # Match the shipped start-clock exclusion, only when
        # every inverter has a fresh readable window. Unknown retains the hold.
        windows = [w for w in windows if not all(self._covered(i, w, now) for i in inverters)]
        addon = self._addon_dispatches(now)
        corroborated: list[DispatchWindow] = []
        if addon is not None:
            for start, end in addon.windows:
                if dispatch_corroborated(start, end, now, status=ev_value, plug=plug_value,
                                         car_kw=car_kw):
                    window = DispatchWindow(start=start, end=end)
                    if not all(self._covered(i, window, now) for i in inverters):
                        corroborated.append(window)
        via_sibling = freshness.get("ev_status", Freshness("unknown", None)).via_sibling
        return EvidenceRead(charging, ev.last_reported if ev and not via_sibling else None,
                            trusted, tuple(windows + corroborated), overnight, via_sibling,
                            corroborated=tuple(corroborated))

    def _addon_dispatches(self, now: datetime) -> "AddonDispatches | None":
        c = self.coordinator
        summary = c.summary
        if ((c.counted_api_version or 0) < 7 or not c.connected or summary is None
                or summary.session_id != c.entry.data.get("session_id")
                or not fresh(c.last_counted, now, 120) or summary.loop_tick is None
                or not -5 <= (now - summary.loop_tick).total_seconds() <= 120):
            return None
        addon = summary.addon_dispatches
        if addon is None or not -KRAKEN_CLOCK_SKEW <= now - addon.fetched_at <= KRAKEN_MAX_AGE:
            return None
        return addon

    def _window(self, inverter: Inverter, now: datetime) -> tuple[time, time] | None:
        reading = self.coordinator.native.reading(inverter, "holding_43141")
        if reading is None or not fresh(reading.read_at, now, 60):
            return None
        return charge_window(reading.registers)

    def _covered(self, inverter: Inverter, window: DispatchWindow, now: datetime) -> bool:
        zone = self.coordinator.control.household_zone()
        resident = self._window(inverter, now)
        if resident is None or zone is None:
            return False
        return in_window(window.start.astimezone(zone).time(), *resident)

    def _overnight(self, inverter: Inverter, now: datetime, options: EvidenceOptions) -> bool:
        c = self.coordinator
        zone = c.control.household_zone()
        resident = self._window(inverter, now)
        if (zone is None or resident is None
                or not in_window(now.astimezone(zone).time(), *resident)):
            return False
        source = options.battery_direction
        if source.source == "native":
            reading = c.native.reading(inverter, "input_33132")
            if (reading is None or reading.registers is None
                    or not fresh(reading.read_at, now, source.max_age_s)):
                return False
            point = next(p for p in POINTS if p.address == 33149)
            watts = decode(point, reading.registers, zone, frame=True)
            if not isinstance(watts, (int, float)):
                return False
            # The direction register signs native battery power (charging
            # positive), so the owner's grid-port power_sign does not apply.
            return watts > -50
        state = c.hass.states.get(source.entity_id) if source.entity_id else None
        if (state is None or not fresh(state.last_reported, now, source.max_age_s)
                or state.attributes.get("unit_of_measurement") != "W"):
            return False
        try:
            watts = float(state.state)
        except (ValueError, OverflowError):
            return False
        return isfinite(watts) and watts * (1 if options.battery_discharge_sign == "positive"
                                           else -1) < 50
