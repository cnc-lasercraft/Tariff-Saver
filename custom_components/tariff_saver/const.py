"""Constants for Tariff Saver."""
from __future__ import annotations

DOMAIN = "tariff_saver"

CONF_SOURCE_TYPE = "source_type"
SOURCE_EKZ = "ekz"
SOURCE_ENTITIES = "entities"

CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_ATTRIBUTE = "price_attribute"
CONF_BASELINE_ENTITY = "baseline_entity"
CONF_BASELINE_ATTRIBUTE = "baseline_attribute"

CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"
CONF_PRICE_SCALE = "price_scale"
CONF_IGNORE_ZERO_PRICES = "ignore_zero_prices"
CONF_PUBLISH_TIME = "publish_time"
DEFAULT_PUBLISH_TIME = "18:15"

DEFAULT_PRICE_ATTRIBUTE = "price_slots"
DEFAULT_BASELINE_ATTRIBUTE = "baseline_slots"
DEFAULT_PRICE_SCALE = 1.0
DEFAULT_IGNORE_ZERO_PRICES = True

SIGNAL_STORE_UPDATED = "tariff_saver_store_updated"

EKZ_DOMAIN = "ekz_tariff"
