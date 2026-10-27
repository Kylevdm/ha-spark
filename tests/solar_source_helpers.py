"""Shared builder for entities-kind solar source contexts in tests (#519).

The plan instant is read through ``sources.datetime`` so a test that freezes
that clock — the established freeze seam for planning — gets the same frozen
instant in the context that ``gather_inputs`` used to read itself.
"""

from __future__ import annotations

from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.solar_source import SolarSource


def entities_source(settings: Settings) -> SolarSource:
    """An entities-kind context at the (possibly frozen) current instant."""
    return SolarSource(
        kind="entities",
        forecast=None,
        received_at=None,
        now=sources.datetime.now(load_timezone(settings.timezone)),
    )
