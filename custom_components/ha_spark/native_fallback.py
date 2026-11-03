"""Pure native fallback schedule rules: no default current and no write permission.

Validation is one function over the saved options and the limits in force now.
It returns either a whole-ampere schedule or one fixed reason code; a missing,
unconfirmed or invalid limit narrows to invalid and never to a guessed current.
The fallback never programs export or discharge.
"""

import math
from dataclasses import dataclass
from datetime import time
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .topology import EffectiveLimits, Topology

FALLBACK_CONTROLS = frozenset({"charge_current_limit", "charge_window"})
REGISTER_CEILING_A = 62.5
_REASON_WINDOW = "fallback_window_invalid"
_REASON_CURRENT = "fallback_current_invalid"
_REASON_INVERTER = "fallback_inverter_limit"
_REASON_BANK = "fallback_bank_limit"
_REASON_SUPPLY = "fallback_supply_limit"


class FallbackOptions(BaseModel):
    """Owner schedule; every native inverter needs its own explicit current."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    window_start: Annotated[str, Field(min_length=1, max_length=32)]
    window_end: Annotated[str, Field(min_length=1, max_length=32)]
    currents: dict[str, float]


@dataclass(frozen=True)
class FallbackSchedule:
    """Validated whole-ampere currents on the household clock face."""

    window_start: time
    window_end: time
    currents: dict[str, int]


def _clock(value: str) -> time | None:
    # time.fromisoformat accepts 24:00 and offsets; the register face is naive HH:MM.
    if len(value) != 5 or value[2] != ":" or value.startswith("24"):
        return None
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        return None
    return parsed


def validate_fallback(
    options: object,
    topology: object,
    limits: object,
    *,
    export: object | None = None,
    controls: frozenset[str] = FALLBACK_CONTROLS,
) -> FallbackSchedule | str:
    """Return a validated schedule or one fixed reason code, never a value."""
    if export is not None:
        return "fallback_export_enabled"
    if controls - FALLBACK_CONTROLS:
        return "fallback_control_unverified"
    if not isinstance(topology, Topology) or not isinstance(limits, EffectiveLimits):
        return "fallback_topology_invalid"
    native = [i for i in topology.inverters if i.transport == "native"]
    if not native:
        return "fallback_topology_invalid"
    if options is None:
        return "fallback_not_configured"
    try:
        parsed = (options if isinstance(options, FallbackOptions)
                  else FallbackOptions.model_validate(options))
    except (ValidationError, TypeError, ValueError):
        return "fallback_invalid"
    start, end = _clock(parsed.window_start), _clock(parsed.window_end)
    if start is None or end is None or start == end:
        return _REASON_WINDOW
    if set(parsed.currents) != {i.id for i in native}:
        return "fallback_incomplete"
    inverter_limits = {i.id: i for i in limits.inverters}
    bank_limits = {b.id: b for b in limits.banks}
    banks = {b.id: b for b in topology.banks}
    connections = {c.id: c for c in limits.connections}
    for inverter in native:
        current = parsed.currents[inverter.id]
        if not math.isfinite(current) or current < 1:
            return _REASON_CURRENT
        inverter_limit = inverter_limits.get(inverter.id)
        if (current > REGISTER_CEILING_A or inverter_limit is None
                or not math.isfinite(inverter_limit.charge_a)
                or current > inverter_limit.charge_a):
            return _REASON_INVERTER
    bank_totals: dict[str, float] = {}
    for bank in topology.banks:
        total = sum(parsed.currents[i.id] for i in native if i.bank_id == bank.id)
        if not total:
            continue
        bank_limit = bank_limits.get(bank.id)
        if (bank_limit is None or not math.isfinite(bank_limit.charge_a)
                or total > bank_limit.charge_a):
            return _REASON_BANK
        bank_totals[bank.id] = total
    for connection in topology.connections:
        ac_w = 0.0
        for bank_id, total in bank_totals.items():
            bank = banks[bank_id]
            if bank.connection_id == connection.id:
                ac_w += total * bank.nominal_voltage_v / bank.one_way_charge_efficiency
        connection_limit = connections.get(connection.id)
        if connection_limit is None:
            return _REASON_SUPPLY
        if ac_w > connection_limit.import_kw * 1000:
            return _REASON_SUPPLY
    return FallbackSchedule(
        start, end,
        {inverter.id: math.floor(parsed.currents[inverter.id] + 1e-9) for inverter in native},
    )
