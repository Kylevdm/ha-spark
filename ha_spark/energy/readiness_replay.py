"""Pure readiness cost replay: the recorded intent over the measured day.

The replay answers one question: what would the recorded Observe intents have
cost, applied slot by slot to the flows the household actually measured? It is
a pure function of one closed qualifying
:class:`~ha_spark.energy.readiness_measurements.DayRecord`: no reads, no
writes, no clocks, no settings. Each half-hour applies its own recorded intent
— the window fraction charges at the recorded current, the rest holds, exports
or self-uses — to that slot's measured load and solar, carrying the replay's
own battery energy forward from the day's actual start. Grid flows are costed
at the rates recorded with the slot (an export event earns its event rate on
every export in the slot, on both sides), plannable EV load is grid import on
both sides, and the day's ending energy difference against the actual end is
valued at the next cheap-slot import rate, so a replay cannot win by emptying
the battery. A day the join excluded, or whose evidence is incomplete, is
refused with the reason rather than guessed at.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ha_spark.energy.readiness_ledger import SlotRecord
from ha_spark.energy.readiness_measurements import DayRecord, MeasuredSlot

SLOT_H = 0.5  # half-hour slot length, in hours


class SlotReplay(BaseModel):
    """One replayed half-hour: its grid flows and the battery energy carried out."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    slot_start: datetime
    import_kwh: float
    export_kwh: float
    energy_after_kwh: float


class ReplayResult(BaseModel):
    """A replayed day, costed at the rates recorded with each slot."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    replay_cost: float  # grid flows plus the ending-energy valuation
    incumbent_cost: float
    delta: float
    ending_valuation: float
    slots: tuple[SlotReplay, ...]


def _floor(intent: SlotRecord) -> float:
    return intent.capacity_kwh * intent.min_soc / 100


def _ceiling(intent: SlotRecord) -> float:
    return intent.capacity_kwh * intent.target_cap / 100


def _earned_export_rate(intent: SlotRecord) -> float:
    """An export event earns its event rate on every export in the slot."""
    return intent.event_rate if intent.event_rate is not None else intent.export_rate


def _net_flows(
    intent: SlotRecord, energy: float, net: float
) -> tuple[float, float, float]:
    """A battery that never discharges: a deficit is imported, surplus solar
    charges to the ceiling and the rest is exported."""
    if net > 0:
        return energy, net, 0.0
    surplus = -net
    charged = min(surplus, max(0.0, _ceiling(intent) - energy))
    return energy + charged, 0.0, surplus - charged


def _export_part(
    intent: SlotRecord, energy: float, net: float, duration_h: float
) -> tuple[float, float, float]:
    """Forced export: the battery supplies net plus the export, never below the
    floor; a shortfall first reduces the forced export, then the rest of net is
    imported. Solar surplus beyond the export charges to the ceiling, and the
    rest is exported in addition."""
    exported = intent.export_kw * duration_h
    wanted = net + exported
    if wanted < 0:
        surplus = -wanted
        charged = min(surplus, max(0.0, _ceiling(intent) - energy))
        return energy + charged, 0.0, exported + (surplus - charged)
    available = max(0.0, energy - _floor(intent))
    if wanted <= available:
        return energy - wanted, 0.0, exported
    shortfall = wanted - available
    reduced = min(exported, shortfall)
    return energy - available, shortfall - reduced, exported - reduced


def _self_part(
    intent: SlotRecord, energy: float, net: float
) -> tuple[float, float, float]:
    """Self-use: the battery covers net down to the floor, the rest is imported."""
    if net > 0:
        served = min(net, max(0.0, energy - _floor(intent)))
        return energy - served, net - served, 0.0
    return _net_flows(intent, energy, net)


def _replay_intent(
    intent: SlotRecord, slot: MeasuredSlot, energy: float
) -> tuple[float, float, float]:
    """One half-hour under the recorded intent: the window fraction charges at
    the recorded current, the rest holds (never discharging), exports or
    self-uses."""
    # EV charge never draws from the battery or the house load.
    net = (
        slot.load_kwh - slot.components["v2l"].kwh - slot.components["solar_generation"].kwh
    )
    fraction = intent.charge_window_fraction
    imported = exported = 0.0
    if fraction > 0:
        current = intent.charge_current_a
        if current is None:
            raise ValueError(
                f"{intent.slot_start.isoformat()}: charge current unknown in a window slot"
            )
        stored = min(
            current * intent.voltage_v * SLOT_H * fraction / 1000,
            max(0.0, _ceiling(intent) - energy),
        )
        energy, i, x = _net_flows(intent, energy + stored, net * fraction)
        imported += i + stored / intent.charge_efficiency
        exported += x
    rest = 1.0 - fraction
    if rest > 0:
        if intent.hold_state == "held":
            energy, i, x = _net_flows(intent, energy, net * rest)
        elif intent.export_kw > 0:
            energy, i, x = _export_part(intent, energy, net * rest, SLOT_H * rest)
        else:
            energy, i, x = _self_part(intent, energy, net * rest)
        imported += i
        exported += x
    return energy, imported + slot.components["ev_charge"].kwh, exported


def replay_day(day: DayRecord) -> ReplayResult:
    """Replay one closed qualifying day; refuse every excluded or incomplete day.

    Raises :class:`ValueError` naming what is missing, so a caller bug can
    never replay a day the day-close join excluded or could not evidence.
    """
    reasons: list[str] = []
    if not day.qualifying:
        reasons.append("excluded day: " + "; ".join(day.reasons))
    if not day.slots:
        reasons.append("no slot records")
    start = day.start_energy_kwh
    end = day.end_energy_kwh
    cheap = day.next_cheap_import_rate
    if start is None:
        reasons.append("actual start energy missing")
    if end is None:
        reasons.append("actual end energy missing")
    if cheap is None:
        reasons.append("next cheap-slot import rate missing")
    if reasons or start is None or end is None or cheap is None:
        raise ValueError(f"{day.local_date.isoformat()} is not replayable: " + "; ".join(reasons))
    energy = start
    played: list[SlotReplay] = []
    replay_cost = 0.0
    incumbent_cost = 0.0
    efficiency = 1.0  # replaced by the last slot's recorded value below
    for slot in day.slots:
        intent = slot.intent
        if intent is None:
            raise ValueError(f"{slot.slot_start.isoformat()}: slot record missing")
        if intent.hold_state == "unknown":
            raise ValueError(f"{slot.slot_start.isoformat()}: hold intent unknown")
        if intent.charge_efficiency <= 0:
            raise ValueError(f"{slot.slot_start.isoformat()}: charge efficiency not positive")
        energy, imported, exported = _replay_intent(intent, slot, energy)
        played.append(SlotReplay(
            slot_start=slot.slot_start, import_kwh=imported, export_kwh=exported,
            energy_after_kwh=energy,
        ))
        rate = _earned_export_rate(intent)
        replay_cost += imported * intent.import_rate - exported * rate
        incumbent_cost += (
            slot.components["grid_import"].kwh * intent.import_rate
            - slot.components["grid_export"].kwh * rate
        )
        efficiency = intent.charge_efficiency
    # The replay's ending shortfall is valued as charge it must buy back.
    valuation = (end - energy) * cheap / efficiency
    return ReplayResult(
        replay_cost=replay_cost + valuation, incumbent_cost=incumbent_cost,
        delta=replay_cost + valuation - incumbent_cost, ending_valuation=valuation,
        slots=tuple(played),
    )
