"""Set up releve from the UI: the server's URL, and its token if it has one."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ReleveAuthError, ReleveClient, ReleveError
from .const import DEFAULT_URL, DOCS_URL, DOMAIN

_TOKEN = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_TOKEN): _TOKEN,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): _TOKEN})


def normalize_url(url: str) -> str:
    """One spelling per server: lower-case scheme and host, no trailing slash."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


class ReleveConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = normalize_url(user_input[CONF_URL])
            token = user_input.get(CONF_TOKEN) or None
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            errors = await self._check(url, token)
            if not errors:
                data = {CONF_URL: url} | ({CONF_TOKEN: token} if token else {})
                return self.async_create_entry(title=urlsplit(url).netloc, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            description_placeholders={"docs_url": DOCS_URL, "example": DEFAULT_URL},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            errors = await self._check(entry.data[CONF_URL], user_input[CONF_TOKEN])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    async def _check(self, url: str, token: str | None) -> dict[str, str]:
        """Errors keyed for the form, or {} when releve answers with usage points."""
        client = ReleveClient(async_get_clientsession(self.hass), url, token)
        try:
            usage_points = await client.usage_points()
        except ReleveAuthError:
            return {"base": "invalid_auth"}
        except ReleveError:
            return {"base": "cannot_connect"}
        if not usage_points:
            return {"base": "no_usage_points"}
        return {}
