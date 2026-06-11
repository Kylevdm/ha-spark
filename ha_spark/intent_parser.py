"""Deterministic offline intent parser — the router's no-LLM fallback.

Handles energy/planner queries only: the message is matched against keyword
groups and answered from the same ``gather_inputs``/``compute_plan`` pipeline
the ``plan`` command uses. Anything unrecognised returns help text listing the
supported queries; this path must never raise.
"""

from __future__ import annotations

from dataclasses import dataclass

from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.report import format_plan
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

_HELP = """\
I couldn't match that to a known query (and the LLM is unavailable).
Offline, I can answer questions containing:
  plan / charge / tonight / overnight   tonight's full charge plan
  soc / battery / state of charge       current battery state of charge
  solar / forecast                      tomorrow's solar forecast
  strategy                              the configured charge strategy
  mode / proactive                      the PROACTIVE_MODE setting
  window                                the off-peak charge window"""


@dataclass(frozen=True)
class IntentResult:
    """The offline parser's answer: text plus whether a query was recognised."""

    text: str
    matched: bool


def _contains(message: str, *keywords: str) -> bool:
    return any(k in message for k in keywords)


def _soc_line(plan: ChargePlan) -> str:
    if not plan.soc_valid:
        return "Battery SoC sensor is unreadable right now."
    return f"Battery is at {plan.soc_now:.0f}% ({plan.usable_now_kwh:.2f} kWh usable)."


def _solar_line(plan: ChargePlan) -> str:
    line = f"Solar forecast for tomorrow: {plan.solar_kwh:.2f} kWh"
    if abs(plan.effective_solar_kwh - plan.solar_kwh) > 1e-9:
        line += f" ({plan.effective_solar_kwh:.2f} kWh after haircut)"
    return line + "."


async def parse_offline(
    message: str, settings: Settings, rest: HomeAssistantRest
) -> IntentResult:
    """Answer an energy query deterministically, without an LLM."""
    text = message.lower()

    if not _contains(
        text,
        "plan", "charge", "tonight", "overnight",
        "soc", "battery", "state of charge",
        "solar", "forecast",
        "strategy", "mode", "proactive", "window",
    ):
        return IntentResult(_HELP, matched=False)

    # Settings-only queries need no HA round-trip.
    if _contains(text, "strategy"):
        return IntentResult(f"Charge strategy: {settings.charge_strategy}.", matched=True)
    if _contains(text, "mode", "proactive"):
        return IntentResult(f"PROACTIVE_MODE: {settings.proactive_mode}.", matched=True)
    if _contains(text, "window"):
        return IntentResult(
            f"Charge window: {settings.charge_window_start} – {settings.charge_window_end}.",
            matched=True,
        )

    # Everything else needs the computed plan.
    try:
        inputs, cfg, load_source = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
    except Exception as exc:  # noqa: BLE001 - the fallback path must never crash
        log.warning("Offline parser could not compute the plan: %r", exc)
        return IntentResult(f"Could not compute the charge plan: {exc}", matched=True)

    if _contains(text, "soc", "battery", "state of charge"):
        return IntentResult(_soc_line(plan), matched=True)
    if _contains(text, "solar", "forecast"):
        return IntentResult(_solar_line(plan), matched=True)
    return IntentResult(format_plan(plan, load_source), matched=True)
