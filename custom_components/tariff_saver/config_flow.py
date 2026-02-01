"""Config flow for Tariff Saver."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN, DEFAULT_PUBLISH_TIME, CONF_PUBLISH_TIME

# Modes
MODE_PUBLIC = "public"
MODE_MYEKZ = "myekz"

# --- Option keys (must match coordinator expectations) ---
OPT_PRICE_MODE = "price_mode"  # "api" | "import"
OPT_IMPORT_PROVIDER = "import_provider"  # e.g. "ekz_api" (placeholder for future)
OPT_SOURCE_INTERVAL_MIN = "source_interval_minutes"  # "15" | "60"
OPT_NORMALIZATION_MODE = "normalization_mode"  # "repeat"

OPT_IMPORT_ENTITY_DYN = "import_entity_dyn"
OPT_BASELINE_MODE = "baseline_mode"  # "api" | "fixed" | "entity"
OPT_BASELINE_VALUE = "baseline_value"
OPT_BASELINE_ENTITY = "baseline_entity"

OPT_PRICE_SCALE = "price_scale"
OPT_IGNORE_ZERO_PRICES = "ignore_zero_prices"

# --- Solar / PV Forecast (optional) ---
OPT_SOLAR_ENABLED = "solar_enabled"
OPT_SOLAR_PROVIDER = "solar_provider"  # placeholder: "solcast"
OPT_SOLAR_ENTITY = "solar_forecast_entity"
OPT_SOLAR_ATTRIBUTE = "solar_forecast_attribute"  # e.g. "detailedForecast"
OPT_SOLAR_INTERVAL_MIN = "solar_interval_minutes"  # "30" | "60"

# --- Defaults ---
DEFAULT_PRICE_MODE = "api"
DEFAULT_IMPORT_PROVIDER = "ekz_api"
DEFAULT_SOURCE_INTERVAL_MIN = "15"
DEFAULT_NORMALIZATION_MODE = "repeat"

DEFAULT_BASELINE_MODE = "api"
DEFAULT_BASELINE_VALUE = 0.0

DEFAULT_PRICE_SCALE = 1.0
DEFAULT_IGNORE_ZERO_PRICES = True

DEFAULT_SOLAR_ENABLED = False
DEFAULT_SOLAR_PROVIDER = "solcast"
DEFAULT_SOLAR_ATTRIBUTE = "detailedForecast"
DEFAULT_SOLAR_INTERVAL_MIN = "30"


def _sensor_entity_selector() -> selector.EntitySelector:
    """Entity selector limited to sensor domain (HA 2026.x compatible)."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=selector.EntityFilterSelectorConfig(domain=["sensor"])
        )
    )


def _select(options: list[str], translation_key: str) -> selector.SelectSelector:
    """Dropdown select with translations (strings.json via translation_key)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=translation_key,
        )
    )


class TariffSaverConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    VERSION = 2

    def __init__(self) -> None:
        self._name: str | None = None
        self._mode: str | None = None

    async def async_step_user(self, user_input=None):
        """Initial step: choose integration name."""
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            return await self.async_step_mode()

        schema = vol.Schema({vol.Required(CONF_NAME): str})
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_mode(self, user_input=None):
        """Choose authentication mode."""
        if user_input is not None:
            self._mode = user_input["mode"]
            if self._mode == MODE_PUBLIC:
                return await self.async_step_public()
            return await self.async_step_myekz()

        schema = vol.Schema(
            {
                vol.Required("mode", default=MODE_PUBLIC): vol.In(
                    {
                        MODE_PUBLIC: "Public (no login)",
                        MODE_MYEKZ: "myEKZ login",
                    }
                )
            }
        )
        return self.async_show_form(step_id="mode", data_schema=schema)

    async def async_step_public(self, user_input=None):
        """Public (no-login) configuration."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_NAME: self._name,
                    "mode": MODE_PUBLIC,
                    "tariff_name": user_input["tariff_name"],
                    "baseline_tariff_name": user_input.get("baseline_tariff_name"),
                    CONF_PUBLISH_TIME: user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
                },
            )

        schema = vol.Schema(
            {
                vol.Required("tariff_name"): str,
                vol.Optional("baseline_tariff_name", default="electricity_standard"): str,
                vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str,  # HH:MM
            }
        )
        return self.async_show_form(step_id="public", data_schema=schema)

    async def async_step_myekz(self, user_input=None):
        """Placeholder for myEKZ OAuth flow."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_NAME: self._name,
                    "mode": MODE_MYEKZ,
                    CONF_PUBLISH_TIME: user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
                },
            )

        schema = vol.Schema({vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str})
        return self.async_show_form(step_id="myekz", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return TariffSaverOptionsFlowHandler(config_entry)


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Tariff Saver."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage options."""
        if user_input is not None:
            opts: dict = dict(self._entry.options)

            # Tariff / pricing
            opts[OPT_PRICE_MODE] = user_input[OPT_PRICE_MODE]
            opts[OPT_IMPORT_PROVIDER] = user_input[OPT_IMPORT_PROVIDER]
            opts[OPT_SOURCE_INTERVAL_MIN] = user_input[OPT_SOURCE_INTERVAL_MIN]
            opts[OPT_NORMALIZATION_MODE] = user_input[OPT_NORMALIZATION_MODE]
            opts[OPT_IMPORT_ENTITY_DYN] = user_input.get(OPT_IMPORT_ENTITY_DYN)

            # Baseline
            opts[OPT_BASELINE_MODE] = user_input[OPT_BASELINE_MODE]
            opts[OPT_BASELINE_VALUE] = float(user_input.get(OPT_BASELINE_VALUE, 0.0) or 0.0)
            opts[OPT_BASELINE_ENTITY] = user_input.get(OPT_BASELINE_ENTITY)

            # Scale / validation
            opts[OPT_PRICE_SCALE] = float(user_input.get(OPT_PRICE_SCALE, 1.0) or 1.0)
            opts[OPT_IGNORE_ZERO_PRICES] = bool(user_input.get(OPT_IGNORE_ZERO_PRICES, True))

            # Solar / PV forecast
            opts[OPT_SOLAR_ENABLED] = bool(user_input.get(OPT_SOLAR_ENABLED, False))
            opts[OPT_SOLAR_PROVIDER] = user_input.get(OPT_SOLAR_PROVIDER, DEFAULT_SOLAR_PROVIDER)
            opts[OPT_SOLAR_ENTITY] = user_input.get(OPT_SOLAR_ENTITY)
            opts[OPT_SOLAR_ATTRIBUTE] = str(
                user_input.get(OPT_SOLAR_ATTRIBUTE, DEFAULT_SOLAR_ATTRIBUTE) or DEFAULT_SOLAR_ATTRIBUTE
            )
            opts[OPT_SOLAR_INTERVAL_MIN] = user_input.get(
                OPT_SOLAR_INTERVAL_MIN, DEFAULT_SOLAR_INTERVAL_MIN
            )

            return self.async_create_entry(title="", data=opts)

        current = dict(self._entry.options)

        # Tariff / pricing
        price_mode = current.get(OPT_PRICE_MODE, DEFAULT_PRICE_MODE)
        import_provider = current.get(OPT_IMPORT_PROVIDER, DEFAULT_IMPORT_PROVIDER)
        source_interval = str(current.get(OPT_SOURCE_INTERVAL_MIN, DEFAULT_SOURCE_INTERVAL_MIN))
        normalization_mode = current.get(OPT_NORMALIZATION_MODE, DEFAULT_NORMALIZATION_MODE)
        import_entity_dyn = current.get(OPT_IMPORT_ENTITY_DYN)

        # Baseline
        baseline_mode = current.get(OPT_BASELINE_MODE, DEFAULT_BASELINE_MODE)
        baseline_value = float(current.get(OPT_BASELINE_VALUE, DEFAULT_BASELINE_VALUE) or 0.0)
        baseline_entity = current.get(OPT_BASELINE_ENTITY)

        # Scale / validation
        price_scale = float(current.get(OPT_PRICE_SCALE, DEFAULT_PRICE_SCALE) or 1.0)
        ignore_zero = bool(current.get(OPT_IGNORE_ZERO_PRICES, DEFAULT_IGNORE_ZERO_PRICES))

        # Solar / PV forecast
        solar_enabled = bool(current.get(OPT_SOLAR_ENABLED, DEFAULT_SOLAR_ENABLED))
        solar_provider = current.get(OPT_SOLAR_PROVIDER, DEFAULT_SOLAR_PROVIDER)
        solar_entity = current.get(OPT_SOLAR_ENTITY)
        solar_attribute = str(current.get(OPT_SOLAR_ATTRIBUTE, DEFAULT_SOLAR_ATTRIBUTE) or DEFAULT_SOLAR_ATTRIBUTE)
        solar_interval = str(current.get(OPT_SOLAR_INTERVAL_MIN, DEFAULT_SOLAR_INTERVAL_MIN))

        schema = vol.Schema(
            {
                # Price source (generic: API vs Import)
                vol.Required(OPT_PRICE_MODE, default=price_mode): _select(
                    ["api", "import"], translation_key="price_mode"
                ),

                # Provider catalog for import (placeholder; will grow later)
                vol.Required(OPT_IMPORT_PROVIDER, default=import_provider): _select(
                    ["ekz_api"], translation_key="import_provider"
                ),

                # Source interval of upstream tariff
                vol.Required(OPT_SOURCE_INTERVAL_MIN, default=source_interval): _select(
                    ["15", "60"], translation_key="source_interval"
                ),

                # Normalization to our internal 15-min slots
                vol.Required(OPT_NORMALIZATION_MODE, default=normalization_mode): _select(
                    ["repeat"], translation_key="normalization_mode"
                ),

                # Import entity (dynamic price)
                vol.Optional(OPT_IMPORT_ENTITY_DYN, default=import_entity_dyn): _sensor_entity_selector(),

                # Baseline mode (API / fixed / entity)
                vol.Required(OPT_BASELINE_MODE, default=baseline_mode): _select(
                    ["api", "fixed", "entity"], translation_key="baseline_mode"
                ),
                vol.Optional(OPT_BASELINE_VALUE, default=baseline_value): vol.Coerce(float),
                vol.Optional(OPT_BASELINE_ENTITY, default=baseline_entity): _sensor_entity_selector(),

                # Scale + zero handling
                vol.Required(OPT_PRICE_SCALE, default=price_scale): vol.Coerce(float),
                vol.Required(OPT_IGNORE_ZERO_PRICES, default=ignore_zero): bool,

                # Solar / PV forecast (optional)
                vol.Required(OPT_SOLAR_ENABLED, default=solar_enabled): bool,
                vol.Required(OPT_SOLAR_PROVIDER, default=solar_provider): _select(
                    ["solcast"], translation_key="solar_provider"
                ),
                vol.Optional(OPT_SOLAR_ENTITY, default=solar_entity): _sensor_entity_selector(),
                vol.Optional(OPT_SOLAR_ATTRIBUTE, default=solar_attribute): str,
                vol.Required(OPT_SOLAR_INTERVAL_MIN, default=solar_interval): _select(
                    ["30", "60"], translation_key="solar_interval"
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
