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
    DEFAULT_FEED_IN_FIXED_PRICE,
    DEFAULT_FEED_IN_PRICE_MODE,
    DEFAULT_PUBLISH_TIME,
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
        errors: dict[str, str] = {}

        if user_input is not None:
            fixed_raw = str(user_input.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE) or "").strip()
            entity_raw = str(user_input.get(CONF_FEED_IN_PRICE_ENTITY, "") or "").strip()
            mode = str(user_input.get(CONF_FEED_IN_PRICE_MODE, DEFAULT_FEED_IN_PRICE_MODE) or DEFAULT_FEED_IN_PRICE_MODE)

            try:
                fixed_price = float(fixed_raw.replace(',', '.')) if fixed_raw != '' else float(DEFAULT_FEED_IN_FIXED_PRICE)
            except ValueError:
                errors[CONF_FEED_IN_FIXED_PRICE] = "invalid_number"
                fixed_price = float(DEFAULT_FEED_IN_FIXED_PRICE)

            if mode == "entity" and not entity_raw:
                errors[CONF_FEED_IN_PRICE_ENTITY] = "required"

            if not errors:
                await self.async_set_unique_id(str(user_input[CONF_NAME]).strip())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=str(user_input[CONF_NAME]).strip(),
                    data={
                        CONF_NAME: str(user_input[CONF_NAME]).strip(),
                        CONF_PUBLISH_TIME: str(user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME) or DEFAULT_PUBLISH_TIME).strip(),
                        CONF_CONSUMPTION_ENERGY_ENTITY: user_input.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
                        CONF_EKZ_ENTRY_ID: str(user_input.get(CONF_EKZ_ENTRY_ID, "") or "").strip(),
                        CONF_FEED_IN_PRICE_MODE: mode,
                        CONF_FEED_IN_FIXED_PRICE: fixed_price,
                        CONF_FEED_IN_PRICE_ENTITY: entity_raw,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=(user_input or {}).get(CONF_NAME, "Tariff Saver")): str,
                vol.Optional(CONF_CONSUMPTION_ENERGY_ENTITY, default=(user_input or {}).get(CONF_CONSUMPTION_ENERGY_ENTITY, "")): _sensor_entity_selector(),
                vol.Optional(CONF_EKZ_ENTRY_ID, default=(user_input or {}).get(CONF_EKZ_ENTRY_ID, "")): str,
                vol.Optional(CONF_FEED_IN_PRICE_MODE, default=(user_input or {}).get(CONF_FEED_IN_PRICE_MODE, DEFAULT_FEED_IN_PRICE_MODE)): _feed_in_price_mode_selector(),
                vol.Optional(CONF_FEED_IN_FIXED_PRICE, default=str((user_input or {}).get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE))): str,
                vol.Optional(CONF_FEED_IN_PRICE_ENTITY, default=(user_input or {}).get(CONF_FEED_IN_PRICE_ENTITY, "")): str,
                vol.Optional(CONF_PUBLISH_TIME, default=(user_input or {}).get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import TariffSaverOptionsFlowHandler

        return TariffSaverOptionsFlowHandler(config_entry)
