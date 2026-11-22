"""The one site bound behind every ledger write's plausibility rule.

A counter reset's recovery shows up in HA statistics as a single interval whose
``change`` equals a lifetime reading. The rule rejects any interval whose energy
exceeds the site's import/export bound (plus 25%) times the interval's length,
so the ledger stores a gap instead of the spike. Reading the accepted limits is
read-only here: nothing in this module writes the topology record, gates a
write or reaches control.
"""

from __future__ import annotations

import math

from ha_spark.config import Settings
from ha_spark.energy.topology import effective_limits
from ha_spark.energy.topology_record import load_topology_record, select_limits

# With no usable limit the rule still rejects lifetime-sized spikes.
DEFAULT_PLAUSIBLE_KW = 30.0
PLAUSIBILITY_MARGIN = 1.25


def plausible_kw(settings: Settings) -> float:
    """One bound for every component: max(site import, site export) x 1.25, else 30 kW."""
    try:
        selection = select_limits(load_topology_record(settings), settings)
        if selection.source == "unavailable":
            return DEFAULT_PLAUSIBLE_KW
        if selection.actuation is None:
            # Legacy settings: the flat site limits read as one connection.
            import_kw = settings.supply_max_current_a * settings.supply_voltage_v / 1000
            export_kw = settings.dno_export_limit_kw
        else:
            connections = effective_limits(selection.actuation).connections
            import_kw = sum(c.import_kw for c in connections)
            export_kw = sum(c.site_export_kw for c in connections)
        bound = max(import_kw, export_kw) * PLAUSIBILITY_MARGIN
        return bound if math.isfinite(bound) and bound > 0 else DEFAULT_PLAUSIBLE_KW
    except Exception:  # noqa: BLE001 - any unreadable limit falls back to the fixed bound
        return DEFAULT_PLAUSIBLE_KW


def plausible(kwh: float, seconds: float, bound_kw: float) -> bool:
    """True when one interval's measured energy fits under the site bound."""
    if not (math.isfinite(kwh) and math.isfinite(seconds) and seconds > 0):
        return False
    return kwh <= bound_kw * seconds / 3600
