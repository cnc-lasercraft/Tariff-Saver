"""Global consumer scheduler — optimizes all consumers for minimum grid cost."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from itertools import product
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .consumer_helpers import (
    _all_in_from_slot,
    _parse_forecast_slots,
    effective_duration_minutes,
    effective_energy_kwh,
    effective_power_kw,
    get_consumer_config,
    required_slot_count,
)
from .const import CONSUMER_COUNT
from .models import ConsumerConfig, PriceSlot
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)

# Maximum combinations before falling back to greedy
_MAX_COMBINATIONS = 500_000

# Minimum price advantage (%) for grid charging to be worthwhile (covers round-trip loss)
_MIN_PRICE_ADVANTAGE_FACTOR = 0.10  # 10%


def _assess_battery_need(
    hass: HomeAssistant,
    entry: ConfigEntry,
    slots: list[PriceSlot],
    store: TariffSaverStore | None,
    prices: list[float],
    scores: list[int | None],
    pv_available: list[float],
    base_profile: dict,
) -> dict[str, Any]:
    """Assess whether grid charging is needed and when.

    Simulates SOC trajectory through all slots, identifies depletion points,
    calculates PV deficit, and finds optimal charging windows.

    Returns dict with:
      grid_charge_needed, missing_kwh, pv_deficit_kwh, charge_slots_needed,
      effective_max_score, excluded_slot_indices (PV window),
      soc_trajectory, depletion_slot, depletion_score, case
    """
    from .const import (
        CONF_BATTERY_ENABLED, CONF_BATTERY_CAPACITY_KWH,
        CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT,
        CONF_BATTERY_SOC_ENTITY, CONF_BATTERY_ROUND_TRIP_LOSS,
        DEFAULT_BATTERY_ROUND_TRIP_LOSS, CONF_BATTERY_MAX_CHARGE_KW,
        DEFAULT_BATTERY_MAX_CHARGE_KW, CONF_BATTERY_PV_CHARGE_THRESHOLD,
        DEFAULT_BATTERY_PV_CHARGE_THRESHOLD, CONF_BATTERY_SOC_MARGIN,
        DEFAULT_BATTERY_SOC_MARGIN,
    )

    config = entry.options if entry.options else entry.data
    no_charge = {
        "grid_charge_needed": False, "missing_kwh": 0.0, "pv_deficit_kwh": 0.0,
        "charge_slots_needed": 0, "effective_max_score": 0,
        "excluded_slot_indices": set(), "soc_trajectory": [],
        "depletion_slot": None, "depletion_score": None, "case": "1a",
    }

    battery_enabled = config.get(CONF_BATTERY_ENABLED, False)
    if not battery_enabled:
        return no_charge

    capacity_kwh = float(config.get(CONF_BATTERY_CAPACITY_KWH, 0.0) or 0.0)
    if capacity_kwh <= 0:
        return no_charge

    min_soc_pct = float(config.get(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT) or DEFAULT_BATTERY_MIN_SOC_PERCENT)
    margin_pct = float(config.get(CONF_BATTERY_SOC_MARGIN, DEFAULT_BATTERY_SOC_MARGIN) or DEFAULT_BATTERY_SOC_MARGIN)
    loss_pct = float(config.get(CONF_BATTERY_ROUND_TRIP_LOSS, DEFAULT_BATTERY_ROUND_TRIP_LOSS) or DEFAULT_BATTERY_ROUND_TRIP_LOSS)
    max_charge_kw = float(config.get(CONF_BATTERY_MAX_CHARGE_KW, DEFAULT_BATTERY_MAX_CHARGE_KW) or DEFAULT_BATTERY_MAX_CHARGE_KW)
    pv_charge_threshold = float(config.get(CONF_BATTERY_PV_CHARGE_THRESHOLD, DEFAULT_BATTERY_PV_CHARGE_THRESHOLD) or DEFAULT_BATTERY_PV_CHARGE_THRESHOLD)

    min_soc_kwh = min_soc_pct / 100.0 * capacity_kwh
    loss_factor = 1.0 + loss_pct / 100.0  # e.g. 1.10

    # Get current SOC
    soc_entity = str(config.get(CONF_BATTERY_SOC_ENTITY, "") or "").strip()
    current_soc_pct = 50.0  # fallback
    if soc_entity:
        soc_state = hass.states.get(soc_entity)
        if soc_state and soc_state.state not in ("unavailable", "unknown", ""):
            try:
                current_soc_pct = float(soc_state.state)
            except (ValueError, TypeError):
                pass
    current_kwh = current_soc_pct / 100.0 * capacity_kwh

    num_slots = len(slots)

    # -- Step 1: Identify PV window (slots where PV > threshold after base load) --
    pv_window: set[int] = set()
    for i in range(num_slots):
        if pv_available[i] >= pv_charge_threshold:
            pv_window.add(i)

    # -- Step 2: Simulate SOC trajectory (no grid charging) --
    soc = current_kwh
    soc_trajectory: list[float] = []
    depletion_slot: int | None = None
    depletion_score: int | None = None

    # Base load per slot (kW)
    base_kw_per_slot: list[float] = []
    for i, slot in enumerate(slots):
        local_dt = dt_util.as_local(slot.start)
        slot_idx = local_dt.hour * 2 + (1 if local_dt.minute >= 30 else 0)
        base_kwh_val = base_profile.get(slot_idx, 0.0) or base_profile.get(str(slot_idx), 0.0) or 0.0
        base_kw_per_slot.append(float(base_kwh_val) * 2)  # kWh per 30min → kW

    for i in range(num_slots):
        # Net flow: PV production minus consumption (kW), converted to kWh for 30min
        pv_kw = pv_available[i] + base_kw_per_slot[i]  # pv_available already has base subtracted, add back for raw PV
        net_kw = pv_kw - base_kw_per_slot[i]  # = pv_available[i], net surplus
        net_kwh = net_kw * 0.5  # 30-min slot

        if net_kwh >= 0:
            # PV surplus → charge battery (capped at capacity)
            soc = min(capacity_kwh, soc + net_kwh)
        else:
            # Deficit → discharge battery
            soc = soc + net_kwh  # net_kwh is negative

        if soc <= min_soc_kwh and depletion_slot is None:
            depletion_slot = i
            depletion_score = scores[i] if scores[i] is not None else 50
            soc = min_soc_kwh  # clamp, battery won't go below min

        soc = max(min_soc_kwh, soc)
        soc_trajectory.append(round(soc / capacity_kwh * 100, 1))

    # -- Step 3: Find first PV window start (where battery will recharge) --
    first_pv_slot = None
    for i in range(num_slots):
        if i in pv_window:
            first_pv_slot = i
            break

    # -- Step 4: Calculate PV deficit (will PV fully recharge the battery?) --
    # Simulate PV-only charging from depletion point (or min SOC) to see max SOC reached
    pv_deficit_kwh = 0.0
    if pv_window:
        sim_soc = min_soc_kwh if depletion_slot is not None else soc_trajectory[-1] / 100.0 * capacity_kwh if soc_trajectory else current_kwh
        for i in sorted(pv_window):
            charge_kwh = pv_available[i] * 0.5  # kWh in 30min
            sim_soc = min(capacity_kwh, sim_soc + charge_kwh)
        # Deficit = how much short of full capacity after PV
        pv_deficit_kwh = max(0.0, capacity_kwh * 0.9 - sim_soc)  # target 90% not 100%

    # -- Step 5: Determine case and charging need --

    # Missing kWh until first PV (energy the battery can't cover)
    missing_until_pv = 0.0
    if depletion_slot is not None and first_pv_slot is not None and depletion_slot < first_pv_slot:
        # Sum deficit from depletion to first PV
        for i in range(depletion_slot, first_pv_slot):
            deficit_kw = max(0.0, base_kw_per_slot[i] - pv_available[i])  # pv_available can be >0 even outside pv_window
            missing_until_pv += deficit_kw * 0.5
    elif depletion_slot is not None and first_pv_slot is None:
        # No PV at all — all remaining deficit
        for i in range(depletion_slot, num_slots):
            deficit_kw = max(0.0, base_kw_per_slot[i] - pv_available[i])
            missing_until_pv += deficit_kw * 0.5

    total_missing = missing_until_pv + pv_deficit_kwh
    needed_kwh = total_missing * loss_factor  # 10% round-trip loss

    # Determine case
    if depletion_slot is None and pv_deficit_kwh <= 0:
        case = "1a"  # Battery lasts + PV enough
        _LOGGER.info("Battery assessment: Case 1a — battery sufficient + PV covers recharge")
        return {**no_charge, "soc_trajectory": soc_trajectory, "excluded_slot_indices": pv_window, "case": case}

    if depletion_slot is None and pv_deficit_kwh > 0:
        case = "1b"  # Battery lasts but PV too weak for full recharge
        needed_kwh = pv_deficit_kwh * loss_factor
        _LOGGER.info("Battery assessment: Case 1b — battery sufficient but PV deficit %.1f kWh", pv_deficit_kwh)
    elif depletion_slot is not None:
        case = "3"  # Battery depletes — need grid charging
        _LOGGER.info("Battery assessment: Case 3+ — battery depletes at slot %d (score %s), missing %.1f kWh",
                     depletion_slot, depletion_score, needed_kwh)
    else:
        case = "1a"
        return {**no_charge, "soc_trajectory": soc_trajectory, "excluded_slot_indices": pv_window, "case": case}

    # -- Step 6: Check if cheap enough window exists (10% cheaper than depletion tarif) --
    if depletion_slot is not None and depletion_score is not None:
        depletion_price = prices[depletion_slot] if depletion_slot < len(prices) else 0.25
    else:
        # For case 1b, use average evening price as reference
        evening_prices = [prices[i] for i in range(num_slots) if i not in pv_window and prices[i] > 0.10]
        depletion_price = sum(evening_prices) / len(evening_prices) if evening_prices else 0.25

    price_threshold = depletion_price * (1.0 - _MIN_PRICE_ADVANTAGE_FACTOR)  # must be 10% cheaper

    # -- Step 7: Dynamic max_grid_score --
    # If battery dies during expensive slot, accept higher charging score
    if depletion_score is not None and depletion_score > 50:
        effective_max_score = min(depletion_score - 10, 80)  # charge up to 10 below depletion, cap at 80
    else:
        effective_max_score = 35  # default conservative

    # Add safety margin (Fall 9): boost by margin_pct of remaining range
    effective_max_score = min(80, effective_max_score + int(margin_pct / 2))

    # -- Step 8: Calculate slots needed --
    charge_energy_per_slot = max_charge_kw * 0.5  # kWh per 30-min slot
    charge_slots_needed = max(1, int((needed_kwh + charge_energy_per_slot - 0.01) / charge_energy_per_slot))

    # Check if any qualifying cheap slots exist outside PV window
    cheap_non_pv = [i for i in range(num_slots)
                    if i not in pv_window
                    and prices[i] <= price_threshold
                    and (scores[i] is None or scores[i] <= effective_max_score)]

    if not cheap_non_pv:
        case = "2"  # No cheap enough window
        _LOGGER.info("Battery assessment: Case 2 — no window cheap enough (need <%.1f Rp, max score %d)",
                     price_threshold * 100, effective_max_score)
        return {
            **no_charge, "case": case, "soc_trajectory": soc_trajectory,
            "excluded_slot_indices": pv_window, "depletion_slot": depletion_slot,
            "depletion_score": depletion_score,
        }

    _LOGGER.info(
        "Battery assessment: Case %s — need %.1f kWh (%d slots), max score %d, %d cheap slots available",
        case, needed_kwh, charge_slots_needed, effective_max_score, len(cheap_non_pv),
    )

    return {
        "grid_charge_needed": True,
        "missing_kwh": round(needed_kwh, 1),
        "pv_deficit_kwh": round(pv_deficit_kwh, 1),
        "charge_slots_needed": charge_slots_needed,
        "effective_max_score": effective_max_score,
        "excluded_slot_indices": pv_window,
        "soc_trajectory": soc_trajectory,
        "depletion_slot": depletion_slot,
        "depletion_score": depletion_score,
        "depletion_price": round(depletion_price, 4),
        "price_threshold": round(price_threshold, 4),
        "case": case,
    }


def optimize_consumer_plans(
    hass: HomeAssistant,
    entry: ConfigEntry,
    active_slots: list[PriceSlot],
    store: TariffSaverStore | None,
    max_grid_power_kw: float = 25.0,
) -> dict[int, dict[str, Any]]:
    """Optimize all consumer plans for minimum grid cost over the day.

    Consumers can run in parallel if PV/grid capacity allows.
    Only consumers with different run_order > 0 cannot overlap.
    """
    if not active_slots:
        return {}

    # Sort slots chronologically
    slots = sorted(active_slots, key=lambda s: s.start)
    num_slots = len(slots)

    # -- Phase 1: Build slot data --
    # Price per slot
    prices = [_all_in_from_slot(s) or 0.0 for s in slots]

    # Score per slot
    from .coordinator import score_from_prices
    valid_prices = [p for p in prices if p > 0.10]
    price_min = min(valid_prices) if valid_prices else None
    price_max = max(valid_prices) if valid_prices else None
    scores = [score_from_prices(p, price_min, price_max) for p in prices]

    # PV forecast per slot (kW available after base load)
    forecast = _parse_forecast_slots(hass, entry)
    base_profile = store.get_seasonal_load_profile() if store else {}
    pv_available = _build_pv_available(slots, forecast, base_profile)

    # -- Phase 1b: Battery need assessment --
    battery_assessment = _assess_battery_need(
        hass, entry, slots, store, prices, scores, pv_available, base_profile,
    )
    _LOGGER.info("Battery assessment result: case=%s, needed=%.1f kWh, slots=%d, max_score=%d",
                 battery_assessment["case"], battery_assessment["missing_kwh"],
                 battery_assessment["charge_slots_needed"], battery_assessment["effective_max_score"])

    # -- Phase 2: Collect schedulable consumers --
    consumers: list[_ConsumerTask] = []
    plans: dict[int, dict[str, Any]] = {}

    for slot_nr in range(1, CONSUMER_COUNT + 1):
        config = get_consumer_config(entry, slot_nr)
        learning = store.get_consumer_learning(str(slot_nr)) if store else {}

        # Skip disabled, pv_opportunist, not configured
        if not config.enabled:
            plans[slot_nr] = _disabled_plan(config)
            continue
        if config.pv_opportunist:
            plans[slot_nr] = _opportunist_plan(config)
            continue
        energy = effective_energy_kwh(config, learning)
        if not energy or energy <= 0:
            plans[slot_nr] = _not_configured_plan(config)
            continue

        # Check due status (hygiene mode)
        due_status = _check_due_status(config, learning, store)
        if due_status == "waiting_window":
            plans[slot_nr] = _waiting_plan(config, due_status, learning, store)
            continue

        power = effective_power_kw(config, learning) or config.power_kw or 1.0
        n_slots = required_slot_count(config, learning)

        # -- Battery-aware overrides for tariff_only consumers --
        excluded = None
        max_score_override = None
        if config.tariff_only:
            ba = battery_assessment
            excluded = ba["excluded_slot_indices"] or None

            if not ba["grid_charge_needed"]:
                # Case 1a or 2: no grid charging needed
                plans[slot_nr] = {
                    "status": "not_needed",
                    "should_run": False,
                    "source": "battery_sufficient",
                    "battery_case": ba["case"],
                    "soc_trajectory": ba["soc_trajectory"],
                }
                if store:
                    store.log_activity("🔋", f"{config.configured_name}: Kein Grid-Laden nötig (Fall {ba['case']})")
                    store.record_battery_event("case", case=ba["case"])
                continue

            # Override n_slots and max_score from battery assessment
            n_slots = ba["charge_slots_needed"]
            max_score_override = ba["effective_max_score"]
            _LOGGER.info("Consumer %d (%s): battery override — %d slots, max_score %d, excluding %d PV slots",
                         slot_nr, config.configured_name, n_slots, max_score_override, len(excluded) if excluded else 0)

        # Find valid start positions for this consumer
        valid_starts = _find_valid_starts(
            config, n_slots, num_slots, scores, config.tariff_only,
            excluded_indices=excluded,
            max_score_override=max_score_override,
        )

        if not valid_starts:
            plans[slot_nr] = _no_window_plan(config, due_status, learning, store)
            if config.tariff_only and store:
                store.log_activity("⚠️", f"{config.configured_name}: Kein passendes Fenster (max Score {max_score_override}, {len(excluded) if excluded else 0} PV-Slots ausgeschlossen)")
            continue

        consumers.append(_ConsumerTask(
            slot_nr=slot_nr,
            config=config,
            learning=learning,
            power_kw=power,
            n_slots=n_slots,
            valid_starts=valid_starts,
            due_status=due_status,
        ))

    if not consumers:
        return plans

    # -- Phase 3: Optimize --
    total_combos = 1
    for c in consumers:
        total_combos *= len(c.valid_starts)

    if total_combos <= _MAX_COMBINATIONS:
        best = _brute_force_optimize(consumers, num_slots, prices, pv_available, max_grid_power_kw)
    else:
        _LOGGER.info("Scheduler: %d combinations too many, using greedy fallback", total_combos)
        best = _greedy_optimize(consumers, num_slots, prices, pv_available, max_grid_power_kw)

    # -- Phase 4: Build output plans --
    for task in consumers:
        start_idx = best.get(task.slot_nr)
        if start_idx is None:
            plans[task.slot_nr] = _no_window_plan(task.config, task.due_status, task.learning, store)
            continue

        slot_start = slots[start_idx].start
        slot_end = slots[start_idx + task.n_slots - 1].start + timedelta(minutes=30)
        window_prices = prices[start_idx:start_idx + task.n_slots]
        avg_price = sum(window_prices) / len(window_prices) if window_prices else 0

        # Determine source: PV or tariff
        if task.config.tariff_only:
            source = "tariff"
        else:
            total_pv = sum(pv_available[start_idx:start_idx + task.n_slots])
            total_need = task.power_kw * task.n_slots  # total kW-slots needed
            source = "pv" if total_pv >= total_need * 0.8 else "tariff"

        plan = _build_plan(
            task, slot_start, slot_end, avg_price, source, slots, store,
        )
        # Attach battery assessment to tariff_only plans
        if task.config.tariff_only and battery_assessment["grid_charge_needed"]:
            plan["battery_case"] = battery_assessment["case"]
            plan["battery_missing_kwh"] = battery_assessment["missing_kwh"]
            plan["battery_pv_deficit_kwh"] = battery_assessment["pv_deficit_kwh"]
            plan["battery_effective_max_score"] = battery_assessment["effective_max_score"]
        plans[task.slot_nr] = plan

    return plans


# -- Data structures --

class _ConsumerTask:
    __slots__ = ("slot_nr", "config", "learning", "power_kw", "n_slots", "valid_starts", "due_status")

    def __init__(self, slot_nr, config, learning, power_kw, n_slots, valid_starts, due_status):
        self.slot_nr = slot_nr
        self.config = config
        self.learning = learning
        self.power_kw = power_kw
        self.n_slots = n_slots
        self.valid_starts = valid_starts
        self.due_status = due_status


# -- Optimization --

def _brute_force_optimize(
    consumers: list[_ConsumerTask],
    num_slots: int,
    prices: list[float],
    pv_available: list[float],
    max_grid_kw: float,
) -> dict[int, int]:
    """Try all combinations, return best assignment {slot_nr: start_idx}."""
    best_cost = float("inf")
    best_assignment: dict[int, int] = {}

    for combo in product(*(c.valid_starts for c in consumers)):
        assignment = {c.slot_nr: start for c, start in zip(consumers, combo)}

        # Check run_order constraints
        if not _check_run_order(consumers, assignment):
            continue

        # Calculate cost
        cost = _calc_grid_cost(consumers, assignment, num_slots, prices, pv_available, max_grid_kw)
        if cost is None:
            continue  # Capacity exceeded

        if cost < best_cost or (cost == best_cost and _prefer_by_pv_surplus(consumers, assignment, best_assignment, pv_available)):
            best_cost = cost
            best_assignment = dict(assignment)

    return best_assignment


def _greedy_optimize(
    consumers: list[_ConsumerTask],
    num_slots: int,
    prices: list[float],
    pv_available: list[float],
    max_grid_kw: float,
) -> dict[int, int]:
    """Greedy fallback: schedule consumers by priority, picking cheapest valid position."""
    # Sort by priority descending (highest first), then run_order
    sorted_consumers = sorted(consumers, key=lambda c: (-c.config.priority, c.config.run_order))
    assignment: dict[int, int] = {}
    used_capacity = [0.0] * num_slots  # kW used per slot

    for task in sorted_consumers:
        best_start = None
        best_cost = float("inf")
        best_pv_surplus = -1.0  # Tiebreaker: prefer highest PV surplus

        for start in task.valid_starts:
            # Check capacity
            ok = True
            for j in range(task.n_slots):
                idx = start + j
                total_kw = used_capacity[idx] + task.power_kw
                if task.config.tariff_only:
                    grid_needed = total_kw  # tariff_only always uses grid
                else:
                    grid_needed = max(0, total_kw - pv_available[idx])
                if grid_needed > max_grid_kw:
                    ok = False
                    break
            if not ok:
                continue

            # Check run_order
            test_assignment = dict(assignment)
            test_assignment[task.slot_nr] = start
            if not _check_run_order(consumers, test_assignment):
                continue

            # Calculate cost for this consumer at this position
            cost = 0.0
            pv_surplus = 0.0
            for j in range(task.n_slots):
                idx = start + j
                total_kw = used_capacity[idx] + task.power_kw
                if task.config.tariff_only:
                    grid_kw = task.power_kw  # tariff_only always pays grid
                else:
                    grid_kw = max(0, total_kw - pv_available[idx])
                    pv_surplus += pv_available[idx] - total_kw  # higher = more headroom
                cost += grid_kw * prices[idx] * 0.5  # 30-min slot

            # Prefer lowest cost, then highest PV surplus (more headroom = safer)
            if cost < best_cost or (cost == best_cost and pv_surplus > best_pv_surplus):
                best_cost = cost
                best_start = start
                best_pv_surplus = pv_surplus

        if best_start is not None:
            assignment[task.slot_nr] = best_start
            for j in range(task.n_slots):
                used_capacity[best_start + j] += task.power_kw

    return assignment


# -- Constraint checks --

def _check_run_order(consumers: list[_ConsumerTask], assignment: dict[int, int]) -> bool:
    """Check that consumers with different run_order > 0 don't overlap."""
    ordered = []
    for c in consumers:
        if c.config.run_order > 0 and c.slot_nr in assignment:
            start = assignment[c.slot_nr]
            end = start + c.n_slots
            ordered.append((c.config.run_order, start, end))

    ordered.sort(key=lambda x: x[0])
    for i in range(len(ordered) - 1):
        ro1, _s1, e1 = ordered[i]
        ro2, s2, _e2 = ordered[i + 1]
        if ro1 != ro2 and s2 < e1:
            return False  # run_order conflict: overlap
    return True


def _calc_grid_cost(
    consumers: list[_ConsumerTask],
    assignment: dict[int, int],
    num_slots: int,
    prices: list[float],
    pv_available: list[float],
    max_grid_kw: float,
) -> float | None:
    """Calculate total grid cost for an assignment. Returns None if capacity exceeded."""
    slot_load = [0.0] * num_slots
    # tariff_only consumers always use grid, don't benefit from PV
    slot_load_tariff_only = [0.0] * num_slots
    for c in consumers:
        start = assignment.get(c.slot_nr)
        if start is None:
            continue
        for j in range(c.n_slots):
            slot_load[start + j] += c.power_kw
            if c.config.tariff_only:
                slot_load_tariff_only[start + j] += c.power_kw

    total_cost = 0.0
    for i in range(num_slots):
        if slot_load[i] <= 0:
            continue
        # tariff_only load always counts as grid, remaining load can use PV
        normal_load = slot_load[i] - slot_load_tariff_only[i]
        grid_from_normal = max(0, normal_load - pv_available[i])
        grid_kw = grid_from_normal + slot_load_tariff_only[i]
        if grid_kw > max_grid_kw:
            return None  # Exceeds grid capacity
        total_cost += grid_kw * prices[i] * 0.5  # CHF for this 30-min slot

    return total_cost


def _prefer_by_pv_surplus(consumers, assignment_a, assignment_b, pv_available) -> bool:
    """Tiebreaker: prefer assignment with higher total PV surplus (more headroom)."""
    surplus_a = 0.0
    surplus_b = 0.0
    for c in consumers:
        start_a = assignment_a.get(c.slot_nr)
        start_b = assignment_b.get(c.slot_nr)
        if not c.config.tariff_only:
            if start_a is not None:
                for j in range(c.n_slots):
                    surplus_a += pv_available[start_a + j] - c.power_kw
            if start_b is not None:
                for j in range(c.n_slots):
                    surplus_b += pv_available[start_b + j] - c.power_kw
    return surplus_a > surplus_b


# -- Helpers --

def _build_pv_available(
    slots: list[PriceSlot],
    forecast: list[dict],
    base_profile: dict,
) -> list[float]:
    """Build list of available PV kW per slot (forecast minus base load)."""
    result = []
    for i, slot in enumerate(slots):
        local_dt = dt_util.as_local(slot.start)
        hour, minute = local_dt.hour, local_dt.minute

        # Find matching forecast
        pv_kw = 0.0
        for fc in forecast:
            fc_start = fc.get("start")
            if isinstance(fc_start, str):
                fc_start = dt_util.parse_datetime(fc_start)
            if fc_start is None:
                continue
            fc_local = dt_util.as_local(fc_start)
            if fc_local.hour == hour and fc_local.minute == minute and fc_local.date() == local_dt.date():
                pv_kw = fc.get("pv_power_kw", 0.0) or 0.0
                break

        # Subtract base load
        slot_idx = hour * 2 + (1 if minute >= 30 else 0)
        base_kwh = base_profile.get(slot_idx, 0.0) or base_profile.get(str(slot_idx), 0.0) or 0.0
        base_kw = float(base_kwh) * 2  # Convert 30-min kWh to average kW

        available = max(0.0, pv_kw - base_kw)
        result.append(available)

    return result


def _find_valid_starts(
    config: ConsumerConfig,
    n_slots: int,
    num_slots: int,
    scores: list[int | None],
    tariff_only: bool,
    excluded_indices: set[int] | None = None,
    max_score_override: int | None = None,
) -> list[int]:
    """Find all valid start positions for a consumer."""
    max_score = max_score_override if max_score_override is not None else config.max_grid_score
    valid = []
    for start in range(num_slots - n_slots + 1):
        ok = True
        for j in range(n_slots):
            idx = start + j
            # Exclude PV window slots for tariff_only consumers
            if excluded_indices and idx in excluded_indices:
                ok = False
                break
            # max_grid_score filter: if any slot score exceeds limit, skip
            if max_score < 100:
                s = scores[idx]
                if s is not None and s > max_score:
                    ok = False
                    break
        if ok:
            valid.append(start)
    return valid


def _check_due_status(config: ConsumerConfig, learning, store) -> str | None:
    """Check if hygiene-mode consumer is due to run."""
    if config.max_days <= 0:
        return None  # Not hygiene mode

    last_run = None
    if isinstance(learning, dict):
        lr = learning.get("last_run_end_utc")
        if isinstance(lr, str):
            last_run = dt_util.parse_datetime(lr)
    elif hasattr(learning, "last_run_end_utc"):
        last_run = learning.last_run_end_utc

    if last_run is None:
        return "forced"

    now = dt_util.now()
    days_since = (now - dt_util.as_local(last_run)).days

    if days_since < config.min_days:
        return "waiting_window"
    if days_since >= config.max_days:
        return "forced"
    return "due"


# -- Plan builders --

def _build_plan(
    task: _ConsumerTask,
    start: datetime,
    end: datetime,
    avg_price: float,
    source: str,
    slots: list[PriceSlot],
    store: TariffSaverStore | None,
) -> dict[str, Any]:
    cfg = task.config
    learning = task.learning
    energy = effective_energy_kwh(cfg, learning)
    last_run = None
    days_since = 0
    if isinstance(learning, dict):
        lr = learning.get("last_run_end_utc")
        if isinstance(lr, str):
            last_run = dt_util.parse_datetime(lr)
    if last_run:
        days_since = (dt_util.now() - dt_util.as_local(last_run)).days

    return {
        "status": "planned",
        "should_run": False,
        "source": source,
        "chosen_start": dt_util.as_local(start).isoformat() if isinstance(start, datetime) else str(start),
        "chosen_end": dt_util.as_local(end).isoformat() if isinstance(end, datetime) else str(end),
        "tariff_window_start": dt_util.as_local(start).isoformat() if isinstance(start, datetime) else str(start),
        "tariff_window_end": dt_util.as_local(end).isoformat() if isinstance(end, datetime) else str(end),
        "tariff_avg_price_chf_per_kwh": round(avg_price, 6),
        "avg_price_chf_per_kwh": round(avg_price, 6),
        "expected_energy_kwh": energy,
        "power_kw": task.power_kw,
        "priority": cfg.priority,
        "run_order": cfg.run_order,
        "due_status": task.due_status,
        "days_since_last_run": days_since,
        "min_days": cfg.min_days,
        "max_days": cfg.max_days,
        "slot_count": task.n_slots,
    }


def _disabled_plan(config: ConsumerConfig) -> dict[str, Any]:
    return {"status": "disabled", "should_run": False, "source": "-"}


def _opportunist_plan(config: ConsumerConfig) -> dict[str, Any]:
    return {"status": "pv_opportunist", "should_run": False, "source": None}


def _not_configured_plan(config: ConsumerConfig) -> dict[str, Any]:
    return {"status": "not_configured", "should_run": False, "source": None}


def _waiting_plan(config, due_status, learning, store) -> dict[str, Any]:
    days_since = 0
    if isinstance(learning, dict):
        lr = learning.get("last_run_end_utc")
        if isinstance(lr, str):
            last_run = dt_util.parse_datetime(lr)
            if last_run:
                days_since = (dt_util.now() - dt_util.as_local(last_run)).days
    return {
        "status": due_status or "waiting_window",
        "should_run": False,
        "source": None,
        "due_status": due_status,
        "days_since_last_run": days_since,
        "min_days": config.min_days,
        "max_days": config.max_days,
    }


def _no_window_plan(config, due_status, learning, store) -> dict[str, Any]:
    days_since = 0
    if isinstance(learning, dict):
        lr = learning.get("last_run_end_utc")
        if isinstance(lr, str):
            last_run = dt_util.parse_datetime(lr)
            if last_run:
                days_since = (dt_util.now() - dt_util.as_local(last_run)).days
    return {
        "status": "unavailable" if not due_status else due_status,
        "should_run": False,
        "source": None,
        "due_status": due_status,
        "days_since_last_run": days_since,
        "min_days": config.min_days,
        "max_days": config.max_days,
    }
