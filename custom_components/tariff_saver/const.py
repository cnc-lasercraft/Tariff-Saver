"""Constants for Tariff Saver."""
from __future__ import annotations

DOMAIN = "tariff_saver"

DEFAULT_PUBLISH_TIME = "18:15"
CONF_PUBLISH_TIME = "publish_time"

# Options (existing)
CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"

# Grade thresholds (percent vs daily average)
# Stored as list of 4 floats: [t1, t2, t3, t4]
# Mapping:
# dev <= t1 -> 1
# t1 < dev <= t2 -> 2
# t2 < dev <  t3 -> 3
# t3 <= dev < t4 -> 4
# dev >= t4 -> 5
CONF_GRADE_THRESHOLDS = "grade_thresholds_percent"

DEFAULT_GRADE_THRESHOLDS = [-10.0, -5.0, 5.0, 10.0]
