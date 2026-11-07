"""Read native protection evidence from HA reports and successful bus reads.

HA states are untrusted; only last_reported establishes entity freshness. The
native direction default never reads an HA copy of the battery-power entity.
"""
from dataclasses import dataclass
from datetime import datetime, time
from math import isfinite
from typing import TYPE_CHECKING

from homeassistant.core import State
from homeassistant.helpers import issue_registry as ir
from pydantic import ValidationError

from .const import DOMAIN
from .native_hold import (
    DispatchWindow,
    EvidenceOptions,
    charge_window,
    dispatch_kept,
    fresh,
    in_window,
)
from .solis_frames import POINTS, decode
from .topology import Inverter

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator


@dataclass(frozen=True)
class EvidenceRead:
    charging: bool | None
    ev_report: datetime | None
    dispatch_trusted: bool
    dispatches: tuple[DispatchWindow, ...]
    overnight: bool


class NativeEvidence:
    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator

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

    @property
    def configured(self) -> bool:
        options = self.options()
        return options is not None and options.configured

    def read(self, now: datetime) -> EvidenceRead:
        c = self.coordinator
        options = self.options()
        if options is None:
            return EvidenceRead(None, None, False, (), False)
        states: dict[str, State | None] = {}
        for key in ("ev_status", "ev_plug", "ev_power", "live_dispatch", "adjusted_rate",
                    "dispatch_grid_power", "battery_direction"):
            source = getattr(options, key)
            state = c.hass.states.get(source.entity_id) if source and source.entity_id else None
            states[key] = state if (state is not None and
                state.state.strip().lower() not in ("unknown", "unavailable") and
                fresh(state.last_reported, now, source.max_age_s)) else None
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
                    plug = states["ev_plug"]
                    if dispatch_kept(window, now, live=dispatch.state.strip().lower() == "on",
                        adjusted=adjusted is True or isinstance(adjusted, str)
                        and adjusted.strip().lower() == "true", status=ev.state if ev else None,
                        plug=plug.state if plug else None, car_kw=power("ev_power"), grid_kw=grid):
                        windows.append(window)
        if not trusted:
            windows = []
        inverters = list(c.native.inverters.values())
        overnight = bool(inverters) and all(self._overnight(i, now, options) for i in inverters)
        # Match the shipped start-clock exclusion, only when
        # every inverter has a fresh readable window. Unknown retains the hold.
        windows = [w for w in windows if not all(self._covered(i, w, now) for i in inverters)]
        return EvidenceRead(charging, ev.last_reported if ev else None,
                            trusted, tuple(windows), overnight)

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
            # Native decoding reports charging positive; apply the same
            # owner-set sign convention as the native guard input.
            if inverter.power_sign == "negative_import":
                watts = -watts
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
