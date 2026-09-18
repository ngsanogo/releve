"""releve: a French electricity meter's history, read from a releve server.

releve (https://github.com/ngsanogo/releve) keeps the meter's complete history
in a local cache. This integration reads that cache over releve's JSON API and
shows it as sensors; the long-term statistics for the Energy dashboard stay the
job of releve's own home_assistant exporter.
"""

from __future__ import annotations

from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ReleveClient
from .coordinator import ReleveConfigEntry, ReleveCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ReleveConfigEntry) -> bool:
    client = ReleveClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data.get(CONF_TOKEN)
    )
    coordinator = ReleveCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ReleveConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
