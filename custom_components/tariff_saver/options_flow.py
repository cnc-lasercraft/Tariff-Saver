"""Options flow for Tariff Saver."""
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
            cleaned = dict(user_input)

            cleaned[CONF_EKZ_ENTRY_ID] = str(
                cleaned.get(CONF_EKZ_ENTRY_ID, "") or ""
            ).strip()
            cleaned[CONF_PV_FORECAST_ATTRIBUTE] = str(
                cleaned.get(CONF_PV_FORECAST_ATTRIBUTE, "detailedForecast") or "detailedForecast"
            ).strip()
            cleaned[CONF_PUBLISH_TIME] = str(
                cleaned.get(CONF_PUBLISH_TIME, "18:15") or "18:15"
            ).strip()
            cleaned[CONF_FEED_IN_PRICE_MODE] = str(
                cleaned.get(CONF_FEED_IN_PRICE_MODE, "fixed") or "fixed"
            ).strip()
            cleaned[CONF_FEED_IN_PRICE_ENTITY] = str(
                cleaned.get(CONF_FEED_IN_PRICE_ENTITY, "") or ""
            ).strip()

            raw_fixed = str(cleaned.get(CONF_FEED_IN_FIXED_PRICE, "") or "").strip()

            # Accept decimals with dot OR comma.
            if raw_fixed == "":
                cleaned[CONF_FEED_IN_FIXED_PRICE] = 0.0
            else:
                try:
                    cleaned[CONF_FEED_IN_FIXED_PRICE] = float(raw_fixed.replace(",", "."))
                except ValueError:
                    errors[CONF_FEED_IN_FIXED_PRICE] = "invalid_number"

            # Only require an entity when entity mode is selected.
            if (
                cleaned.get(CONF_FEED_IN_PRICE_MODE) == "entity"
                and not cleaned.get(CONF_FEED_IN_PRICE_ENTITY)
            ):
                errors[CONF_FEED_IN_PRICE_ENTITY] = "required"

            if not errors:
                return self.async_create_entry(title="", data=cleaned)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CONSUMPTION_ENERGY_ENTITY,
                    default=options.get(CONF_CONSUMPTION_ENERGY_ENTITY),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    CONF_EKZ_ENTRY_ID,
                    default=options.get(CONF_EKZ_ENTRY_ID, ""),
                ): TextSelector(),
                vol.Optional(
                    CONF_PV_FORECAST_ENTITY,
                    default=options.get(CONF_PV_FORECAST_ENTITY),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    CONF_PV_FORECAST_ATTRIBUTE,
                    default=options.get(CONF_PV_FORECAST_ATTRIBUTE, "detailedForecast"),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Required(
                    CONF_FEED_IN_PRICE_MODE,
                    default=options.get(CONF_FEED_IN_PRICE_MODE, "fixed"),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["fixed", "entity"],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                # Text field on purpose: this avoids integer-only browser widgets.
                vol.Optional(
                    CONF_FEED_IN_FIXED_PRICE,
                    default=str(options.get(CONF_FEED_IN_FIXED_PRICE, "0.0")),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                # Text field on purpose: empty value must be allowed.
                vol.Optional(
                    CONF_FEED_IN_PRICE_ENTITY,
                    default=options.get(CONF_FEED_IN_PRICE_ENTITY, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    CONF_PUBLISH_TIME,
                    default=options.get(CONF_PUBLISH_TIME, "18:15"),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
