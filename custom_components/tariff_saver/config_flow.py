"""Config flow for Tariff Saver."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.const import CONF_NAME
from homeassistant.core import callback

from .const import (
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_EKZ_ENTRY_ID,
    CONF_FEED_IN_FIXED_PRICE,
    CONF_FEED_IN_PRICE_ENTITY,
    CONF_FEED_IN_PRICE_MODE,
    CONF_PUBLISH_TIME,
    CONF_PV_FORECAST_ATTRIBUTE,
    CONF_PV_FORECAST_ENTITY,
    DEFAULT_FEED_IN_FIXED_PRICE,
    DEFAULT_FEED_IN_PRICE_MODE,
    DEFAULT_PUBLISH_TIME,
    DEFAULT_PV_FORECAST_ATTRIBUTE,
    DOMAIN,
)


def _sensor_entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=selector.EntityFilterSelectorConfig(domain=["sensor"])
        )
    )


def _feed_in_price_mode_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["fixed", "entity"],
            translation_key="feed_in_price_mode",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    VERSION = 4

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
                    CONF_PV_FORECAST_ENTITY: user_input.get(CONF_PV_FORECAST_ENTITY, ""),
                    CONF_PV_FORECAST_ATTRIBUTE: user_input.get(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE),
                    CONF_FEED_IN_PRICE_MODE: user_input.get(CONF_FEED_IN_PRICE_MODE, DEFAULT_FEED_IN_PRICE_MODE),
                    CONF_FEED_IN_FIXED_PRICE: user_input.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE),
                    CONF_FEED_IN_PRICE_ENTITY: user_input.get(CONF_FEED_IN_PRICE_ENTITY, ""),
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Optional(CONF_CONSUMPTION_ENERGY_ENTITY, default=""): _sensor_entity_selector(),
                vol.Optional(CONF_EKZ_ENTRY_ID, default=""): str,
                vol.Optional(CONF_PV_FORECAST_ENTITY, default=""): _sensor_entity_selector(),
                vol.Optional(CONF_PV_FORECAST_ATTRIBUTE, default=DEFAULT_PV_FORECAST_ATTRIBUTE): str,
                vol.Optional(CONF_FEED_IN_PRICE_MODE, default=DEFAULT_FEED_IN_PRICE_MODE): _feed_in_price_mode_selector(),
                vol.Optional(CONF_FEED_IN_FIXED_PRICE, default=DEFAULT_FEED_IN_FIXED_PRICE): vol.Coerce(float),
                vol.Optional(CONF_FEED_IN_PRICE_ENTITY, default=""): _sensor_entity_selector(),
                vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import TariffSaverOptionsFlowHandler

        return TariffSaverOptionsFlowHandler(config_entry)
