"""Options flow for Tariff Saver."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

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


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Tariff Saver."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            merged = dict(self._entry.options)
            merged.update(user_input)
            return self.async_create_entry(title="", data=merged)

        opts = dict(self._entry.options)
        data = dict(self._entry.data)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CONSUMPTION_ENERGY_ENTITY,
                    default=opts.get(CONF_CONSUMPTION_ENERGY_ENTITY, data.get(CONF_CONSUMPTION_ENERGY_ENTITY, "")),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_EKZ_ENTRY_ID,
                    default=opts.get(CONF_EKZ_ENTRY_ID, data.get(CONF_EKZ_ENTRY_ID, "")),
                ): str,
                vol.Optional(
                    CONF_PV_FORECAST_ENTITY,
                    default=opts.get(CONF_PV_FORECAST_ENTITY, data.get(CONF_PV_FORECAST_ENTITY, "")),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_PV_FORECAST_ATTRIBUTE,
                    default=opts.get(CONF_PV_FORECAST_ATTRIBUTE, data.get(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE)),
                ): str,
                vol.Optional(
                    CONF_FEED_IN_PRICE_MODE,
                    default=opts.get(CONF_FEED_IN_PRICE_MODE, data.get(CONF_FEED_IN_PRICE_MODE, DEFAULT_FEED_IN_PRICE_MODE)),
                ): _feed_in_price_mode_selector(),
                vol.Optional(
                    CONF_FEED_IN_FIXED_PRICE,
                    default=opts.get(CONF_FEED_IN_FIXED_PRICE, data.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE)),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_FEED_IN_PRICE_ENTITY,
                    default=opts.get(CONF_FEED_IN_PRICE_ENTITY, data.get(CONF_FEED_IN_PRICE_ENTITY, "")),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_PUBLISH_TIME,
                    default=opts.get(CONF_PUBLISH_TIME, data.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
