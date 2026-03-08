"""Constants for Tariff Saver."""
from __future__ import annotations

DOMAIN = "tariff_saver"

CONF_PUBLISH_TIME = "publish_time"
DEFAULT_PUBLISH_TIME = "18:15"

CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"
CONF_EKZ_ENTRY_ID = "ekz_entry_id"
CONF_PV_FORECAST_ENTITY = "pv_forecast_entity"
CONF_PV_FORECAST_ATTRIBUTE = "pv_forecast_attribute"
DEFAULT_PV_FORECAST_ATTRIBUTE = "detailedForecast"
CONF_FEED_IN_PRICE_MODE = "feed_in_price_mode"
FEED_IN_PRICE_MODE_FIXED = "fixed"
FEED_IN_PRICE_MODE_ENTITY = "entity"
DEFAULT_FEED_IN_PRICE_MODE = FEED_IN_PRICE_MODE_FIXED
CONF_FEED_IN_FIXED_PRICE = "feed_in_fixed_price"
DEFAULT_FEED_IN_FIXED_PRICE = 0.0
CONF_FEED_IN_PRICE_ENTITY = "feed_in_price_entity"

# legacy keys kept for backward compatibility with existing entries/options
CONF_TARIFF_NAME = "tariff_name"
CONF_BASELINE_TARIFF_NAME = "baseline_tariff_name"
CONF_MODE = "mode"
MODE_PUBLIC = "public"
MODE_MYEKZ = "myekz"
CONF_EMS_INSTANCE_ID = "ems_instance_id"
CONF_REDIRECT_URI = "redirect_uri"

CONF_ENABLE_COST_TRACKING = "enable_cost_tracking"
DEFAULT_ENABLE_COST_TRACKING = True
