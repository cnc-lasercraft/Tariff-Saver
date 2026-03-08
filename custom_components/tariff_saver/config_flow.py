"""Config flow for Tariff Saver."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import DOMAIN


class TariffSaverConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ):
        if user_input is not None:
            name = str(user_input.get("name", "Tariff Saver") or "Tariff Saver").strip()
            if not name:
                name = "Tariff Saver"
            return self.async_create_entry(
                title=name,
                data={"name": name},
            )

        schema = vol.Schema(
            {
                vol.Required("name", default="Tariff Saver"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import TariffSaverOptionsFlow

        return TariffSaverOptionsFlow(config_entry)
