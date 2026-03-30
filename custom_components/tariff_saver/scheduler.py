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

        # Find valid start positions for this consumer
        valid_starts = _find_valid_starts(
            config, n_slots, num_slots, scores, config.tariff_only,
        )

        if not valid_starts:
            plans[slot_nr] = _no_window_plan(config, due_status, learning, store)
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
        total_pv = sum(pv_available[start_idx:start_idx + task.n_slots])
        total_need = task.power_kw * task.n_slots  # total kW-slots needed
        source = "pv" if total_pv >= total_need * 0.8 else "tariff"

        plans[task.slot_nr] = _build_plan(
            task, slot_start, slot_end, avg_price, source, slots, store,
        )

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

        if cost < best_cost or (cost == best_cost and _prefer_by_priority(consumers, assignment, best_assignment)):
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

        for start in task.valid_starts:
            # Check capacity
            ok = True
            for j in range(task.n_slots):
                idx = start + j
                total_kw = used_capacity[idx] + task.power_kw
                avail_pv = pv_available[idx]
                grid_needed = max(0, total_kw - avail_pv)
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
            for j in range(task.n_slots):
                idx = start + j
                total_kw = used_capacity[idx] + task.power_kw
                grid_kw = max(0, total_kw - pv_available[idx])
                cost += grid_kw * prices[idx] * 0.5  # 30-min slot

            if cost < best_cost:
                best_cost = cost
                best_start = start

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
    for c in consumers:
        start = assignment.get(c.slot_nr)
        if start is None:
            continue
        for j in range(c.n_slots):
            slot_load[start + j] += c.power_kw

    total_cost = 0.0
    for i in range(num_slots):
        if slot_load[i] <= 0:
            continue
        grid_kw = max(0, slot_load[i] - pv_available[i])
        if grid_kw > max_grid_kw:
            return None  # Exceeds grid capacity
        total_cost += grid_kw * prices[i] * 0.5  # CHF for this 30-min slot

    return total_cost


def _prefer_by_priority(consumers, assignment_a, assignment_b) -> bool:
    """Tiebreaker: prefer assignment where higher-priority consumers get earlier slots."""
    for c in sorted(consumers, key=lambda c: -c.config.priority):
        a = assignment_a.get(c.slot_nr, 999)
        b = assignment_b.get(c.slot_nr, 999)
        if a < b:
            return True
        if a > b:
            return False
    return False


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
) -> list[int]:
    """Find all valid start positions for a consumer."""
    valid = []
    for start in range(num_slots - n_slots + 1):
        ok = True
        for j in range(n_slots):
            idx = start + j
            # max_grid_score filter: if any slot score exceeds limit, skip
            if config.max_grid_score < 100:
                s = scores[idx]
                if s is not None and s > config.max_grid_score:
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
