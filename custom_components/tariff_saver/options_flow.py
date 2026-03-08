"""Options flow for Tariff Saver."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_BASELINE_ATTRIBUTE,
    CONF_BASELINE_ENTITY,
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_IGNORE_ZERO_PRICES,
    CONF_PRICE_ATTRIBUTE,
    CONF_PRICE_ENTITY,
    CONF_PRICE_SCALE,
    CONF_PUBLISH_TIME,
    CONF_SOURCE_TYPE,
    DEFAULT_BASELINE_ATTRIBUTE,
    DEFAULT_IGNORE_ZERO_PRICES,
    DEFAULT_PRICE_ATTRIBUTE,
    DEFAULT_PRICE_SCALE,
    DEFAULT_PUBLISH_TIME,
    SOURCE_EKZ,
    SOURCE_ENTITIES,
)


def _sensor_entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=selector.EntityFilterSelectorConfig(domain=["sensor"])
        )
    )


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Tariff Saver options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._pending: dict[str, object] = {}

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            self._pending = dict(self._entry.options)
            self._pending.update(user_input)
            return await self._next_step()

        opts = dict(self._entry.options)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SOURCE_TYPE,
                    default=opts.get(CONF_SOURCE_TYPE, SOURCE_EKZ),
                ): vol.In({SOURCE_EKZ: "EKZ provider integration", SOURCE_ENTITIES: "Existing entities"}),
                vol.Required(
                    CONF_CONSUMPTION_ENERGY_ENTITY,
                    default=opts.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
                ): _sensor_entity_selector(),
                vol.Required(
                    CONF_PUBLISH_TIME,
                    default=opts.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
                ): str,
                vol.Required(
                    CONF_PRICE_SCALE,
                    default=float(opts.get(CONF_PRICE_SCALE, DEFAULT_PRICE_SCALE)),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_IGNORE_ZERO_PRICES,
                    default=bool(opts.get(CONF_IGNORE_ZERO_PRICES, DEFAULT_IGNORE_ZERO_PRICES)),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_entities(self, user_input=None):
        if user_input is not None:
            self._pending.update(user_input)
            return await self._next_step()

        opts = {**self._entry.options, **self._pending}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PRICE_ENTITY,
                    default=opts.get(CONF_PRICE_ENTITY, ""),
                ): _sensor_entity_selector(),
                vol.Required(
                    CONF_PRICE_ATTRIBUTE,
                    default=opts.get(CONF_PRICE_ATTRIBUTE, DEFAULT_PRICE_ATTRIBUTE),
                ): str,
                vol.Optional(
                    CONF_BASELINE_ENTITY,
                    default=opts.get(CONF_BASELINE_ENTITY, ""),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_BASELINE_ATTRIBUTE,
                    default=opts.get(CONF_BASELINE_ATTRIBUTE, DEFAULT_BASELINE_ATTRIBUTE),
                ): str,
            }
        )
        return self.async_show_form(step_id="entities", data_schema=schema)

    async def _next_step(self):
        source_type = str(self._pending.get(CONF_SOURCE_TYPE, SOURCE_EKZ))

        if source_type == SOURCE_ENTITIES and CONF_PRICE_ENTITY not in self._pending:
            return await self.async_step_entities()

        if source_type != SOURCE_ENTITIES:
            self._pending.pop(CONF_PRICE_ENTITY, None)
            self._pending.pop(CONF_PRICE_ATTRIBUTE, None)
            self._pending.pop(CONF_BASELINE_ENTITY, None)
            self._pending.pop(CONF_BASELINE_ATTRIBUTE, None)

        return self.async_create_entry(title="", data=self._pending)
