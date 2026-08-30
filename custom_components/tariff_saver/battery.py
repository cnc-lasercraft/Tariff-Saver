"""Real-time battery mode manager for Tariff Saver.

Controls battery charge/discharge via Huawei Solar services:
- Hold: Keep battery at current SOC during cheap tariff (use grid instead)
- Charging: Grid-charge battery when Consumer 9 plan is active
- Normal: Let battery discharge naturally (expensive tariff or PV surplus)

State machine: normal ↔ hold ↔ charging (min 15 min dwell time)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Minimum time in a mode before switching (prevents flapping)
_MIN_DWELL_MINUTES = 15


def _get_cfg(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


async def async_battery_tick(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> None:
    """Battery mode control tick — called every 5 minutes."""
    from .const import (
        CONF_BATTERY_ENABLED, CONF_BATTERY_SOC_ENTITY,
        CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT,
        CONF_BATTERY_MAX_CHARGE_KW, DEFAULT_BATTERY_MAX_CHARGE_KW,
        CONF_BATTERY_DEVICE_ID, DEFAULT_BATTERY_DEVICE_ID,
        CONF_BATTERY_HOLD_ENTER_SCORE, DEFAULT_BATTERY_HOLD_ENTER_SCORE,
        CONF_BATTERY_HOLD_EXIT_SCORE, DEFAULT_BATTERY_HOLD_EXIT_SCORE,
        CONF_PV_SURPLUS_ENTITY,
        CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD,
    )
    from .sensor import _current_score_live

    config = entry.options if entry.options else entry.data

    # --- Guard: battery enabled? ---
    if not config.get(CONF_BATTERY_ENABLED, False):
        return

    # --- Read current state ---
    soc_entity = str(config.get(CONF_BATTERY_SOC_ENTITY, "") or "").strip()
    if not soc_entity:
        return

    soc_state = hass.states.get(soc_entity)
    if not soc_state or soc_state.state in ("unavailable", "unknown", ""):
        return
    try:
        current_soc = float(soc_state.state)
    except (ValueError, TypeError):
        return

    min_soc = float(config.get(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT) or DEFAULT_BATTERY_MIN_SOC_PERCENT)
    max_charge_kw = float(config.get(CONF_BATTERY_MAX_CHARGE_KW, DEFAULT_BATTERY_MAX_CHARGE_KW) or DEFAULT_BATTERY_MAX_CHARGE_KW)
    charge_power_w = int(max_charge_kw * 1000)
    pv_threshold = float(config.get(CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD) or DEFAULT_AMPEL_PV_THRESHOLD)
    hold_enter = int(float(config.get(CONF_BATTERY_HOLD_ENTER_SCORE, DEFAULT_BATTERY_HOLD_ENTER_SCORE) or DEFAULT_BATTERY_HOLD_ENTER_SCORE))
    hold_exit = int(float(config.get(CONF_BATTERY_HOLD_EXIT_SCORE, DEFAULT_BATTERY_HOLD_EXIT_SCORE) or DEFAULT_BATTERY_HOLD_EXIT_SCORE))

    # Huawei device ID
    device_id = str(config.get(CONF_BATTERY_DEVICE_ID, DEFAULT_BATTERY_DEVICE_ID) or DEFAULT_BATTERY_DEVICE_ID).strip()
    if not device_id:
        return  # No device configured

    score = _current_score_live(coordinator)
    if score is None:
        score = 50

    # PV surplus
    from .slot_helpers import state_to_kw
    pv_surplus_entity = str(config.get(CONF_PV_SURPLUS_ENTITY, "") or "").strip()
    pv_surplus_kw = state_to_kw(hass.states.get(pv_surplus_entity)) if pv_surplus_entity else 0.0

    # Consumer 9 should_run state
    consumer9_active = False
    consumer9_state = hass.states.get("binary_sensor.tariff_saver_consumer_9_should_run")
    if consumer9_state and consumer9_state.state == "on":
        consumer9_active = True

    # Consumer 9 plan target SOC — prefer scheduler's assessment (in store), fallback to naive sensor
    target_soc_from_plan = None
    store_for_target = getattr(coordinator, "store", None)
    if store_for_target and store_for_target.consumer_plans:
        plan9 = store_for_target.consumer_plans.get("9") or store_for_target.consumer_plans.get(9)
        if isinstance(plan9, dict):
            t = plan9.get("battery_target_soc_pct")
            if isinstance(t, (int, float)) and t > 0:
                target_soc_from_plan = int(round(float(t)))
    if target_soc_from_plan is None:
        energy_until_pv = hass.states.get("sensor.tariff_saver_energy_until_pv")
        if energy_until_pv and energy_until_pv.state not in ("unavailable", "unknown", ""):
            rec = energy_until_pv.attributes.get("recommended_target_soc")
            if isinstance(rec, (int, float)) and rec > 0:
                target_soc_from_plan = int(rec)

    # --- State machine ---
    store = getattr(coordinator, "store", None)
    now = dt_util.now()

    # Read persisted state
    battery_state = _get_battery_state(coordinator)
    current_mode = battery_state.get("mode", "normal")
    mode_since_str = battery_state.get("mode_since")
    mode_since = dt_util.parse_datetime(mode_since_str) if mode_since_str else now

    minutes_in_mode = (now - mode_since).total_seconds() / 60.0
    can_switch = minutes_in_mode >= _MIN_DWELL_MINUTES

    # --- Determine desired mode ---
    desired_mode = "normal"
    reason = ""

    # Safety: SOC too low → always allow charging, never hold
    if current_soc <= min_soc + 5:
        if consumer9_active and target_soc_from_plan:
            desired_mode = "charging"
            reason = f"SOC {current_soc:.0f}% kritisch, Grid-Laden auf {target_soc_from_plan}%"
        else:
            desired_mode = "normal"
            reason = f"SOC {current_soc:.0f}% kritisch, normaler Betrieb"

    # Safety: SOC high → never grid-charge
    elif current_soc >= 95:
        desired_mode = "normal"
        reason = f"SOC {current_soc:.0f}% fast voll"

    # PV surplus → always normal (let PV charge naturally)
    elif pv_surplus_kw >= pv_threshold:
        desired_mode = "normal"
        reason = f"PV Überschuss {pv_surplus_kw:.1f} kW"

    # Consumer 9 active → grid charging
    elif consumer9_active:
        if target_soc_from_plan and target_soc_from_plan > current_soc:
            desired_mode = "charging"
            reason = f"Grid-Laden aktiv, Ziel {target_soc_from_plan}%"
        else:
            desired_mode = "normal"
            reason = "Grid-Laden Plan aktiv aber Ziel bereits erreicht"

    # Cheap tariff → hold (preserve battery)
    elif score <= hold_enter or (current_mode == "hold" and score < hold_exit):
        desired_mode = "hold"
        reason = f"Score {score} billig, Akku schonen"

    # Default: normal (battery discharges)
    else:
        desired_mode = "normal"
        reason = f"Score {score}, normaler Betrieb"

    # --- Apply dwell time ---
    if desired_mode != current_mode and not can_switch:
        # Exception: safety overrides always switch immediately
        if desired_mode == "charging" and current_soc <= min_soc + 5:
            pass  # Allow immediate switch for safety
        elif desired_mode == "normal" and pv_surplus_kw >= pv_threshold:
            pass  # Allow immediate switch for PV (Fall 13)
        else:
            desired_mode = current_mode  # Stay in current mode
            reason = f"Wartezeit ({_MIN_DWELL_MINUTES - minutes_in_mode:.0f} Min verbleibend)"

    # --- Execute mode change ---
    if desired_mode != current_mode:
        _LOGGER.info("Battery mode: %s → %s (%s)", current_mode, desired_mode, reason)

        try:
            if desired_mode == "hold":
                # Hold: charge to current SOC (prevents discharge, forces grid consumption)
                await hass.services.async_call(
                    "huawei_solar", "forcible_charge_soc",
                    {"device_id": device_id, "target_soc": int(current_soc), "power": str(charge_power_w)},
                    blocking=True,
                )
            elif desired_mode == "charging":
                # Grid charge to target SOC
                target = target_soc_from_plan or min(100, int(current_soc + 20))
                await hass.services.async_call(
                    "huawei_solar", "forcible_charge_soc",
                    {"device_id": device_id, "target_soc": target, "power": str(charge_power_w)},
                    blocking=True,
                )
            elif desired_mode == "normal":
                # Release: stop forced mode, let battery work naturally
                await hass.services.async_call(
                    "huawei_solar", "stop_forcible_charge",
                    {"device_id": device_id},
                    blocking=True,
                )
        except Exception as err:
            _LOGGER.error("Battery mode change failed: %s", err)
            if store:
                store.log_activity("💥", f"Batterie-Modus Fehler: {err}")
            return

        # Log mode change
        if store:
            icons = {"normal": "🔋", "hold": "🛡️", "charging": "⚡"}
            labels = {"normal": "Normal", "hold": "Hold (Grid)", "charging": "Grid-Laden"}
            store.log_activity(
                icons.get(desired_mode, "🔋"),
                f"Batterie → {labels.get(desired_mode, desired_mode)} — {reason}",
            )

        # Record statistics
        if store:
            if desired_mode == "hold":
                store.record_battery_event("hold")

        # Persist new state
        _set_battery_state(coordinator, {
            "mode": desired_mode,
            "mode_since": now.isoformat(),
            "reason": reason,
            "soc": current_soc,
            "score": score,
        })

    elif desired_mode == "hold":
        # Record hold statistics (every tick while holding)
        if store:
            store.record_battery_event("hold")
        # While in hold mode, update target SOC to track current SOC
        # (battery may have charged slightly from PV trickle)
        forcible_state = hass.states.get("sensor.batteries_forcible_charge")
        if forcible_state and forcible_state.state != "Stopped":
            try:
                await hass.services.async_call(
                    "huawei_solar", "forcible_charge_soc",
                    {"device_id": device_id, "target_soc": int(current_soc), "power": str(charge_power_w)},
                    blocking=True,
                )
            except Exception:
                pass  # Non-critical, will retry next tick


def _get_battery_state(coordinator) -> dict:
    """Read battery state from coordinator data."""
    data = coordinator.data if isinstance(coordinator.data, dict) else {}
    return data.get("battery_state", {})


def _set_battery_state(coordinator, state: dict) -> None:
    """Persist battery state in coordinator data."""
    if coordinator.data is None:
        return
    data = dict(coordinator.data) if isinstance(coordinator.data, dict) else {}
    data["battery_state"] = state
    coordinator.async_set_updated_data(data)
