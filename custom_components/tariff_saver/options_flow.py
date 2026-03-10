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
    DEFAULT_FEED_IN_FIXED_PRICE,
    DEFAULT_FEED_IN_PRICE_MODE,
    DEFAULT_PUBLISH_TIME,
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
        errors: dict[str, str] = {}
        opts = dict(self._entry.options)
        data = dict(self._entry.data)

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
                merged = dict(self._entry.options)
                merged.update(user_input)
                merged[CONF_EKZ_ENTRY_ID] = str(user_input.get(CONF_EKZ_ENTRY_ID, "") or "").strip()
                merged[CONF_FEED_IN_PRICE_MODE] = mode
                merged[CONF_FEED_IN_FIXED_PRICE] = fixed_price
                merged[CONF_FEED_IN_PRICE_ENTITY] = entity_raw
                merged[CONF_PUBLISH_TIME] = str(user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME) or DEFAULT_PUBLISH_TIME).strip()
                return self.async_create_entry(title="", data=merged)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CONSUMPTION_ENERGY_ENTITY,
                    default=(user_input or {}).get(CONF_CONSUMPTION_ENERGY_ENTITY, opts.get(CONF_CONSUMPTION_ENERGY_ENTITY, data.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""))),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_EKZ_ENTRY_ID,
                    default=(user_input or {}).get(CONF_EKZ_ENTRY_ID, opts.get(CONF_EKZ_ENTRY_ID, data.get(CONF_EKZ_ENTRY_ID, ""))),
                ): str,
                vol.Optional(
                    CONF_FEED_IN_PRICE_MODE,
                    default=(user_input or {}).get(CONF_FEED_IN_PRICE_MODE, opts.get(CONF_FEED_IN_PRICE_MODE, data.get(CONF_FEED_IN_PRICE_MODE, DEFAULT_FEED_IN_PRICE_MODE))),
                ): _feed_in_price_mode_selector(),
                vol.Optional(
                    CONF_FEED_IN_FIXED_PRICE,
                    default=str((user_input or {}).get(CONF_FEED_IN_FIXED_PRICE, opts.get(CONF_FEED_IN_FIXED_PRICE, data.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE)))),
                ): str,
                vol.Optional(
                    CONF_FEED_IN_PRICE_ENTITY,
                    default=(user_input or {}).get(CONF_FEED_IN_PRICE_ENTITY, opts.get(CONF_FEED_IN_PRICE_ENTITY, data.get(CONF_FEED_IN_PRICE_ENTITY, ""))),
                ): str,
                vol.Optional(
                    CONF_PUBLISH_TIME,
                    default=(user_input or {}).get(CONF_PUBLISH_TIME, opts.get(CONF_PUBLISH_TIME, data.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME))),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
