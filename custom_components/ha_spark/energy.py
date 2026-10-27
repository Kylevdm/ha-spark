"""Solar forecast platform for Home Assistant's Energy dashboard.

Only cached P50 half-hours feed the Energy dashboard; an hour missing a half
is left out rather than shown partly filled. Keys are ISO 8601 UTC hour
starts, matching the Energy platform contract.
"""

from datetime import datetime

from homeassistant.components.energy.types import SolarForecastType
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str,
) -> SolarForecastType | None:
    """Return cached P50 energy per complete UTC hour, or no forecast."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if (
        entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED
        or (coordinator := getattr(entry, "runtime_data", None)) is None
    ):
        return None
    solcast = coordinator.solcast
    if solcast is None or not solcast.data.periods:
        return None
    halves: dict[datetime, list[float]] = {}
    for period in solcast.data.periods:
        halves.setdefault(period.start.replace(minute=0), []).append(period.p50)
    return {"wh_hours": {
        hour.isoformat(): round(sum(values) * 500, 3)
        for hour, values in sorted(halves.items()) if len(values) == 2
    }}
