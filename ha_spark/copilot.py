"""Ground the chat tier in the live computed plan (Phase 5, NL copilot).

The router's Ollama tier otherwise answers from the user's message alone, with
no idea what tonight's plan actually is. ``build_grounding`` computes the same
plan the ``plan`` command prints and renders it as a compact facts block;
``grounded_system_prompt`` wraps it in instructions that keep the model to
explaining real decisions in the home-energy domain — it never claims to have
changed a setting (the deterministic planner decides and acts, a ROADMAP
non-goal for the LLM).
"""

from __future__ import annotations

from ha_spark.config import Settings
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.report import format_plan
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

COPILOT_SYSTEM = (
    "You are ha-spark's home-energy assistant. Answer the user's question using "
    "ONLY the plan facts provided below — do not invent numbers. Be concise and "
    "concrete: cite the relevant figures and explain the reasoning behind the "
    "plan (why this charge current, what it costs or saves, how solar/load/"
    "dispatches drove it). If the question is not about home energy, the battery, "
    "solar, the tariff, or charging, say that you only cover home energy. You "
    "explain and report only: never claim to have changed a setting or controlled "
    "any device — the planner decides and acts, not you."
)


async def build_grounding(settings: Settings, rest: HomeAssistantRest) -> str | None:
    """Render tonight's computed plan as a grounding block; None if unavailable.

    Reuses the exact ``plan`` pipeline and report, so the copilot explains the
    same decision the planner would apply — and never crashes the chat path.
    """
    try:
        inputs, cfg, load_source = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
        return format_plan(plan, load_source)
    except Exception as exc:  # noqa: BLE001 - grounding is best-effort
        log.warning("Could not build copilot grounding (%s); answering ungrounded", exc)
        return None


def grounded_system_prompt(grounding: str | None) -> str:
    """The copilot system prompt, with the live plan facts when available."""
    if grounding is None:
        return (
            f"{COPILOT_SYSTEM}\n\nThe current plan is unavailable right now, so say "
            "so rather than guessing specific figures."
        )
    return f"{COPILOT_SYSTEM}\n\nCurrent plan and live state:\n{grounding}"
