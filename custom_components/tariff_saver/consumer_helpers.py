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
    CONF_CONSUMER_MAX_DAYS,
    CONF_CONSUMER_MIN_DAYS,
    CONF_PV_FORECAST_ATTRIBUTE,
    CONF_PV_FORECAST_ENTITY,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_CONSUMER10_MAX_DAYS,
    DEFAULT_CONSUMER10_MIN_DAYS,
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
        min_days=int(raw.get(CONF_CONSUMER_MIN_DAYS, DEFAULT_CONSUMER10_MIN_DAYS if slot == 10 else 0) or (DEFAULT_CONSUMER10_MIN_DAYS if slot == 10 else 0)),
        max_days=int(raw.get(CONF_CONSUMER_MAX_DAYS, DEFAULT_CONSUMER10_MAX_DAYS if slot == 10 else 0) or (DEFAULT_CONSUMER10_MAX_DAYS if slot == 10 else 0)),
        max_grid_score=max(0, min(100, int(raw.get("max_grid_score", 100) or 100))),
        tariff_only=bool(raw.get("tariff_only", False)),
        pv_opportunist=bool(raw.get("pv_opportunist", False)),
        min_runtime_minutes=int(raw.get("min_runtime_minutes", 0) or 0),
        run_order=int(raw.get("run_order", 0) or 0),
    )


def effective_energy_kwh(
    config: ConsumerConfig, learning: ConsumerLearning | dict[str, Any] | None
) -> float | None:
    manual = getattr(config, "manual_energy_kwh", None)
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
    if isinstance(manual, (int, float)) and manual > 0:
        return float(manual)
    if config.energy_kwh > 0:
        return float(config.energy_kwh)
    if config.power_kw > 0 and config.duration_minutes > 0:
        return float(config.power_kw) * float(config.duration_minutes) / 60.0
    return learned if learned > 0 else None


def effective_duration_minutes(
    config: ConsumerConfig, learning: ConsumerLearning | dict[str, Any] | None
) -> int | None:
    if config.duration_minutes > 0:
        return int(config.duration_minutes)
    learned = None
    if isinstance(learning, ConsumerLearning):
        learned = getattr(learning, "avg_duration_minutes", None)
        if learned is None:
            learned = getattr(learning, "avg_duration_min", None)
    elif isinstance(learning, dict):
        learned = learning.get("avg_duration_minutes") or learning.get("avg_duration_min")
    if isinstance(learned, (int, float)) and learned > 0:
        return int(round(float(learned)))
    if config.power_kw > 0 and config.energy_kwh > 0:
        return int(round((float(config.energy_kwh) / float(config.power_kw)) * 60.0))
    return None


def effective_power_kw(
    config: ConsumerConfig, learning: ConsumerLearning | dict[str, Any] | None
) -> float | None:
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


def _read_entity_forecast(hass: HomeAssistant, entity_id: str, attr_name: str) -> list[dict[str, Any]]:
    """Read forecast slots from one entity attribute."""
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
        pv_power_kw = float(item.get("pv_estimate", 0.0) or 0.0)
        out.append(
            {
                "start": dt_util.as_local(dtp),
                "pv_power_kw": pv_power_kw,
                "pv_energy_kwh": pv_power_kw * 0.5,
            }
        )
    return out


def _parse_forecast_slots(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    entity_id = str(
        entry.options.get(CONF_PV_FORECAST_ENTITY, entry.data.get(CONF_PV_FORECAST_ENTITY, "")) or ""
    ).strip()
    attr_name = str(
        entry.options.get(
            CONF_PV_FORECAST_ATTRIBUTE,
            entry.data.get(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE),
        )
        or DEFAULT_PV_FORECAST_ATTRIBUTE
    ).strip()
    if not entity_id:
        return []

    # Read primary entity
    slots = _read_entity_forecast(hass, entity_id, attr_name)

    # Auto-detect companion "morgen" entity for Solcast (enables 30h planning window)
    companion_id: str | None = None
    for today_term, tomorrow_term in [
        ("prognose_heute", "prognose_morgen"),
        ("forecast_today", "forecast_tomorrow"),
        ("_today", "_tomorrow"),
    ]:
        if today_term in entity_id:
            companion_id = entity_id.replace(today_term, tomorrow_term, 1)
            break

    if companion_id:
        slots += _read_entity_forecast(hass, companion_id, attr_name)

    # Deduplicate by start time and sort
    seen: dict = {}
    for s in slots:
        k = s["start"]
        if k not in seen:
            seen[k] = s
    return sorted(seen.values(), key=lambda x: x["start"])


def battery_available_kwh(hass: HomeAssistant, entry: ConfigEntry) -> float:
    enabled = bool(entry.options.get(CONF_BATTERY_ENABLED, entry.data.get(CONF_BATTERY_ENABLED, False)))
    if not enabled:
        return 0.0
    entity_id = str(
        entry.options.get(CONF_BATTERY_SOC_ENTITY, entry.data.get(CONF_BATTERY_SOC_ENTITY, "")) or ""
    ).strip()
    if not entity_id:
        return 0.0
    state = hass.states.get(entity_id)
    if state is None:
        return 0.0
    try:
        soc = float(state.state)
    except Exception:
        return 0.0
    capacity = float(
        entry.options.get(CONF_BATTERY_CAPACITY_KWH, entry.data.get(CONF_BATTERY_CAPACITY_KWH, 0.0))
        or 0.0
    )
    reserve = float(
        entry.options.get(
            CONF_BATTERY_MIN_SOC_PERCENT,
            entry.data.get(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT),
        )
        or DEFAULT_BATTERY_MIN_SOC_PERCENT
    )
    if capacity <= 0:
        return 0.0
    return max(0.0, (soc - reserve) / 100.0 * capacity)


def _pv_covers_window(forecast: list[dict], base_profile: dict, w_start, w_end, pv_threshold_kw: float = 0.5) -> bool:
    """Check if PV forecast exceeds base load for ALL slots in the window."""
    if not forecast:
        return False
    slot_minutes = 30
    current = w_start
    while current < w_end:
        slot_idx = current.hour * 2 + (1 if current.minute >= 30 else 0)
        # Get base load for this slot (kW = kWh per 30min * 2)
        base_kwh = float(base_profile.get(str(slot_idx), base_profile.get(slot_idx, 0.3)))
        base_kw = base_kwh * 2  # convert 30-min kWh to average kW

        # Find PV forecast for this slot
        pv_kw = 0.0
        for f in forecast:
            f_start = f.get("start")
            if f_start is not None:
                if hasattr(f_start, "hour"):
                    if f_start.hour == current.hour and ((f_start.minute < 30) == (current.minute < 30)):
                        pv_kw = float(f.get("pv_power_kw", 0.0))
                        break

        # If PV doesn't exceed base load + threshold, window is NOT PV-covered
        if pv_kw < base_kw + pv_threshold_kw:
            return False

        current = current + __import__("datetime").timedelta(minutes=slot_minutes)
    return True


def build_consumer_plan(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config: ConsumerConfig,
    learning: ConsumerLearning | dict[str, Any] | None,
    active_slots: list[PriceSlot],
    claimed_windows: list[tuple] | None = None,
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
        return {
            "status": "disabled",
            "should_run": False,
            "required_energy_kwh": req_energy,
            "slot_count": slot_count,
        }

    # PV-Opportunist: run whenever PV surplus exceeds consumer power
    if config.pv_opportunist:
        from .const import CONF_PV_SURPLUS_ENTITY
        pv_entity = str(
            entry.options.get(CONF_PV_SURPLUS_ENTITY, entry.data.get(CONF_PV_SURPLUS_ENTITY, "")) or ""
        ).strip()
        pv_surplus_kw = 0.0
        if pv_entity:
            pv_state = hass.states.get(pv_entity)
            if pv_state and pv_state.state not in ("unavailable", "unknown", ""):
                try:
                    val = float(pv_state.state)
                    pv_surplus_kw = val / 1000.0 if val > 100 else val
                except (ValueError, TypeError):
                    pass
        # Hysteresis: start at 120% of power, stop at 80%
        threshold_on = power_kw * 1.2 if power_kw > 0 else 0.1
        threshold_off = power_kw * 0.8 if power_kw > 0 else 0.05

        # Check min runtime
        _store = None
        for _eid, _coord in hass.data.get("tariff_saver", {}).items():
            if hasattr(_coord, "store") and _coord.store is not None:
                _store = _coord.store
                break
        _last_start_key = f"_opp_start_{config.slot}"
        _last_start = getattr(_store, _last_start_key, None) if _store else None
        _min_rt = config.min_runtime_minutes

        # Determine current state from binary sensor
        _bs = hass.states.get(f"binary_sensor.tariff_saver_consumer_{config.slot}_should_run")
        _currently_on = _bs.state == "on" if _bs else False

        if _currently_on:
            # Already running: use lower threshold (hysteresis) + check min runtime
            if _min_rt > 0 and _last_start:
                elapsed = (dt_util.utcnow() - _last_start).total_seconds() / 60.0
                if elapsed < _min_rt:
                    should_run = True  # min runtime not reached
                else:
                    should_run = pv_surplus_kw >= threshold_off
            else:
                should_run = pv_surplus_kw >= threshold_off
        else:
            # Not running: use higher threshold to start
            should_run = pv_surplus_kw >= threshold_on
            if should_run and _store and _min_rt > 0:
                setattr(_store, _last_start_key, dt_util.utcnow())

        return {
            "status": "pv_opportunist",
            "should_run": should_run,
            "source": "pv_opportunist" if should_run else None,
            "required_energy_kwh": round(float(req_energy), 3) if req_energy else 0,
            "power_kw": power_kw,
            "duration_minutes": duration_min,
            "slot_count": slot_count,
            "battery_available_kwh": 0,
            "pv_surplus_kw": round(pv_surplus_kw, 2),
            "chosen_start": now_local.isoformat() if should_run else None,
            "chosen_end": None,
            "expected_pv_energy_kwh": None,
            "tariff_window_start": None,
            "tariff_window_end": None,
            "tariff_avg_price_chf_per_kwh": None,
            "days_since_last_run": None,
            "due_status": None,
        }

    if req_energy is None or req_energy <= 0:
        return {
            "status": "not_configured",
            "should_run": False,
            "required_energy_kwh": None,
            "slot_count": slot_count,
        }

    days_since_last_run: int | None = None
    if isinstance(last_run, datetime):
        days_since_last_run = max(0, (now_local.date() - dt_util.as_local(last_run).date()).days)

    # Any consumer with max_days > 0 uses due-date scheduling
    hygiene_mode = config.max_days > 0

    future_price_slots = [
        s
        for s in sorted(active_slots, key=lambda x: x.start)
        if dt_util.as_local(s.start) >= now_local.replace(second=0, microsecond=0)
    ]
    if len(future_price_slots) < slot_count:
        future_price_slots = sorted(active_slots, key=lambda x: x.start)

    # Compute min/max prices for score calculation (used by max_grid_score filter)
    all_prices = [_all_in_from_slot(s) for s in active_slots]
    all_prices = [p for p in all_prices if isinstance(p, (int, float)) and p > 0.10]
    price_min = min(all_prices) if all_prices else None
    price_max = max(all_prices) if all_prices else None

    best_tariff = None
    for i in range(0, max(0, len(future_price_slots) - slot_count + 1)):
        window = future_price_slots[i : i + slot_count]
        w_start = dt_util.as_local(window[0].start)
        w_end = dt_util.as_local(window[-1].start) + timedelta(minutes=30)
        if claimed_windows and any(w_start < ce and cs < w_end for cs, ce in claimed_windows):
            continue
        prices = [_all_in_from_slot(s) for s in window]
        if any(p is None for p in prices):
            continue
        # Skip windows with unrealistic prices (incomplete data, e.g. only regional_fees)
        if any(p < 0.10 for p in prices):
            continue
        avg = sum(prices) / len(prices)
        # Skip windows where any slot exceeds max_grid_score
        if config.max_grid_score < 100:
            from .coordinator import score_from_prices
            window_scores = [score_from_prices(p, price_min, price_max) for p in prices]
            if any(s is not None and s > config.max_grid_score for s in window_scores):
                continue
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
    best_pv_covering = None
    if forecast and len(forecast) >= slot_count:
        for i in range(0, len(forecast) - slot_count + 1):
            window = forecast[i : i + slot_count]
            w_start = window[0]["start"]
            w_end = window[-1]["start"] + timedelta(minutes=30)
            if claimed_windows and any(w_start < ce and cs < w_end for cs, ce in claimed_windows):
                continue
            ok = True
            for j in range(1, len(window)):
                if window[j]["start"] - window[j - 1]["start"] != timedelta(minutes=30):
                    ok = False
                    break
            if not ok:
                continue
            energy = sum(float(s["pv_energy_kwh"]) for s in window)
            candidate = {
                "start": window[0]["start"],
                "end": window[-1]["start"] + timedelta(minutes=30),
                "expected_pv_energy_kwh": energy,
                "slot_count": slot_count,
            }
            if best_pv is None or energy > best_pv["expected_pv_energy_kwh"]:
                best_pv = candidate
            if energy >= req_energy and (best_pv_covering is None or energy > best_pv_covering["expected_pv_energy_kwh"]):
                best_pv_covering = candidate

    batt_avail = battery_available_kwh(hass, entry)

    chosen = None
    source = None
    due_status = None

    if hygiene_mode:
        min_days = max(0, int(config.min_days)) if config.min_days is not None else 0
        max_days = max(min_days, int(config.max_days)) if config.max_days is not None and config.max_days > 0 else max(min_days, 1)
        if days_since_last_run is None:
            days_since_last_run = max_days

        if days_since_last_run < min_days:
            due_status = "waiting_window"
        elif days_since_last_run < max_days:
            if best_pv_covering:
                chosen = best_pv_covering
                source = "pv"
                due_status = "pv_window"
            else:
                due_status = "waiting_pv"
        else:
            due_status = "forced"
            # When forced, prefer TODAY's window - do not defer to tomorrow's PV
            today_date = now_local.date()
            best_tariff_today = None
            for _fi in range(0, max(0, len(future_price_slots) - slot_count + 1)):
                _fw = future_price_slots[_fi : _fi + slot_count]
                _fws = dt_util.as_local(_fw[0].start)
                if _fws.date() != today_date:
                    continue
                _fwe = dt_util.as_local(_fw[-1].start) + timedelta(minutes=30)
                if claimed_windows and any(_fws < ce and cs < _fwe for cs, ce in claimed_windows):
                    continue
                _fp = [_all_in_from_slot(s) for s in _fw]
                if any(p is None for p in _fp):
                    continue
                _favg = sum(_fp) / len(_fp)
                if best_tariff_today is None or _favg < best_tariff_today["avg_price_chf_per_kwh"]:
                    best_tariff_today = {
                        "start": _fws,
                        "end": _fwe,
                        "avg_price_chf_per_kwh": _favg,
                        "slot_count": slot_count,
                    }
            best_pv_today = None
            for _fi in range(0, max(0, len(forecast) - slot_count + 1)):
                _fw = forecast[_fi : _fi + slot_count]
                _fws = _fw[0]["start"]
                if _fws.date() != today_date:
                    continue
                _fwe = _fw[-1]["start"] + timedelta(minutes=30)
                if claimed_windows and any(_fws < ce and cs < _fwe for cs, ce in claimed_windows):
                    continue
                if not all(_fw[j]["start"] - _fw[j-1]["start"] == timedelta(minutes=30) for j in range(1, len(_fw))):
                    continue
                _fen = sum(float(s["pv_energy_kwh"]) for s in _fw)
                if _fen >= req_energy and (best_pv_today is None or _fen > best_pv_today["expected_pv_energy_kwh"]):
                    best_pv_today = {
                        "start": _fws,
                        "end": _fwe,
                        "expected_pv_energy_kwh": _fen,
                        "slot_count": slot_count,
                    }
            if best_pv_today:
                chosen = best_pv_today
                source = "pv"
            elif best_tariff_today:
                chosen = best_tariff_today
                source = "tariff"
            elif best_pv_covering:
                chosen = best_pv_covering
                source = "pv"
            elif best_tariff:
                chosen = best_tariff
                source = "tariff"
    else:
        pass

        if config.tariff_only:
            if best_tariff:
                chosen = None
                source = None
                # For tariff_only: prefer earliest window without PV coverage
                # Get base load profile from store
                _store = None
                for _eid, _coord in hass.data.get("tariff_saver", {}).items():
                    if hasattr(_coord, "store") and _coord.store is not None:
                        _store = _coord.store
                        break
                _season_entity = str(
                    entry.options.get("season_entity", entry.data.get("season_entity", "")) or ""
                ).strip()
                _season = None
                if _season_entity:
                    _ss = hass.states.get(_season_entity)
                    if _ss and _ss.state not in ("unavailable", "unknown", ""):
                        _season = _ss.state
                _base_profile = _store.get_seasonal_load_profile(_season) if _store else {}

                for i in range(0, max(0, len(future_price_slots) - slot_count + 1)):
                    window = future_price_slots[i : i + slot_count]
                    w_start = dt_util.as_local(window[0].start)
                    w_end = dt_util.as_local(window[-1].start) + timedelta(minutes=30)
                    if claimed_windows and any(w_start < ce and cs < w_end for cs, ce in claimed_windows):
                        continue
                    prices = [_all_in_from_slot(s) for s in window]
                    if any(p is None for p in prices):
                        continue
                    if any(p < 0.10 for p in prices):
                        continue
                    # Skip windows where PV covers the base load
                    if _base_profile and forecast and _pv_covers_window(forecast, _base_profile, w_start, w_end):
                        continue
                    if config.max_grid_score < 100:
                        window_scores = [score_from_prices(p, price_min, price_max) for p in prices]
                        if any(s is not None and s > config.max_grid_score for s in window_scores):
                            continue
                    avg = sum(prices) / len(prices)
                    candidate = {
                        "start": w_start,
                        "end": w_end,
                        "avg_price_chf_per_kwh": avg,
                        "slot_count": slot_count,
                    }
                    if chosen is None or avg < chosen.get("avg_price_chf_per_kwh", 999):
                        chosen = candidate
        elif best_pv and (best_pv["expected_pv_energy_kwh"] + batt_avail) >= req_energy:
            chosen = best_pv
            source = "pv" if best_pv["expected_pv_energy_kwh"] >= req_energy else "pv_battery"
        elif best_tariff:
            chosen = best_tariff
            source = "tariff"

    should_run = False
    if chosen and isinstance(chosen.get("start"), datetime) and isinstance(chosen.get("end"), datetime):
        should_run = chosen["start"] <= now_local < chosen["end"]

    status = "planned" if chosen else (due_status or "unavailable")
    return {
        "status": status,
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
        "days_since_last_run": days_since_last_run,
        "due_status": due_status,
        "min_days": int(config.min_days or 0),
        "max_days": int(config.max_days or 0),
    }


def build_all_consumer_plans(
    hass: HomeAssistant,
    entry: ConfigEntry,
    active_slots: list[PriceSlot],
    store: Any,
) -> dict[int, dict[str, Any]]:
    """Build plans for all consumers in priority order, coordinating time windows.

    Higher-priority consumers (lower priority number) get first pick of PV and
    tariff windows. Once a consumer claims a time window, subsequent consumers
    cannot choose an overlapping window, preventing simultaneous operation.
    """
    from .const import CONSUMER_COUNT

    # Collect all consumers sorted by priority descending (10 = highest priority)
    consumers: list[tuple[int, ConsumerConfig]] = []
    for slot in range(1, CONSUMER_COUNT + 1):
        config = get_consumer_config(entry, slot)
        consumers.append((slot, config))
    consumers.sort(key=lambda x: (not x[1].enabled, -x[1].priority))

    claimed_windows: list[tuple] = []
    plans: dict[int, dict[str, Any]] = {}

    for slot, config in consumers:
        learning = store.get_consumer_learning(str(slot)) if store is not None else {}
        plan = build_consumer_plan(
            hass, entry, config, learning, active_slots,
            claimed_windows=claimed_windows,
        )
        plans[slot] = plan

        # Register chosen time window so subsequent (lower-priority) consumers avoid it
        chosen_start = plan.get("chosen_start")
        chosen_end = plan.get("chosen_end")
        if chosen_start and chosen_end:
            start_dt = dt_util.parse_datetime(str(chosen_start))
            end_dt = dt_util.parse_datetime(str(chosen_end))
            if start_dt and end_dt:
                claimed_windows.append((start_dt, end_dt))

    return plans
