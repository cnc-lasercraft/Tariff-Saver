"""Constants for Tariff Saver."""
from __future__ import annotations

DOMAIN = "tariff_saver"

# Slot-Granularität (seit 2026-04-13 von 30 auf 15 Min umgestellt)
SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES        # 4
SLOTS_PER_DAY = 24 * SLOTS_PER_HOUR        # 96
SLOT_HOUR_FRACTION = 1.0 / SLOTS_PER_HOUR  # 0.25

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


CONF_MAX_GRID_POWER_KW = "max_grid_power_kw"
DEFAULT_MAX_GRID_POWER_KW = 25.0

CONSUMER_COUNT = 15
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
CONF_BATTERY_ROUND_TRIP_LOSS = "battery_round_trip_loss_percent"
DEFAULT_BATTERY_ROUND_TRIP_LOSS = 10.0
CONF_BATTERY_MAX_CHARGE_KW = "battery_max_charge_kw"
DEFAULT_BATTERY_MAX_CHARGE_KW = 10.0
CONF_BATTERY_PV_CHARGE_THRESHOLD = "battery_pv_charge_threshold_kw"
DEFAULT_BATTERY_PV_CHARGE_THRESHOLD = 2.0
CONF_BATTERY_DEVICE_ID = "battery_device_id"
DEFAULT_BATTERY_DEVICE_ID = ""
CONF_BATTERY_HOLD_ENTER_SCORE = "battery_hold_enter_score"
DEFAULT_BATTERY_HOLD_ENTER_SCORE = 30
CONF_BATTERY_HOLD_EXIT_SCORE = "battery_hold_exit_score"
DEFAULT_BATTERY_HOLD_EXIT_SCORE = 40
CONF_BATTERY_PV_FULL_CHARGE_RATIO = "battery_pv_full_charge_ratio"
DEFAULT_BATTERY_PV_FULL_CHARGE_RATIO = 1.0  # PV forecast < ratio × daily consumption → charge to 100%
CONF_BATTERY_CHARGE_COST_TOLERANCE_PCT = "battery_charge_cost_tolerance_pct"
DEFAULT_BATTERY_CHARGE_COST_TOLERANCE_PCT = 15.0  # Audit 4.4: bei Grundlast-Defizit bis PV-Start trotzdem laden, wenn ≤ X % teurer als Morgen-Netz

CONF_AMPEL_PV_THRESHOLD = "ampel_pv_threshold_kw"
DEFAULT_AMPEL_PV_THRESHOLD = 2.0
CONF_AMPEL_SCORE_GOOD = "ampel_score_good"
DEFAULT_AMPEL_SCORE_GOOD = 30
CONF_AMPEL_SCORE_BAD = "ampel_score_bad"
DEFAULT_AMPEL_SCORE_BAD = 70
CONF_AMPEL_PV_ENTITY = "ampel_pv_entity"
CONF_AMPEL_BATTERY_MARGIN_KWH = "ampel_battery_margin_kwh"
DEFAULT_AMPEL_BATTERY_MARGIN_KWH = 5.0

# Heating / WP control
CONF_HEATING_TEMP_SENSOR_1 = "heating_temp_sensor_1"
CONF_HEATING_TEMP_SENSOR_2 = "heating_temp_sensor_2"
CONF_HEATING_TEMP_SENSOR_3 = "heating_temp_sensor_3"
CONF_HEATING_COMFORT_MIN = "heating_comfort_min"
DEFAULT_HEATING_COMFORT_MIN = 21.0
CONF_HEATING_PV_MAX = "heating_pv_max"
DEFAULT_HEATING_PV_MAX = 23.0
CONF_HEATING_WP_ENTITY = "heating_wp_entity"
CONF_HEATING_BLOCK_ENTITY = "heating_block_entity"  # on → Heizungsmodul greift nicht ein (z.B. input_boolean.boiler_hygiene_aktiv, Audit 9.3)
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
DEFAULT_HEATING_MAX_SCORE = 50
CONF_HEATING_MAX_SCORE_ABSOLUTE = "heating_max_score_absolute"
DEFAULT_HEATING_MAX_SCORE_ABSOLUTE = 90
CONF_HEATING_FROST_MIN = "heating_frost_min"
DEFAULT_HEATING_FROST_MIN = 16.0

CONF_STROM_SPAREN_SCORE = "strom_sparen_score"
DEFAULT_STROM_SPAREN_SCORE = 60

# Baseline tariff per season (netto CHF/kWh, without VAT)
CONF_BASELINE_WINTER = "baseline_winter"
CONF_BASELINE_FRUEHLING = "baseline_fruehling"
CONF_BASELINE_SOMMER = "baseline_sommer"
CONF_BASELINE_HERBST = "baseline_herbst"
# Defaults: EKZ 2026 netto (brutto / 1.081)
DEFAULT_BASELINE_WINTER = 0.2399  # 25.93 Rp brutto
DEFAULT_BASELINE_FRUEHLING = 0.1969  # 21.28 Rp brutto
DEFAULT_BASELINE_SOMMER = 0.1969  # 21.28 Rp brutto
DEFAULT_BASELINE_HERBST = 0.2399  # 25.93 Rp brutto

CONF_FLAT_TARIFF_SPREAD_RP = "flat_tariff_spread_rp"
DEFAULT_FLAT_TARIFF_SPREAD_RP = 3.0  # Tages-Spread < 3 Rp → Tarif gilt als flat
CONF_FLAT_GRID_EARLIEST_HOUR = "flat_grid_earliest_hour"
DEFAULT_FLAT_GRID_EARLIEST_HOUR = 15  # Bei flat: Grid-Consumer frühestens ab dieser Stunde

CONF_MIN_VALID_PRICE = "min_valid_price"
DEFAULT_MIN_VALID_PRICE = 0.05
CONF_MAX_VALID_PRICE = "max_valid_price"
DEFAULT_MAX_VALID_PRICE = 1.00
CONF_HEATING_PV_WAIT_MINUTES = "heating_pv_wait_minutes"
DEFAULT_HEATING_PV_WAIT_MINUTES = 60
CONF_HEATING_ENABLED = "heating_enabled"
CONF_HEATING_PV_SURPLUS_ENTITY = "heating_pv_surplus_entity"
CONF_PV_SURPLUS_ENTITY = "pv_surplus_entity"
CONF_HEATING_WP_MIN_RUNTIME = "heating_wp_min_runtime"
DEFAULT_HEATING_WP_MIN_RUNTIME = 15

# PV-Notlauf: Wenn keine Tarif-Daten (plan_status error_no_data), fällige
# Scheduler-Consumer trotzdem bei echtem PV-Überschuss laufen lassen.
CONF_EMERGENCY_PV_RUN_ENABLED = "emergency_pv_run_enabled"
DEFAULT_EMERGENCY_PV_RUN_ENABLED = True
EMERGENCY_PV_START_FACTOR = 1.2  # Start: Surplus ≥ 120% der Consumer-Leistung
EMERGENCY_PV_STOP_FACTOR = 0.8   # Stop: Surplus < 80% (Hysterese)

# Pool
CONF_POOL_FILTER_MIN_SURPLUS_KW = "pool_filter_min_surplus_kw"
DEFAULT_POOL_FILTER_MIN_SURPLUS_KW = 1.0
CONF_POOL_WP_MIN_SURPLUS_KW = "pool_wp_min_surplus_kw"
DEFAULT_POOL_WP_MIN_SURPLUS_KW = 3.0  # Filter (1kW) + WP (2kW)
CONF_POOL_DWELL_MINUTES = "pool_dwell_minutes"
DEFAULT_POOL_DWELL_MINUTES = 5
CONF_POOL_WP_MIN_RUNTIME = "pool_wp_min_runtime"
DEFAULT_POOL_WP_MIN_RUNTIME = 15

# Light Auto-Off (seit 2026-04-25)
CONF_LIGHT_HELPER_PATTERN = "light_helper_pattern"
DEFAULT_LIGHT_HELPER_PATTERN = "input_select.licht_mode_*"
CONF_LIGHT_AUTO_OFF_TIMEOUT_MINUTES = "light_auto_off_timeout_minutes"
DEFAULT_LIGHT_AUTO_OFF_TIMEOUT_MINUTES = 10  # nach Push, ohne Reaktion
LIGHT_AUTO_OFF_LABEL_PREFIX = "auto_off_"
LIGHT_AUTO_OFF_HOURS = (1, 2, 4, 6)
LIGHT_AUTO_OFF_TEST_MINUTES = (1,)  # Test-Label: auto_off_1m → 1 min Schwelle (feuert beim nächsten Tick)
LIGHT_AUTO_OFF_TICK_MINUTES = 5  # Produktions-Default; Test-Schwelle 1m feuert dann nach 1-6 min
LIGHT_MODE_OFF_VALUE = "Aus"

# PV-Überschuss Dump-Loads (seit 2026-07-05)
# Label-basierte Geräteliste: alle switch/light/input_boolean mit Label
# PV_DUMP_LABEL werden bei deutlicher Netzeinspeisung gemeinsam eingeschaltet.
CONF_PV_DUMP_ENABLED = "pv_dump_enabled"
DEFAULT_PV_DUMP_ENABLED = False
CONF_PV_DUMP_GRID_OUT_ENTITY = "pv_dump_grid_out_entity"  # Netzeinspeisung in W (positiv = ins Netz)
DEFAULT_PV_DUMP_GRID_OUT_ENTITY = "sensor.emma_grid_out_power"
CONF_PV_DUMP_GRID_IN_ENTITY = "pv_dump_grid_in_entity"  # Netzbezug in W (positiv = aus Netz)
DEFAULT_PV_DUMP_GRID_IN_ENTITY = "sensor.emma_grid_in_power"
CONF_PV_DUMP_ON_WATTS = "pv_dump_on_watts"
DEFAULT_PV_DUMP_ON_WATTS = 2000  # Einspeisung darüber (für Dwell) → einschalten
CONF_PV_DUMP_OFF_WATTS = "pv_dump_off_watts"
DEFAULT_PV_DUMP_OFF_WATTS = 500  # Netzbezug darüber (für Dwell) → ausschalten
CONF_PV_DUMP_DWELL_SECONDS = "pv_dump_dwell_seconds"
DEFAULT_PV_DUMP_DWELL_SECONDS = 120  # Schwelle muss so lange anhalten, gegen Wolken-Flackern
CONF_PV_DUMP_MIN_ON_MINUTES = "pv_dump_min_on_minutes"
DEFAULT_PV_DUMP_MIN_ON_MINUTES = 15  # Mindest-Laufzeit nach Einschalten (gegen Flattern bei "alle gleichzeitig")
CONF_PV_DUMP_PV_POWER_ENTITY = "pv_dump_pv_power_entity"  # PV-Produktion in W — bei ~0 ausschalten
DEFAULT_PV_DUMP_PV_POWER_ENTITY = "sensor.emma_pv_output_power"
PV_DUMP_PV_ZERO_WATTS = 50  # PV-Produktion darunter (für Dwell) gilt als "aus" → Geräte aus.
                            # Abends deckt der Hausakku die Last → grid_in erreicht off_watts nie.
PV_DUMP_LABEL = "pv_ueberschuss"
PV_DUMP_TICK_SECONDS = 30

# Standby-Wächter (seit 2026-08-19)
# Switches mit Label STANDBY_WATCH_LABEL werden überwacht: Gerät ist an, zieht
# aber ununterbrochen weniger als max_watts über die konfigurierte Dauer →
# direkt abschalten + Activity-Log + Info-Push. Der Leistungssensor wird
# automatisch über die Device-Registry gefunden (gleiches Gerät, device_class
# power). Dauer pro Gerät via CONF_STANDBY_WATCH_HOURS, Schwelle pro Gerät via
# CONF_STANDBY_WATCH_WATTS (beides JSON auf Settings-Card, leer = global).
CONF_STANDBY_WATCH_ENABLED = "standby_watch_enabled"
DEFAULT_STANDBY_WATCH_ENABLED = False
CONF_STANDBY_WATCH_MAX_WATTS = "standby_watch_max_watts"
DEFAULT_STANDBY_WATCH_MAX_WATTS = 10.0  # darunter gilt das Gerät als "dümpelt nur"
CONF_STANDBY_WATCH_DEFAULT_HOURS = "standby_watch_default_hours"
DEFAULT_STANDBY_WATCH_DEFAULT_HOURS = 2.0  # Dauer für Geräte ohne eigenen Eintrag
CONF_STANDBY_WATCH_HOURS = "standby_watch_hours"  # JSON {entity_id: stunden}
DEFAULT_STANDBY_WATCH_HOURS = "{}"
CONF_STANDBY_WATCH_WATTS = "standby_watch_watts"  # JSON {entity_id: watt}, leer = max_watts
DEFAULT_STANDBY_WATCH_WATTS = "{}"
STANDBY_WATCH_LABEL = "standby_aus"
STANDBY_WATCH_TICK_MINUTES = 5
STANDBY_WATCH_HOUR_CHOICES = [1, 2, 4, 8]

# Ladeempfehlung / EV-Ladeermahnung (seit 2026-08-12)
# binary_sensor.tariff_saver_ladeempfehlung: ON wenn Anstecken der EV-Fahrzeuge
# lohnt — günstiges Nachtfenster (Score auf Jahres-Skala) ODER PV-Überschuss morgen.
# Die Ermahnungs-Pushes selbst sind HA-Automationen (pro Fahrzeug, 19:30).
CONF_LADEEMPFEHLUNG_ENABLED = "ladeempfehlung_enabled"
DEFAULT_LADEEMPFEHLUNG_ENABLED = True
CONF_LADEEMPFEHLUNG_MAX_SCORE = "ladeempfehlung_max_score"
DEFAULT_LADEEMPFEHLUNG_MAX_SCORE = 30  # Fenster-Score (Jahres-Skala wie day_score) ≤ → günstig
CONF_LADEEMPFEHLUNG_WINDOW_HOURS = "ladeempfehlung_window_hours"
DEFAULT_LADEEMPFEHLUNG_WINDOW_HOURS = 3  # zusammenhängendes Nachtfenster dieser Länge
CONF_LADEEMPFEHLUNG_MIN_PV_KWH = "ladeempfehlung_min_pv_kwh"
DEFAULT_LADEEMPFEHLUNG_MIN_PV_KWH = 5.0  # PV-Überschuss morgen (nach Grundlast) ≥ → lohnt (WN7 braucht 4-5 kWh)
LADEEMPFEHLUNG_NIGHT_START_HOUR = 21  # Nachtfenster 21:00–07:00 lokal
LADEEMPFEHLUNG_NIGHT_END_HOUR = 7

# Ferienmodus (seit 2026-07-16)
# Overlay-Unterdrückung: solange die Ferien-Entity im konfigurierten State ist,
# werden markierte Consumer (pause_on_vacation) nicht geplant und die PV-Dump-Loads
# pausiert. Enabled-Flags bleiben unangetastet — kein Restore nötig.
CONF_VACATION_ENTITY = "vacation_entity"
DEFAULT_VACATION_ENTITY = ""
CONF_VACATION_STATE = "vacation_state"
DEFAULT_VACATION_STATE = "on"
CONF_PV_DUMP_PAUSE_ON_VACATION = "pv_dump_pause_on_vacation"
DEFAULT_PV_DUMP_PAUSE_ON_VACATION = True

# Billing / Abrechnung
CONF_BILLING_PERIOD_MONTHS = "billing_period_months"
DEFAULT_BILLING_PERIOD_MONTHS = 3
CONF_BILLING_START_DATE = "billing_start_date"
CONF_MWST_PERCENT = "mwst_percent"
DEFAULT_MWST_PERCENT = 8.1
CONF_BILLING_FIXED_COSTS = "billing_fixed_costs"
