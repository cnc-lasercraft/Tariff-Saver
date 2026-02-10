"""Options flow for Tariff Saver.

Requirements implemented:
- Solar forecast can only be configured if a solar system is enabled (solar_installed == True).
- Solar energy cost (Rp/kWh) is requested whenever solar_installed == True,
  independent of whether a forecast provider is used.

Multi-step routing (to avoid invalid selectors for unused fields):
1) init: common options + solar_installed
2) import: only if price_mode == "import"
3) baseline_entity / baseline_fixed: only if baseline_mode requires it
4) solar: only if solar_installed == True (asks solar_cost + forecast_provider)
5) solar_forecast: only if forecast_provider != "none"
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector


# -----------------------------
# Option keys (keep stable)
# -----------------------------
OPT_PRICE_MODE = "price_mode"  # "fetch" | "import"
OPT_IMPORT_PROVIDER = "import_provider"  # placeholder, future-proof
OPT_SOURCE_INTERVAL_MIN = "source_interval_minutes"  # 15 | 60
OPT_NORMALIZATION_MODE = "normalization_mode"  # "repeat"

OPT_IMPORT_ENTITY_DYN = "import_entity_dyn"  # required only for import mode
OPT_IMPORT_ENTITY_BASE = "import_entity_base"  # optional

OPT_BASELINE_MODE = "baseline_mode"  # "api" | "entity" | "fixed" | "none"
OPT_BASELINE_ENTITY = "baseline_entity"  # required only for baseline_mode == entity
OPT_BASELINE_FIXED_RP_KWH = "baseline_value"  # Rp/kWh (keep this key to match existing installs)

OPT_PRICE_SCALE = "price_scale"
OPT_IGNORE_ZERO_PRICES = "ignore_zero_prices"

# Consumption / energy input (required for cost calculation)
# Keep this key EXACTLY stable because sensor.py reads it.
OPT_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"

# Solar / PV
OPT_SOLAR_INSTALLED = "solar_installed"  # True if PV exists
OPT_SOLAR_COST_RP_KWH = "solar_cost_rp_per_kwh"  # Rp/kWh (always if solar_installed)
OPT_SOLAR_FORECAST_PROVIDER = "solar_forecast_provider"  # "none" | "solcast" | (future)

# Forecast mapping (only if provider != none)
OPT_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"
OPT_SOLAR_FORECAST_ATTRIBUTE = "solar_forecast_attribute"
OPT_SOLAR_INTERVAL_MIN = "solar_interval_minutes"


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
        self._pending: dict[str, object] = {}

    # -----------------------------
    # Step 1: Common options
    # -----------------------------
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            # IMPORTANT: merge, don't replace — otherwise non-form keys get deleted.
            self._pending = dict(self._entry.options)
            self._pending.update(user_input)
            return await self._next_step()

        opts = dict(self._entry.options)

        schema = vol.Schema(
            {
                # Price source
                vol.Required(OPT_PRICE_MODE, default=opts.get(OPT_PRICE_MODE, "fetch")): vol.In(
                    {
                        "fetch": "From API",
                        "import": "Import from existing entities",
                    }
                ),
                vol.Optional(OPT_IMPORT_PROVIDER, default=opts.get(OPT_IMPORT_PROVIDER, "ekz_api")): vol.In(
                    {
                        "ekz_api": "EKZ API",
                    }
                ),

                # Interval & normalization
                vol.Required(OPT_SOURCE_INTERVAL_MIN, default=int(opts.get(OPT_SOURCE_INTERVAL_MIN, 15))): vol.In(
                    {15: "15 minutes", 60: "60 minutes"}
                ),
                vol.Required(OPT_NORMALIZATION_MODE, default=opts.get(OPT_NORMALIZATION_MODE, "repeat")): vol.In(
                    {"repeat": "Repeat to 15-minute slots"}
                ),

                # Baseline source
                vol.Required(OPT_BASELINE_MODE, default=opts.get(OPT_BASELINE_MODE, "api")): vol.In(
                    {
                        "api": "From API / source",
                        "entity": "From entity",
                        "fixed": "Fixed value",
                        "none": "No baseline",
                    }
                ),

                # Consumption / energy entity (kWh total) used for cost calculation
                vol.Required(
                    OPT_CONSUMPTION_ENERGY_ENTITY,
                    default=opts.get(OPT_CONSUMPTION_ENERGY_ENTITY, ""),
                ): _sensor_entity_selector(),

                # Scaling / hygiene
                vol.Required(OPT_PRICE_SCALE, default=float(opts.get(OPT_PRICE_SCALE, 1.0))): vol.Coerce(float),
                vol.Required(OPT_IGNORE_ZERO_PRICES, default=bool(opts.get(OPT_IGNORE_ZERO_PRICES, True))): bool,

                # Solar installed (gate for any PV forecast settings)
                vol.Required(OPT_SOLAR_INSTALLED, default=bool(opts.get(OPT_SOLAR_INSTALLED, False))): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    # -----------------------------
    # Step 2: Import entities
    # -----------------------------
    async def async_step_import(self, user_input=None):
        if user_input is not None:
            self._pending.update(user_input)
            return await self._next_step()

        opts = dict(self._entry.options)
        schema = vol.Schema(
            {
                vol.Required(OPT_IMPORT_ENTITY_DYN, default=opts.get(OPT_IMPORT_ENTITY_DYN, "")): _sensor_entity_selector(),
                vol.Optional(OPT_IMPORT_ENTITY_BASE, default=opts.get(OPT_IMPORT_ENTITY_BASE, "")): _sensor_entity_selector(),
            }
        )
        return self.async_show_form(step_id="import", data_schema=schema)

    # -----------------------------
    # Step 3a: Baseline entity
    # -----------------------------
    async def async_step_baseline_entity(self, user_input=None):
        if user_input is not None:
            self._pending.update(user_input)
            return await self._next_step()

        opts = dict(self._entry.options)
        schema = vol.Schema(
            {
                vol.Required(OPT_BASELINE_ENTITY, default=opts.get(OPT_BASELINE_ENTITY, "")): _sensor_entity_selector(),
            }
        )
        return self.async_show_form(step_id="baseline_entity", data_schema=schema)

    # -----------------------------
    # Step 3b: Baseline fixed
    # -----------------------------
    async def async_step_baseline_fixed(self, user_input=None):
        if user_input is not None:
            self._pending.update(user_input)
            return await self._next_step()

        opts = dict(self._entry.options)
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_BASELINE_FIXED_RP_KWH,
                    default=float(opts.get(OPT_BASELINE_FIXED_RP_KWH, 0.0)),
                ): vol.Coerce(float),
            }
        )
        return self.async_show_form(step_id="baseline_fixed", data_schema=schema)

    # -----------------------------
    # Step 4: Solar basics (cost + forecast provider)
    # -----------------------------
    async def async_step_solar(self, user_input=None):
        if user_input is not None:
            self._pending.update(user_input)
            return await self._next_step()

        opts = dict(self._entry.options)
        schema = vol.Schema(
            {
                # Always requested when solar_installed == True
                vol.Required(OPT_SOLAR_COST_RP_KWH, default=float(opts.get(OPT_SOLAR_COST_RP_KWH, 0.0))): vol.Coerce(float),
                # Forecast provider suggests whether to add mapping step
                vol.Required(
                    OPT_SOLAR_FORECAST_PROVIDER,
                    default=opts.get(OPT_SOLAR_FORECAST_PROVIDER, "none"),
                ): vol.In(
                    {
                        "none": "No forecast (disabled)",
                        "solcast": "Solcast PV Forecast",
                    }
                ),
            }
        )
        return self.async_show_form(step_id="solar", data_schema=schema)

    # -----------------------------
    # Step 5: Forecast mapping (only if provider != none)
    # -----------------------------
    async def async_step_solar_forecast(self, user_input=None):
        if user_input is not None:
            self._pending.update(user_input)
            return await self._next_step()

        opts = dict(self._entry.options)
        schema = vol.Schema(
            {
                vol.Required(OPT_SOLAR_FORECAST_ENTITY, default=opts.get(OPT_SOLAR_FORECAST_ENTITY, "")): _sensor_entity_selector(),
                vol.Required(OPT_SOLAR_FORECAST_ATTRIBUTE, default=opts.get(OPT_SOLAR_FORECAST_ATTRIBUTE, "detailedForecast")): str,
                vol.Required(OPT_SOLAR_INTERVAL_MIN, default=int(opts.get(OPT_SOLAR_INTERVAL_MIN, 30))): vol.In(
                    {30: "30 minutes"}
                ),
            }
        )
        return self.async_show_form(step_id="solar_forecast", data_schema=schema)

    # -----------------------------
    # Step routing
    # -----------------------------
    async def _next_step(self):
        price_mode = str(self._pending.get(OPT_PRICE_MODE, "fetch"))
        baseline_mode = str(self._pending.get(OPT_BASELINE_MODE, "api"))
        solar_installed = bool(self._pending.get(OPT_SOLAR_INSTALLED, False))
        forecast_provider = str(self._pending.get(OPT_SOLAR_FORECAST_PROVIDER, "none"))

        # 1) Import mapping
        if price_mode == "import" and OPT_IMPORT_ENTITY_DYN not in self._pending:
            return await self.async_step_import()

        # 2) Baseline details
        if baseline_mode == "entity" and OPT_BASELINE_ENTITY not in self._pending:
            return await self.async_step_baseline_entity()
        if baseline_mode == "fixed" and OPT_BASELINE_FIXED_RP_KWH not in self._pending:
            return await self.async_step_baseline_fixed()

        # 3) Solar basics (gated by solar_installed)
        if solar_installed and OPT_SOLAR_COST_RP_KWH not in self._pending:
            return await self.async_step_solar()

        # 4) Forecast mapping (only if enabled and solar_installed)
        if solar_installed and forecast_provider != "none" and OPT_SOLAR_FORECAST_ENTITY not in self._pending:
            return await self.async_step_solar_forecast()

        # -------- cleanup stale values --------
        if price_mode != "import":
            self._pending.pop(OPT_IMPORT_ENTITY_DYN, None)
            self._pending.pop(OPT_IMPORT_ENTITY_BASE, None)

        if baseline_mode != "entity":
            self._pending.pop(OPT_BASELINE_ENTITY, None)
        if baseline_mode != "fixed":
            self._pending.pop(OPT_BASELINE_FIXED_RP_KWH, None)

        if not solar_installed:
            self._pending.pop(OPT_SOLAR_COST_RP_KWH, None)
            self._pending.pop(OPT_SOLAR_FORECAST_PROVIDER, None)
            self._pending.pop(OPT_SOLAR_FORECAST_ENTITY, None)
            self._pending.pop(OPT_SOLAR_FORECAST_ATTRIBUTE, None)
            self._pending.pop(OPT_SOLAR_INTERVAL_MIN, None)
        else:
            if forecast_provider == "none":
                self._pending.pop(OPT_SOLAR_FORECAST_ENTITY, None)
                self._pending.pop(OPT_SOLAR_FORECAST_ATTRIBUTE, None)
                self._pending.pop(OPT_SOLAR_INTERVAL_MIN, None)

        return self.async_create_entry(title="", data=self._pending)
