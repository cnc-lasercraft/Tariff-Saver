"""Options flow for Tariff Saver."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
)

from .const import DOMAIN

CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Tariff Saver options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY, "")

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CONSUMPTION_ENERGY_ENTITY,
                    default=current,
                ): EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor"],
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )


@callback
def async_get_options_flow(config_entry: config_entries.ConfigEntry):
    return TariffSaverOptionsFlowHandler(config_entry)
