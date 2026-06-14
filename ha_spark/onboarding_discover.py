"""Entity auto-discovery for onboarding (Phase 4).

Given a live ``get_states()`` dump, propose which entity maps to each ha-spark
config field, ranked by confidence. Pure functions over :class:`EntityState`
so they test against a synthetic dump with no HA round-trip. Heuristics are
deliberately conservative — domain is a hard filter and a candidate must score
at least one positive signal — so the wizard *proposes*, the user confirms; it
never silently rewrites config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ha_spark.config import Settings
from ha_spark.ha.models import EntityState


@dataclass(frozen=True)
class Rule:
    """How to recognise the entity for one config field."""

    config_field: str
    domains: tuple[str, ...]
    device_classes: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    id_keywords: tuple[str, ...] = ()
    require_attrs: tuple[str, ...] = ()
    optional: bool = False


# Field rules, ordered roughly as the plan report uses them. Domain is a hard
# filter; device_class/unit/attribute/keyword hits accumulate a score.
RULES: tuple[Rule, ...] = (
    Rule("soc_entity", ("sensor",), ("battery",), ("%",), ("soc", "battery")),
    Rule("battery_voltage_entity", ("sensor",), ("voltage",), ("V",), ("battery", "voltage")),
    Rule(
        "solar_tomorrow_entity", ("sensor",), (), (),
        ("solcast", "forecast", "tomorrow"), ("detailedForecast",),
    ),
    Rule("octopus_rate_entity", ("sensor",), ("monetary",), (), ("octopus", "rate")),
    Rule("dispatch_entity", ("binary_sensor",), (), (), ("octopus", "dispatch", "intelligent")),
    Rule("ev_plug_entity", ("sensor",), (), (), ("zappi", "plug", "ev")),
    Rule("ev_status_entity", ("sensor",), (), (), ("zappi", "status", "ev")),
    Rule(
        "consumption_energy_entity", ("sensor",), ("energy",), ("kWh",),
        ("consumption", "usage", "load", "household"),
    ),
    Rule(
        "grid_power_entity", ("sensor",), ("power",), ("W",),
        ("grid", "supply", "house", "mains"), optional=True,
    ),
    Rule("charge_current_entity", ("number",), ("current",), ("A",), ("charge", "current")),
    Rule(
        "inverter_power_switch_entity", ("select",), (), (),
        ("inverter", "power", "switch"),
    ),
    Rule("heatpump_energy_entity", ("sensor",), ("energy",), ("kWh",),
         ("heatpump", "heat_pump", "ashp"), optional=True),
    Rule("outdoor_weather_entity", ("weather",), (), (), ("home", "forecast"),
         ("temperature",), optional=True),
)

# Score weights.
_DEVICE_CLASS_W = 3
_UNIT_W = 2
_ATTR_W = 3
_KEYWORD_W = 1


@dataclass(frozen=True)
class Candidate:
    """A proposed entity for a field, with why it matched."""

    entity_id: str
    friendly_name: str
    score: int
    reasons: tuple[str, ...]


@dataclass
class FieldProposal:
    """Discovery outcome for one config field."""

    config_field: str
    optional: bool
    current: str
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def status(self) -> str:
        """``match`` | ``differs`` | ``missing`` versus the configured value."""
        if self.best is None:
            return "missing"
        return "match" if self.best.entity_id == self.current else "differs"


def _score(state: EntityState, rule: Rule) -> Candidate | None:
    """Score one state against a rule; None if it earns nothing."""
    score = 0
    reasons: list[str] = []
    device_class = str(state.attributes.get("device_class") or "")
    unit = str(state.attributes.get("unit_of_measurement") or "")

    if rule.device_classes and device_class in rule.device_classes:
        score += _DEVICE_CLASS_W
        reasons.append(f"device_class={device_class}")
    if rule.units and unit in rule.units:
        score += _UNIT_W
        reasons.append(f"unit={unit}")
    for attr in rule.require_attrs:
        if attr in state.attributes:
            score += _ATTR_W
            reasons.append(f"has {attr}")
    eid = state.entity_id.lower()
    hits = [kw for kw in rule.id_keywords if kw in eid]
    if hits:
        score += _KEYWORD_W * len(hits)
        reasons.append("id~" + "+".join(hits))

    if score <= 0:
        return None
    return Candidate(state.entity_id, state.friendly_name, score, tuple(reasons))


def discover(states: list[EntityState], *, top_n: int = 3) -> dict[str, list[Candidate]]:
    """Rank candidate entities per config field from a live state dump."""
    out: dict[str, list[Candidate]] = {}
    for rule in RULES:
        scored = [
            c
            for s in states
            if s.domain in rule.domains and (c := _score(s, rule)) is not None
        ]
        scored.sort(key=lambda c: (-c.score, c.entity_id))
        out[rule.config_field] = scored[:top_n]
    return out


def propose(states: list[EntityState], settings: Settings) -> list[FieldProposal]:
    """Discovery joined with each field's currently configured value."""
    ranked = discover(states)
    proposals: list[FieldProposal] = []
    for rule in RULES:
        proposals.append(
            FieldProposal(
                config_field=rule.config_field,
                optional=rule.optional,
                current=str(getattr(settings, rule.config_field, "")),
                candidates=ranked[rule.config_field],
            )
        )
    return proposals
