"""Global preflight dispatcher.

Entscheidet pro Coordinator-Tick, welche Consumer-Starts approved werden.
Verteilt gemeinsame Ressourcen (PV-Surplus, Grid-Kapazität) nach Priority
und wahrt run_order-Constraints beim Start.

Kombiniert (a) PV-Share, (b) run_order, (c) Grid-Cap. Die tariff_only SOC-Prüfung
und die replan-Auslösung bei Reject bleiben im binary_sensor.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .const import (
    CONF_MAX_GRID_POWER_KW,
    CONF_PV_SURPLUS_ENTITY,
    CONSUMER_COUNT,
    DEFAULT_MAX_GRID_POWER_KW,
)
from .consumer_helpers import get_consumer_config
from .slot_helpers import state_to_kw

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

RUNNING_POWER_THRESHOLD_KW = 0.05  # measurement_entity > 50 W → Consumer läuft


def _running_power_kw(hass: "HomeAssistant", entity_id: str) -> float:
    """Live-Leistung des Consumers in kW — nur von echten Leistungssensoren (W/kW).

    `measurement_entity` ist bei C1/C10 der kWh-Zähler des WP-Kreises; ohne diesen Guard
    wurde der Zählerstand (z.B. 4123 kWh) als 4123 W gelesen → Consumer galt dauerhaft als
    „läuft" → nie Dispatcher-Kandidat (26.08.). Energie-/unbekannte Sensoren → 0 (= nicht laufend).
    """
    if not entity_id:
        return 0.0
    st = hass.states.get(entity_id)
    if st is None:
        return 0.0
    unit = str(st.attributes.get("unit_of_measurement") or "").strip().lower()
    dev_class = str(st.attributes.get("device_class") or "").lower()
    if unit not in ("w", "kw") and dev_class != "power":
        return 0.0
    return max(0.0, float(state_to_kw(st)))


def _plan_for(coordinator, slot: int) -> dict:
    data = coordinator.data or {}
    plans = data.get("consumer_plans") or {}
    p = plans.get(str(slot)) or plans.get(slot) or {}
    return p if isinstance(p, dict) else {}


def _plan_says_run_now(plan: dict) -> bool:
    """Plan `planned` und Fenster läuft. (Vorher `plan["should_run"]` — im Store immer
    False → Dispatcher hatte nie Kandidaten, Audit 7.3.)"""
    if plan.get("status") != "planned":
        return False
    cs = plan.get("chosen_start")
    ce = plan.get("chosen_end")
    if not cs or not ce:
        return False
    start = dt_util.parse_datetime(str(cs))
    end = dt_util.parse_datetime(str(ce))
    if not start or not end:
        return False
    now = dt_util.as_local(dt_util.utcnow())
    return start <= now < end


def evaluate(hass: "HomeAssistant", entry: "ConfigEntry", coordinator) -> dict[int, bool]:
    """Berechne Approvals für alle Consumer-Slots im aktuellen Tick.

    Cached anhand coordinator.last_update_success (HA DataUpdateCoordinator Property),
    damit alle binary_sensors im gleichen Tick dieselbe Entscheidung sehen.
    """
    # Cache-Key: 30-s-Zeitraster (vorher bool last_update_success → nie invalidiert, Audit 7.3)
    tick_id = int(time.time() // 30)
    cached = getattr(coordinator, "_preflight_cache", None)
    if cached and cached.get("tick") == tick_id:
        return cached["decisions"]

    opts = {**entry.data, **entry.options}
    pv_entity = str(opts.get(CONF_PV_SURPLUS_ENTITY, "") or "").strip()
    max_grid_kw = float(opts.get(CONF_MAX_GRID_POWER_KW, DEFAULT_MAX_GRID_POWER_KW) or DEFAULT_MAX_GRID_POWER_KW)
    pv_surplus_kw = state_to_kw(hass.states.get(pv_entity)) if pv_entity else 0.0

    candidates: list[tuple[int, object, dict]] = []
    running_pv_kw = 0.0
    running_grid_kw = 0.0
    running_run_order: int | None = None

    for slot in range(1, CONSUMER_COUNT + 1):
        cfg = get_consumer_config(entry, slot)
        if not cfg.enabled or cfg.trigger_entity:
            continue
        plan = _plan_for(coordinator, slot)
        if not _plan_says_run_now(plan):
            continue
        power = float(cfg.power_kw or 0.0)
        actual_kw = _running_power_kw(hass, cfg.measurement_entity)
        is_running = actual_kw > RUNNING_POWER_THRESHOLD_KW
        if is_running:
            if cfg.tariff_only:
                running_grid_kw += power
            elif cfg.pv_opportunist:
                running_pv_kw += min(power, actual_kw)
            else:
                running_pv_kw += power
            if cfg.run_order > 0 and running_run_order is None:
                running_run_order = int(cfg.run_order)
        else:
            candidates.append((slot, cfg, plan))

    candidates.sort(key=lambda t: (-int(t[1].priority or 0), int(t[1].run_order or 0), int(t[0])))

    decisions: dict[int, bool] = {}
    committed_pv_kw = running_pv_kw
    committed_grid_kw = running_grid_kw
    active_run_order = running_run_order

    for slot, cfg, plan in candidates:
        power = float(cfg.power_kw or 0.0)
        name = cfg.configured_name
        # Plan-Quellen (Cluster 2): nur der PV-Anteil braucht Live-PV; Akku-/Netz-Anteil zählt aufs Grid-Cap
        pv_kwh = float(plan.get("expected_pv_kwh") or 0.0)
        tot = pv_kwh + float(plan.get("expected_battery_kwh") or 0.0) + float(plan.get("expected_grid_kwh") or 0.0)
        pv_share = (pv_kwh / tot) if tot > 0 else (0.0 if cfg.tariff_only else 1.0)

        if cfg.run_order > 0 and active_run_order is not None and active_run_order != int(cfg.run_order):
            _LOGGER.info("Preflight %s: run_order %d blockiert (aktiv: %d)", name, cfg.run_order, active_run_order)
            decisions[slot] = False
            continue

        if cfg.tariff_only:
            if committed_grid_kw + power > max_grid_kw + 1e-6:
                _LOGGER.info("Preflight %s: Grid-Cap %.1f+%.1f > %.1f → reject", name, committed_grid_kw, power, max_grid_kw)
                decisions[slot] = False
                continue
            decisions[slot] = True
            committed_grid_kw += power
            if cfg.run_order > 0:
                active_run_order = int(cfg.run_order)
            continue

        if cfg.pv_opportunist:
            decisions[slot] = True
            committed_pv_kw += power
            if cfg.run_order > 0:
                active_run_order = int(cfg.run_order)
            continue

        available_pv = max(0.0, pv_surplus_kw - committed_pv_kw)
        needed_pv = power * pv_share
        if pv_share > 0.05 and available_pv + 0.5 < needed_pv:
            _LOGGER.info("Preflight %s: PV-Share %.2f < %.2f kW (Plan %.0f%% PV) → reject", name, available_pv, needed_pv, pv_share * 100)
            decisions[slot] = False
            continue

        pv_used = min(power, available_pv)
        if committed_grid_kw + (power - pv_used) > max_grid_kw + 1e-6:
            _LOGGER.info("Preflight %s: Grid-Cap überschritten (%.1f+%.1f > %.1f) → reject", name, committed_grid_kw, power - pv_used, max_grid_kw)
            decisions[slot] = False
            continue

        decisions[slot] = True
        committed_pv_kw += pv_used
        committed_grid_kw += power - pv_used
        _LOGGER.info("Preflight %s: approved (PV %.1f kW von %.1f, Plan %.0f%% PV)", name, pv_used, power, pv_share * 100)
        if cfg.run_order > 0:
            active_run_order = int(cfg.run_order)

    coordinator._preflight_cache = {"tick": tick_id, "decisions": decisions}
    return decisions


def check_slot(hass: "HomeAssistant", entry: "ConfigEntry", coordinator, slot: int) -> bool:
    """True wenn Slot in diesem Tick starten darf. Default True wenn nicht im Dispatcher-Scope."""
    decisions = evaluate(hass, entry, coordinator)
    return decisions.get(slot, True)
