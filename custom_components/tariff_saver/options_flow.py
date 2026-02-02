"""Options flow for Tariff Saver.

Progressive disclosure within a single flow step:
- Start with common fields.
- If user enables a mode that needs extra fields (Import, Baseline Entity/Fixed, Solar/Solcast),
  the flow re-renders with those extra fields after Submit.
- This avoids HA's limitation of not dynamically hiding/showing fields live.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector


# --- Option keys (must match what you already have in your UI/options) ---
OPT_PRICE_MODE = "price_mode"  # "fetch" | "import"
OPT_IMPORT_PROVIDER = "import_provider"
OPT_SOURCE_INTERVAL_MIN = "source_interval_minutes"  # 15 | 60
OPT_NORMALIZATION_MODE = "normalization_mode"  # "repeat"

OPT_IMPORT_ENTITY_DYN = "import_entity_dyn"
OPT_IMPORT_ENTITY_BASE = "import_entity_base"

OPT_BASELINE_MODE = "baseline_mode"  # "api" | "entity" | "fixed" | "none"
OPT_BASELINE_FIXED_RP_KWH = "baseline_value"  # existing key in your UI
OPT_BASELINE_ENTITY = "baseline_entity"

OPT_PRICE_SCALE = "price_scale"
OPT_IGNORE_ZERO_PRICES = "ignore_zero_prices"

# Solar / Solcast (existing UI keys)
OPT_SOLAR_ENABLED = "solar_enabled"  # use solcast?
OPT_SOLAR_PROVIDER = "solar_provider"
OPT_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"
OPT_SOLAR_FORECAST_ATTRIBUTE = "solar_forecast_attribute"
OPT_SOLAR_INTERVAL_MIN = "solar_interval_minutes"

# New: Solar cost (Rp/kWh)
OPT_SOLAR_COST_RP_KWH = "solar_cost_rp_per_kwh"


def _sensor_entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=selector.EntityFilterSelectorConfig(domain=["sensor"])
        )
    )


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Tariff Saver."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            # Determine which extra fields are needed
            price_mode = user_input.get(OPT_PRICE_MODE, "fetch")
            baseline_mode = user_input.get(OPT_BASELINE_MODE, "api")
            solar_enabled = bool(user_input.get(OPT_SOLAR_ENABLED, False))

            needs_import = price_mode == "import"
            needs_baseline_entity = baseline_mode == "entity"
            needs_baseline_fixed = baseline_mode == "fixed"
            needs_solar = solar_enabled

            # If user enabled a feature but the extra fields were not shown yet,
            # re-render the form with those fields (progressive disclosure).
            missing_required_fields = False
            if needs_import and OPT_IMPORT_ENTITY_DYN not in user_input:
                missing_required_fields = True
            if needs_baseline_entity and OPT_BASELINE_ENTITY not in user_input:
                missing_required_fields = True
            if needs_baseline_fixed and OPT_BASELINE_FIXED_RP_KWH not in user_input:
                missing_required_fields = True
            if needs_solar and (OPT_SOLAR_FORECAST_ENTITY not in user_input or OPT_SOLAR_COST_RP_KWH not in user_input):
                missing_required_fields = True

            if missing_required_fields:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._build_schema(user_input, force_extras=True),
                )

            # Now validate only what is actually needed
            if needs_import and not user_input.get(OPT_IMPORT_ENTITY_DYN):
                errors[OPT_IMPORT_ENTITY_DYN] = "required"
            if needs_baseline_entity and not user_input.get(OPT_BASELINE_ENTITY):
                errors[OPT_BASELINE_ENTITY] = "required"
            if needs_solar and not user_input.get(OPT_SOLAR_FORECAST_ENTITY):
                errors[OPT_SOLAR_FORECAST_ENTITY] = "required"

            # solar cost must be a number; allow 0
            if needs_solar:
                try:
                    float(user_input.get(OPT_SOLAR_COST_RP_KWH, 0.0))
                except Exception:
                    errors[OPT_SOLAR_COST_RP_KWH] = "invalid"

            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._build_schema(user_input, force_extras=True),
                    errors=errors,
                )

            return self.async_create_entry(title="", data=user_input)

        # Initial form: show only common fields based on stored options
        return self.async_show_form(
            step_id="init",
            data_schema=self._build_schema(),
        )

    def _build_schema(self, user_input: dict | None = None, force_extras: bool = False) -> vol.Schema:
        """Build schema. When force_extras=True, include extra fields based on user_input."""
        opts = dict(self._entry.options)

        def d(key: str, fallback):
            if user_input is not None and key in user_input:
                return user_input.get(key, fallback)
            return opts.get(key, fallback)

        price_mode = d(OPT_PRICE_MODE, "fetch")
        baseline_mode = d(OPT_BASELINE_MODE, "api")
        solar_enabled = bool(d(OPT_SOLAR_ENABLED, False))

        needs_import = (price_mode == "import")
        needs_baseline_entity = (baseline_mode == "entity")
        needs_baseline_fixed = (baseline_mode == "fixed")
        needs_solar = solar_enabled

        schema: dict[vol.Marker, object] = {
            vol.Required(OPT_PRICE_MODE, default=price_mode): vol.In(
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

            vol.Required(OPT_BASELINE_MODE, default=baseline_mode): vol.In(
                {"api": "From API", "entity": "From entity", "fixed": "Fixed value", "none": "No baseline"}
            ),

            vol.Required(OPT_PRICE_SCALE, default=float(d(OPT_PRICE_SCALE, 1.0))): vol.Coerce(float),
            vol.Required(OPT_IGNORE_ZERO_PRICES, default=bool(d(OPT_IGNORE_ZERO_PRICES, True))): bool,

            # Solar/Solcast usage (independent)
            vol.Required(OPT_SOLAR_ENABLED, default=solar_enabled): bool,
        }

        # Extra fields only when needed; to show them immediately after enabling, set force_extras=True
        if force_extras and needs_import:
            schema.update(
                {
                    vol.Required(OPT_IMPORT_ENTITY_DYN, default=d(OPT_IMPORT_ENTITY_DYN, "")): _sensor_entity_selector(),
                    vol.Optional(OPT_IMPORT_ENTITY_BASE, default=d(OPT_IMPORT_ENTITY_BASE, "")): _sensor_entity_selector(),
                }
            )

        if force_extras and needs_baseline_entity:
            schema.update(
                {
                    vol.Required(OPT_BASELINE_ENTITY, default=d(OPT_BASELINE_ENTITY, "")): _sensor_entity_selector(),
                }
            )

        if force_extras and needs_baseline_fixed:
            schema.update(
                {
                    vol.Required(OPT_BASELINE_FIXED_RP_KWH, default=float(d(OPT_BASELINE_FIXED_RP_KWH, 0.0))): vol.Coerce(float),
                }
            )

        if force_extras and needs_solar:
            schema.update(
                {
                    vol.Required(OPT_SOLAR_PROVIDER, default=d(OPT_SOLAR_PROVIDER, "solcast")): vol.In(
                        {"solcast": "Solcast PV Forecast"}
                    ),
                    vol.Required(OPT_SOLAR_FORECAST_ENTITY, default=d(OPT_SOLAR_FORECAST_ENTITY, "")): _sensor_entity_selector(),
                    vol.Required(OPT_SOLAR_FORECAST_ATTRIBUTE, default=d(OPT_SOLAR_FORECAST_ATTRIBUTE, "detailedForecast")): str,
                    vol.Required(OPT_SOLAR_INTERVAL_MIN, default=int(d(OPT_SOLAR_INTERVAL_MIN, 30))): vol.In(
                        {30: "30 minutes"}
                    ),
                    vol.Required(OPT_SOLAR_COST_RP_KWH, default=float(d(OPT_SOLAR_COST_RP_KWH, 0.0))): vol.Coerce(float),
                }
            )

        return vol.Schema(schema)
