
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_FEED_IN_FIXED_PRICE,
    CONF_FEED_IN_PRICE_ENTITY,
    CONF_FEED_IN_PRICE_MODE,
)

class TariffSaverOptionsFlow(OptionsFlow):
    """Handle options for Tariff Saver."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = dict(self.config_entry.options)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_FEED_IN_PRICE_MODE,
                    default=options.get(CONF_FEED_IN_PRICE_MODE, "fixed"),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["fixed", "entity"],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),

                # FIX: allow decimal values (float) instead of only integers
                vol.Optional(
                    CONF_FEED_IN_FIXED_PRICE,
                    default=options.get(CONF_FEED_IN_FIXED_PRICE, 0.0),
                ): vol.Coerce(float),

                vol.Optional(
                    CONF_FEED_IN_PRICE_ENTITY,
                    default=options.get(CONF_FEED_IN_PRICE_ENTITY),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
