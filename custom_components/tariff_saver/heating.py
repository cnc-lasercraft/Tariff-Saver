"""Heating / heat pump control for Tariff Saver."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


def _get_cfg(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _get_avg_temp(hass: HomeAssistant, entry: ConfigEntry) -> float | None:
    """Get average temperature from configured sensors."""
    from .const import CONF_HEATING_TEMP_SENSOR_1, CONF_HEATING_TEMP_SENSOR_2, CONF_HEATING_TEMP_SENSOR_3
    sensors = [
        str(_get_cfg(entry, CONF_HEATING_TEMP_SENSOR_1, "") or "").strip(),
        str(_get_cfg(entry, CONF_HEATING_TEMP_SENSOR_2, "") or "").strip(),
        str(_get_cfg(entry, CONF_HEATING_TEMP_SENSOR_3, "") or "").strip(),
    ]
    temps = []
    for entity_id in sensors:
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            continue
        try:
            temps.append(float(state.state))
        except (ValueError, TypeError):
            continue
    if not temps:
        return None
    return round(sum(temps) / len(temps), 2)


def _get_pv_surplus_kw(hass: HomeAssistant, entry: ConfigEntry) -> float:
    """Get current PV surplus in kW from dedicated heating PV entity."""
    from .const import CONF_HEATING_PV_SURPLUS_ENTITY
    pv_entity = str(_get_cfg(entry, CONF_HEATING_PV_SURPLUS_ENTITY, "") or "").strip()
    if not pv_entity:
        return 0.0
    state = hass.states.get(pv_entity)
    if state is None:
        return 0.0
    try:
        val = float(state.state)
        return val / 1000.0 if val > 100 else val  # auto-detect W vs kW
    except (ValueError, TypeError):
        return 0.0


def _minutes_until_pv(hass: HomeAssistant, entry: ConfigEntry, threshold_kw: float) -> int | None:
    """Return minutes until PV forecast exceeds threshold. None if no forecast or no PV today."""
    from .const import CONF_PV_FORECAST_ENTITY, CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE
    pv_entity = str(_get_cfg(entry, CONF_PV_FORECAST_ENTITY, "") or "").strip()
    pv_attr = str(_get_cfg(entry, CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE) or DEFAULT_PV_FORECAST_ATTRIBUTE).strip()
    if not pv_entity:
        return None
    state = hass.states.get(pv_entity)
    if state is None:
        return None
    forecast = state.attributes.get(pv_attr, [])
    if not isinstance(forecast, list):
        return None
    now_ts = dt_util.utcnow().timestamp()
    for slot in forecast:
        if not isinstance(slot, dict):
            continue
        period = slot.get("period_start")
        pv_est = slot.get("pv_estimate", 0)
        if period and isinstance(pv_est, (int, float)) and pv_est >= threshold_kw:
            try:
                slot_dt = dt_util.parse_datetime(str(period))
                if slot_dt and slot_dt.timestamp() > now_ts:
                    minutes = int((slot_dt.timestamp() - now_ts) / 60)
                    return minutes
            except Exception:
                pass
    return None


def _get_temp_trend(store, minutes: int = 30) -> float | None:
    """Get temperature trend in °C/hour from stored readings. Negative = falling."""
    readings = getattr(store, "_heating_temp_history", [])
    if len(readings) < 2:
        return None
    now = dt_util.utcnow().timestamp()
    cutoff = now - (minutes * 60)
    recent = [(ts, t) for ts, t in readings if ts >= cutoff]
    if len(recent) < 2:
        return None
    oldest_ts, oldest_t = recent[0]
    newest_ts, newest_t = recent[-1]
    dt_hours = (newest_ts - oldest_ts) / 3600.0
    if dt_hours < 0.05:  # less than 3 minutes
        return None
    return round((newest_t - oldest_t) / dt_hours, 2)


def _record_temp(store, temp: float) -> None:
    """Record temperature reading for trend calculation."""
    if not hasattr(store, "_heating_temp_history"):
        store._heating_temp_history = []
    now = dt_util.utcnow().timestamp()
    store._heating_temp_history.append((now, temp))
    # Keep last 2 hours
    cutoff = now - 7200
    store._heating_temp_history = [(ts, t) for ts, t in store._heating_temp_history if ts >= cutoff]


async def _set_wp(hass: HomeAssistant, entity_id: str, value: str) -> None:
    """Set WP entity to given value, auto-detecting the domain."""
    if not entity_id or not value:
        return
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    _LOGGER.info("Heating: setting %s to '%s'", entity_id, value)
    if domain == "switch":
        service = "turn_on" if value.lower() in ("on", "true", "1") else "turn_off"
        await hass.services.async_call("switch", service, {"entity_id": entity_id})
    elif domain in ("select", "input_select"):
        await hass.services.async_call(domain, "select_option", {"entity_id": entity_id, "option": value})
    elif domain == "climate":
        await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": value})
    else:
        _LOGGER.warning("Heating: unknown domain '%s' for entity %s", domain, entity_id)


async def async_heating_tick(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> None:
    """Main heating control tick — called every 5 minutes."""
    from .const import (
        CONF_HEATING_WP_ENTITY,
        CONF_HEATING_WP_OFF_VALUE, DEFAULT_HEATING_WP_OFF_VALUE,
        CONF_HEATING_SEASONS, DEFAULT_HEATING_SEASONS,
        CONF_HEATING_COMFORT_MIN, DEFAULT_HEATING_COMFORT_MIN,
        CONF_HEATING_PV_MAX, DEFAULT_HEATING_PV_MAX,
        CONF_HEATING_WP_HEAT_VALUE, DEFAULT_HEATING_WP_HEAT_VALUE,
        CONF_HEATING_MAX_SCORE, DEFAULT_HEATING_MAX_SCORE,
        CONF_HEATING_MAX_SCORE_ABSOLUTE, DEFAULT_HEATING_MAX_SCORE_ABSOLUTE,
        CONF_HEATING_FROST_MIN, DEFAULT_HEATING_FROST_MIN,
        CONF_HEATING_PV_WAIT_MINUTES, DEFAULT_HEATING_PV_WAIT_MINUTES,
        CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD,
    )
    from .sensor import _current_score_live

    # --- Config ---
    seasons_str = str(_get_cfg(entry, CONF_HEATING_SEASONS, DEFAULT_HEATING_SEASONS) or DEFAULT_HEATING_SEASONS)
    active_seasons = [s.strip() for s in seasons_str.split(",") if s.strip()]
    comfort_min = float(_get_cfg(entry, CONF_HEATING_COMFORT_MIN, DEFAULT_HEATING_COMFORT_MIN) or DEFAULT_HEATING_COMFORT_MIN)
    pv_max = float(_get_cfg(entry, CONF_HEATING_PV_MAX, DEFAULT_HEATING_PV_MAX) or DEFAULT_HEATING_PV_MAX)
    wp_entity = str(_get_cfg(entry, CONF_HEATING_WP_ENTITY, "") or "").strip()
    wp_off = str(_get_cfg(entry, CONF_HEATING_WP_OFF_VALUE, DEFAULT_HEATING_WP_OFF_VALUE) or DEFAULT_HEATING_WP_OFF_VALUE)
    wp_heat = str(_get_cfg(entry, CONF_HEATING_WP_HEAT_VALUE, DEFAULT_HEATING_WP_HEAT_VALUE) or DEFAULT_HEATING_WP_HEAT_VALUE)
    max_score = int(float(_get_cfg(entry, CONF_HEATING_MAX_SCORE, DEFAULT_HEATING_MAX_SCORE) or DEFAULT_HEATING_MAX_SCORE))
    max_score_abs = int(float(_get_cfg(entry, CONF_HEATING_MAX_SCORE_ABSOLUTE, DEFAULT_HEATING_MAX_SCORE_ABSOLUTE) or DEFAULT_HEATING_MAX_SCORE_ABSOLUTE))
    frost_min = float(_get_cfg(entry, CONF_HEATING_FROST_MIN, DEFAULT_HEATING_FROST_MIN) or DEFAULT_HEATING_FROST_MIN)
    pv_wait = int(float(_get_cfg(entry, CONF_HEATING_PV_WAIT_MINUTES, DEFAULT_HEATING_PV_WAIT_MINUTES) or DEFAULT_HEATING_PV_WAIT_MINUTES))
    pv_threshold = float(_get_cfg(entry, CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD) or DEFAULT_AMPEL_PV_THRESHOLD)

    # --- Guard: WP Sperre ---
    sperre = hass.states.get("input_boolean.tariff_saver_wp_sperre")
    if sperre and sperre.state == "on":
        wp_entity = str(_get_cfg(entry, CONF_HEATING_WP_ENTITY, "") or "").strip()
        wp_off = str(_get_cfg(entry, CONF_HEATING_WP_OFF_VALUE, DEFAULT_HEATING_WP_OFF_VALUE) or DEFAULT_HEATING_WP_OFF_VALUE)
        if wp_entity:
            wp_state_obj = hass.states.get(wp_entity)
            if wp_state_obj and wp_state_obj.state != wp_off:
                _LOGGER.info("WP Sperre aktiv: %s → '%s'", wp_entity, wp_off)
                store = getattr(coordinator, "store", None)
                if store:
                    store.log_activity("🚫", f"WP Sperre aktiv → {wp_off}")
                    hass.async_create_task(store.async_save())
                await _set_wp(hass, wp_entity, wp_off)
        return

    # --- Guard: enabled check ---
    from .const import CONF_HEATING_ENABLED
    enabled = _get_cfg(entry, CONF_HEATING_ENABLED, False)
    if not enabled:
        return

    # --- Guard: need WP entity ---
    if not wp_entity:
        return

    # --- Guard: check season ---
    store = getattr(coordinator, "store", None)
    from .__init__ import _get_season
    current_season = _get_season(hass, entry)
    if current_season not in active_seasons:
        _LOGGER.debug("Heating: season '%s' not in active seasons %s, skipping", current_season, active_seasons)
        return

    # --- Get inputs ---
    avg_temp = _get_avg_temp(hass, entry)
    if avg_temp is None:
        _LOGGER.debug("Heating: no temperature readings, skipping")
        return

    # Record temperature for trend
    if store:
        _record_temp(store, avg_temp)

    score = _current_score_live(coordinator)
    if score is None:
        score = 50

    pv_surplus = _get_pv_surplus_kw(hass, entry)
    minutes_to_pv = _minutes_until_pv(hass, entry, pv_threshold)
    temp_trend = _get_temp_trend(store) if store else None  # °C/hour

    # Current WP state
    wp_state_obj = hass.states.get(wp_entity)
    wp_current = wp_state_obj.state if wp_state_obj else None

    # Remember user's baseline state (what the WP was set to before heating module changed it)
    # This includes "Aus" — if user manually turned WP off, we respect that.
    if store and not hasattr(store, "_heating_wp_previous"):
        store._heating_wp_previous = None
    if store and not hasattr(store, "_heating_module_active"):
        store._heating_module_active = False
    if store and wp_current and not store._heating_module_active:
        # Heating module hasn't changed WP yet — current state is user's baseline
        store._heating_wp_previous = wp_current

    # --- Decision ---
    action = None  # None = no change, "heat" or "off"
    reason = ""

    # --- Score-Stufe 2: Absolut-Grenze (auch bei Kälte aus, nur Frostschutz) ---
    if score > max_score_abs and avg_temp >= frost_min and pv_surplus < pv_threshold:
        if wp_current != wp_off:
            action = "off"
            reason = f"Score {score} > Absolut-Grenze {max_score_abs}, Temp {avg_temp}°C über Frostschutz {frost_min}°C"
        else:
            reason = f"Score {score} > Absolut-Grenze {max_score_abs}, WP bleibt aus"

    # --- Score-Stufe 1: Teuer-Grenze (aus, ausser unter Komfort-Min) ---
    elif score > max_score and avg_temp >= comfort_min and pv_surplus < pv_threshold:
        if wp_current == wp_heat:
            action = "off"
            reason = f"Score {score} > Teuer-Grenze {max_score}, Temp {avg_temp}°C über Komfort {comfort_min}°C"
        else:
            reason = f"Score {score} > Teuer-Grenze {max_score}, WP bleibt aus"

    # --- Temperaturzonen (normale Logik, Score unter Grenzwerten) ---
    elif avg_temp >= pv_max:
        # Above PV-Max: stop heating, restore to previous (e.g. WW)
        if wp_current == wp_heat:
            action = "restore"
            reason = f"Temp {avg_temp}°C >= PV-Max {pv_max}°C"

    elif avg_temp >= comfort_min:
        # Between comfort and PV-Max: only PV surplus
        if pv_surplus >= pv_threshold:
            if wp_current != wp_heat:
                action = "heat"
                reason = f"PV-Überschuss {pv_surplus:.1f} kW, Temp {avg_temp}°C im PV-Bereich"
        else:
            if wp_current == wp_heat:
                action = "restore"
                reason = f"Kein PV-Überschuss, Temp {avg_temp}°C über Komfort-Min"

    else:
        # Below comfort minimum (and score <= max_score_abs or temp < frost_min)
        if pv_surplus >= pv_threshold:
            # PV available: heat!
            if wp_current != wp_heat:
                action = "heat"
                reason = f"PV-Überschuss {pv_surplus:.1f} kW, Temp {avg_temp}°C unter Komfort"

        else:
            # No PV: check if we should wait
            effective_wait = pv_wait
            if temp_trend is not None and temp_trend < -0.5:
                effective_wait = max(15, pv_wait // 2)
                _LOGGER.info("Heating: temp falling fast (%.1f°C/h), halving wait to %d min", temp_trend, effective_wait)

            if minutes_to_pv is not None and minutes_to_pv <= effective_wait:
                # PV coming soon: stop grid heating, restore to WW
                if wp_current == wp_heat:
                    action = "restore"
                    reason = f"PV in {minutes_to_pv} min, stoppe Grid-Heizen, warte auf PV"
                else:
                    reason = f"Temp {avg_temp}°C unter Komfort, PV in {minutes_to_pv} min, warte"
            else:
                # No PV soon: must heat from grid
                if wp_current != wp_heat:
                    action = "heat"
                    pv_info = f"PV in {minutes_to_pv} min" if minutes_to_pv else "kein PV heute"
                    reason = f"Temp {avg_temp}°C unter Komfort, {pv_info}, heize vom Netz"

    # --- Min runtime check ---
    from .const import CONF_HEATING_WP_MIN_RUNTIME
    wp_min_runtime = int(float(_get_cfg(entry, CONF_HEATING_WP_MIN_RUNTIME, 15) or 15))
    if not hasattr(store, "_heating_wp_started_at"):
        store._heating_wp_started_at = None if store else None
    if store and action in ("restore", "off") and store._heating_wp_started_at:
        elapsed = (dt_util.utcnow() - store._heating_wp_started_at).total_seconds() / 60.0
        if elapsed < wp_min_runtime:
            _LOGGER.debug("Heating: min runtime %d min not reached (%.1f min), keeping on", wp_min_runtime, elapsed)
            action = None
            reason = f"Min. Laufzeit {wp_min_runtime} min nicht erreicht ({int(elapsed)} min)"
    if store and action == "heat":
        if store._heating_wp_started_at is None:
            store._heating_wp_started_at = dt_util.utcnow()
    elif store and action in ("restore", "off"):
        store._heating_wp_started_at = None

    # --- Guard: respect user's manual "Aus" (don't heat if user turned WP off) ---
    # Exception: frost protection always heats
    if action == "heat" and store:
        baseline = getattr(store, "_heating_wp_previous", None)
        if baseline == wp_off and avg_temp >= frost_min:
            _LOGGER.debug("Heating: user baseline is '%s', skipping heat (frost_min=%.1f, avg=%.1f)", wp_off, frost_min, avg_temp)
            action = None
            reason = f"WP manuell aus, Temp {avg_temp}°C über Frostschutz {frost_min}°C"

    # --- Execute ---
    if action == "heat":
        _LOGGER.info("Heating ON: %s → '%s' (%s)", wp_entity, wp_heat, reason)
        if store:
            store._heating_module_active = True
            store.log_activity("🔥", f"WP → {wp_heat} ({reason})")
            hass.async_create_task(store.async_save())
        await _set_wp(hass, wp_entity, wp_heat)
    elif action == "off":
        _LOGGER.info("Heating OFF: %s → '%s' (%s)", wp_entity, wp_off, reason)
        if store:
            store._heating_module_active = True
            store.log_activity("⏹️", f"WP → {wp_off} ({reason})")
            hass.async_create_task(store.async_save())
        await _set_wp(hass, wp_entity, wp_off)
    elif action == "restore":
        # Restore to user's baseline (WW, Aus, etc.)
        restore = getattr(store, "_heating_wp_previous", None) if store else None
        target = restore if restore and restore != wp_heat else wp_off
        _LOGGER.info("Heating RESTORE: %s → '%s' (%s)", wp_entity, target, reason)
        if store:
            store._heating_module_active = False
            store.log_activity("↩️", f"WP → {target} ({reason})")
            hass.async_create_task(store.async_save())
        await _set_wp(hass, wp_entity, target)
    else:
        _LOGGER.debug("Heating: no change needed. WP='%s', Temp=%.1f°C, Score=%d. %s",
                       wp_current, avg_temp, score, reason)
        # If heating module is not actively controlling and WP changed externally, update baseline
        if store and not store._heating_module_active and wp_current != getattr(store, "_heating_wp_previous", None):
            store._heating_wp_previous = wp_current
