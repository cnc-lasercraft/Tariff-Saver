"""Options flow for Tariff Saver.

Single-step form with conditional validation:
- Only require import_entity_dyn when price_mode == "import"
- Only require baseline_entity when baseline_mode == "entity"
- Only require solar_forecast_entity when solar_enabled == True
- Solar_enabled is independent from solar_installed (as requested)
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector


# --- Option keys (keep stable; matches what you already see in UI) ---
OPT_PRICE_MODE = "price_mode"  # "fetch" | "import"
OPT_IMPORT_PROVIDER = "import_provider"  # future-proof
OPT_SOURCE_INTERVAL_MIN = "source_interval_minutes"  # 15 | 60
OPT_NORMALIZATION_MODE = "normalization_mode"  # "repeat"

OPT_IMPORT_ENTITY_DYN = "import_entity_dyn"
OPT_IMPORT_ENTITY_BASE = "import_entity_base"

OPT_BASELINE_MODE = "baseline_mode"  # "api" | "entity" | "fixed" | "none"
OPT_BASELINE_FIXED_RP_KWH = "baseline_value"  # NOTE: matches your current UI key
OPT_BASELINE_ENTITY = "baseline_entity"

OPT_PRICE_SCALE = "price_scale"
OPT_IGNORE_ZERO_PRICES = "ignore_zero_prices"

# Solar / Solcast (keep existing keys)
OPT_SOLAR_INSTALLED = "solar_installed"  # optional informational toggle
OPT_SOLAR_ENABLED = "solar_enabled"  # = "use Solcast" as per your request
OPT_SOLAR_PROVIDER = "solar_provider"
OPT_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"
OPT_SOLAR_FORECAST_ATTRIBUTE = "solar_forecast_attribute"
OPT_SOLAR_INTERVAL_MIN = "solar_interval_minutes"

# New: Solar cost (Rp/kWh)
OPT_SOLAR_COST_RP_KWH = "solar_cost_rp_per_kwh"


def _sensor_entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(filter=selector.EntityFilterSelectorConfig(domain=["sensor"]))
    )


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Tariff Saver."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # --- Conditional validation (prevents "unused fields" being required) ---
            price_mode = user_input.get(OPT_PRICE_MODE, "fetch")
            baseline_mode = user_input.get(OPT_BASELINE_MODE, "api")
            solar_enabled = bool(user_input.get(OPT_SOLAR_ENABLED, False))

            if price_mode == "import" and not user_input.get(OPT_IMPORT_ENTITY_DYN):
                errors[OPT_IMPORT_ENTITY_DYN] = "required"

            if baseline_mode == "entity" and not user_input.get(OPT_BASELINE_ENTITY):
                errors[OPT_BASELINE_ENTITY] = "required"

            if solar_enabled and not user_input.get(OPT_SOLAR_FORECAST_ENTITY):
                errors[OPT_SOLAR_FORECAST_ENTITY] = "required"

            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._build_schema(user_input),
                    errors=errors,
                )

            return self.async_create_entry(title="", data=user_input)

        # initial display -> use stored values as defaults
        return self.async_show_form(
            step_id="init",
            data_schema=self._build_schema(),
        )

    def _build_schema(self, user_input: dict | None = None) -> vol.Schema:
        """Build schema with defaults. Fields are mostly Optional; validation is done in async_step_init."""
        opts = dict(self._entry.options)

        def d(key: str, fallback):
            if user_input is not None and key in user_input:
                return user_input.get(key, fallback)
            return opts.get(key, fallback)

        schema_dict: dict[vol.Marker, object] = {
            vol.Required(OPT_PRICE_MODE, default=d(OPT_PRICE_MODE, "fetch")): vol.In(
                {"fetch": "From API", "import": "Import from existing entities"}
            ),
            vol.Optional(OPT_IMPORT_PROVIDER, default=d(OPT_IMPORT_PROVIDER, "ekz_api")): vol.In(
                {"ekz_api": "EKZ API"}
            ),

            vol.Required(OPT_SOURCE_INTERVAL_MIN, default=int(d(OPT_SOURCE_INTERVAL_MIN, 15))): vol.In(
                {15: "15 minutes", 60: "60 minutes"}
            ),
            vol.Required(OPT_NORMALIZATION_MODE, default=d(OPT_NORMALIZATION_MODE, "repeat")): vol.In(
                {"repeat": "Repeat to 15-minute slots"}
            ),

            # Import entities (only required via validation when price_mode == import)
            vol.Optional(OPT_IMPORT_ENTITY_DYN, default=d(OPT_IMPORT_ENTITY_DYN, "")): _sensor_entity_selector(),
            vol.Optional(OPT_IMPORT_ENTITY_BASE, default=d(OPT_IMPORT_ENTITY_BASE, "")): _sensor_entity_selector(),

            # Baseline
            vol.Required(OPT_BASELINE_MODE, default=d(OPT_BASELINE_MODE, "api")): vol.In(
                {"api": "From API", "entity": "From entity", "fixed": "Fixed value", "none": "No baseline"}
            ),
            vol.Optional(OPT_BASELINE_FIXED_RP_KWH, default=float(d(OPT_BASELINE_FIXED_RP_KWH, 0.0))): vol.Coerce(float),
            vol.Optional(OPT_BASELINE_ENTITY, default=d(OPT_BASELINE_ENTITY, "")): _sensor_entity_selector(),

            # Scaling / hygiene
            vol.Required(OPT_PRICE_SCALE, default=float(d(OPT_PRICE_SCALE, 1.0))): vol.Coerce(float),
            vol.Required(OPT_IGNORE_ZERO_PRICES, default=bool(d(OPT_IGNORE_ZERO_PRICES, True))): bool,

            # Solar installed (pure info; does NOT gate Solcast usage)
            vol.Optional(OPT_SOLAR_INSTALLED, default=bool(d(OPT_SOLAR_INSTALLED, False))): bool,

            # Solcast usage toggle (independent)
            vol.Required(OPT_SOLAR_ENABLED, default=bool(d(OPT_SOLAR_ENABLED, False))): bool,

            # Solcast provider & mapping (only required via validation when solar_enabled == True)
            vol.Optional(OPT_SOLAR_PROVIDER, default=d(OPT_SOLAR_PROVIDER, "solcast")): vol.In(
                {"solcast": "Solcast PV Forecast"}
            ),
            vol.Optional(OPT_SOLAR_FORECAST_ENTITY, default=d(OPT_SOLAR_FORECAST_ENTITY, "")): _sensor_entity_selector(),
            vol.Optional(OPT_SOLAR_FORECAST_ATTRIBUTE, default=d(OPT_SOLAR_FORECAST_ATTRIBUTE, "detailedForecast")): str,
            vol.Optional(OPT_SOLAR_INTERVAL_MIN, default=int(d(OPT_SOLAR_INTERVAL_MIN, 30))): vol.In(
                {30: "30 minutes"}
            ),

            # NEW: solar cost assumption (Rp/kWh)
            vol.Optional(OPT_SOLAR_COST_RP_KWH, default=float(d(OPT_SOLAR_COST_RP_KWH, 0.0))): vol.Coerce(float),
        }

        return vol.Schema(schema_dict)
