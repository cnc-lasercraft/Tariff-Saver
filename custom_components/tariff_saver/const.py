"""Constants for Tariff Saver."""
from __future__ import annotations

DOMAIN = "tariff_saver"

CONF_NAME = "name"
CONF_PUBLISH_TIME = "publish_time"
DEFAULT_PUBLISH_TIME = "18:30"
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

CONF_CONSUMER_MIN_DAYS = "min_days"
CONF_CONSUMER_MAX_DAYS = "max_days"
DEFAULT_CONSUMER10_MIN_DAYS = 5
DEFAULT_CONSUMER10_MAX_DAYS = 8

CONF_SEASON_ENTITY = "season_entity"
DEFAULT_SEASON_ENTITY = ""

CONF_BATTERY_SOC_MARGIN = "battery_soc_margin_percent"
DEFAULT_BATTERY_SOC_MARGIN = 10.0

CONF_AMPEL_PV_THRESHOLD = "ampel_pv_threshold_kw"
DEFAULT_AMPEL_PV_THRESHOLD = 2.0
CONF_AMPEL_SCORE_GOOD = "ampel_score_good"
DEFAULT_AMPEL_SCORE_GOOD = 30
CONF_AMPEL_SCORE_BAD = "ampel_score_bad"
DEFAULT_AMPEL_SCORE_BAD = 70
CONF_AMPEL_PV_ENTITY = "ampel_pv_entity"

# Heating / WP control
CONF_HEATING_TEMP_SENSOR_1 = "heating_temp_sensor_1"
CONF_HEATING_TEMP_SENSOR_2 = "heating_temp_sensor_2"
CONF_HEATING_TEMP_SENSOR_3 = "heating_temp_sensor_3"
CONF_HEATING_COMFORT_MIN = "heating_comfort_min"
DEFAULT_HEATING_COMFORT_MIN = 21.0
CONF_HEATING_PV_MAX = "heating_pv_max"
DEFAULT_HEATING_PV_MAX = 23.0
CONF_HEATING_WP_ENTITY = "heating_wp_entity"
CONF_HEATING_WP_POWER_KW = "heating_wp_power_kw"
DEFAULT_HEATING_WP_POWER_KW = 2.2
CONF_HEATING_SEASONS = "heating_seasons"
DEFAULT_HEATING_SEASONS = "Frühling,Herbst"
CONF_HEATING_WP_OFF_VALUE = "heating_wp_off_value"
DEFAULT_HEATING_WP_OFF_VALUE = "Aus"
CONF_HEATING_WP_WW_VALUE = "heating_wp_ww_value"
DEFAULT_HEATING_WP_WW_VALUE = "Nur Warmwasser"
CONF_HEATING_WP_HEAT_VALUE = "heating_wp_heat_value"
DEFAULT_HEATING_WP_HEAT_VALUE = "Heizung & Warmwasser"
CONF_HEATING_MAX_SCORE = "heating_max_score"
DEFAULT_HEATING_MAX_SCORE = 90
CONF_HEATING_PV_WAIT_MINUTES = "heating_pv_wait_minutes"
DEFAULT_HEATING_PV_WAIT_MINUTES = 60
CONF_HEATING_ENABLED = "heating_enabled"
CONF_HEATING_PV_SURPLUS_ENTITY = "heating_pv_surplus_entity"
CONF_PV_SURPLUS_ENTITY = "pv_surplus_entity"
CONF_HEATING_WP_MIN_RUNTIME = "heating_wp_min_runtime"
DEFAULT_HEATING_WP_MIN_RUNTIME = 15
