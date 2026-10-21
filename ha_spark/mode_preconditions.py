"""Read-only entry checks for Protect and Optimise, never authority grants.

Uses the driver's model-qualified hold contract and existing fallback validator.
The daemon supplies this tick's checked SoC; diagnostic callers may check a fresh
observation but must not treat a prior tick's measurement as a new grant.
"""
from __future__ import annotations

from typing import Any

from ha_spark.config import Settings
from ha_spark.devices import get_device
from ha_spark.devices.inverters.solis import validate_fallback_schedule
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.energy.soc_monitor import observe_soc
from ha_spark.ha.rest import HomeAssistantRest, HomeAssistantRestError
from ha_spark.transitions import PreconditionReport

NO_FALLBACK_PLATFORM = "no validated fallback charging schedule for this inverter configuration"


def fallback_schedule_problem(settings: Settings) -> str | None:
    """Why ``settings`` has no valid fallback schedule, or ``None`` when it has one.

    Only a single Solis inverter has a fallback schedule; its current, window and
    limits are then checked by the driver's validator.
    """
    inverters = [config for config in settings.devices if config.type == "inverter"]
    if len(inverters) != 1 or inverters[0].driver != "solis":
        return NO_FALLBACK_PLATFORM
    reason = validate_fallback_schedule(settings)
    return reason if isinstance(reason, str) else None


async def entry_preconditions(settings: Settings) -> dict[str, Any]:
    """Read live diagnostic entry conditions without initialising or saving authority."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout,
    ) as rest:
        soc = await observe_soc(settings, rest)
        report = await check_preconditions(settings, rest, soc)
    return {
        "observe": {"passed": True, "reasons": []},
        "protect": {"passed": not report.protect, "reasons": list(report.protect)},
        "optimise": {"passed": not report.optimise, "reasons": list(report.optimise)},
    }


async def check_preconditions(
    settings: Settings, rest: HomeAssistantRest, soc: SocMeasurement | None,
) -> PreconditionReport:
    """Report every failed entry condition with GETs only; no refresh services."""
    failures: list[str] = []
    inverters = [config for config in settings.devices if config.type == "inverter"]
    if len(inverters) != 1:
        failures.append("Protect requires exactly one inverter")
    for config in inverters:
        try:
            device = get_device(config, settings, rest)
        except ValueError:
            failures.append("inverter driver is not supported")
            continue
        validation = await device.hold_validation()
        if not validation.validated:
            failures.append(validation.reason)
        switch = config.entities.get("power_switch", "")
        if not switch:
            failures.append("power_switch entity and read-back must be configured")
        else:
            try:
                readable = (await rest.get_state(switch)).state.strip().lower() in ("on", "off")
            except HomeAssistantRestError:
                readable = False
            if not readable:
                failures.append("power_switch read-back is unavailable")
    # EV status and battery direction are required even if dispatch is unused.
    for field in ("ev_status_entity", "battery_power_entity"):
        if not getattr(settings, field):
            failures.append(f"{field} must be configured for protection evidence")
    for source, configured in (
        ("ev_status", settings.ev_status_entity),
        ("ev_plug", settings.ev_plug_entity),
        ("ev_power", settings.ev_power_entity),
        ("live_dispatch", settings.dispatch_entity),
        ("adjusted_rate", settings.octopus_rate_entity),
        ("dispatch_grid_power", settings.dispatch_grid_power_entity),
        ("battery_power", settings.battery_power_entity),
        ("octopus_dispatch", settings.octopus_api_key and settings.octopus_account_number),
    ):
        option = f"{source}_max_age_seconds"
        if configured and getattr(settings, option) is None:
            failures.append(f"{option} must be configured for protection evidence")
    optimise = list(failures)
    fallback = fallback_schedule_problem(settings)
    if fallback is not None:
        optimise.append(fallback)
    if soc is None or not soc.ok:
        # Do not echo raw untrusted entity states (which could contain secrets).
        optimise.append("this tick's checked SoC measurement must pass")
    return PreconditionReport(protect=tuple(failures), optimise=tuple(optimise))
