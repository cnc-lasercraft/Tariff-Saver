"""Config flow for Tariff Saver."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow as HAConfigFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback

from .const import DOMAIN


class ConfigFlow(HAConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    VERSION = 3

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={CONF_NAME: user_input[CONF_NAME]},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default="Tariff Saver"): str}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import TariffSaverOptionsFlowHandler

        return TariffSaverOptionsFlowHandler(config_entry)
