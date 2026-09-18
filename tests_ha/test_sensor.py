"""The sensors: what releve publishes, and what happens when it cannot answer."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_URL, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.releve.const import DOMAIN
from custom_components.releve.diagnostics import async_get_config_entry_diagnostics
from tests_ha.conftest import PDL, URL, Serve


async def set_up(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={CONF_URL: URL})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def state_of(hass: HomeAssistant, unique_id: str) -> str | None:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} is registered but has no state (disabled?)"
    return state.state


async def test_sensors_follow_what_releve_publishes(hass: HomeAssistant, releve_api: Serve) -> None:
    del releve_api
    entry = await set_up(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert state_of(hass, f"{PDL}_energy_yesterday_kwh") == "6.028"
    assert state_of(hass, f"{PDL}_energy_last_7_days_kwh") == STATE_UNKNOWN  # null, not zero
    assert state_of(hass, f"{PDL}_max_power_yesterday_va") == "6100"
    assert state_of(hass, f"{PDL}_latest_day") == "2026-09-11"
    assert state_of(hass, f"{PDL}_last_success") == "2026-09-12T10:00:00+00:00"
    assert state_of(hass, f"{PDL}_production_yesterday_kwh") is None  # not configured: no entity
    grid = f"{entry.entry_id}_grid"
    assert state_of(hass, f"{grid}_tempo_today") == "white"
    assert state_of(hass, f"{grid}_ecowatt_today") == "orange"
    assert state_of(hass, f"{grid}_tempo_days_left_red") == STATE_UNKNOWN

    meter = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, PDL), entry.entry_id)
    assert meter is not None
    assert (meter.name, meter.manufacturer, meter.serial_number) == (f"Linky {PDL}", "Enedis", PDL)
    assert meter.configuration_url == f"{URL}/usage-points/{PDL}"


async def test_no_grid_device_when_releve_has_grid_signals_off(
    hass: HomeAssistant, releve_api: Serve
) -> None:
    releve_api(grid=None)
    entry = await set_up(hass)
    assert state_of(hass, f"{PDL}_energy_yesterday_kwh") == "6.028"
    assert state_of(hass, f"{entry.entry_id}_grid_tempo_today") is None


async def test_an_unreachable_releve_is_retried_then_goes_unavailable(
    hass: HomeAssistant, releve_api: Serve
) -> None:
    releve_api(exc=aiohttp.ClientError())
    entry = await set_up(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    releve_api()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, f"{PDL}_energy_yesterday_kwh") == "6.028"

    releve_api(exc=aiohttp.ClientError())
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, f"{PDL}_energy_yesterday_kwh") == STATE_UNAVAILABLE


async def test_a_refused_token_asks_for_a_new_one(hass: HomeAssistant, releve_api: Serve) -> None:
    entry = await set_up(hass)
    releve_api(status=401)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_unload(hass: HomeAssistant, releve_api: Serve) -> None:
    del releve_api
    entry = await set_up(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_leave_out_the_token_and_the_pdl(
    hass: HomeAssistant, releve_api: Serve
) -> None:
    del releve_api
    entry = await set_up(hass)
    hass.config_entries.async_update_entry(entry, data={CONF_URL: URL, "token": "s3cret"})
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["token"] == "**REDACTED**"
    assert diagnostics["releve_version"] == "0.2.0"
    assert diagnostics["meters"][0]["state"]["energy_yesterday_kwh"] == 6.028
    assert PDL not in repr(diagnostics)
