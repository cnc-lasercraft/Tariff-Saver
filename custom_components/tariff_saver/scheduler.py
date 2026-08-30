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
    effective_duration_minutes,
    effective_energy_kwh,
    effective_power_kw,
    get_consumer_config,
    required_slot_count,
)
from .const import CONSUMER_COUNT, SLOT_MINUTES, SLOTS_PER_HOUR, SLOT_HOUR_FRACTION
from .models import ConsumerConfig, PriceSlot
from .slot_helpers import energy_source_label, score_from_prices
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)

# Maximum combinations before falling back to greedy
_MAX_COMBINATIONS = 500_000

# Battery-Assessment-Log: das Assessment läuft bei jedem Re-Plan (15 min, 2x pro Plan)
# und lieferte damit ~220 identische INFO-Zeilen/Tag. Auf INFO kommt nur noch der
# Zustandswechsel; der Heartbeat sorgt dafür, dass der aktuelle Stand auch nach
# Log-Rotation im Log steht. Vollständige Rechenwege stehen auf DEBUG.
_ASSESSMENT_HEARTBEAT = timedelta(hours=6)
_last_assessment: dict[str, tuple[tuple, datetime]] = {}


def _log_assessment(entry_id: str, assessment: dict[str, Any]) -> None:
    """Assessment-Ergebnis loggen — INFO nur bei Zustandswechsel, sonst DEBUG."""
    key = (
        assessment["case"],
        bool(assessment["grid_charge_needed"]),
        int(assessment["charge_slots_needed"]),
        int(assessment["effective_max_score"]),
        round(float(assessment["missing_kwh"])),
    )
    now = dt_util.utcnow()
    prev = _last_assessment.get(entry_id)
    changed = prev is None or prev[0] != key
    heartbeat = prev is not None and not changed and now - prev[1] >= _ASSESSMENT_HEARTBEAT
    log = _LOGGER.info if (changed or heartbeat) else _LOGGER.debug
    _last_assessment[entry_id] = (key, now if (changed or heartbeat) else prev[1])
    log(
        "Battery assessment result: case=%s, needed=%.1f kWh, slots=%d, max_score=%d%s",
        assessment["case"], assessment["missing_kwh"],
        assessment["charge_slots_needed"], assessment["effective_max_score"],
        "" if changed else " (unverändert)",
    )


def _assess_battery_need(
    hass: HomeAssistant,
    entry: ConfigEntry,
    slots: list[PriceSlot],
    store: TariffSaverStore | None,
    prices: list[float],
    scores: list[int | None],
    pv_available: list[float],
    base_profile: dict,
    extra_load_kw_per_slot: list[float] | None = None,
) -> dict[str, Any]:
    """Assess whether grid charging is needed.

    Decision flow:
      1. Project SOC from now → first slot (covers the gap between fetch time and midnight).
      2. Simulate SOC trajectory across all slots with PV and base load
         (+ `extra_load_kw_per_slot` = bereits geplante Consumer-Lasten, Audit 2.2).
         Liegt slots[0] in der Vergangenheit, startet die Simulation beim aktuellen
         Slot (Audit 4.2 — vergangene Slots werden nicht nochmals entladen/geladen).
      3. Compute raw PV ratio vs. daily base load.
         - ratio < full_charge_ratio → target = charge battery to 100%
         - else                      → target = missing_until_pv + pv_deficit_kwh
      4. Cost comparison: charging cost (N cheapest non-PV slots × loss) vs. morning grid cost.
         Case 2 (nicht laden) nur wenn kein Grundlast-Defizit bis PV-Start besteht;
         sonst wird bis `battery_charge_cost_tolerance_pct` teurer trotzdem geladen (Audit 4.4, P0/P4).

    Liefert zusätzlich die Grundlagen fürs Akku-Budget (Cluster 2): `soc_before_kwh`,
    `base_kw_per_slot`, `raw_pv_kw_per_slot`, `min_soc_kwh`, `capacity_kwh`,
    `first_pv_slot`, `sim_from`, `refill_price` (Wiederbeschaffungspreis CHF/kWh inkl. Verlust).
    """
    from .const import (
        CONF_BATTERY_ENABLED, CONF_BATTERY_CAPACITY_KWH,
        CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT,
        CONF_BATTERY_SOC_ENTITY, CONF_BATTERY_ROUND_TRIP_LOSS,
        DEFAULT_BATTERY_ROUND_TRIP_LOSS, CONF_BATTERY_MAX_CHARGE_KW,
        DEFAULT_BATTERY_MAX_CHARGE_KW, CONF_BATTERY_PV_CHARGE_THRESHOLD,
        DEFAULT_BATTERY_PV_CHARGE_THRESHOLD,
        CONF_BATTERY_PV_FULL_CHARGE_RATIO, DEFAULT_BATTERY_PV_FULL_CHARGE_RATIO,
        CONF_BATTERY_CHARGE_COST_TOLERANCE_PCT, DEFAULT_BATTERY_CHARGE_COST_TOLERANCE_PCT,
    )

    config = entry.options if entry.options else entry.data
    no_charge = {
        "grid_charge_needed": False, "missing_kwh": 0.0, "pv_deficit_kwh": 0.0,
        "charge_slots_needed": 0, "effective_max_score": 0,
        "excluded_slot_indices": set(), "soc_trajectory": [],
        "depletion_slot": None, "depletion_score": None, "case": "1a",
        "battery_enabled": False, "soc_before_kwh": [], "base_kw_per_slot": [],
        "raw_pv_kw_per_slot": [], "min_soc_kwh": 0.0, "capacity_kwh": 0.0,
        "first_pv_slot": None, "sim_from": 0, "refill_price": 0.25,
        "missing_until_pv_kwh": 0.0,
    }

    battery_enabled = config.get(CONF_BATTERY_ENABLED, False)
    if not battery_enabled:
        return no_charge

    capacity_kwh = float(config.get(CONF_BATTERY_CAPACITY_KWH, 0.0) or 0.0)
    if capacity_kwh <= 0:
        return no_charge

    min_soc_pct = float(config.get(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT) or DEFAULT_BATTERY_MIN_SOC_PERCENT)
    loss_pct = float(config.get(CONF_BATTERY_ROUND_TRIP_LOSS, DEFAULT_BATTERY_ROUND_TRIP_LOSS) or DEFAULT_BATTERY_ROUND_TRIP_LOSS)
    max_charge_kw = float(config.get(CONF_BATTERY_MAX_CHARGE_KW, DEFAULT_BATTERY_MAX_CHARGE_KW) or DEFAULT_BATTERY_MAX_CHARGE_KW)
    pv_charge_threshold = float(config.get(CONF_BATTERY_PV_CHARGE_THRESHOLD, DEFAULT_BATTERY_PV_CHARGE_THRESHOLD) or DEFAULT_BATTERY_PV_CHARGE_THRESHOLD)
    full_charge_ratio = float(config.get(CONF_BATTERY_PV_FULL_CHARGE_RATIO, DEFAULT_BATTERY_PV_FULL_CHARGE_RATIO) or DEFAULT_BATTERY_PV_FULL_CHARGE_RATIO)
    cost_tolerance = float(config.get(CONF_BATTERY_CHARGE_COST_TOLERANCE_PCT, DEFAULT_BATTERY_CHARGE_COST_TOLERANCE_PCT) or 0.0) / 100.0

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
            except (ValueError, TypeError) as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "current_soc_pct = float(soc_state.state)", _err)
    current_kwh = current_soc_pct / 100.0 * capacity_kwh

    num_slots = len(slots)
    if num_slots == 0:
        return no_charge

    # -- Step 0: Project SOC from "now" to first slot (Bug 2 fix) --
    # Between EKZ fetch time (~18:15) and slots[0].start (00:00 next day),
    # the battery continues to drain under base load. Simulate that gap.
    now_utc = dt_util.utcnow()
    first_slot_start = slots[0].start
    gap_kwh = 0.0
    # Audit 4.2: liegt der Horizont schon in der Vergangenheit (Re-Plan am Zieltag),
    # beginnt die Simulation beim aktuellen Slot — vergangene Slots bleiben auf dem
    # aktuellen SOC (kein doppeltes Entladen/Laden).
    sim_from = 0
    if first_slot_start <= now_utc:
        for i, s in enumerate(slots):
            if s.start + timedelta(minutes=SLOT_MINUTES) > now_utc:
                sim_from = i
                break
        else:
            sim_from = num_slots
    if first_slot_start > now_utc:
        cursor = dt_util.as_local(now_utc).replace(second=0, microsecond=0)
        # align to next slot boundary (15 min)
        next_minute = ((cursor.minute // SLOT_MINUTES) + 1) * SLOT_MINUTES
        if next_minute >= 60:
            cursor = (cursor + timedelta(hours=1)).replace(minute=0)
        else:
            cursor = cursor.replace(minute=next_minute)
        first_local = dt_util.as_local(first_slot_start)
        while cursor < first_local:
            slot_idx = cursor.hour * SLOTS_PER_HOUR + cursor.minute // SLOT_MINUTES
            gap_kwh += float(base_profile.get(slot_idx, 0.0) or base_profile.get(str(slot_idx), 0.0) or 0.0)
            cursor += timedelta(minutes=SLOT_MINUTES)
    projected_start_kwh = max(min_soc_kwh, current_kwh - gap_kwh)
    _LOGGER.debug("Battery assessment: current SOC %.1f%% (%.1f kWh), gap %.1f kWh, projected start %.1f%% (%.1f kWh)",
                 current_soc_pct, current_kwh, gap_kwh,
                 projected_start_kwh / capacity_kwh * 100, projected_start_kwh)

    # -- Step 1: Build raw PV and base-load arrays --
    # PV kommt jetzt direkt aus slot.pv_kw (befüllt via tariff_saver.update_pv_forecast)
    base_kw_per_slot: list[float] = []
    for slot in slots:
        local_dt = dt_util.as_local(slot.start)
        slot_idx = local_dt.hour * SLOTS_PER_HOUR + local_dt.minute // SLOT_MINUTES
        base_kwh_val = base_profile.get(slot_idx, 0.0) or base_profile.get(str(slot_idx), 0.0) or 0.0
        base_kw_per_slot.append(float(base_kwh_val) * SLOTS_PER_HOUR)

    raw_pv_kw_per_slot: list[float] = [float(getattr(slot, "pv_kw", 0.0) or 0.0) for slot in slots]

    # Audit 2.2: bereits geplante Consumer-Lasten in die Simulation aufnehmen
    load_kw_per_slot = list(base_kw_per_slot)
    if extra_load_kw_per_slot:
        for i in range(min(num_slots, len(extra_load_kw_per_slot))):
            load_kw_per_slot[i] += max(0.0, float(extra_load_kw_per_slot[i] or 0.0))

    # PV window (slots with meaningful net surplus — for "excluded" list)
    pv_window: set[int] = set()
    for i in range(num_slots):
        if pv_available[i] >= pv_charge_threshold:
            pv_window.add(i)

    # -- Step 2: Simulate SOC trajectory (uses RAW PV, so night-time discharge works) --
    soc = projected_start_kwh
    soc_trajectory: list[float] = []
    soc_before_kwh: list[float] = []  # SOC VOR Slot i (Basis fürs Akku-Budget)
    depletion_slot: int | None = None
    depletion_score: int | None = None

    for i in range(num_slots):
        soc_before_kwh.append(soc)
        if i < sim_from:
            soc_trajectory.append(round(soc / capacity_kwh * 100, 1))
            continue
        net_kwh = (raw_pv_kw_per_slot[i] - load_kw_per_slot[i]) * SLOT_HOUR_FRACTION
        if net_kwh >= 0:
            soc = min(capacity_kwh, soc + net_kwh)
        else:
            soc += net_kwh  # discharge (net_kwh negative)

        if soc <= min_soc_kwh and depletion_slot is None:
            depletion_slot = i
            depletion_score = scores[i] if scores[i] is not None else 50
            soc = min_soc_kwh

        soc = max(min_soc_kwh, soc)
        soc_trajectory.append(round(soc / capacity_kwh * 100, 1))

    # -- Step 3: First PV slot --
    first_pv_slot = None
    for i in range(num_slots):
        if i in pv_window:
            first_pv_slot = i
            break

    # -- Step 4: PV deficit (will PV fully recharge battery?) --
    pv_deficit_kwh = 0.0
    if pv_window:
        sim_soc = min_soc_kwh if depletion_slot is not None else (
            soc_trajectory[-1] / 100.0 * capacity_kwh if soc_trajectory else projected_start_kwh
        )
        for i in sorted(pv_window):
            sim_soc = min(capacity_kwh, sim_soc + pv_available[i] * SLOT_HOUR_FRACTION)
        pv_deficit_kwh = max(0.0, capacity_kwh * 0.9 - sim_soc)

    # -- Step 5: missing_until_pv --
    missing_until_pv = 0.0
    if depletion_slot is not None and first_pv_slot is not None and depletion_slot < first_pv_slot:
        for i in range(depletion_slot, first_pv_slot):
            deficit_kw = max(0.0, load_kw_per_slot[i] - pv_available[i])
            missing_until_pv += deficit_kw * SLOT_HOUR_FRACTION
    elif depletion_slot is not None and first_pv_slot is None:
        for i in range(depletion_slot, num_slots):
            deficit_kw = max(0.0, load_kw_per_slot[i] - pv_available[i])
            missing_until_pv += deficit_kw * SLOT_HOUR_FRACTION

    # -- Step 6: Raw PV forecast vs. daily base load (full-charge decision) --
    raw_pv_kwh_day = sum(pv * SLOT_HOUR_FRACTION for pv in raw_pv_kw_per_slot)
    daily_base_load_kwh = sum(b * SLOT_HOUR_FRACTION for b in base_kw_per_slot)
    pv_ratio = raw_pv_kwh_day / daily_base_load_kwh if daily_base_load_kwh > 0 else 999.0

    # Wiederbeschaffungspreis (Cluster 2 / P2): was eine Akku-kWh kostet, wenn sie
    # nachts aus dem Netz nachgeladen werden muss = Verlustfaktor × Ø der günstigsten
    # Nicht-PV-Slots (2 h). Bewertet Akku-Energie im Kostenmodell — weder gratis noch Netzpreis.
    refill_candidates = sorted(
        p for i, p in enumerate(prices) if i >= sim_from and i not in pv_window and p > 0.10
    )[:2 * SLOTS_PER_HOUR]
    refill_price = round(
        loss_factor * (sum(refill_candidates) / len(refill_candidates) if refill_candidates else 0.25), 4
    )

    base_info = {
        "battery_enabled": True,
        "soc_before_kwh": soc_before_kwh,
        "base_kw_per_slot": base_kw_per_slot,
        "raw_pv_kw_per_slot": raw_pv_kw_per_slot,
        "min_soc_kwh": min_soc_kwh,
        "capacity_kwh": capacity_kwh,
        "first_pv_slot": first_pv_slot,
        "sim_from": sim_from,
        "refill_price": refill_price,
        "missing_until_pv_kwh": round(missing_until_pv, 2),
    }

    # -- Step 7: Decide charge target --
    if pv_ratio < full_charge_ratio:
        target_charge_kwh = max(0.0, capacity_kwh - projected_start_kwh)
        target_reason = f"low_pv (ratio {pv_ratio:.2f} < {full_charge_ratio:.2f}) → fill to 100%"
    else:
        target_charge_kwh = missing_until_pv + pv_deficit_kwh
        target_reason = f"gap_fill (missing {missing_until_pv:.1f} + pv_deficit {pv_deficit_kwh:.1f})"
    # Nie über die Restkapazität hinaus (sonst Ziel-SOC > 100 %)
    target_charge_kwh = min(target_charge_kwh, max(0.0, capacity_kwh - projected_start_kwh))

    if target_charge_kwh < 0.1:
        case = "1a"
        _LOGGER.debug("Battery assessment: Case 1a — no charge needed (%s)", target_reason)
        return {**no_charge, **base_info, "soc_trajectory": soc_trajectory,
                "excluded_slot_indices": pv_window, "case": case,
                "projected_start_soc_pct": round(projected_start_kwh / capacity_kwh * 100, 1),
                "pv_ratio": round(pv_ratio, 2)}

    needed_grid_kwh = target_charge_kwh * loss_factor
    charge_energy_per_slot = max_charge_kw * SLOT_HOUR_FRACTION
    charge_slots_needed = max(1, int((needed_grid_kwh + charge_energy_per_slot - 0.01) / charge_energy_per_slot))

    # -- Step 8: Cost comparison (Bug 1 fix — replaces 10%-threshold heuristic) --
    # Alternative cost: pulling from grid when battery would otherwise be empty.
    # Use avg price of the slots between depletion and first PV (morning hours).
    if depletion_slot is not None and first_pv_slot is not None and first_pv_slot > depletion_slot:
        alt_prices = [prices[i] for i in range(depletion_slot, first_pv_slot) if prices[i] > 0.10]
    else:
        alt_prices = [prices[i] for i in range(sim_from, num_slots) if i not in pv_window and prices[i] > 0.10]
    avg_alt_price = sum(alt_prices) / len(alt_prices) if alt_prices else 0.25
    cost_without_charge = target_charge_kwh * avg_alt_price

    # Charge cost: take N cheapest non-PV slots (nur zukünftige, Audit 4.2)
    non_pv_candidates = sorted(
        [(i, prices[i]) for i in range(sim_from, num_slots) if i not in pv_window and prices[i] > 0.10],
        key=lambda x: x[1],
    )
    chosen = non_pv_candidates[:charge_slots_needed]
    if len(chosen) < charge_slots_needed:
        case = "2"
        _LOGGER.debug("Battery assessment: Case 2 — not enough non-PV slots (%d available, %d needed)",
                     len(chosen), charge_slots_needed)
        return {**no_charge, **base_info, "case": case, "soc_trajectory": soc_trajectory,
                "excluded_slot_indices": pv_window, "depletion_slot": depletion_slot,
                "depletion_score": depletion_score,
                "projected_start_soc_pct": round(projected_start_kwh / capacity_kwh * 100, 1),
                "pv_ratio": round(pv_ratio, 2)}

    avg_charge_price = sum(p for _, p in chosen) / len(chosen)
    cost_with_charge = needed_grid_kwh * avg_charge_price

    # Audit 4.4 / P0+P4: Besteht ein Grundlast-Defizit bis PV-Start, ist Morgen-Netzbezug
    # KEINE Alternative — dann wird geladen, solange es nicht mehr als die Toleranz
    # teurer ist. Nur ohne Defizit (reine Vorratsladung) gilt der harte Kostenvergleich.
    if missing_until_pv > 0:
        charge_limit = cost_without_charge * (1.0 + cost_tolerance)
    else:
        charge_limit = cost_without_charge
    if cost_with_charge > charge_limit:
        case = "2"
        _LOGGER.debug("Battery assessment: Case 2 — charging not worth it (%.2f CHF vs. %.2f CHF direct grid, limit %.2f, target %.1f kWh)",
                     cost_with_charge, cost_without_charge, charge_limit, target_charge_kwh)
        return {**no_charge, **base_info, "case": case, "soc_trajectory": soc_trajectory,
                "excluded_slot_indices": pv_window, "depletion_slot": depletion_slot,
                "depletion_score": depletion_score,
                "avg_charge_price": round(avg_charge_price, 4),
                "avg_alt_price": round(avg_alt_price, 4),
                "cost_with_charge": round(cost_with_charge, 2),
                "cost_without_charge": round(cost_without_charge, 2),
                "projected_start_soc_pct": round(projected_start_kwh / capacity_kwh * 100, 1),
                "pv_ratio": round(pv_ratio, 2)}

    # Let the optimizer pick from non-PV slots; cost is price-driven, not score-gated.
    effective_max_score = 100
    case = "3" if depletion_slot is not None else "1b"
    savings = cost_without_charge - cost_with_charge
    target_soc_pct = round((projected_start_kwh + target_charge_kwh) / capacity_kwh * 100, 1)
    _LOGGER.debug(
        "Battery assessment: Case %s — charge %.1f kWh (grid %.1f kWh, %d slots, target %.0f%% SOC). "
        "%s. Charge %.3f CHF/kWh vs. alt %.3f CHF/kWh → save %.2f CHF",
        case, target_charge_kwh, needed_grid_kwh, charge_slots_needed, target_soc_pct,
        target_reason, avg_charge_price, avg_alt_price, savings,
    )

    return {
        **base_info,
        "grid_charge_needed": True,
        "missing_kwh": round(needed_grid_kwh, 1),
        "pv_deficit_kwh": round(pv_deficit_kwh, 1),
        "charge_slots_needed": charge_slots_needed,
        "effective_max_score": effective_max_score,
        "excluded_slot_indices": pv_window,
        "soc_trajectory": soc_trajectory,
        "depletion_slot": depletion_slot,
        "depletion_score": depletion_score,
        "target_charge_kwh": round(target_charge_kwh, 1),
        "target_soc_pct": target_soc_pct,
        "projected_start_soc_pct": round(projected_start_kwh / capacity_kwh * 100, 1),
        "pv_ratio": round(pv_ratio, 2),
        "avg_charge_price": round(avg_charge_price, 4),
        "avg_alt_price": round(avg_alt_price, 4),
        "cost_with_charge": round(cost_with_charge, 2),
        "cost_without_charge": round(cost_without_charge, 2),
        "case": case,
    }


def _build_battery_budget(ba: dict[str, Any], pv_available: list[float], num_slots: int) -> list[float]:
    """Akku-Budget pro Slot in kWh (Audit 2.1 / SOLL A1).

    budget(i) = SOC vor Slot i − min_soc − Reserve(i), wobei Reserve(i) die Energie ist,
    die der Akku von Slot i bis zum nächsten PV-Start noch für die Grundlast liefern muss
    (Σ max(0, Grundlast − PV) × 0.25). Nach dem letzten PV-Fenster des Tages wird die
    Reserve für die kommende Nacht mit dem heutigen Profil bis zum heutigen PV-Start
    fortgeschrieben (Annahme: morgen wie heute). Ohne PV-Fenster im Horizont ist die
    Reserve = Rest des Horizonts + ein voller Tag → Budget praktisch 0 (A1: kein Akku
    für Verschiebbares, wenn nichts nachkommt).

    Verschiebbare Verbraucher dürfen nur dieses Budget aus dem Akku beziehen; darüber
    hinaus zählt das Kostenmodell Netzpreis. Akku deaktiviert → Budget überall 0
    (= bisheriges Verhalten).
    """
    if not ba.get("battery_enabled") or num_slots == 0:
        return [0.0] * num_slots
    soc_before = ba.get("soc_before_kwh") or []
    base = ba.get("base_kw_per_slot") or []
    raw_pv = ba.get("raw_pv_kw_per_slot") or []
    if len(soc_before) < num_slots or len(base) < num_slots or len(raw_pv) < num_slots:
        return [0.0] * num_slots
    min_soc = float(ba.get("min_soc_kwh") or 0.0)
    sim_from = int(ba.get("sim_from") or 0)

    deficit = [max(0.0, base[i] - raw_pv[i]) * SLOT_HOUR_FRACTION for i in range(num_slots)]
    pv_slots = sorted(int(i) for i in (ba.get("excluded_slot_indices") or set()))
    first_pv = pv_slots[0] if pv_slots else None

    # Reserve chronologisch von hinten aufbauen: reserve[i] = deficit[i] + reserve[i+1],
    # zurückgesetzt auf 0 an jedem PV-Start (dort füllt PV den Akku wieder).
    # Hinter dem letzten PV-Fenster: Nacht-Reserve = Σ deficit bis Horizont-Ende + Σ deficit[0:first_pv]
    tail = sum(deficit[:first_pv]) if first_pv is not None else sum(deficit)
    pv_set = set(pv_slots)
    reserve = [0.0] * num_slots
    running = tail
    for i in range(num_slots - 1, -1, -1):
        if i in pv_set:
            # Innerhalb PV-Fenster: Akku wird geladen, Reserve für Verbraucher hier = 0
            reserve[i] = 0.0
            running = 0.0
            continue
        running += deficit[i]
        reserve[i] = running

    budget = []
    for i in range(num_slots):
        if i < sim_from:
            budget.append(0.0)
            continue
        budget.append(max(0.0, float(soc_before[i]) - min_soc - reserve[i]))
    return budget


def optimize_consumer_plans(
    hass: HomeAssistant,
    entry: ConfigEntry,
    active_slots: list[PriceSlot],
    store: TariffSaverStore | None,
    max_grid_power_kw: float = 25.0,
    target_date: date | None = None,
) -> dict[int, dict[str, Any]]:
    """Optimize all consumer plans for minimum grid cost over the day.

    Consumers can run in parallel if PV/grid capacity allows.
    Only consumers with different run_order > 0 cannot overlap.

    `target_date` ist das Datum, FÜR das geplant wird (Plan-Zieltag, nicht heute).
    Wichtig für `days_since_last_run` bei Daily-Hygiene-Consumern: ein Plan, der
    abends für morgen erstellt wird, muss days_since aus Sicht des Zieltags
    rechnen. Default: heute.
    """
    if not active_slots:
        return {}

    if target_date is None:
        target_date = dt_util.now().date()

    # Sort slots chronologically
    slots = sorted(active_slots, key=lambda s: s.start)
    num_slots = len(slots)

    # Past-slot exclusion: never plan a start into the past
    now_local = dt_util.now()
    now_utc = dt_util.utcnow()
    past_indices = {i for i, s in enumerate(slots) if dt_util.as_local(s.start) < now_local}

    # Pin: Consumer die JETZT laufen (persistierter Plan, chosen_start ≤ now < chosen_end)
    # werden nicht neu geplant — ihr Original-Fenster wird unverändert weitergetragen.
    pinned_plans: dict[int, dict[str, Any]] = {}
    charger_ctx: dict[str, Any] | None = None  # C9-Kontext für Pass 2 (Audit 2.2)

    # Ferienmodus: einmal pro Lauf prüfen (Overlay für pause_on_vacation-Consumer)
    from .vacation import is_vacation_active
    vacation_active = is_vacation_active(hass, entry)

    # -- Phase 1: Build slot data --
    # Price per slot
    prices = [_all_in_from_slot(s) or 0.0 for s in slots]

    # Score per slot
    valid_prices = [p for p in prices if p > 0.10]
    price_min = min(valid_prices) if valid_prices else None
    price_max = max(valid_prices) if valid_prices else None
    scores = [score_from_prices(p, price_min, price_max) for p in prices]

    # PV forecast ist bereits in slot.pv_kw gemappt (via tariff_saver.update_pv_forecast)
    base_profile = store.get_seasonal_load_profile() if store else {}
    pv_available = _build_pv_available(slots, base_profile)

    # -- Phase 1b: Battery need assessment --
    battery_assessment = _assess_battery_need(
        hass, entry, slots, store, prices, scores, pv_available, base_profile,
    )
    _log_assessment(entry.entry_id, battery_assessment)

    # -- Phase 1c: Compute PV windows for PV-Opportunist consumers --
    from .const import CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD
    pv_threshold = float(
        entry.options.get(CONF_AMPEL_PV_THRESHOLD, entry.data.get(CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD)) or DEFAULT_AMPEL_PV_THRESHOLD
    )
    pv_opp_windows = _compute_pv_windows(slots, pv_available, threshold_kw=pv_threshold, cutoff_minutes=60)
    _LOGGER.debug("PV-Opp windows: %s", pv_opp_windows)

    # -- Phase 2: Collect schedulable consumers --
    consumers: list[_ConsumerTask] = []
    plans: dict[int, dict[str, Any]] = {}

    for slot_nr in range(1, CONSUMER_COUNT + 1):
        config = get_consumer_config(entry, slot_nr)
        learning = store.get_consumer_learning(str(slot_nr)) if store else {}

        # Skip disabled, pv_opportunist, on_demand, not configured
        if not config.enabled:
            plans[slot_nr] = _disabled_plan(config)
            continue
        if config.pv_opportunist:
            plans[slot_nr] = _opportunist_plan(config, pv_windows=pv_opp_windows)
            continue
        if config.trigger_entity:
            # on_demand consumer — planned by on_demand.py when trigger fires
            continue
        if config.kind == "session":
            # Session-Consumer (Wallbox) — planned by _replan_sessions via session_start service
            continue

        # Ferienmodus: markierten Consumer pausieren (Overlay, enabled bleibt unangetastet).
        # VOR dem Pin-Block: ein laufender Consumer wird beim Ferien-Start gestoppt.
        # Activity-Log nur beim Übergang (alter Plan war noch nicht "vacation") — sonst
        # würde jeder 15-min-Re-Plan über Wochen loggen.
        if config.pause_on_vacation and vacation_active:
            if store:
                old = store.consumer_plans.get(slot_nr) or store.consumer_plans.get(str(slot_nr))
                if not isinstance(old, dict) or old.get("status") != "vacation":
                    store.log_activity("🏖️", f"{config.configured_name}: Ferienmodus — pausiert")
            plans[slot_nr] = {"status": "vacation", "should_run": False, "source": "-"}
            continue

        # Pin: läuft dieser Consumer GERADE (persistierter Plan, chosen_start ≤ now
        # < chosen_end)? Dann Original-Fenster unverändert weitertragen — VOR allen
        # weiteren Checks, damit skip_next_run/energy/due_status einen laufenden
        # Consumer nicht mitten im Fenster abbrechen. Task mit genau EINER Position
        # bleibt im Optimizer, damit run_order/Kapazität der anderen korrekt um ihn
        # herum geplant werden. Battery-Charger (C9) ausgenommen: der wird weiter von
        # seinem Assessment gesteuert (darf mitten im Fenster stoppen wenn Akku reicht).
        if not config.is_battery_charger:
            pin = _pinned_window(store, slot_nr, slots, now_utc)
            # Bedarfs-Consumer ohne Bedarf: Fenster läuft leer (Start-Automation hat nicht
            # gefeuert) → kein Phantom-Pin, sonst blockiert das leere Fenster jeden Re-Plan
            # bis Fensterende (Audit 7.2-Muster, 26.08. 14:00 Boiler bei ww_bedarf off).
            demand = str(getattr(config, "demand_entity", "") or "").strip()
            if pin is not None and demand:
                d_st = hass.states.get(demand)
                if d_st is None or d_st.state != "on":
                    pin = None
            if pin is not None:
                locked_idx, pinned_n = pin
                old_plan = store.consumer_plans.get(slot_nr) or store.consumer_plans.get(str(slot_nr))
                pinned_plans[slot_nr] = dict(old_plan)
                consumers.append(_ConsumerTask(
                    slot_nr=slot_nr,
                    config=config,
                    learning=learning,
                    power_kw=effective_power_kw(config, learning) or config.power_kw or 1.0,
                    n_slots=pinned_n,
                    valid_starts=[locked_idx],
                    due_status=None,
                ))
                if store:
                    store.log_activity("📌", f"{config.configured_name}: läuft — Start fixiert (kein Re-Plan)")
                continue

        if config.skip_next_run:
            plans[slot_nr] = {"status": "skipped_oneshot", "should_run": False, "source": "-"}
            if store:
                store.log_activity("⏭️", f"{config.configured_name}: OneShot — ausgelassen")
            continue
        energy = effective_energy_kwh(config, learning)
        if not energy or energy <= 0:
            plans[slot_nr] = _not_configured_plan(config)
            continue

        # Check due status (hygiene mode)
        due_status = _check_due_status(config, learning, store, target_date, hass=hass)
        if due_status in ("waiting_window", "waiting_demand"):
            plans[slot_nr] = _waiting_plan(config, due_status, learning, store, target_date)
            continue

        power = effective_power_kw(config, learning) or config.power_kw or 1.0
        n_slots = required_slot_count(config, learning)

        # -- Battery-aware overrides (only for the dedicated battery-charger consumer) --
        excluded = None
        max_score_override = None
        if config.is_battery_charger:
            ba = battery_assessment
            excluded = ba["excluded_slot_indices"] or None
            charger_ctx = {"slot_nr": slot_nr, "config": config, "learning": learning, "power": power}

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
                    store.record_battery_event("case", case=ba["case"])
                continue

            # Override n_slots and max_score from battery assessment
            n_slots = ba["charge_slots_needed"]
            max_score_override = ba["effective_max_score"]
            _LOGGER.info("Consumer %d (%s): battery override — %d slots, max_score %d, excluding %d PV slots",
                         slot_nr, config.configured_name, n_slots, max_score_override, len(excluded) if excluded else 0)

        # Build allowed-time mask if consumer has a time window restriction
        allowed_indices = _build_allowed_indices(config, slots) if (config.allowed_from or config.allowed_until) else None

        # Runtime filter: if runtime_sensor shows >= duration today, drop today's slots
        allowed_indices, runtime_done = _apply_runtime_filter(hass, config, slots, allowed_indices)
        if runtime_done:
            plans[slot_nr] = {
                "status": "runtime_reached",
                "should_run": False,
                "source": config.runtime_sensor,
            }
            if store:
                store.log_activity("✅", f"{config.configured_name}: Tages-Laufzeit schon erreicht — übersprungen")
            continue

        # Find valid start positions for this consumer (merge past-slot exclusion)
        merged_excluded = (excluded | past_indices) if excluded else past_indices
        valid_starts = _find_valid_starts(
            config, n_slots, num_slots, scores, config.tariff_only,
            excluded_indices=merged_excluded,
            max_score_override=max_score_override,
            allowed_indices=allowed_indices,
        )

        if not valid_starts:
            plans[slot_nr] = _no_window_plan(config, due_status, learning, store, target_date)
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

    # -- Phase 3: Optimize (dreistufiges Kostenmodell PV → Akku → Netz, Audit 2.1) --
    budget = _build_battery_budget(battery_assessment, pv_available, num_slots)
    ctx = _CostCtx(prices, pv_available, budget, battery_assessment["refill_price"], max_grid_power_kw)
    _LOGGER.debug("Scheduler: Akku-Budget max %.1f kWh, Akkupreis %.3f CHF/kWh",
                 max(budget) if budget else 0.0, ctx.battery_price)

    if not consumers:
        _persist_battery_budget(store, slots, budget, ctx.battery_price, [])
        return plans

    def _optimize(tasks: list[_ConsumerTask]) -> dict[int, int]:
        total_combos = 1
        for c in tasks:
            total_combos *= len(c.valid_starts)
        if total_combos <= _MAX_COMBINATIONS:
            return _brute_force_optimize(tasks, num_slots, ctx)
        _LOGGER.info("Scheduler: %d combinations too many, using greedy fallback", total_combos)
        return _greedy_optimize(tasks, num_slots, ctx)

    best = _optimize(consumers)

    # -- Phase 3b: Assessment mit geplanten Consumer-Lasten wiederholen (Audit 2.2) --
    # Die Nacht-Consumer aus Pass 1 entladen den Akku zusätzlich zur Grundlast.
    # Ändert sich dadurch der C9-Bedarf (Status/Slots), C9 nachziehen und einmal
    # neu optimieren (Budget bleibt das von Pass 1 — sonst würden die Consumer ihre
    # eigene Last doppelt zählen).
    if charger_ctx is not None:
        extra_load = [0.0] * num_slots
        for c in consumers:
            st = best.get(c.slot_nr)
            if st is None or getattr(c.config, "is_battery_charger", False):
                continue
            for j in range(c.n_slots):
                if st + j < num_slots:
                    extra_load[st + j] += c.power_kw
        if any(extra_load):
            ba2 = _assess_battery_need(
                hass, entry, slots, store, prices, scores, pv_available, base_profile,
                extra_load_kw_per_slot=extra_load,
            )
            ba1 = battery_assessment
            changed = (
                ba2["grid_charge_needed"] != ba1["grid_charge_needed"]
                or ba2["charge_slots_needed"] != ba1["charge_slots_needed"]
            )
            if changed:
                _LOGGER.info(
                    "Battery assessment Pass 2 (mit %.1f kWh Consumer-Last): case %s→%s, slots %d→%d",
                    sum(extra_load) * SLOT_HOUR_FRACTION, ba1["case"], ba2["case"],
                    ba1["charge_slots_needed"], ba2["charge_slots_needed"],
                )
                battery_assessment = ba2
                c_slot = charger_ctx["slot_nr"]
                consumers = [c for c in consumers if c.slot_nr != c_slot]
                plans.pop(c_slot, None)
                if not ba2["grid_charge_needed"]:
                    plans[c_slot] = {
                        "status": "not_needed", "should_run": False,
                        "source": "battery_sufficient", "battery_case": ba2["case"],
                        "soc_trajectory": ba2["soc_trajectory"],
                    }
                    if store:
                        store.record_battery_event("case", case=ba2["case"])
                else:
                    c_cfg = charger_ctx["config"]
                    c_excl = ba2["excluded_slot_indices"] or set()
                    c_allowed = _build_allowed_indices(c_cfg, slots) if (c_cfg.allowed_from or c_cfg.allowed_until) else None
                    c_starts = _find_valid_starts(
                        c_cfg, ba2["charge_slots_needed"], num_slots, scores, c_cfg.tariff_only,
                        excluded_indices=(c_excl | past_indices),
                        max_score_override=ba2["effective_max_score"],
                        allowed_indices=c_allowed,
                    )
                    if c_starts:
                        consumers.append(_ConsumerTask(
                            slot_nr=c_slot, config=c_cfg, learning=charger_ctx["learning"],
                            power_kw=charger_ctx["power"], n_slots=ba2["charge_slots_needed"],
                            valid_starts=c_starts, due_status=None,
                        ))
                    else:
                        plans[c_slot] = _no_window_plan(c_cfg, None, charger_ctx["learning"], store, target_date)
                best = _optimize(consumers)

    _, split = _split_assignment(consumers, best, num_slots, ctx)

    # -- Phase 4: Build output plans --
    for task in consumers:
        # Pinned (gerade laufend): Original-Plan unverändert weitertragen —
        # nie _build_plan/best, damit chosen_start/chosen_end exakt erhalten bleiben
        # (should_run läuft lückenlos weiter, auch falls best leer wäre).
        if task.slot_nr in pinned_plans:
            pinned = dict(pinned_plans[task.slot_nr])
            pinned["pinned"] = True
            plans[task.slot_nr] = pinned
            continue

        start_idx = best.get(task.slot_nr)
        if start_idx is None:
            plans[task.slot_nr] = _no_window_plan(task.config, task.due_status, task.learning, store, target_date)
            continue

        slot_start = slots[start_idx].start
        slot_end = slots[start_idx + task.n_slots - 1].start + timedelta(minutes=SLOT_MINUTES)
        window_prices = prices[start_idx:start_idx + task.n_slots]
        avg_price = sum(window_prices) / len(window_prices) if window_prices else 0

        # Quelle aus dem Energie-Split (pv / battery / grid / mixed)
        sp = split.get(task.slot_nr, {})
        if getattr(task.config, "is_battery_charger", False):
            source = "grid"
        else:
            source = energy_source_label(sp.get("pv_kwh", 0.0), sp.get("battery_kwh", 0.0), sp.get("grid_kwh", 0.0))

        plan = _build_plan(
            task, slot_start, slot_end, avg_price, source, slots, store, target_date,
        )
        plan["expected_pv_kwh"] = round(sp.get("pv_kwh", 0.0), 3)
        plan["expected_battery_kwh"] = round(sp.get("battery_kwh", 0.0), 3)
        plan["expected_grid_kwh"] = round(sp.get("grid_kwh", 0.0), 3)
        plan["expected_cost_chf"] = round(sp.get("cost_chf", 0.0), 4)
        # Attach battery assessment to tariff_only plans
        if task.config.tariff_only and battery_assessment["grid_charge_needed"]:
            plan["battery_case"] = battery_assessment["case"]
            plan["battery_missing_kwh"] = battery_assessment["missing_kwh"]
            plan["battery_pv_deficit_kwh"] = battery_assessment["pv_deficit_kwh"]
            plan["battery_effective_max_score"] = battery_assessment["effective_max_score"]
            plan["battery_target_soc_pct"] = battery_assessment["target_soc_pct"]
            if getattr(task.config, "is_battery_charger", False):
                # Echte Lademenge statt power × konfigurierte Dauer (Audit 3.1)
                plan["expected_energy_kwh"] = float(battery_assessment["missing_kwh"])
                plan["expected_grid_kwh"] = float(battery_assessment["missing_kwh"])
        plans[task.slot_nr] = plan

    # Budget-Kurve für on_demand/Hysterese persistieren (gleiches Modell, Audit 2.3)
    charger_windows = []
    for c in consumers:
        st = best.get(c.slot_nr)
        if st is not None and getattr(c.config, "is_battery_charger", False):
            charger_windows.append((st, st + c.n_slots))
    _persist_battery_budget(store, slots, budget, ctx.battery_price, charger_windows)

    return plans


def _persist_battery_budget(store, slots, budget: list[float], battery_price: float,
                            charger_windows: list[tuple[int, int]]) -> None:
    """Budget-Kurve im Store ablegen — on_demand und Hysterese rechnen damit (ein Modell, drei Aufrufer)."""
    if store is None:
        return
    store.battery_budget = {
        "computed_at": dt_util.now().isoformat(),
        "battery_price": battery_price,
        "slots": {
            dt_util.as_local(slots[i].start).isoformat(): {
                "budget_kwh": round(budget[i], 3),
                "charger": any(a <= i < b for a, b in charger_windows),
            }
            for i in range(len(slots))
        },
    }


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


def _pinned_window(store, slot_nr: int, slots: list, now_utc) -> tuple[int, int] | None:
    """Wenn Consumer `slot_nr` GERADE läuft (persistierter Plan, status 'planned',
    chosen_start ≤ now < chosen_end), liefere (locked_idx, n_slots) seines
    Original-Fensters — sonst None.

    locked_idx = Index in `slots` des Slots, der chosen_start enthält.
    n_slots    = Slot-Anzahl des Original-Fensters (chosen_end - chosen_start),
                 auf das verfügbare Slot-Ende geklemmt.
    Erkennung identisch zu abort.evaluate_aborts."""
    plans = getattr(store, "consumer_plans", None) if store is not None else None
    if not plans:
        return None
    plan = plans.get(slot_nr) or plans.get(str(slot_nr))
    if not isinstance(plan, dict) or plan.get("status") != "planned":
        return None
    cs = dt_util.parse_datetime(str(plan.get("chosen_start") or ""))
    ce = dt_util.parse_datetime(str(plan.get("chosen_end") or ""))
    if not cs or not ce:
        return None
    cs_utc = dt_util.as_utc(cs)
    ce_utc = dt_util.as_utc(ce)
    if not (cs_utc <= now_utc < ce_utc):
        return None
    # Slot finden, der chosen_start enthält (tolerant: start ≤ cs < start + SLOT)
    slot_delta = timedelta(minutes=SLOT_MINUTES)
    locked_idx = None
    for i, s in enumerate(slots):
        s_utc = dt_util.as_utc(s.start)
        if s_utc <= cs_utc < s_utc + slot_delta:
            locked_idx = i
            break
    if locked_idx is None:
        return None
    n_slots = max(1, round((ce_utc - cs_utc).total_seconds() / 60 / SLOT_MINUTES))
    n_slots = min(n_slots, len(slots) - locked_idx)
    return (locked_idx, n_slots)


# -- Optimization --

class _CostCtx:
    """Slot-Daten fürs dreistufige Kostenmodell PV → Akku → Netz (Cluster 2)."""

    __slots__ = ("prices", "pv_available", "budget", "battery_price", "max_grid_kw")

    def __init__(self, prices, pv_available, budget, battery_price, max_grid_kw):
        self.prices = prices
        self.pv_available = pv_available
        self.budget = budget            # kWh Akku-Budget pro Slot (kumulativ verbraucht)
        self.battery_price = battery_price
        self.max_grid_kw = max_grid_kw


def _brute_force_optimize(
    consumers: list[_ConsumerTask],
    num_slots: int,
    ctx: _CostCtx,
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
        cost = _calc_grid_cost(consumers, assignment, num_slots, ctx)
        if cost is None:
            continue  # Capacity exceeded

        if cost < best_cost or (cost == best_cost and _prefer_by_pv_surplus(consumers, assignment, best_assignment, ctx.pv_available)):
            best_cost = cost
            best_assignment = dict(assignment)

    return best_assignment


def _greedy_optimize(
    consumers: list[_ConsumerTask],
    num_slots: int,
    ctx: _CostCtx,
) -> dict[int, int]:
    """Greedy fallback: schedule consumers by priority, picking cheapest valid position.

    Bewertet jede Position mit dem vollen Kostenmodell (bisher platzierte + Kandidat),
    damit PV-/Akku-/Netz-Aufteilung und Kapazität konsistent zur Brute-Force bleiben.
    """
    sorted_consumers = sorted(consumers, key=lambda c: (-c.config.priority, c.config.run_order))
    assignment: dict[int, int] = {}

    for task in sorted_consumers:
        best_start = None
        best_cost = float("inf")
        best_pv_surplus = -1.0  # Tiebreaker: prefer highest PV surplus

        for start in task.valid_starts:
            test_assignment = dict(assignment)
            test_assignment[task.slot_nr] = start
            if not _check_run_order(consumers, test_assignment):
                continue
            cost = _calc_grid_cost(consumers, test_assignment, num_slots, ctx)
            if cost is None:
                continue
            pv_surplus = 0.0
            if not task.config.tariff_only:
                for j in range(task.n_slots):
                    pv_surplus += ctx.pv_available[start + j] - task.power_kw
            if cost < best_cost or (cost == best_cost and pv_surplus > best_pv_surplus):
                best_cost = cost
                best_start = start
                best_pv_surplus = pv_surplus

        if best_start is not None:
            assignment[task.slot_nr] = best_start

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


def _split_assignment(
    consumers: list[_ConsumerTask],
    assignment: dict[int, int],
    num_slots: int,
    ctx: _CostCtx,
) -> tuple[float | None, dict[int, dict[str, float]]]:
    """Dreistufiges Kostenmodell (Audit 2.1 / SOLL S5, P1–P4), chronologisch über die Slots.

    Pro Slot: Last → PV-Überschuss (0 CHF) → Akku-Budget (Wiederbeschaffungspreis)
    → Netz (Slot-Preis). Das Akku-Budget wird über die Slots hinweg verbraucht (was um
    22:00 aus dem Akku geht, fehlt um 05:00). tariff_only ignoriert nur PV, nicht den
    Akku. Der Akku-Lader (is_battery_charger) ist immer Netz und blockiert in seinen
    Slots den Akku für alle anderen (forcible charge → Haus am Netz).

    Rückgabe: (Gesamtkosten CHF oder None bei Netz-Kapazitätsüberschreitung,
               {slot_nr: {pv_kwh, battery_kwh, grid_kwh, cost_chf}}).
    """
    hours = SLOT_HOUR_FRACTION
    # Lasten pro Slot einsammeln
    normal: list[list[_ConsumerTask]] = [[] for _ in range(num_slots)]
    tariff_only: list[list[_ConsumerTask]] = [[] for _ in range(num_slots)]
    charger_kw = [0.0] * num_slots
    charger_tasks: list[list[_ConsumerTask]] = [[] for _ in range(num_slots)]
    for c in consumers:
        start = assignment.get(c.slot_nr)
        if start is None:
            continue
        for j in range(c.n_slots):
            i = start + j
            if i >= num_slots:
                break
            if getattr(c.config, "is_battery_charger", False):
                charger_kw[i] += c.power_kw
                charger_tasks[i].append(c)
            elif c.config.tariff_only:
                tariff_only[i].append(c)
            else:
                normal[i].append(c)

    split: dict[int, dict[str, float]] = {
        c.slot_nr: {"pv_kwh": 0.0, "battery_kwh": 0.0, "grid_kwh": 0.0, "cost_chf": 0.0}
        for c in consumers if c.slot_nr in assignment
    }
    total_cost = 0.0
    battery_used = 0.0
    for i in range(num_slots):
        if not normal[i] and not tariff_only[i] and charger_kw[i] <= 0:
            continue
        price = ctx.prices[i]
        pv_left = max(0.0, ctx.pv_available[i]) * hours
        batt_blocked = charger_kw[i] > 0
        batt_avail = 0.0 if batt_blocked else max(0.0, ctx.budget[i] - battery_used)
        grid_kw = charger_kw[i]

        # Lader: immer Netz
        for c in charger_tasks[i]:
            e = c.power_kw * hours
            split[c.slot_nr]["grid_kwh"] += e
            split[c.slot_nr]["cost_chf"] += e * price
            total_cost += e * price

        # Normale Consumer: PV zuerst (anteilig nach Last), dann Akku, dann Netz
        for group, use_pv in ((normal[i], True), (tariff_only[i], False)):
            for c in group:
                need = c.power_kw * hours
                pv_kwh = min(need, pv_left) if use_pv else 0.0
                pv_left -= pv_kwh
                rest = need - pv_kwh
                b_kwh = min(rest, batt_avail)
                batt_avail -= b_kwh
                battery_used += b_kwh
                g_kwh = rest - b_kwh
                grid_kw += g_kwh / hours
                cost = b_kwh * ctx.battery_price + g_kwh * price
                sp = split[c.slot_nr]
                sp["pv_kwh"] += pv_kwh
                sp["battery_kwh"] += b_kwh
                sp["grid_kwh"] += g_kwh
                sp["cost_chf"] += cost
                total_cost += cost

        if grid_kw > ctx.max_grid_kw + 1e-9:
            return None, split

    return total_cost, split


def _calc_grid_cost(
    consumers: list[_ConsumerTask],
    assignment: dict[int, int],
    num_slots: int,
    ctx: _CostCtx,
) -> float | None:
    """Gesamtkosten einer Zuordnung (PV 0 / Akku Wiederbeschaffung / Netz). None bei Kapazitätsüberschreitung."""
    cost, _ = _split_assignment(consumers, assignment, num_slots, ctx)
    return cost


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
    base_profile: dict,
) -> list[float]:
    """Build list of available PV kW per slot (slot.pv_kw minus base load)."""
    result = []
    for slot in slots:
        local_dt = dt_util.as_local(slot.start)
        slot_idx = local_dt.hour * SLOTS_PER_HOUR + local_dt.minute // SLOT_MINUTES
        base_kwh = base_profile.get(slot_idx, 0.0) or base_profile.get(str(slot_idx), 0.0) or 0.0
        base_kw = float(base_kwh) * SLOTS_PER_HOUR

        pv_kw = float(getattr(slot, "pv_kw", 0.0) or 0.0)
        available = max(0.0, pv_kw - base_kw)
        result.append(available)

    return result


def _compute_pv_windows(
    slots: list[PriceSlot],
    pv_available: list[float],
    threshold_kw: float = 0.5,
    cutoff_minutes: int = 60,
) -> list[dict[str, str]]:
    """Compute PV windows from forecast — contiguous periods where PV >= threshold.

    Returns list of {start: ISO, end: ISO} dicts.
    end is cut off by cutoff_minutes (default 60) to allow battery charging before PV ends.
    """
    if not slots or not pv_available:
        return []

    windows: list[dict[str, str]] = []
    in_window = False
    window_start = None

    for i, pv_kw in enumerate(pv_available):
        if pv_kw >= threshold_kw:
            if not in_window:
                window_start = slots[i].start
                in_window = True
        else:
            if in_window:
                window_end = slots[i].start
                windows.append({
                    "start": dt_util.as_local(window_start).isoformat(),
                    "end": dt_util.as_local(window_end).isoformat(),
                })
                in_window = False

    # Close last window if still open
    if in_window and window_start is not None:
        last_end = slots[-1].start + timedelta(minutes=SLOT_MINUTES)
        windows.append({
            "start": dt_util.as_local(window_start).isoformat(),
            "end": dt_util.as_local(last_end).isoformat(),
        })

    # Apply cutoff: shorten end by cutoff_minutes
    if cutoff_minutes > 0:
        shortened = []
        for w in windows:
            end_dt = dt_util.parse_datetime(w["end"])
            start_dt = dt_util.parse_datetime(w["start"])
            if end_dt and start_dt:
                new_end = end_dt - timedelta(minutes=cutoff_minutes)
                if new_end > start_dt:
                    shortened.append({"start": w["start"], "end": new_end.isoformat()})
        return shortened

    return windows


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    s = (s or "").strip()
    if not s or ":" not in s:
        return None
    try:
        hh, mm = s.split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return None


def _read_runtime_today_minutes(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a runtime sensor (value in hours) and return minutes. None if unavailable."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if not st or st.state in ("unknown", "unavailable", "", None):
        return None
    try:
        return float(st.state) * 60.0
    except (TypeError, ValueError):
        return None


def _apply_runtime_filter(
    hass: HomeAssistant,
    config: ConsumerConfig,
    slots,
    allowed_indices: set[int] | None,
) -> tuple[set[int] | None, bool]:
    """If runtime_sensor reports >= duration_minutes, exclude today-dated slots.
    Returns (new_allowed_indices, runtime_reached_and_no_future_slots).
    """
    if not config.runtime_sensor or config.duration_minutes <= 0:
        return allowed_indices, False
    mins = _read_runtime_today_minutes(hass, config.runtime_sensor)
    if mins is None or mins < config.duration_minutes:
        return allowed_indices, False
    today_local = dt_util.now().date()
    today_idx = {i for i, s in enumerate(slots) if dt_util.as_local(s.start).date() == today_local}
    if not today_idx:
        return allowed_indices, False
    if allowed_indices is None:
        new_allowed = set(range(len(slots))) - today_idx
    else:
        new_allowed = allowed_indices - today_idx
    runtime_done = len(new_allowed) == 0
    return new_allowed, runtime_done


def _build_allowed_indices(config: ConsumerConfig, slots) -> set[int] | None:
    """Return set of slot indices that fall within [allowed_from, allowed_until] (local time).
    allowed_until < allowed_from means range crosses midnight.
    Returns None if neither bound is set (no restriction).
    """
    from_t = _parse_hhmm(config.allowed_from)
    until_t = _parse_hhmm(config.allowed_until)
    if from_t is None and until_t is None:
        return None
    allowed: set[int] = set()
    for idx, s in enumerate(slots):
        local = dt_util.as_local(s.start)
        mins = local.hour * 60 + local.minute
        fm = from_t[0] * 60 + from_t[1] if from_t else 0
        um = until_t[0] * 60 + until_t[1] if until_t else 24 * 60
        if fm <= um:
            ok = fm <= mins < um
        else:
            # Crosses midnight: allowed if mins >= fm OR mins < um
            ok = mins >= fm or mins < um
        if ok:
            allowed.add(idx)
    return allowed


def _find_valid_starts(
    config: ConsumerConfig,
    n_slots: int,
    num_slots: int,
    scores: list[int | None],
    tariff_only: bool,
    excluded_indices: set[int] | None = None,
    max_score_override: int | None = None,
    allowed_indices: set[int] | None = None,
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
            # Allowed-time window filter (if set)
            if allowed_indices is not None and idx not in allowed_indices:
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


def _check_due_status(config: ConsumerConfig, learning, store, target_date: date | None = None,
                      hass: HomeAssistant | None = None) -> str | None:
    """Check if hygiene-mode consumer is due to run on `target_date` (default: today).

    Mit `demand_entity` (Audit 9.1 / WW1) entscheidet ausschliesslich das Bedarfs-Signal:
    on → `forced` (auch mehrfach pro Tag, kein Backstop über days_since — Betreiber-Entscheid
    2026-08-26), off/unbekannt → `waiting_demand` (kein Fenster).
    """
    demand = str(getattr(config, "demand_entity", "") or "").strip()
    if demand and hass is not None:
        st = hass.states.get(demand)
        return "forced" if (st is not None and st.state == "on") else "waiting_demand"
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

    ref_date = target_date or dt_util.now().date()
    days_since = max(0, (ref_date - dt_util.as_local(last_run).date()).days)

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
    target_date: date | None = None,
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
        ref_date = target_date or dt_util.now().date()
        days_since = max(0, (ref_date - dt_util.as_local(last_run).date()).days)

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
        "tariff_only": bool(cfg.tariff_only),
        "is_battery_charger": bool(getattr(cfg, "is_battery_charger", False)),
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


def _opportunist_plan(config: ConsumerConfig, pv_windows: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"status": "pv_opportunist", "should_run": False, "source": None, "pv_windows": pv_windows or []}


def _not_configured_plan(config: ConsumerConfig) -> dict[str, Any]:
    return {"status": "not_configured", "should_run": False, "source": None}


def _waiting_plan(config, due_status, learning, store, target_date: date | None = None) -> dict[str, Any]:
    days_since = 0
    if isinstance(learning, dict):
        lr = learning.get("last_run_end_utc")
        if isinstance(lr, str):
            last_run = dt_util.parse_datetime(lr)
            if last_run:
                ref_date = target_date or dt_util.now().date()
                days_since = max(0, (ref_date - dt_util.as_local(last_run).date()).days)
    return {
        "status": due_status or "waiting_window",
        "should_run": False,
        "source": None,
        "due_status": due_status,
        "days_since_last_run": days_since,
        "min_days": config.min_days,
        "max_days": config.max_days,
    }


def _no_window_plan(config, due_status, learning, store, target_date: date | None = None) -> dict[str, Any]:
    days_since = 0
    if isinstance(learning, dict):
        lr = learning.get("last_run_end_utc")
        if isinstance(lr, str):
            last_run = dt_util.parse_datetime(lr)
            if last_run:
                ref_date = target_date or dt_util.now().date()
                days_since = max(0, (ref_date - dt_util.as_local(last_run).date()).days)
    return {
        "status": "unavailable" if not due_status else due_status,
        "source": None,
        "due_status": due_status,
        "days_since_last_run": days_since,
        "min_days": config.min_days,
        "max_days": config.max_days,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Session-Consumer Scheduler (kind=session, z.B. Wallbox — Phase 2 Sub 2d)
#  Greedy-Algorithmus:
#    1. Filtere Slots zwischen now und deadline
#    2. Baue Kandidaten (pro Slot: PV-Modus und/oder Grid-Modus)
#    3. Sortiere nach Effektiv-Preis pro kWh
#    4. Greedy-Pick bis energy_needed_kwh gedeckt (jeder Slot max 1x)
# ─────────────────────────────────────────────────────────────────────────────


def _compute_grid_load_per_slot(
    consumer_plans: dict[str, Any],
) -> dict[datetime, float]:
    """Liefert pro Slot-Start (UTC) die Grid-Leistung anderer daily_fixed-Consumer in W.

    Nur Plans mit source in {"tariff"} zählen — PV-Consumer nutzen Sonne, nicht Netz.
    Session-Plans und disabled/skipped Pläne werden ignoriert.
    """
    load: dict[datetime, float] = {}
    for slot_key, plan in (consumer_plans or {}).items():
        if not isinstance(plan, dict):
            continue
        if plan.get("kind") == "session":
            continue
        source = str(plan.get("source", "") or "")
        if source not in ("tariff",):
            continue
        cs_raw = plan.get("chosen_start")
        ce_raw = plan.get("chosen_end")
        power_kw = float(plan.get("power_kw", 0.0) or 0.0)
        if not cs_raw or not ce_raw or power_kw <= 0:
            continue
        try:
            cs = dt_util.as_utc(dt_util.parse_datetime(str(cs_raw)))
            ce = dt_util.as_utc(dt_util.parse_datetime(str(ce_raw)))
        except Exception:
            continue
        if not cs or not ce or ce <= cs:
            continue
        power_w = power_kw * 1000.0
        # Alle 15-min-Slots innerhalb [cs, ce) markieren
        cursor = cs.replace(second=0, microsecond=0)
        cursor = cursor.replace(minute=(cursor.minute // SLOT_MINUTES) * SLOT_MINUTES)
        while cursor < ce:
            load[cursor] = load.get(cursor, 0.0) + power_w
            cursor = cursor + timedelta(minutes=SLOT_MINUTES)
    return load


def build_session_plan(
    session: dict[str, Any],
    active_slots: list[PriceSlot],
    now_utc: datetime,
    grid_load_per_slot: dict[datetime, float] | None = None,
    max_grid_power_w: float = 25000.0,
) -> dict[str, Any]:
    """Berechnet Plan für eine Session (Wallbox).

    Args:
        session: dict aus storage.consumer_sessions[slot] (energy_needed_kwh, deadline_utc, min/max_power_w, prefer_pv)
        active_slots: alle PriceSlots aus coordinator.data["active"]
        now_utc: aktuelle Zeit UTC

    Returns:
        dict mit chosen_windows, delivered_kwh, feasible, total_cost_chf, target_now_w
    """
    if not isinstance(session, dict) or not session.get("active"):
        return _empty_session_plan()

    energy_needed = float(session.get("energy_needed_kwh", 0.0) or 0.0)
    if energy_needed <= 0.0:
        return _empty_session_plan(reason="no_energy_needed")

    deadline_raw = session.get("deadline_utc")
    if not deadline_raw:
        # Pure-PV-ohne-Deadline — TS plant NICHT (huawei_solar v1 übernimmt direkt)
        return _empty_session_plan(reason="no_deadline_pv_only")

    try:
        deadline_utc = dt_util.as_utc(dt_util.parse_datetime(str(deadline_raw)))
    except Exception:
        return _empty_session_plan(reason="invalid_deadline")

    min_power_w = int(session.get("min_power_w", 1380) or 1380)
    max_power_w = int(session.get("max_power_w", 11000) or 11000)
    prefer_pv = bool(session.get("prefer_pv", True))

    # Puffer: 15 min vor echter Deadline (Design 6.3)
    effective_deadline = deadline_utc - timedelta(minutes=15)

    # Slots im Fenster [now, effective_deadline] — duck-typing (s.start), weil
    # coordinator.py und models.py zwei verschiedene PriceSlot-Klassen haben
    candidate_slots = [
        s for s in active_slots
        if hasattr(s, "start")
        and dt_util.as_utc(s.start) >= now_utc
        and dt_util.as_utc(s.start) < effective_deadline
    ]
    if not candidate_slots:
        return _empty_session_plan(reason="no_slots_in_window", needed=energy_needed)

    # Baue Kandidatenliste: pro Slot PV-Variante + Grid-Variante
    candidates = []
    slot_duration_h = SLOT_HOUR_FRACTION  # 0.25h
    pv_bonus_chf_per_kwh = 0.05 if prefer_pv else 0.0  # Bonus wenn PV bevorzugt
    grid_load_per_slot = grid_load_per_slot or {}

    for slot in candidate_slots:
        price = _all_in_from_slot(slot)
        if price is None or price < 0.10:
            continue  # Dummy-Preis ignorieren
        pv_kw = float(getattr(slot, "pv_kw", 0.0) or 0.0)
        pv_surplus_w = int(pv_kw * 1000)

        # Grid-Cap: andere daily_fixed-tariff-Consumer reservieren Grid-Leistung in diesem Slot
        slot_start_utc = dt_util.as_utc(slot.start)
        other_load_w = float(grid_load_per_slot.get(slot_start_utc, 0.0))
        available_grid_w = int(max(0.0, max_grid_power_w - other_load_w))

        # PV-Modus: nur wenn PV ausreicht für Min-Power (Grid-Cap irrelevant, PV ist nicht Netz)
        if pv_surplus_w >= min_power_w:
            pv_power = min(max_power_w, pv_surplus_w)
            pv_energy = pv_power * slot_duration_h / 1000.0
            # Cost = grid_fraction × price. PV-Anteil "gratis" (prefer_pv) oder feed_in (sonst)
            grid_fraction = max(0.0, pv_power - pv_surplus_w) / max(1, pv_power)
            pv_cost_per_kwh = price * grid_fraction
            if prefer_pv:
                pv_cost_per_kwh = max(0.0, pv_cost_per_kwh - pv_bonus_chf_per_kwh)
            candidates.append({
                "slot_start": slot.start,
                "mode": "pv",
                "power_w": pv_power,
                "energy_kwh": round(pv_energy, 4),
                "price_chf_per_kwh": round(price, 6),
                "effective_cost_per_kwh": round(pv_cost_per_kwh, 6),
            })
        # Grid-Modus: nur wenn verfügbare Grid-Leistung ≥ min_power_w
        grid_power = min(max_power_w, available_grid_w)
        if grid_power >= min_power_w:
            grid_energy = grid_power * slot_duration_h / 1000.0
            candidates.append({
                "slot_start": slot.start,
                "mode": "grid",
                "power_w": grid_power,
                "energy_kwh": round(grid_energy, 4),
                "price_chf_per_kwh": round(price, 6),
                "effective_cost_per_kwh": round(price, 6),
            })

    if not candidates:
        return _empty_session_plan(reason="no_valid_candidates", needed=energy_needed)

    # Greedy: günstigste zuerst
    candidates.sort(key=lambda c: c["effective_cost_per_kwh"])

    chosen: list[dict[str, Any]] = []
    delivered = 0.0
    total_cost = 0.0
    used_starts: set = set()

    for c in candidates:
        if delivered >= energy_needed:
            break
        if c["slot_start"] in used_starts:
            continue
        chosen.append(c)
        delivered += c["energy_kwh"]
        total_cost += c["energy_kwh"] * c["price_chf_per_kwh"]
        used_starts.add(c["slot_start"])

    # Sortiere chronologisch für Anzeige
    chosen.sort(key=lambda c: c["slot_start"])

    feasible = delivered >= energy_needed

    # Target jetzt: gibt es einen gewählten Slot der aktuell läuft?
    target_now_w = 0
    current_window = None
    for c in chosen:
        slot_start_utc = dt_util.as_utc(c["slot_start"])
        slot_end_utc = slot_start_utc + timedelta(minutes=SLOT_MINUTES)
        if slot_start_utc <= now_utc < slot_end_utc:
            target_now_w = int(c["power_w"])
            current_window = c
            break

    # Next window start (falls aktuell keiner läuft)
    next_window = None
    if current_window is None:
        for c in chosen:
            if dt_util.as_utc(c["slot_start"]) > now_utc:
                next_window = c
                break

    return {
        "chosen_windows": [
            {
                "start": c["slot_start"].isoformat() if hasattr(c["slot_start"], "isoformat") else str(c["slot_start"]),
                "mode": c["mode"],
                "power_w": c["power_w"],
                "energy_kwh": c["energy_kwh"],
                "price_chf_per_kwh": c["price_chf_per_kwh"],
            }
            for c in chosen
        ],
        "delivered_kwh": round(delivered, 3),
        "energy_needed_kwh": round(energy_needed, 3),
        "feasible": feasible,
        "total_cost_chf": round(total_cost, 4),
        "target_now_w": target_now_w,
        "current_window": _serialize_window(current_window),
        "next_window": _serialize_window(next_window),
        "deadline_utc": deadline_raw,
        "computed_at_utc": now_utc.isoformat(),
    }


def _empty_session_plan(reason: str = "no_active_session", needed: float = 0.0) -> dict[str, Any]:
    return {
        "chosen_windows": [],
        "delivered_kwh": 0.0,
        "energy_needed_kwh": needed,
        "feasible": True,
        "total_cost_chf": 0.0,
        "target_now_w": 0,
        "current_window": None,
        "next_window": None,
        "reason": reason,
    }


def _serialize_window(c: dict[str, Any] | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "start": c["slot_start"].isoformat() if hasattr(c["slot_start"], "isoformat") else str(c["slot_start"]),
        "mode": c["mode"],
        "power_w": c["power_w"],
        "energy_kwh": c["energy_kwh"],
        "price_chf_per_kwh": c["price_chf_per_kwh"],
    }


def compute_pv_reserve_for_wallbox(
    active_plans: dict[str, Any],
    now_utc: datetime,
) -> int:
    """PV-Leistung die andere TS-Consumer aktuell oder in Kürze brauchen.

    Die Wallbox-Session muss diese W frei lassen. huawei_solar zieht den Wert
    vom rohen PV-Surplus ab bevor es lädt.

    Args:
        active_plans: coordinator-plans (store.consumer_plans) für alle daily_fixed-Consumer
        now_utc: aktuelle Zeit UTC

    Returns:
        W die für andere Consumer reserviert sind
    """
    reserve_w = 0
    lookahead = now_utc + timedelta(minutes=30)
    for slot_key, plan in (active_plans or {}).items():
        if not isinstance(plan, dict):
            continue
        # Session-Consumer überspringen (konkurrieren nicht mit sich selbst)
        if plan.get("kind") == "session":
            continue
        cs_raw = plan.get("chosen_start")
        ce_raw = plan.get("chosen_end")
        if not cs_raw or not ce_raw:
            continue
        try:
            cs = dt_util.as_utc(dt_util.parse_datetime(str(cs_raw)))
            ce = dt_util.as_utc(dt_util.parse_datetime(str(ce_raw)))
        except Exception:
            continue
        if not cs or not ce:
            continue
        # Wenn Consumer aktuell läuft oder in nächsten 30 min startet und PV-Modus
        if cs < lookahead and ce > now_utc:
            power_kw = float(plan.get("power_kw", 0.0) or 0.0)
            source = str(plan.get("source", "") or "")
            # Nur PV-basierte Consumer reservieren PV
            if source in ("pv", "pv_opportunist") and power_kw > 0:
                reserve_w += int(power_kw * 1000)
    return reserve_w
