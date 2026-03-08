"""Options flow for Tariff Saver."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_EKZ_ENTRY_ID,
    CONF_FEED_IN_FIXED_PRICE,
    CONF_FEED_IN_PRICE_ENTITY,
    CONF_FEED_IN_PRICE_MODE,
    CONF_PUBLISH_TIME,
    CONF_PV_FORECAST_ATTRIBUTE,
    CONF_PV_FORECAST_ENTITY,
    SOURCE_EKZ,
)


class TariffSaverOptionsFlow(OptionsFlow):
    """Handle options for Tariff Saver."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)

        if user_input is not None:
            mode = user_input.get(CONF_FEED_IN_PRICE_MODE, "fixed")
            feed_in_entity = str(user_input.get(CONF_FEED_IN_PRICE_ENTITY, "") or "").strip()

            # Empty entity is allowed unless entity mode is selected.
            if mode == "entity" and not feed_in_entity:
                errors[CONF_FEED_IN_PRICE_ENTITY] = "required"

            if not errors:
                cleaned = dict(user_input)
                cleaned[CONF_EKZ_ENTRY_ID] = str(cleaned.get(CONF_EKZ_ENTRY_ID, "") or "").strip()
                cleaned[CONF_PV_FORECAST_ATTRIBUTE] = str(
                    cleaned.get(CONF_PV_FORECAST_ATTRIBUTE, "") or ""
                ).strip()
                cleaned[CONF_FEED_IN_PRICE_ENTITY] = feed_in_entity
                cleaned[CONF_PUBLISH_TIME] = str(
                    cleaned.get(CONF_PUBLISH_TIME, "18:15") or "18:15"
                ).strip()
                return self.async_create_entry(title="", data=cleaned)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CONSUMPTION_ENERGY_ENTITY,
                    default=options.get(CONF_CONSUMPTION_ENERGY_ENTITY),
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    CONF_EKZ_ENTRY_ID,
                    default=options.get(CONF_EKZ_ENTRY_ID, ""),
                ): TextSelector(),
                vol.Optional(
                    CONF_PV_FORECAST_ENTITY,
                    default=options.get(CONF_PV_FORECAST_ENTITY),
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    CONF_PV_FORECAST_ATTRIBUTE,
                    default=options.get(CONF_PV_FORECAST_ATTRIBUTE, "detailedForecast"),
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(
                    CONF_FEED_IN_PRICE_MODE,
                    default=options.get(CONF_FEED_IN_PRICE_MODE, "fixed"),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["fixed", "entity"],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_FEED_IN_FIXED_PRICE,
                    default=float(options.get(CONF_FEED_IN_FIXED_PRICE, 0.0) or 0.0),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=-10.0,
                        max=10.0,
                        step=0.001,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                # Use TextSelector on purpose so an empty value is accepted.
                # Validation is handled manually above only when mode == "entity".
                vol.Optional(
                    CONF_FEED_IN_PRICE_ENTITY,
                    default=options.get(CONF_FEED_IN_PRICE_ENTITY, ""),
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(
                    CONF_PUBLISH_TIME,
                    default=options.get(CONF_PUBLISH_TIME, "18:15"),
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
