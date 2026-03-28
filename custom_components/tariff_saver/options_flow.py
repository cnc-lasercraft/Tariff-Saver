
"""Options flow for Tariff Saver."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CONSUMERS,
    CONF_CONSUMER_MAX_DAYS,
    CONF_CONSUMER_MIN_DAYS,
    CONF_BATTERY_SOC_MARGIN,
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_SEASON_ENTITY,
    CONF_EKZ_ENTRY_ID,
    CONF_FEED_IN_FIXED_PRICE,
    CONF_FEED_IN_PRICE_ENTITY,
    CONF_FEED_IN_PRICE_MODE,
    CONF_PUBLISH_TIME,
    CONF_PV_FORECAST_ATTRIBUTE,
    CONF_PV_FORECAST_ENTITY,
    CONSUMER_COUNT,
    CONSUMER_MODE_AUTO,
    CONSUMER_MODES,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_CONSUMER10_MAX_DAYS,
    DEFAULT_CONSUMER10_MIN_DAYS,
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


def _consumer_mode_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(CONSUMER_MODES),
            translation_key="consumer_mode",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _consumer_defaults(slot: int, source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = source or {}
    return {
        "enabled": bool(source.get("enabled", False)),
        "name": str(source.get("name", "") or ""),
        "mode": str(source.get("mode", CONSUMER_MODE_AUTO) or CONSUMER_MODE_AUTO),
        "power_kw": float(source.get("power_kw", 0.0) or 0.0),
        "duration_minutes": int(source.get("duration_minutes", 0) or 0),
        "energy_kwh": float(source.get("energy_kwh", 0.0) or 0.0),
        "measurement_entity": str(source.get("measurement_entity", "") or ""),
        "priority": max(1, min(10, int(source.get("priority", 5) or 5))),
        "pv_required": bool(source.get("pv_required", False)),
        "learning_enabled": bool(source.get("learning_enabled", True)),
        "min_days": int(source.get("min_days", DEFAULT_CONSUMER10_MIN_DAYS if slot == CONSUMER_COUNT else 0) or (DEFAULT_CONSUMER10_MIN_DAYS if slot == CONSUMER_COUNT else 0)),
        "max_days": int(source.get("max_days", DEFAULT_CONSUMER10_MAX_DAYS if slot == CONSUMER_COUNT else 0) or (DEFAULT_CONSUMER10_MAX_DAYS if slot == CONSUMER_COUNT else 0)),
        "max_grid_score": max(0, min(100, int(source.get("max_grid_score", 100) or 100))),
        "tariff_only": bool(source.get("tariff_only", False)),
        "slot": slot,
    }


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Tariff Saver."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._selected_consumer = 1

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            next_step = user_input.get("next_step", "general")
            if next_step == "consumer":
                return await self.async_step_consumer_select()
            return await self.async_step_general()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("next_step", default="general"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["general", "consumer"],
                            translation_key="options_menu",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_general(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        opts = dict(self._entry.options)
        data = dict(self._entry.data)

        if user_input is not None:
            fixed_raw = str(user_input.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE) or "").strip()
            entity_raw = str(user_input.get(CONF_FEED_IN_PRICE_ENTITY, "") or "").strip()
            mode = str(user_input.get(CONF_FEED_IN_PRICE_MODE, DEFAULT_FEED_IN_PRICE_MODE) or DEFAULT_FEED_IN_PRICE_MODE)

            try:
                fixed_price = float(fixed_raw.replace(",", ".")) if fixed_raw != "" else float(DEFAULT_FEED_IN_FIXED_PRICE)
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
                merged[CONF_PUBLISH_TIME] = str(
                    user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME) or DEFAULT_PUBLISH_TIME
                ).strip()
                merged[CONF_SEASON_ENTITY] = str(user_input.get(CONF_SEASON_ENTITY, "") or "").strip()
                merged[CONF_CONSUMERS] = dict(opts.get(CONF_CONSUMERS, {}))
                return self.async_create_entry(title="", data=merged)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_BATTERY_SOC_MARGIN,
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_SEASON_ENTITY,
                    default=(user_input or {}).get(
                        CONF_BATTERY_SOC_MARGIN,
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_SEASON_ENTITY,
                        opts.get(CONF_CONSUMPTION_ENERGY_ENTITY, data.get(CONF_CONSUMPTION_ENERGY_ENTITY, "")),
                    ),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_SEASON_ENTITY,
                    default=(user_input or {}).get(
                        CONF_SEASON_ENTITY,
                        opts.get(CONF_SEASON_ENTITY, data.get(CONF_SEASON_ENTITY, "")),
                    ),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_EKZ_ENTRY_ID,
                    default=(user_input or {}).get(
                        CONF_EKZ_ENTRY_ID, opts.get(CONF_EKZ_ENTRY_ID, data.get(CONF_EKZ_ENTRY_ID, ""))
                    ),
                ): str,
                vol.Optional(
                    CONF_FEED_IN_PRICE_MODE,
                    default=(user_input or {}).get(
                        CONF_FEED_IN_PRICE_MODE,
                        opts.get(CONF_FEED_IN_PRICE_MODE, data.get(CONF_FEED_IN_PRICE_MODE, DEFAULT_FEED_IN_PRICE_MODE)),
                    ),
                ): _feed_in_price_mode_selector(),
                vol.Optional(
                    CONF_FEED_IN_FIXED_PRICE,
                    default=str(
                        (user_input or {}).get(
                            CONF_FEED_IN_FIXED_PRICE,
                            opts.get(CONF_FEED_IN_FIXED_PRICE, data.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE)),
                        )
                    ),
                ): str,
                vol.Optional(
                    CONF_FEED_IN_PRICE_ENTITY,
                    default=(user_input or {}).get(
                        CONF_FEED_IN_PRICE_ENTITY, opts.get(CONF_FEED_IN_PRICE_ENTITY, data.get(CONF_FEED_IN_PRICE_ENTITY, ""))
                    ),
                ): str,
                vol.Optional(
                    CONF_PV_FORECAST_ENTITY,
                    default=(user_input or {}).get(
                        CONF_PV_FORECAST_ENTITY, opts.get(CONF_PV_FORECAST_ENTITY, data.get(CONF_PV_FORECAST_ENTITY, ""))
                    ),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_PV_FORECAST_ATTRIBUTE,
                    default=(user_input or {}).get(
                        CONF_PV_FORECAST_ATTRIBUTE, opts.get(CONF_PV_FORECAST_ATTRIBUTE, data.get(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE))
                    ),
                ): str,
                vol.Optional(
                    CONF_BATTERY_ENABLED,
                    default=(user_input or {}).get(
                        CONF_BATTERY_ENABLED, opts.get(CONF_BATTERY_ENABLED, data.get(CONF_BATTERY_ENABLED, False))
                    ),
                ): bool,
                vol.Optional(
                    CONF_BATTERY_SOC_ENTITY,
                    default=(user_input or {}).get(
                        CONF_BATTERY_SOC_ENTITY, opts.get(CONF_BATTERY_SOC_ENTITY, data.get(CONF_BATTERY_SOC_ENTITY, ""))
                    ),
                ): _sensor_entity_selector(),
                vol.Optional(
                    CONF_BATTERY_CAPACITY_KWH,
                    default=float((user_input or {}).get(
                        CONF_BATTERY_CAPACITY_KWH, opts.get(CONF_BATTERY_CAPACITY_KWH, data.get(CONF_BATTERY_CAPACITY_KWH, 0.0))
                    )),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_BATTERY_MIN_SOC_PERCENT,
                    default=float((user_input or {}).get(
                        CONF_BATTERY_MIN_SOC_PERCENT, opts.get(CONF_BATTERY_MIN_SOC_PERCENT, data.get(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT))
                    )),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_PUBLISH_TIME,
                    default=(user_input or {}).get(
                        CONF_PUBLISH_TIME, opts.get(CONF_PUBLISH_TIME, data.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME))
                    ),
                ): str,
            }
        )
        return self.async_show_form(step_id="general", data_schema=schema, errors=errors)

    async def async_step_consumer_select(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._selected_consumer = int(user_input.get("consumer_slot", 1))
            return await self.async_step_consumer_edit()

        return self.async_show_form(
            step_id="consumer_select",
            data_schema=vol.Schema(
                {
                    vol.Required("consumer_slot", default=self._selected_consumer): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=CONSUMER_COUNT, step=1, mode=selector.NumberSelectorMode.BOX)
                    )
                }
            ),
        )

    async def async_step_consumer_edit(self, user_input: dict[str, Any] | None = None):
        opts = dict(self._entry.options)
        consumers = dict(opts.get(CONF_CONSUMERS, {}))
        key = str(self._selected_consumer)
        current = _consumer_defaults(
            self._selected_consumer,
            consumers.get(key) if isinstance(consumers.get(key), dict) else {},
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            updated = _consumer_defaults(self._selected_consumer, user_input)

            if updated["priority"] < 1 or updated["priority"] > 10:
                errors["priority"] = "invalid_number"

            if updated["measurement_entity"] and "." not in updated["measurement_entity"]:
                errors["measurement_entity"] = "invalid_entity"

            if self._selected_consumer == CONSUMER_COUNT:
                if updated["min_days"] < 0 or updated["max_days"] < 0:
                    errors[CONF_CONSUMER_MIN_DAYS] = "invalid_number"
                elif updated["max_days"] < updated["min_days"]:
                    errors[CONF_CONSUMER_MAX_DAYS] = "invalid_number"

            if not errors:
                consumers[key] = updated
                merged = dict(self._entry.options)
                merged[CONF_CONSUMERS] = consumers
                return self.async_create_entry(title="", data=merged)

        schema = vol.Schema(
            {
                vol.Optional("enabled", default=(user_input or {}).get("enabled", current["enabled"])): bool,
                vol.Optional("name", default=(user_input or {}).get("name", current["name"])): str,
                vol.Optional("mode", default=(user_input or {}).get("mode", current["mode"])): _consumer_mode_selector(),
                vol.Optional("power_kw", default=float((user_input or {}).get("power_kw", current["power_kw"]))): vol.Coerce(float),
                vol.Optional("duration_minutes", default=int((user_input or {}).get("duration_minutes", current["duration_minutes"]))): vol.Coerce(int),
                vol.Optional("energy_kwh", default=float((user_input or {}).get("energy_kwh", current["energy_kwh"]))): vol.Coerce(float),
                vol.Optional("measurement_entity", default=(user_input or {}).get("measurement_entity", current["measurement_entity"])): _sensor_entity_selector(),
                vol.Optional("priority", default=int((user_input or {}).get("priority", current["priority"]))): vol.Coerce(int),
                vol.Optional("pv_required", default=(user_input or {}).get("pv_required", current["pv_required"])): bool,
                vol.Optional("learning_enabled", default=(user_input or {}).get("learning_enabled", current["learning_enabled"])): bool,
                vol.Optional("tariff_only", default=(user_input or {}).get("tariff_only", current.get("tariff_only", False))): bool,
                vol.Optional("max_grid_score", default=int((user_input or {}).get("max_grid_score", current.get("max_grid_score", 100)))): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                **({
                    vol.Optional(CONF_CONSUMER_MIN_DAYS, default=int((user_input or {}).get(CONF_CONSUMER_MIN_DAYS, current["min_days"]))): vol.Coerce(int),
                    vol.Optional(CONF_CONSUMER_MAX_DAYS, default=int((user_input or {}).get(CONF_CONSUMER_MAX_DAYS, current["max_days"]))): vol.Coerce(int),
                } if self._selected_consumer == CONSUMER_COUNT else {}),
            }
        )
        return self.async_show_form(
            step_id="consumer_edit",
            data_schema=schema,
            errors=errors,
            description_placeholders={"slot": str(self._selected_consumer)},
        )
