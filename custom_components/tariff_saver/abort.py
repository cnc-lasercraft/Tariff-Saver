"""Cost-based abort of running consumers (Gewitterwolken-Case).

Runs before each rolling re-plan tick + on PV-forecast change.
For each consumer currently in its planned window, compare:
  - cost_continue: grid-cost of remaining slots under CURRENT PV forecast,
                   crediting available battery SOC.
  - cost_replan:   cheapest future window of same remaining length, same model.

If cost_replan <= cost_continue × HYSTERESIS → clear plan (chosen_end=now).
The subsequent non-hysterese re-plan fills the freed slot properly.

Skipped:
  - tariff_only consumers  (PV-unabhängig — kein Sinn)
  - trigger_entity consumers (on_demand, laufen autonom durch)
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONSUMER_COUNT,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    SLOT_HOUR_FRACTION,
    SLOT_MINUTES,
    SLOTS_PER_HOUR,
)
from .consumer_helpers import (
    _all_in_from_slot,
    effective_power_kw,
    get_consumer_config,
)

_LOGGER = logging.getLogger(__name__)

HYSTERESIS = 0.9


def _battery_credit_kwh(hass: HomeAssistant, entry: ConfigEntry) -> float:
    cfg = entry.options if entry.options else entry.data
    if not cfg.get(CONF_BATTERY_ENABLED, False):
        return 0.0
    cap = float(cfg.get(CONF_BATTERY_CAPACITY_KWH, 0.0) or 0.0)
    if cap <= 0:
        return 0.0
    min_pct = float(cfg.get(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT) or DEFAULT_BATTERY_MIN_SOC_PERCENT)
    soc_entity = str(cfg.get(CONF_BATTERY_SOC_ENTITY, "") or "").strip()
    if not soc_entity:
        return 0.0
    st = hass.states.get(soc_entity)
    if not st or st.state in ("unavailable", "unknown", ""):
        return 0.0
    try:
        soc = float(st.state)
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (soc - min_pct) / 100.0 * cap)


def _window_cost(
    slots: list, base_profile: dict, start_idx: int, n_slots: int,
    power_kw: float, battery_kwh: float,
) -> float:
    """Grid-cost of running `power_kw` over n_slots from start_idx, using PV + battery credit."""
    cost = 0.0
    remaining_battery = battery_kwh
    for j in range(n_slots):
        idx = start_idx + j
        if idx >= len(slots):
            break
        slot = slots[idx]
        local_dt = dt_util.as_local(slot.start)
        slot_idx = local_dt.hour * SLOTS_PER_HOUR + local_dt.minute // SLOT_MINUTES
        base_kwh = float(base_profile.get(slot_idx, 0.0) or base_profile.get(str(slot_idx), 0.0) or 0.0)
        base_kw = base_kwh * SLOTS_PER_HOUR
        pv_kw = float(getattr(slot, "pv_kw", 0.0) or 0.0)
        pv_avail = max(0.0, pv_kw - base_kw)
        grid_kw = max(0.0, power_kw - pv_avail)
        grid_kwh = grid_kw * SLOT_HOUR_FRACTION
        credit = min(grid_kwh, remaining_battery)
        remaining_battery -= credit
        grid_kwh -= credit
        price = _all_in_from_slot(slot) or 0.0
        cost += grid_kwh * price
    return cost


def evaluate_aborts(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> list[int]:
    """Check all running consumers. Clear plans where abort+replan is cheaper.
    Returns list of aborted consumer slots."""
    store = getattr(coordinator, "store", None)
    if store is None or not store.consumer_plans:
        return []
    active = (coordinator.data or {}).get("active", []) if isinstance(coordinator.data, dict) else []
    if not active:
        return []
    slots_sorted = sorted(active, key=lambda s: s.start)
    base_profile = store.get_seasonal_load_profile() or {}

    now_utc = dt_util.utcnow()
    battery_kwh = _battery_credit_kwh(hass, entry)
    aborts: list[int] = []

    plans = store.consumer_plans
    for slot_nr in range(1, CONSUMER_COUNT + 1):
        plan = plans.get(slot_nr) or plans.get(str(slot_nr))
        if not isinstance(plan, dict) or plan.get("status") != "planned":
            continue
        config = get_consumer_config(entry, slot_nr)
        if config.tariff_only or config.trigger_entity:
            continue
        cs = dt_util.parse_datetime(str(plan.get("chosen_start") or ""))
        ce = dt_util.parse_datetime(str(plan.get("chosen_end") or ""))
        if not cs or not ce:
            continue
        cs_utc = dt_util.as_utc(cs)
        ce_utc = dt_util.as_utc(ce)
        if not (cs_utc <= now_utc < ce_utc):
            continue

        remaining_idx = [i for i, s in enumerate(slots_sorted) if s.start >= now_utc and s.start < ce_utc]
        if not remaining_idx:
            continue
        first_remaining = remaining_idx[0]
        n_remaining = len(remaining_idx)
        learning = store.get_consumer_learning(str(slot_nr)) if store else {}
        power = effective_power_kw(config, learning) or config.power_kw or 1.0

        cost_continue = _window_cost(
            slots_sorted, base_profile, first_remaining, n_remaining, power, battery_kwh,
        )

        best_replan: float | None = None
        for i in range(len(slots_sorted) - n_remaining + 1):
            if i == first_remaining:
                continue
            if slots_sorted[i].start < now_utc:
                continue
            c = _window_cost(slots_sorted, base_profile, i, n_remaining, power, battery_kwh)
            if best_replan is None or c < best_replan:
                best_replan = c

        if best_replan is None or cost_continue <= 0:
            continue
        if best_replan > cost_continue * HYSTERESIS:
            continue

        _LOGGER.info(
            "Abort Consumer %d (%s): continue=%.3f CHF vs replan=%.3f CHF → abort",
            slot_nr, config.configured_name, cost_continue, best_replan,
        )
        store.log_activity(
            "🛑",
            f"{config.configured_name} abgebrochen: Re-Plan spart {(cost_continue - best_replan):.2f} CHF",
        )
        plan["chosen_end"] = dt_util.as_local(now_utc).isoformat()
        plan["aborted"] = True
        key = slot_nr if slot_nr in plans else str(slot_nr)
        plans[key] = plan
        aborts.append(slot_nr)

    if aborts:
        store.dirty = True
    return aborts
