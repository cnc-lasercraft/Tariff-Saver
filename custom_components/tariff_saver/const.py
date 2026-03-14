"""Constants for Tariff Saver."""
from __future__ import annotations

DOMAIN = "tariff_saver"

CONF_NAME = "name"
CONF_PUBLISH_TIME = "publish_time"
DEFAULT_PUBLISH_TIME = "13:00"
CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"
CONF_EKZ_ENTRY_ID = "ekz_entry_id"
CONF_FEED_IN_PRICE_MODE = "feed_in_price_mode"
CONF_FEED_IN_FIXED_PRICE = "feed_in_fixed_price"
CONF_FEED_IN_PRICE_ENTITY = "feed_in_price_entity"
DEFAULT_FEED_IN_PRICE_MODE = "fixed"
DEFAULT_FEED_IN_FIXED_PRICE = 0.0
FEED_IN_PRICE_MODE_FIXED = "fixed"
FEED_IN_PRICE_MODE_ENTITY = "entity"


CONSUMER_COUNT = 10
CONF_CONSUMERS = "consumers"

CONSUMER_MODE_AUTO = "auto"
CONSUMER_MODE_FIXED_DURATION = "fixed_duration"
CONSUMER_MODE_FIXED_ENERGY = "fixed_energy"
CONSUMER_MODES = (
    CONSUMER_MODE_AUTO,
    CONSUMER_MODE_FIXED_DURATION,
    CONSUMER_MODE_FIXED_ENERGY,
)


CONF_PV_FORECAST_ENTITY = "pv_forecast_entity"
CONF_PV_FORECAST_ATTRIBUTE = "pv_forecast_attribute"
DEFAULT_PV_FORECAST_ATTRIBUTE = "detailedForecast"

CONF_BATTERY_ENABLED = "battery_enabled"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_MIN_SOC_PERCENT = "battery_min_soc_percent"
DEFAULT_BATTERY_MIN_SOC_PERCENT = 20.0
