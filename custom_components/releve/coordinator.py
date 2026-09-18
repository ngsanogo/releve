"""One poll of releve's API for every entity of a config entry."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import JsonObject, ReleveAuthError, ReleveClient, ReleveError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

type ReleveConfigEntry = ConfigEntry[ReleveCoordinator]


@dataclass(frozen=True, slots=True)
class Meter:
    """One usage point: what releve says about it, and its published state."""

    info: JsonObject
    state: JsonObject


@dataclass(frozen=True, slots=True)
class ReleveData:
    meters: dict[str, Meter]
    grid: JsonObject | None
    version: str | None


class ReleveCoordinator(DataUpdateCoordinator[ReleveData]):
    """Reads the usage points, their state and the grid signals together."""

    config_entry: ReleveConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ReleveConfigEntry, client: ReleveClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> ReleveData:
        try:
            meters = {
                info["id"]: Meter(info, await self.client.state(info["id"]))
                for info in await self.client.usage_points()
            }
            grid = await self.client.grid_state()
            version = await self.client.version()
        except ReleveAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ReleveError as err:
            raise UpdateFailed(str(err)) from err
        return ReleveData(meters=meters, grid=grid, version=version)
