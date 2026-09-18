"""Setting releve up from the UI, and giving it a new token."""

from __future__ import annotations

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.releve.const import DOMAIN
from tests_ha.conftest import URL, Serve


async def test_a_reachable_releve_becomes_an_entry(hass: HomeAssistant, releve_api: Serve) -> None:
    del releve_api
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert form["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_URL: "HTTP://Releve.test:8080/"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "releve.test:8080"
    assert result["data"] == {CONF_URL: URL}  # one spelling per server, no empty token
    assert result["result"].unique_id == URL


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"status": 401}, "invalid_auth"),
        ({"exc": aiohttp.ClientError()}, "cannot_connect"),
        ({"usage_points": []}, "no_usage_points"),
    ],
)
async def test_the_form_says_what_went_wrong_then_recovers(
    hass: HomeAssistant, releve_api: Serve, answer: dict[str, object], error: str
) -> None:
    releve_api(**answer)
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    failed = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_URL: URL, CONF_TOKEN: "s3cret"}
    )
    assert failed["type"] is FlowResultType.FORM
    assert failed["errors"] == {"base": error}

    releve_api()
    done = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_URL: URL, CONF_TOKEN: "s3cret"}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"] == {CONF_URL: URL, CONF_TOKEN: "s3cret"}


async def test_a_server_is_set_up_once(hass: HomeAssistant, releve_api: Serve) -> None:
    del releve_api
    MockConfigEntry(domain=DOMAIN, unique_id=URL, data={CONF_URL: URL}).add_to_hass(hass)
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_URL: URL})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_replaces_the_token(hass: HomeAssistant, releve_api: Serve) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={CONF_URL: URL, CONF_TOKEN: "old"})
    entry.add_to_hass(hass)
    form = await entry.start_reauth_flow(hass)
    assert form["step_id"] == "reauth_confirm"

    releve_api(status=401)
    refused = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_TOKEN: "bad"})
    assert refused["errors"] == {"base": "invalid_auth"}

    releve_api()
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_TOKEN: "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_URL: URL, CONF_TOKEN: "new"}
