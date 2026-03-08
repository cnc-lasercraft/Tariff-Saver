"""Constants for Tariff Saver."""
from __future__ import annotations

DOMAIN = "tariff_saver"

CONF_PUBLISH_TIME = "publish_time"
DEFAULT_PUBLISH_TIME = "18:15"

CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"
CONF_EKZ_ENTRY_ID = "ekz_entry_id"

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
