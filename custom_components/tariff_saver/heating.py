"""Heating / heat pump control for Tariff Saver."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

MODUS_ENTITY = "input_select.tariff_saver_wp_modus"
MODUS_AUTO = "Auto"
MODUS_HEIZEN = "Heizen"
MODUS_NUR_WW = "Nur Warmwasser"
MODUS_AUS = "Aus"


def _get_cfg(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _get_avg_temp(hass: HomeAssistant, entry: ConfigEntry) -> float | None:
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
    from .const import CONF_HEATING_PV_SURPLUS_ENTITY
    from .slot_helpers import state_to_kw
    pv_entity = str(_get_cfg(entry, CONF_HEATING_PV_SURPLUS_ENTITY, "") or "").strip()
    if not pv_entity:
        return 0.0
    return state_to_kw(hass.states.get(pv_entity))


def _minutes_until_pv(hass: HomeAssistant, entry: ConfigEntry, threshold_kw: float) -> int | None:
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
                    return int((slot_dt.timestamp() - now_ts) / 60)
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "return int((slot_dt.timestamp() - now_ts", _err)
    return None


async def _set_wp(hass: HomeAssistant, entity_id: str, value: str) -> None:
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


def _ww_consumer_blocks(hass: HomeAssistant, entry: ConfigEntry, wp_current: str | None, wp_ww: str) -> bool:
    """True wenn ein WW-/Hygiene-Prozess läuft — Heizungsmodul greift dann nicht ein (Audit 9.3).

    Blockiert bei (a) `heating_block_entity` == on (z.B. input_boolean.boiler_hygiene_aktiv —
    deckt Hygiene-Phase 2 mit Heizstab bei WP „Aus") oder (b) einem run_order>0-Consumer
    mit should_run on — unabhängig vom aktuellen WP-Modus (vorher nur bei WP == WW-Modus).
    """
    from .const import CONF_HEATING_BLOCK_ENTITY
    block_entity = str(_get_cfg(entry, CONF_HEATING_BLOCK_ENTITY, "") or "").strip()
    if block_entity:
        st = hass.states.get(block_entity)
        if st is not None and st.state == "on":
            return True
    consumers = entry.options.get("consumers", {})
    for slot, cfg in consumers.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        try:
            ro = int(cfg.get("run_order", 0) or 0)
        except (ValueError, TypeError):
            ro = 0
        if ro <= 0:
            continue
        should_run = hass.states.get(f"binary_sensor.tariff_saver_consumer_{slot}_should_run")
        if should_run and should_run.state == "on":
            return True
    return False


def _decide_winter(score: int, avg_temp: float, max_score_abs: int, frost_min: float,
                   wp_current: str | None, wp_heat: str, wp_off: str) -> tuple[str | None, str]:
    """Winter: immer heizen, ausser score > max_score_abs (Frost übersteuert)."""
    if score > max_score_abs and avg_temp >= frost_min:
        if wp_current != wp_off:
            return "off", f"Winter: Score {score} > Absolut-Grenze {max_score_abs}, Temp {avg_temp}°C über Frost {frost_min}°C"
        return None, f"Winter: Score {score} > {max_score_abs}, WP bleibt aus"
    if wp_current != wp_heat:
        return "heat", f"Winter: Score {score}, Temp {avg_temp}°C → heizen"
    return None, f"Winter: WP heizt bereits"


def _decide_shoulder(score: int, avg_temp: float, pv_surplus: float, minutes_to_pv: int | None,
                     comfort_min: float, pv_max: float, frost_min: float,
                     max_score: int, max_score_abs: int, wp_power_kw: float, pv_wait: int,
                     wp_current: str | None, wp_heat: str) -> tuple[str | None, str]:
    """Frühling/Herbst: frost/comfort/pv_max + score + pv-wait logic.
    PV-Schwelle für WP-Start ist die WP-Leistung selbst — erst wenn Überschuss die WP deckt."""
    pv_ok = pv_surplus >= wp_power_kw

    # Frost übersteuert alles ausser Absolut-Grenze
    if avg_temp < frost_min:
        if score > max_score_abs and not pv_ok:
            return ("restore", f"Frost {avg_temp}°C, aber Score {score} > Absolut {max_score_abs}") if wp_current == wp_heat else (None, "Frost, aber Absolut-Sperre")
        if wp_current != wp_heat:
            return "heat", f"Frostschutz: Temp {avg_temp}°C < {frost_min}°C"
        return None, "Frostschutz aktiv"

    # Absolut-Grenze (aus auch bei Kälte, ausser PV)
    if score > max_score_abs and not pv_ok:
        return ("restore", f"Score {score} > Absolut {max_score_abs}") if wp_current == wp_heat else (None, f"Score {score} > Absolut, aus")

    # Temp >= PV-Max → aus
    if avg_temp >= pv_max:
        return ("restore", f"Temp {avg_temp}°C >= PV-Max {pv_max}°C") if wp_current == wp_heat else (None, "Temp über PV-Max")

    # Temp >= comfort: nur PV (und zwar genug um WP zu decken)
    if avg_temp >= comfort_min:
        if pv_ok:
            return ("heat", f"PV-Überschuss {pv_surplus:.2f} kW ≥ WP {wp_power_kw:.1f} kW, Temp {avg_temp}°C") if wp_current != wp_heat else (None, "PV heizt")
        return ("restore", f"PV {pv_surplus:.2f} kW < WP {wp_power_kw:.1f} kW, Temp {avg_temp}°C über Komfort") if wp_current == wp_heat else (None, "Kein PV nötig")

    # Temp < comfort
    if pv_ok:
        return ("heat", f"PV-Überschuss {pv_surplus:.2f} kW ≥ WP {wp_power_kw:.1f} kW, Temp {avg_temp}°C unter Komfort") if wp_current != wp_heat else (None, "PV heizt unter Komfort")

    # Teuer-Grenze: keine Netz-Heizung
    if score > max_score:
        return ("restore", f"Score {score} > Teuer {max_score}, kein PV") if wp_current == wp_heat else (None, f"Score {score} > Teuer, aus")

    # Warten auf PV?
    if minutes_to_pv is not None and minutes_to_pv <= pv_wait:
        return ("restore", f"PV in {minutes_to_pv} min, warte") if wp_current == wp_heat else (None, f"Temp {avg_temp}°C unter Komfort, warte {minutes_to_pv}min auf PV")

    # Grid-Heizung
    pv_info = f"PV in {minutes_to_pv} min" if minutes_to_pv else "kein PV heute"
    return ("heat", f"Temp {avg_temp}°C unter Komfort, {pv_info}, Score {score}") if wp_current != wp_heat else (None, "heizt bereits")


async def async_heating_tick(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> None:
    """Main heating control tick — called every 5 minutes."""
    from .const import (
        CONF_HEATING_WP_ENTITY, CONF_HEATING_ENABLED,
        CONF_HEATING_WP_OFF_VALUE, DEFAULT_HEATING_WP_OFF_VALUE,
        CONF_HEATING_WP_HEAT_VALUE, DEFAULT_HEATING_WP_HEAT_VALUE,
        CONF_HEATING_WP_WW_VALUE, DEFAULT_HEATING_WP_WW_VALUE,
        CONF_HEATING_WP_MIN_RUNTIME,
        CONF_HEATING_WP_POWER_KW, DEFAULT_HEATING_WP_POWER_KW,
        CONF_HEATING_COMFORT_MIN, DEFAULT_HEATING_COMFORT_MIN,
        CONF_HEATING_PV_MAX, DEFAULT_HEATING_PV_MAX,
        CONF_HEATING_MAX_SCORE, DEFAULT_HEATING_MAX_SCORE,
        CONF_HEATING_MAX_SCORE_ABSOLUTE, DEFAULT_HEATING_MAX_SCORE_ABSOLUTE,
        CONF_HEATING_FROST_MIN, DEFAULT_HEATING_FROST_MIN,
        CONF_HEATING_PV_WAIT_MINUTES, DEFAULT_HEATING_PV_WAIT_MINUTES,
        CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD,
    )
    from .sensor import _current_score_live

    wp_entity = str(_get_cfg(entry, CONF_HEATING_WP_ENTITY, "") or "").strip()
    if not wp_entity:
        return

    wp_off = str(_get_cfg(entry, CONF_HEATING_WP_OFF_VALUE, DEFAULT_HEATING_WP_OFF_VALUE) or DEFAULT_HEATING_WP_OFF_VALUE)
    wp_heat = str(_get_cfg(entry, CONF_HEATING_WP_HEAT_VALUE, DEFAULT_HEATING_WP_HEAT_VALUE) or DEFAULT_HEATING_WP_HEAT_VALUE)
    wp_ww = str(_get_cfg(entry, CONF_HEATING_WP_WW_VALUE, DEFAULT_HEATING_WP_WW_VALUE) or DEFAULT_HEATING_WP_WW_VALUE)

    wp_state_obj = hass.states.get(wp_entity)
    wp_current = wp_state_obj.state if wp_state_obj else None
    store = getattr(coordinator, "store", None)

    # --- Modus-Override (Aus/Heizen) übersteuert alles ---
    modus_state = hass.states.get(MODUS_ENTITY)
    modus = modus_state.state if modus_state else MODUS_AUTO

    if modus == MODUS_AUS:
        if wp_current != wp_off:
            _LOGGER.info("Modus=Aus: %s → '%s'", wp_entity, wp_off)
            if store:
                store.heating_module_active = True
                store.log_activity("🚫", f"Modus Aus → {wp_off}")
                hass.async_create_task(store.async_save())
            await _set_wp(hass, wp_entity, wp_off)
        return

    if modus == MODUS_HEIZEN:
        if wp_current != wp_heat:
            _LOGGER.info("Modus=Heizen: %s → '%s'", wp_entity, wp_heat)
            if store:
                store.heating_module_active = True
                store.log_activity("🔥", f"Modus Heizen → {wp_heat}")
                hass.async_create_task(store.async_save())
            await _set_wp(hass, wp_entity, wp_heat)
        return

    if modus == MODUS_NUR_WW:
        if wp_current != wp_ww:
            _LOGGER.info("Modus=Nur Warmwasser: %s → '%s'", wp_entity, wp_ww)
            if store:
                store.heating_module_active = True
                store.log_activity("💧", f"Modus Nur WW → {wp_ww}")
                hass.async_create_task(store.async_save())
            await _set_wp(hass, wp_entity, wp_ww)
        return

    # --- Auto-Modus ---
    if not _get_cfg(entry, CONF_HEATING_ENABLED, False):
        return

    if _ww_consumer_blocks(hass, entry, wp_current, wp_ww):
        _LOGGER.debug("Heating: WW-/Hygiene-Prozess aktiv — skipping")
        return

    from .__init__ import _get_season
    current_season = _get_season(hass, entry)

    # Sommer: Modul macht nichts
    if current_season == "Sommer":
        if store and store.heating_module_active:
            store.heating_module_active = False
            hass.async_create_task(store.async_save())
        return

    # Baseline capture: WP-Zustand erfassen, wenn Modul inaktiv ist.
    # Bei erster Runde (baseline=None) NICHT übernehmen — User-Absicht unbekannt nach Restart.
    if store and wp_current and not store.heating_module_active:
        if store.heating_wp_previous is not None and wp_current != store.heating_wp_previous:
            store.heating_wp_previous = wp_current

    avg_temp = _get_avg_temp(hass, entry)
    if avg_temp is None:
        return

    score = _current_score_live(coordinator)
    if score is None:
        score = 50

    comfort_min = float(_get_cfg(entry, CONF_HEATING_COMFORT_MIN, DEFAULT_HEATING_COMFORT_MIN) or DEFAULT_HEATING_COMFORT_MIN)
    pv_max = float(_get_cfg(entry, CONF_HEATING_PV_MAX, DEFAULT_HEATING_PV_MAX) or DEFAULT_HEATING_PV_MAX)
    max_score = int(float(_get_cfg(entry, CONF_HEATING_MAX_SCORE, DEFAULT_HEATING_MAX_SCORE) or DEFAULT_HEATING_MAX_SCORE))
    max_score_abs = int(float(_get_cfg(entry, CONF_HEATING_MAX_SCORE_ABSOLUTE, DEFAULT_HEATING_MAX_SCORE_ABSOLUTE) or DEFAULT_HEATING_MAX_SCORE_ABSOLUTE))
    frost_min = float(_get_cfg(entry, CONF_HEATING_FROST_MIN, DEFAULT_HEATING_FROST_MIN) or DEFAULT_HEATING_FROST_MIN)
    pv_wait = int(float(_get_cfg(entry, CONF_HEATING_PV_WAIT_MINUTES, DEFAULT_HEATING_PV_WAIT_MINUTES) or DEFAULT_HEATING_PV_WAIT_MINUTES))
    pv_threshold = float(_get_cfg(entry, CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD) or DEFAULT_AMPEL_PV_THRESHOLD)
    wp_power_kw = float(_get_cfg(entry, CONF_HEATING_WP_POWER_KW, DEFAULT_HEATING_WP_POWER_KW) or DEFAULT_HEATING_WP_POWER_KW)
    pv_surplus = _get_pv_surplus_kw(hass, entry)
    minutes_to_pv = _minutes_until_pv(hass, entry, pv_threshold)

    # Saison-Dispatcher
    if current_season == "Winter":
        action, reason = _decide_winter(score, avg_temp, max_score_abs, frost_min, wp_current, wp_heat, wp_off)
    else:
        action, reason = _decide_shoulder(
            score, avg_temp, pv_surplus, minutes_to_pv,
            comfort_min, pv_max, frost_min, max_score, max_score_abs,
            wp_power_kw, pv_wait, wp_current, wp_heat,
        )

    # Min-Runtime respektieren (nur für Stop-Actions)
    wp_min_runtime = int(float(_get_cfg(entry, CONF_HEATING_WP_MIN_RUNTIME, 15) or 15))
    if store and not hasattr(store, "_heating_wp_started_at"):
        store._heating_wp_started_at = None
    if store and action in ("restore", "off") and store._heating_wp_started_at:
        elapsed = (dt_util.utcnow() - store._heating_wp_started_at).total_seconds() / 60.0
        if elapsed < wp_min_runtime:
            _LOGGER.debug("Heating: min runtime %d min not reached (%.1f min)", wp_min_runtime, elapsed)
            return
    if store and action == "heat" and store._heating_wp_started_at is None:
        store._heating_wp_started_at = dt_util.utcnow()
    elif store and action in ("restore", "off"):
        store._heating_wp_started_at = None

    # Baseline-Guard: User hat WP manuell "Aus" gestellt → nicht gegen User heizen
    # (Frostschutz im Shoulder-decide gewinnt, weil avg_temp<frost_min bereits dort handled)
    if action == "heat" and store and store.heating_wp_previous == wp_off and avg_temp >= frost_min:
        _LOGGER.debug("Heating: User-Baseline=Aus, Temp %.1f°C über Frost, skip", avg_temp)
        return

    if action is None:
        _LOGGER.debug("Heating no-op: WP=%s Temp=%.1f Score=%d Saison=%s. %s",
                      wp_current, avg_temp, score, current_season, reason)
        return

    if action == "heat":
        _LOGGER.info("Heating ON: %s → '%s' (%s)", wp_entity, wp_heat, reason)
        if store:
            store.heating_module_active = True
            store.log_activity("🔥", f"WP → {wp_heat} ({reason})")
            hass.async_create_task(store.async_save())
        await _set_wp(hass, wp_entity, wp_heat)
    elif action == "off":
        _LOGGER.info("Heating OFF: %s → '%s' (%s)", wp_entity, wp_off, reason)
        if store:
            store.heating_module_active = True
            store.log_activity("⏹️", f"WP → {wp_off} ({reason})")
            hass.async_create_task(store.async_save())
        await _set_wp(hass, wp_entity, wp_off)
    elif action == "restore":
        target = store.heating_wp_previous if (store and store.heating_wp_previous and store.heating_wp_previous != wp_heat) else wp_off
        _LOGGER.info("Heating RESTORE: %s → '%s' (%s)", wp_entity, target, reason)
        if store:
            store.heating_module_active = False
            store.log_activity("↩️", f"WP → {target} ({reason})")
            hass.async_create_task(store.async_save())
        await _set_wp(hass, wp_entity, target)
