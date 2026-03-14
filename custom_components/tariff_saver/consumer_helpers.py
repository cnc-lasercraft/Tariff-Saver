
"""Helpers for consumer planning."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CONSUMERS,
    CONF_PV_FORECAST_ATTRIBUTE,
    CONF_PV_FORECAST_ENTITY,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_PV_FORECAST_ATTRIBUTE,
)
from .models import ConsumerConfig, ConsumerLearning, PriceSlot
from .storage import IMPORT_ALLIN_COMPONENTS, TariffSaverStore

def get_consumer_config(entry: ConfigEntry, slot: int) -> ConsumerConfig:
    consumers = entry.options.get(CONF_CONSUMERS, {})
    raw = consumers.get(str(slot), {}) if isinstance(consumers, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return ConsumerConfig(
        slot=slot,
        enabled=bool(raw.get("enabled", False)),
        name=str(raw.get("name", "") or ""),
        mode=str(raw.get("mode", "auto") or "auto"),
        power_kw=float(raw.get("power_kw", 0.0) or 0.0),
        duration_minutes=int(raw.get("duration_minutes", 0) or 0),
        energy_kwh=float(raw.get("energy_kwh", 0.0) or 0.0),
        measurement_entity=str(raw.get("measurement_entity", "") or ""),
        priority=max(1, min(10, int(raw.get("priority", 5) or 5))),
        pv_required=bool(raw.get("pv_required", False)),
        learning_enabled=bool(raw.get("learning_enabled", True)),
    )

def effective_energy_kwh(config: ConsumerConfig, learning: ConsumerLearning | dict[str, Any] | None) -> float | None:
    manual = config.manual_energy_kwh
    learned = 0.0
    if isinstance(learning, ConsumerLearning):
        learned = float(learning.avg_energy_kwh or 0.0)
    elif isinstance(learning, dict):
        learned = float(learning.get("avg_energy_kwh", 0.0) or 0.0)

    if config.mode == "fixed_energy":
        return float(config.energy_kwh) if config.energy_kwh > 0 else (learned if learned > 0 else None)
    if config.mode == "fixed_duration":
        if config.power_kw > 0 and config.duration_minutes > 0:
            return float(config.power_kw) * float(config.duration_minutes) / 60.0
        return learned if learned > 0 else None
    if manual and manual > 0:
        return manual
    return learned if learned > 0 else None

def effective_duration_minutes(config: ConsumerConfig, learning: ConsumerLearning | dict[str, Any] | None) -> int | None:
    if config.duration_minutes > 0:
        return int(config.duration_minutes)
    learned = None
    if isinstance(learning, ConsumerLearning):
        learned = learning.avg_duration_minutes
    elif isinstance(learning, dict):
        learned = learning.get("avg_duration_minutes") or learning.get("avg_duration_min")
    if isinstance(learned, (int, float)) and learned > 0:
        return int(round(float(learned)))
    if config.power_kw > 0 and config.energy_kwh > 0:
        return int(round((float(config.energy_kwh) / float(config.power_kw)) * 60.0))
    return None

def effective_power_kw(config: ConsumerConfig, learning: ConsumerLearning | dict[str, Any] | None) -> float | None:
    if config.power_kw > 0:
        return float(config.power_kw)
    learned = None
    if isinstance(learning, ConsumerLearning):
        learned = learning.avg_power_kw
    elif isinstance(learning, dict):
        learned = learning.get("avg_power_kw")
    if isinstance(learned, (int, float)) and learned > 0:
        return float(learned)
    return None

def required_slot_count(config: ConsumerConfig, learning: ConsumerLearning | dict[str, Any] | None) -> int:
    dur = effective_duration_minutes(config, learning)
    if isinstance(dur, int) and dur > 0:
        return max(1, (dur + 29) // 30)
    energy = effective_energy_kwh(config, learning)
    power = effective_power_kw(config, learning)
    if energy and power and power > 0:
        minutes = int(round((float(energy) / float(power)) * 60.0))
        return max(1, (minutes + 29) // 30)
    return 2

def _all_in_from_slot(slot: PriceSlot | None) -> float | None:
    if slot is None:
        return None
    comps = slot.components_chf_per_kwh or {}
    for key in ("integrated", "all_in"):
        value = comps.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    total = TariffSaverStore.sum_components(comps, IMPORT_ALLIN_COMPONENTS)
    return total if total > 0 else None

def _parse_forecast_slots(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    entity_id = str(entry.options.get(CONF_PV_FORECAST_ENTITY, entry.data.get(CONF_PV_FORECAST_ENTITY, "")) or "").strip()
    attr_name = str(entry.options.get(CONF_PV_FORECAST_ATTRIBUTE, entry.data.get(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE)) or DEFAULT_PV_FORECAST_ATTRIBUTE).strip()
    if not entity_id:
        return []
    state = hass.states.get(entity_id)
    if state is None:
        return []
    raw = state.attributes.get(attr_name)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        dtp = dt_util.parse_datetime(str(item.get("period_start", "")))
        if dtp is None:
            continue
        out.append({
            "start": dt_util.as_local(dtp),
            "pv_power_kw": float(item.get("pv_estimate", 0.0) or 0.0),
            "pv_energy_kwh": float(item.get("pv_estimate", 0.0) or 0.0) * 0.5,
        })
    out.sort(key=lambda x: x["start"])
    return out

def battery_available_kwh(hass: HomeAssistant, entry: ConfigEntry) -> float:
    enabled = bool(entry.options.get(CONF_BATTERY_ENABLED, entry.data.get(CONF_BATTERY_ENABLED, False)))
    if not enabled:
        return 0.0
    entity_id = str(entry.options.get(CONF_BATTERY_SOC_ENTITY, entry.data.get(CONF_BATTERY_SOC_ENTITY, "")) or "").strip()
    if not entity_id:
        return 0.0
    state = hass.states.get(entity_id)
    if state is None:
        return 0.0
    try:
        soc = float(state.state)
    except Exception:
        return 0.0
    capacity = float(entry.options.get(CONF_BATTERY_CAPACITY_KWH, entry.data.get(CONF_BATTERY_CAPACITY_KWH, 0.0)) or 0.0)
    reserve = float(entry.options.get(CONF_BATTERY_MIN_SOC_PERCENT, entry.data.get(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT)) or DEFAULT_BATTERY_MIN_SOC_PERCENT)
    if capacity <= 0:
        return 0.0
    return max(0.0, (soc - reserve) / 100.0 * capacity)

def build_consumer_plan(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config: ConsumerConfig,
    learning: ConsumerLearning | dict[str, Any] | None,
    active_slots: list[PriceSlot],
) -> dict[str, Any]:
    now_local = dt_util.as_local(dt_util.utcnow())
    req_energy = effective_energy_kwh(config, learning)
    slot_count = required_slot_count(config, learning)
    power_kw = effective_power_kw(config, learning)
    duration_min = effective_duration_minutes(config, learning)
    last_run = None
    if isinstance(learning, ConsumerLearning):
        last_run = learning.last_run_end_utc
    elif isinstance(learning, dict):
        last_run = learning.get("last_run_end_utc")
        if isinstance(last_run, str):
            last_run = dt_util.parse_datetime(last_run)

    if not config.enabled:
        return {"status": "disabled", "should_run": False, "required_energy_kwh": req_energy, "slot_count": slot_count}
    if req_energy is None or req_energy <= 0:
        return {"status": "not_configured", "should_run": False, "required_energy_kwh": None, "slot_count": slot_count}
    if isinstance(last_run, datetime) and dt_util.as_local(last_run).date() == now_local.date():
        return {"status": "done", "should_run": False, "required_energy_kwh": req_energy, "slot_count": slot_count, "last_run_end_utc": dt_util.as_utc(last_run).isoformat()}

    future_price_slots = [s for s in sorted(active_slots, key=lambda x: x.start) if dt_util.as_local(x.start) >= now_local.replace(second=0, microsecond=0)]
    if len(future_price_slots) < slot_count:
        future_price_slots = sorted(active_slots, key=lambda x: x.start)

    # choose cheapest tariff window
    best_tariff = None
    for i in range(0, max(0, len(future_price_slots) - slot_count + 1)):
        window = future_price_slots[i:i+slot_count]
        prices = [_all_in_from_slot(s) for s in window]
        if any(p is None for p in prices):
            continue
        avg = sum(prices)/len(prices)
        if best_tariff is None or avg < best_tariff["avg_price_chf_per_kwh"]:
            best_tariff = {
                "start": dt_util.as_local(window[0].start),
                "end": dt_util.as_local(window[-1].start) + timedelta(minutes=30),
                "avg_price_chf_per_kwh": avg,
                "slot_count": slot_count,
            }

    forecast = _parse_forecast_slots(hass, entry)
    now_floor = now_local.replace(minute=0 if now_local.minute < 30 else 30, second=0, microsecond=0)
    forecast = [f for f in forecast if f["start"] >= now_floor]
    best_pv = None
    if forecast and len(forecast) >= slot_count:
        for i in range(0, len(forecast) - slot_count + 1):
            window = forecast[i:i+slot_count]
            # contiguous 30m only
            ok = True
            for j in range(1, len(window)):
                if window[j]["start"] - window[j-1]["start"] != timedelta(minutes=30):
                    ok = False
                    break
            if not ok:
                continue
            energy = sum(float(s["pv_energy_kwh"]) for s in window)
            if best_pv is None or energy > best_pv["expected_pv_energy_kwh"]:
                best_pv = {
                    "start": window[0]["start"],
                    "end": window[-1]["start"] + timedelta(minutes=30),
                    "expected_pv_energy_kwh": energy,
                    "slot_count": slot_count,
                }

    batt_avail = battery_available_kwh(hass, entry)
    chosen = None
    source = "tariff"
    if best_pv and (best_pv["expected_pv_energy_kwh"] + batt_avail) >= req_energy:
        chosen = best_pv
        source = "pv" if best_pv["expected_pv_energy_kwh"] >= req_energy else "pv_battery"
    elif best_tariff:
        chosen = best_tariff
        source = "tariff"

    should_run = False
    if chosen and isinstance(chosen.get("start"), datetime) and isinstance(chosen.get("end"), datetime):
        should_run = chosen["start"] <= now_local < chosen["end"]

    return {
        "status": "planned" if chosen else "unavailable",
        "should_run": should_run,
        "source": source if chosen else None,
        "required_energy_kwh": round(float(req_energy), 3),
        "power_kw": power_kw,
        "duration_minutes": duration_min,
        "slot_count": slot_count,
        "battery_available_kwh": round(float(batt_avail), 3),
        "chosen_start": chosen["start"].isoformat() if chosen else None,
        "chosen_end": chosen["end"].isoformat() if chosen else None,
        "expected_pv_energy_kwh": round(float(best_pv["expected_pv_energy_kwh"]), 3) if best_pv else 0.0,
        "tariff_window_start": best_tariff["start"].isoformat() if best_tariff else None,
        "tariff_window_end": best_tariff["end"].isoformat() if best_tariff else None,
        "tariff_avg_price_chf_per_kwh": round(float(best_tariff["avg_price_chf_per_kwh"]), 6) if best_tariff else None,
        "last_run_end_utc": dt_util.as_utc(last_run).isoformat() if isinstance(last_run, datetime) else None,
    }
