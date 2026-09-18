"""Diagnostics: releve's answers as the integration sees them, token and PDLs left out."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from .coordinator import ReleveConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ReleveConfigEntry
) -> dict[str, Any]:
    del hass
    data = entry.runtime_data.data
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_TOKEN}),
        "releve_version": data.version,
        # A PDL identifies a home: meters are listed, not keyed by it.
        "meters": [
            {"datasets": meter.info.get("datasets"), "state": meter.state}
            for meter in data.meters.values()
        ],
        "grid": data.grid,
    }
