"""Config flow for Tariff Saver."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries, selector
from homeassistant.const import CONF_NAME
from homeassistant.core import callback

from .const import (
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_EKZ_ENTRY_ID,
    CONF_PUBLISH_TIME,
    DEFAULT_PUBLISH_TIME,
    DOMAIN,
)


def _sensor_entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=selector.EntityFilterSelectorConfig(domain=["sensor"])
        )
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    VERSION = 3

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_NAME])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_PUBLISH_TIME: user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
                    CONF_CONSUMPTION_ENERGY_ENTITY: user_input.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
                    CONF_EKZ_ENTRY_ID: user_input.get(CONF_EKZ_ENTRY_ID, ""),
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Optional(CONF_CONSUMPTION_ENERGY_ENTITY, default=""): _sensor_entity_selector(),
                vol.Optional(CONF_EKZ_ENTRY_ID, default=""): str,
                vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import TariffSaverOptionsFlowHandler

        return TariffSaverOptionsFlowHandler(config_entry)
