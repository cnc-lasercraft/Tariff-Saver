"""Tariff Saver integration."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_SEASON_ENTITY,
    CONF_PUBLISH_TIME,
    DEFAULT_PUBLISH_TIME,
    DOMAIN,
    SLOT_MINUTES,
)
from .coordinator import TariffSaverCoordinator

PLATFORMS: list[str] = ["sensor", "binary_sensor"]
RETRY_INTERVAL = timedelta(minutes=30)


@websocket_api.websocket_command({vol.Required("type"): "tariff_saver/price_curve"})
@websocket_api.async_response
async def _ws_price_curve(hass: HomeAssistant, connection, msg: dict) -> None:
    """Return active price slots on demand (replaces former state attribute)."""
    from .sensor import _active_slots, _get_baseline_price, _all_in_from_slot

    coordinator = next(
        (
            v for k, v in (hass.data.get(DOMAIN, {}) or {}).items()
            if isinstance(k, str) and isinstance(v, TariffSaverCoordinator)
        ),
        None,
    )
    if coordinator is None:
        connection.send_result(msg["id"], {"interval_minutes": SLOT_MINUTES, "baseline_chf_per_kwh": None, "slots": []})
        return
    active = _active_slots(coordinator)
    baseline = _get_baseline_price(coordinator)
    connection.send_result(msg["id"], {
        "interval_minutes": SLOT_MINUTES,
        "baseline_chf_per_kwh": round(baseline, 6) if baseline else None,
        "slots": [
            {
                "start": slot.start.isoformat(),
                "price_all_in_chf_per_kwh": _all_in_from_slot(slot),
            }
            for slot in active
        ],
    })


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up Tariff Saver from yaml (unused)."""
    hass.data.setdefault(DOMAIN, {})
    if not hass.data[DOMAIN].get("_ws_registered"):
        websocket_api.async_register_command(hass, _ws_price_curve)
        hass.data[DOMAIN]["_ws_registered"] = True
    # Herold-Topics registrieren (optional, no-op wenn Herold fehlt)
    from . import herold as _herold
    hass.async_create_task(_herold.register_topics(hass))
    return True


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hh, mm = value.strip().split(":")
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception as _err:
        _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "_parse_hhmm", _err)
    return 18, 15


def _has_valid_prices(coordinator: TariffSaverCoordinator) -> bool:
    data = coordinator.data or {}
    active = data.get("active") if isinstance(data, dict) else None
    return isinstance(active, list) and len(active) > 0


def _next_local_midnight(now_local: datetime) -> datetime:
    tomorrow = (now_local + timedelta(days=1)).date()
    return dt_util.as_local(
        dt_util.as_utc(datetime.combine(tomorrow, time(0, 0), tzinfo=now_local.tzinfo))
    )



def _get_season(hass, entry) -> str:
    """Get current season from configured entity or derive from date."""
    from .const import CONF_SEASON_ENTITY
    season_entity = str(
        entry.options.get(CONF_SEASON_ENTITY, entry.data.get(CONF_SEASON_ENTITY, "")) or ""
    ).strip()
    if season_entity:
        state = hass.states.get(season_entity)
        if state and state.state not in ("unavailable", "unknown", ""):
            return state.state
    # Fallback: derive from month
    month = dt_util.now().month
    if month in (12, 1, 2):
        return "Winter"
    elif month in (3, 4, 5):
        return "Frühling"
    elif month in (6, 7, 8):
        return "Sommer"
    else:
        return "Herbst"


def _get_consumption_entity(entry) -> str:
    """Get configured consumption energy entity."""
    return str(
        entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY, entry.data.get(CONF_CONSUMPTION_ENERGY_ENTITY, "")) or ""
    ).strip()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tariff Saver from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    config = dict(entry.data)
    config.update(dict(entry.options))

    coordinator = TariffSaverCoordinator(hass, config=config)
    coordinator.entry = entry
    hass.data[DOMAIN][entry.entry_id] = coordinator

    publish_time = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
    hour, minute = _parse_hhmm(publish_time)

    retry_state_key = f"{entry.entry_id}_retry_until"
    hass.data[DOMAIN][retry_state_key] = None

    # Listen for EKZ Tariff validated data via HA bus event
    # --- PV-Forecast → Price-Slot Mapping ---

    async def _update_pv_forecast_in_slots() -> int:
        """Liest Solcast-Forecast-Entities und mapped pv_kw auf alle bekannten Price-Slots.

        Matching: 30-min Solcast-Slot wird auf 2 × 15-min-Price-Slots gesplittet
        (gleiche Leistung für beide 15-min-Hälften der 30-min-Periode).

        Returns: Anzahl der aktualisierten Slots.
        """
        from .const import CONF_PV_FORECAST_ENTITY, CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE
        if coordinator.store is None:
            return 0

        fc_entity = str(config.get(CONF_PV_FORECAST_ENTITY, "") or "").strip()
        if not fc_entity:
            return 0
        fc_attr = str(config.get(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE) or DEFAULT_PV_FORECAST_ATTRIBUTE).strip()

        # Sammle Forecast-Daten aus heute + morgen Entities (Auto-Companion-Detect)
        forecast_entities = [fc_entity]
        for today_term, tomorrow_term in [
            ("prognose_heute", "prognose_morgen"),
            ("forecast_today", "forecast_tomorrow"),
            ("_today", "_tomorrow"),
        ]:
            if today_term in fc_entity:
                forecast_entities.append(fc_entity.replace(today_term, tomorrow_term, 1))
                break

        # Mapping: 15-min-Slot-Start (UTC-ISO) → pv_kw
        pv_by_slot: dict[str, float] = {}
        for eid in forecast_entities:
            state = hass.states.get(eid)
            if state is None:
                continue
            raw = state.attributes.get(fc_attr)
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                period_start = item.get("period_start")
                if not period_start:
                    continue
                dt = dt_util.parse_datetime(str(period_start))
                if dt is None:
                    continue
                pv_kw = float(item.get("pv_estimate", 0.0) or 0.0)
                # 30-min-Forecast → 2× 15-min-Slot mit gleicher Power
                slot0 = dt_util.as_utc(dt).isoformat()
                slot1 = dt_util.as_utc(dt + timedelta(minutes=15)).isoformat()
                pv_by_slot[slot0] = pv_kw
                pv_by_slot[slot1] = pv_kw

        # Updates auf alle bekannten Price-Slots anwenden
        updated = 0
        for key, _slot_data in list(coordinator.store.price_slots.items()):
            pv_kw_new = pv_by_slot.get(key, 0.0)
            current = _slot_data.get("pv_kw", 0.0) if isinstance(_slot_data, dict) else 0.0
            try:
                current_val = float(current) if current is not None else 0.0
            except (TypeError, ValueError):
                current_val = 0.0
            if abs(current_val - pv_kw_new) > 0.001:
                slot_dt = dt_util.parse_datetime(key)
                if slot_dt:
                    coordinator.store.set_slot_pv_kw(slot_dt, pv_kw_new)
                    updated += 1

        if updated:
            await coordinator.store.async_save()
            # Refresh so scheduler reads fresh pv_kw values from coordinator.data["active"]
            try:
                await coordinator.async_refresh()
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_refresh", _err)

        _LOGGER.info("PV-Forecast update: %d slots aktualisiert (von %d total)", updated, len(coordinator.store.price_slots))
        return updated

    # --- Rolling Plan: shared plan-computation helper ---

    HYSTERESIS_FACTOR = 0.90  # neuer Plan nur übernehmen wenn Netzkosten ≤ 90 % des alten

    def _plan_grid_cost(plans: dict, slots: list, pv_available: list[float]) -> float:
        """Reale Kosten eines Plan-Satzes in CHF nach dem dreistufigen Modell PV → Akku → Netz.

        Gleiches Modell wie `_split_assignment` im Scheduler (S4/S5): PV-gedeckte Slots
        kosten 0, Akku-Energie den Wiederbeschaffungspreis (Budget aus
        `store.battery_budget`, chronologisch verbraucht), der Rest den Slot-Preis.
        tariff_only ignoriert nur PV; der Akku-Lader ist immer Netz und blockiert in
        seinen Slots den Akku. Alter und neuer Plan werden mit denselben aktuellen
        Preisen/PV/Budget bewertet. Ersetzt seit 2026-08-26 `energy × avg_price` (Audit 3.1)
        und seit Cluster-2-Umsetzung die reine PV/Netz-Sicht (Audit 2.1).
        """
        from .consumer_helpers import get_consumer_config
        from .const import SLOT_HOUR_FRACTION
        from .sensor import _all_in_from_slot
        from .slot_helpers import split_slot_energy
        n_slots_total = len(slots)
        idx_by_start = {dt_util.as_local(sl.start).isoformat(): i for i, sl in enumerate(slots)}
        prices = [float(_all_in_from_slot(sl) or 0.0) for sl in slots]
        bb = getattr(coordinator.store, "battery_budget", {}) or {}
        bb_slots = bb.get("slots") or {}
        battery_price = float(bb.get("battery_price") or 0.25)
        budget = [float((bb_slots.get(k) or {}).get("budget_kwh") or 0.0) for k in idx_by_start]

        # Lasten pro Slot: [(power_kw, ignore_pv, is_charger)]
        loads: list[list[tuple[float, bool, bool]]] = [[] for _ in range(n_slots_total)]
        for slot_key, plan in (plans or {}).items():
            if not isinstance(plan, dict) or plan.get("status") != "planned":
                continue
            cs, ce = plan.get("chosen_start"), plan.get("chosen_end")
            cs_dt = cs if isinstance(cs, datetime) else dt_util.parse_datetime(str(cs or ""))
            ce_dt = ce if isinstance(ce, datetime) else dt_util.parse_datetime(str(ce or ""))
            if not cs_dt or not ce_dt:
                continue
            power = float(plan.get("power_kw") or 0.0)
            if power <= 0:
                continue
            tariff_only = plan.get("tariff_only")
            is_charger = plan.get("is_battery_charger")
            if tariff_only is None or is_charger is None:
                try:
                    cfg = get_consumer_config(coordinator.entry, int(slot_key))
                    tariff_only = bool(cfg.tariff_only) if tariff_only is None else tariff_only
                    is_charger = bool(cfg.is_battery_charger) if is_charger is None else is_charger
                except Exception:
                    tariff_only, is_charger = bool(tariff_only), bool(is_charger)
            i = idx_by_start.get(dt_util.as_local(cs_dt).isoformat())
            if i is None:
                continue
            n = max(1, int(round((ce_dt - cs_dt).total_seconds() / 900)))
            for j in range(i, min(i + n, n_slots_total)):
                loads[j].append((power, bool(tariff_only), bool(is_charger)))

        total = 0.0
        battery_used = 0.0
        for i in range(n_slots_total):
            if not loads[i]:
                continue
            blocked = any(ch for _, _, ch in loads[i])
            pv_left_kw = pv_available[i]
            batt_avail = 0.0 if blocked else max(0.0, budget[i] - battery_used)
            # Reihenfolge wie im Scheduler: Lader (Netz), dann PV-fähige, dann tariff_only
            for power, ignore_pv, is_charger in sorted(loads[i], key=lambda t: (not t[2], t[1])):
                if is_charger:
                    total += power * SLOT_HOUR_FRACTION * prices[i]
                    continue
                pv_kwh, b_kwh, _g, cost = split_slot_energy(
                    power, pv_left_kw, batt_avail, prices[i], battery_price,
                    ignore_pv=ignore_pv, battery_blocked=blocked, hours=SLOT_HOUR_FRACTION,
                )
                pv_left_kw = max(0.0, pv_left_kw - pv_kwh / SLOT_HOUR_FRACTION)
                batt_avail -= b_kwh
                battery_used += b_kwh
                total += cost
        return total

    def _plans_comparable(old_plans: dict, new_plans: dict) -> tuple[bool, str]:
        """Hysterese nur zwischen gleichartigen Plan-Sätzen.

        Weicht die Menge der geplanten Consumer ab (neu dazu / weggefallen) oder
        ändert sich Status bzw. Ziel-SOC des Akku-Laders, ist der Vergleich Äpfel/Birnen —
        dann wird der neue Plan ohne Hysterese übernommen (Audit 3.1: blockiertes C9-Laden).
        """
        def planned(p):
            return {str(k) for k, v in (p or {}).items() if isinstance(v, dict) and v.get("status") == "planned"}
        po, pn = planned(old_plans), planned(new_plans)
        if po != pn:
            return False, f"Consumer-Menge geändert (+{sorted(pn - po)} −{sorted(po - pn)})"
        for k, v in (new_plans or {}).items():
            if not isinstance(v, dict) or not v.get("is_battery_charger"):
                continue
            o = (old_plans or {}).get(str(k)) or (old_plans or {}).get(k) or {}
            if v.get("status") != o.get("status") or v.get("battery_target_soc_pct") != o.get("battery_target_soc_pct"):
                return False, "Akku-Assessment geändert"
        return True, ""

    async def _replan_sessions(reason: str) -> None:
        """Berechnet Session-Consumer-Pläne (kind=session) und merged in store.consumer_plans.

        Ändert bestehende daily_fixed-Pläne nicht — schreibt nur Session-Slots.
        """
        if coordinator.store is None:
            return
        active_all = (coordinator.data or {}).get("active", []) if isinstance(coordinator.data, dict) else []
        if not active_all:
            return
        try:
            active_sessions = coordinator.store.get_active_sessions()
        except Exception:
            return
        if not active_sessions:
            return
        from .scheduler import build_session_plan, _compute_grid_load_per_slot
        now_utc = dt_util.utcnow()
        merged = dict(coordinator.store.consumer_plans or {})
        max_grid_power_kw = float(config.get("max_grid_power_kw", 25.0) or 25.0)
        grid_load_per_slot = _compute_grid_load_per_slot(merged)
        for slot_key, session in active_sessions.items():
            try:
                session_plan = build_session_plan(
                    session,
                    active_all,
                    now_utc,
                    grid_load_per_slot=grid_load_per_slot,
                    max_grid_power_w=max_grid_power_kw * 1000.0,
                )
            except Exception as e:
                _LOGGER.error("build_session_plan slot %s failed: %s", slot_key, e)
                continue
            session_plan["kind"] = "session"
            session_plan["slot"] = int(slot_key) if str(slot_key).isdigit() else slot_key
            merged[str(slot_key)] = session_plan
            # Bus-Event
            try:
                hass.bus.async_fire("tariff_saver_session_plan_updated", {
                    "slot": session_plan["slot"],
                    "reason": reason,
                    "windows": session_plan.get("chosen_windows", []),
                    "feasible": session_plan.get("feasible", False),
                    "total_grid_cost_chf": session_plan.get("total_cost_chf", 0.0),
                    "delivered_kwh": session_plan.get("delivered_kwh", 0.0),
                    "energy_needed_kwh": session_plan.get("energy_needed_kwh", 0.0),
                })
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "})", _err)
            if not session_plan.get("feasible", True):
                shortfall = float(session_plan.get("energy_needed_kwh", 0.0)) - float(session_plan.get("delivered_kwh", 0.0))
                try:
                    hass.bus.async_fire("tariff_saver_session_infeasible", {
                        "slot": session_plan["slot"],
                        "shortfall_kwh": round(max(0.0, shortfall), 3),
                        "deadline": session.get("deadline_utc"),
                    })
                except Exception as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "})", _err)
                try:
                    from . import herold as _herold
                    await _herold.senden(
                        hass,
                        topic="tariff_saver/wallbox/session_infeasible",
                        titel="Wallbox: Deadline nicht erreichbar",
                        message=f"Session Slot {session_plan['slot']}: {round(shortfall,1)} kWh fehlen bis Deadline. Best-Effort läuft weiter.",
                        severity="warnung",
                    )
                except Exception as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "senden", _err)
        coordinator.store.consumer_plans = merged
        coordinator.store.dirty = True
        await coordinator.store.async_save()
        try:
            coordinator.async_set_updated_data(coordinator.data)
        except Exception as _err:
            _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_save", _err)

    def _rolling_target_date() -> str | None:
        """Zieltag für die Re-Plan-Trigger: der geplante Tag, aber nie einer in der Vergangenheit.

        `plans_tariff_date` wird nur beim EKZ-Event bzw. bei der Invalidierung
        fortgeschrieben — einen Mitternachts-Rollover gibt es nicht. Bleibt EKZ aus,
        stünde der Wert weiter auf gestern, und die Ticks wuerden einen vergangenen
        Tag neu planen, dessen Slots noch im Store liegen. Der `no slots`-Zweig griffe
        nie, `error_no_data` würde nie gesetzt, und der PV-Notlauf schärfte genau an
        dem Tag nicht, für den er gebaut ist. Belegt am 01.09.2026: EKZ lieferte
        8/96 Slots für den 01.09., die Pläne standen den ganzen Morgen auf dem 31.08.
        """
        if coordinator.store is None:
            return None
        today = dt_util.now().date()
        stored = coordinator.store.plans_tariff_date
        stored_dt = dt_util.parse_date(stored) if stored else None
        if stored_dt is None or stored_dt < today:
            return today.isoformat()
        return stored_dt.isoformat()

    async def _invalidate_plans_no_data(target_dt, reason: str) -> None:
        """Markiere alle bestehenden Consumer-Pläne als 'error_no_data'.

        Aufgerufen wenn keine validen Slots für target_dt vorliegen (EKZ-Fail, Validator-Reject).
        Verhindert dass alte Pläne (z.B. vom Vortag) auf dem Sensor stehenbleiben und
        `should_run` fälschlich auf chosen_start in der Vergangenheit zeigt.
        """
        if coordinator.store is None:
            return
        old = dict(coordinator.store.consumer_plans or {})
        had_valid = any(
            (p or {}).get("status") not in (None, "error_no_data", "disabled", "not_configured")
            for p in old.values()
        )
        invalidated = {}
        for slot, plan in old.items():
            invalidated[slot] = {
                "status": "error_no_data",
                "should_run": False,
                "source": None,
                "chosen_start": None,
                "chosen_end": None,
                "reason": f"no_slots_for_{target_dt.isoformat()}",
            }
        coordinator.store.consumer_plans = invalidated
        coordinator.store.plans_tariff_date = target_dt.isoformat()
        coordinator.store.dirty = True
        await coordinator.store.async_save()
        try:
            updated_data = dict(coordinator.data) if coordinator.data else {}
            updated_data["consumer_plans"] = invalidated
            coordinator.async_set_updated_data(updated_data)
        except Exception as _err:
            _LOGGER.debug("silent %s in invalidate updated_data: %s", type(_err).__name__, _err)
        # Activity-Log UND Push nur beim ok→error-Übergang: der 15-Min-Tick läuft an
        # einem No-Data-Tag sonst dauerhaft hier durch und spült das Log (Cap 30) leer.
        if had_valid:
            coordinator.store.log_activity(
                "⚠️", f"Keine Tarif-Daten für {target_dt.strftime('%d.%m.%Y')} — Pläne invalidiert ({reason})"
            )
            try:
                from . import herold as _herold
                await _herold.senden(
                    hass,
                    topic="tariff_saver/plan_no_data",
                    titel="Keine Tarif-Daten",
                    message=(
                        f"EKZ hat keine validen Slots für {target_dt.strftime('%d.%m.%Y')} geliefert. "
                        f"Alle Consumer-Pläne wurden invalidiert."
                    ),
                    severity="warnung",
                )
            except Exception as _err:
                _LOGGER.debug("silent %s in herold plan_no_data: %s", type(_err).__name__, _err)

    async def _compute_and_persist_plans(target_date: str, reason: str, hysteresis: bool) -> None:
        """Rolling-Plan-Helper: rechnet Plan für target_date und persistiert bei Bedarf.

        Bei `hysteresis=True`: nur übernehmen wenn neuer Plan ≤ HYSTERESIS_FACTOR × alter Plan.
        """
        from .const import SLOT_MINUTES
        if coordinator.store is None or not hasattr(coordinator, "entry"):
            _LOGGER.warning("Re-Plan (%s): store or entry not ready — skip", reason)
            return
        active_all = (coordinator.data or {}).get("active", []) if isinstance(coordinator.data, dict) else []
        if not active_all:
            _LOGGER.warning("Re-Plan (%s): no active slots available — skip", reason)
            return
        target_dt = dt_util.parse_date(target_date) if target_date else None
        if target_dt is None:
            target_dt = (dt_util.now() + timedelta(days=1)).date()
            target_date = target_dt.isoformat()
        tomorrow_slots = [s for s in active_all if dt_util.as_local(s.start).date() == target_dt]
        if not tomorrow_slots:
            _LOGGER.info("Re-Plan (%s): no slots for %s — invalidate stale plans", reason, target_dt)
            await _invalidate_plans_no_data(target_dt, reason)
            return

        from .consumer_helpers import get_consumer_config
        from .scheduler import optimize_consumer_plans
        max_grid_kw = float(config.get("max_grid_power_kw", 25.0) or 25.0)
        try:
            plans = optimize_consumer_plans(
                hass=hass,
                entry=coordinator.entry,
                active_slots=tomorrow_slots,
                store=coordinator.store,
                max_grid_power_kw=max_grid_kw,
                target_date=target_dt,
            )
        except Exception as err:
            _LOGGER.error("Re-Plan (%s) failed: %s", reason, err)
            if coordinator.store:
                coordinator.store.log_activity("💥", f"Re-Plan fehlgeschlagen ({reason}): {err}")
            return

        # Reale Netzkosten neuer vs. bestehender Plan (beide mit aktuellen Preisen/PV) — für Log + Hysterese
        from .scheduler import _build_pv_available
        old_plans = coordinator.store.consumer_plans or {}
        base_profile = coordinator.store.get_seasonal_load_profile() or {}
        pv_available = _build_pv_available(tomorrow_slots, base_profile)
        new_cost = _plan_grid_cost(plans, tomorrow_slots, pv_available)
        old_cost = _plan_grid_cost(old_plans, tomorrow_slots, pv_available)
        if hysteresis:
            comparable, why = _plans_comparable(old_plans, plans)
            if not comparable:
                _LOGGER.info("Re-Plan (%s): Hysterese übersprungen — %s", reason, why)
            elif old_cost > 0 and new_cost > old_cost * HYSTERESIS_FACTOR:
                _LOGGER.info(
                    "Re-Plan (%s): keep old plan — new grid cost %.3f CHF vs. old %.3f CHF (Hysterese %.0f%%)",
                    reason, new_cost, old_cost, HYSTERESIS_FACTOR * 100,
                )
                return

        # Persist new plans
        serializable = {}
        for slot, plan in plans.items():
            sp = dict(plan)
            for k in ("start", "end", "chosen_start", "chosen_end", "tariff_window_start", "tariff_window_end"):
                v = sp.get(k)
                if hasattr(v, "isoformat"):
                    sp[k] = v.isoformat()
            # Defensive: chosen_start must belong to target_dt — sonst stale (Scheduler-Bug)
            cs = sp.get("chosen_start")
            if cs and sp.get("status") == "planned":
                try:
                    cs_dt = dt_util.parse_datetime(str(cs))
                    if cs_dt and dt_util.as_local(cs_dt).date() != target_dt:
                        _LOGGER.warning(
                            "Re-Plan (%s) slot %s: chosen_start %s ≠ target %s — invalidating",
                            reason, slot, cs, target_dt,
                        )
                        sp = {
                            "status": "error_no_data",
                            "should_run": False,
                            "source": None,
                            "chosen_start": None,
                            "chosen_end": None,
                            "reason": "stale_chosen_start",
                        }
                except Exception as _err:
                    _LOGGER.debug("silent %s in chosen_start check: %s", type(_err).__name__, _err)
            serializable[slot] = sp

        coordinator.store.consumer_plans = serializable
        coordinator.store.plans_tariff_date = target_date
        coordinator.store.dirty = True
        await coordinator.store.async_save()

        # Session-Re-Plan (Wallbox etc.) als separater Hook, merged in consumer_plans
        await _replan_sessions(reason=f"after_{reason}")

        tariff_date_fmt = target_date
        try:
            tariff_date_fmt = target_dt.strftime("%d.%m.%Y")
        except Exception as _err:
            _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "_replan_sessions", _err)
        if reason == "ekz":
            coordinator.store.log_activity("📅", f"Slots für {tariff_date_fmt} berechnet")
        else:
            savings = old_cost - new_cost if old_cost > 0 else 0.0
            coordinator.store.log_activity(
                "🔁",
                f"Re-Plan ({reason}): neue Kosten {new_cost:.2f} CHF"
                + (f" (−{savings:.2f} vs. alt)" if savings > 0 else "")
            )

        # Log planned consumers (for activity log, only in EKZ case to avoid spam)
        if reason == "ekz":
            for _slot, _plan in plans.items():
                _status = _plan.get("status", "")
                _source = _plan.get("source", "")
                _start = _plan.get("start") or _plan.get("chosen_start")
                _end = _plan.get("end") or _plan.get("chosen_end")
                try:
                    _cfg = get_consumer_config(coordinator.entry, int(_slot))
                    _name = _cfg.configured_name
                except Exception:
                    _name = f"Consumer {_slot}"
                if _status == "planned" and _start:
                    _time_str = ""
                    try:
                        _s = dt_util.parse_datetime(str(_start))
                        _e = dt_util.parse_datetime(str(_end)) if _end else None
                        if _s:
                            _time_str = dt_util.as_local(_s).strftime("%H:%M")
                            if _e:
                                _time_str += "-" + dt_util.as_local(_e).strftime("%H:%M")
                    except Exception as _err:
                        _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "_time_str += '-' + dt_util.as_local(_e).", _err)
                    _price = _plan.get("avg_price_chf_per_kwh") or _plan.get("tariff_avg_price_chf_per_kwh")
                    _detail = f"{_source}"
                    if _price and isinstance(_price, (int, float)):
                        _detail += f" {round(_price * 100, 1)} Rp"
                    coordinator.store.log_activity("\U0001f4cb", f"{_name} geplant ({_detail} {_time_str})")

        updated_data = dict(coordinator.data) if coordinator.data else {}
        updated_data["consumer_plans"] = plans
        coordinator.async_set_updated_data(updated_data)
        _LOGGER.info("Re-Plan (%s): persisted for %s, cost=%.3f CHF", reason, target_date, new_cost)

    async def _on_ekz_bus_event(event) -> None:
        """EKZ Tariff v2: Slots come directly in event payload."""
        _LOGGER.info("Received ekz_tariff_new_data event: date=%s, slots=%d",
                      event.data.get("date", "?"), len(event.data.get("slots", [])))
        target_date = event.data.get("date", "")
        raw_slots = event.data.get("slots", [])

        if not raw_slots:
            _LOGGER.warning("ekz_tariff_new_data: no slots in payload — ignoring")
            return

        # 1. Convert raw dicts to PriceSlot objects (15-min)
        from .coordinator import PriceSlot
        from .const import SLOT_MINUTES
        active_15m = []
        for raw in raw_slots:
            start = dt_util.parse_datetime(raw.get("start", ""))
            if start is None:
                continue
            components = {k: float(v) for k, v in raw.items() if k != "start" and isinstance(v, (int, float))}
            electricity = float(raw.get("electricity", 0.0))
            if electricity <= 0:
                electricity = float(components.get("electricity", 0.0))
            active_15m.append(PriceSlot(
                start=dt_util.as_utc(start),
                electricity_chf_per_kwh=electricity,
                components_chf_per_kwh=components,
            ))

        if not active_15m:
            _LOGGER.warning("ekz_tariff_new_data: no valid slots after conversion")
            return

        # Store in Tariff Saver's own price_slots (for sensors, stats, restart)
        if coordinator.store:
            for s in active_15m:
                coordinator.store.set_price_slot(s.start, dyn_components_chf_per_kwh=s.components_chf_per_kwh)
            coordinator.store.dirty = True
            await coordinator.store.async_save()

        # Refresh coordinator so that coordinator.data["active"] reflects new slots
        try:
            await coordinator.async_refresh()
        except Exception as _err:
            _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_refresh", _err)

        # PV-Forecast auf neu gespeicherte Slots mappen
        try:
            await _update_pv_forecast_in_slots()
        except Exception as err:
            _LOGGER.warning("PV-Forecast update nach EKZ-Event fehlgeschlagen: %s", err)

        _LOGGER.info("Plan calculation: EKZ event → %s", target_date)
        # EKZ-Event: immer persistieren (keine Hysterese — neue Preise sind autoritativ)
        await _compute_and_persist_plans(target_date=target_date, reason="ekz", hysteresis=False)

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_ekz_signal"] = hass.bus.async_listen(
        "ekz_tariff_new_data", _on_ekz_bus_event
    )

    # --- Rolling Plan Trigger ---

    async def _scheduled_replan_tick(now) -> None:
        """Alle 15 Min an Slot-Grenzen: Abort-Check laufender Consumer + Re-Plan mit Hysterese."""
        now_local = dt_util.as_local(now) if not isinstance(now, type(None)) else dt_util.now()
        # Nur an Slot-Grenzen (0/15/30/45). async_track_time_change mit */15 feuert exakt dort.
        if coordinator.store is None:
            return
        target_date = _rolling_target_date()
        if not target_date:
            return
        try:
            from .abort import evaluate_aborts
            aborted = evaluate_aborts(hass, coordinator.entry, coordinator)
        except Exception as err:
            _LOGGER.warning("Abort evaluation failed: %s", err)
            aborted = []
        if aborted:
            await coordinator.store.async_save()
            await _compute_and_persist_plans(
                target_date=target_date, reason=f"abort_{','.join(map(str, aborted))}", hysteresis=False,
            )
            await coordinator.async_refresh()
            return
        await _compute_and_persist_plans(target_date=target_date, reason="timer_15min", hysteresis=True)

    # Timer: alle 15 Min an Slot-Grenze (minute=0/15/30/45)
    hass.data[DOMAIN][f"{entry.entry_id}_unsub_replan_timer"] = async_track_time_change(
        hass, _scheduled_replan_tick, minute=[0, 15, 30, 45], second=5
    )

    _soc_last_seen: dict[str, float] = {"soc": -999.0}

    @callback
    def _on_soc_change(event) -> None:
        """Re-Plan bei SOC-Delta > 10 %."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unavailable", "unknown", ""):
            return
        try:
            new_soc = float(new_state.state)
        except (ValueError, TypeError):
            return
        prev = _soc_last_seen.get("soc", -999.0)
        if abs(new_soc - prev) < 10.0:
            return
        _soc_last_seen["soc"] = new_soc
        if coordinator.store is None:
            return
        target_date = _rolling_target_date()
        if not target_date:
            return
        hass.async_create_task(_compute_and_persist_plans(
            target_date=target_date, reason=f"soc_change_{new_soc:.0f}%", hysteresis=True
        ))

    async def _pv_forecast_then_replan() -> None:
        """PV-Forecast in Slots updaten, Abort-Check laufender Consumer, dann Re-Plan."""
        try:
            await _update_pv_forecast_in_slots()
        except Exception as err:
            _LOGGER.warning("PV-Forecast update fehlgeschlagen: %s", err)
        if coordinator.store is None:
            return
        target_date = _rolling_target_date()
        if not target_date:
            return
        try:
            from .abort import evaluate_aborts
            aborted = evaluate_aborts(hass, coordinator.entry, coordinator)
        except Exception as err:
            _LOGGER.warning("Abort evaluation failed: %s", err)
            aborted = []
        if aborted:
            await coordinator.store.async_save()
            await _compute_and_persist_plans(
                target_date=target_date, reason=f"abort_pv_{','.join(map(str, aborted))}", hysteresis=False,
            )
            await coordinator.async_refresh()
            return
        await _compute_and_persist_plans(
            target_date=target_date, reason="pv_forecast", hysteresis=True,
        )

    @callback
    def _on_pv_forecast_change(event) -> None:
        """Re-Plan wenn PV-Forecast-Entity ihren State ändert (neue Prognose)."""
        hass.async_create_task(_pv_forecast_then_replan())

    # Setup SOC + PV-Forecast Listener
    from .const import CONF_BATTERY_SOC_ENTITY, CONF_PV_FORECAST_ENTITY
    _soc_entity = str(config.get(CONF_BATTERY_SOC_ENTITY, "") or "").strip()
    if _soc_entity:
        hass.data[DOMAIN][f"{entry.entry_id}_unsub_soc"] = async_track_state_change_event(
            hass, [_soc_entity], _on_soc_change
        )
    _pv_fc_entity = str(config.get(CONF_PV_FORECAST_ENTITY, "") or "").strip()
    if _pv_fc_entity:
        hass.data[DOMAIN][f"{entry.entry_id}_unsub_pv_forecast"] = async_track_state_change_event(
            hass, [_pv_fc_entity], _on_pv_forecast_change
        )

    # Ferienmodus-Listener: Übergang aktiv↔inaktiv → Re-Plan OHNE Hysterese.
    # Ohne diesen Listener würde beim Ferien-Ende die 90%-Hysterese den billigen
    # Pause-Plan festpinnen (Consumer kämen erst am Folgetag zurück).
    # Entity-Änderung via Settings-Card braucht reload_config_entry (wie SOC/PV-Listener).
    from .const import CONF_VACATION_ENTITY, CONF_VACATION_STATE, DEFAULT_VACATION_STATE
    _vac_entity = str(config.get(CONF_VACATION_ENTITY, "") or "").strip()
    if _vac_entity:
        _vac_state = str(config.get(CONF_VACATION_STATE, DEFAULT_VACATION_STATE) or DEFAULT_VACATION_STATE).strip()

        @callback
        def _on_vacation_change(event) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if new_state is None or new_state.state in ("unavailable", "unknown", ""):
                return
            old_s = old_state.state if old_state else None
            if old_s in (None, "", "unavailable", "unknown"):
                return  # Boot-/Restore-Rauschen — kein echter Wechsel
            was_active = old_s == _vac_state
            now_active = new_state.state == _vac_state
            if was_active == now_active:
                return
            store = coordinator.store
            if store is not None:
                if now_active:
                    store.log_activity("🏖️", "Ferienmodus aktiv — markierte Consumer & Dump-Loads pausiert")
                else:
                    store.log_activity("🏠", "Ferienmodus beendet — Normalbetrieb")
            target_date = _rolling_target_date()
            if not target_date:
                return
            hass.async_create_task(_compute_and_persist_plans(
                target_date=target_date,
                reason="vacation_on" if now_active else "vacation_off",
                hysteresis=False,
            ))

        hass.data[DOMAIN][f"{entry.entry_id}_unsub_vacation"] = async_track_state_change_event(
            hass, [_vac_entity], _on_vacation_change
        )

    async def _manual_replan_service(call: ServiceCall) -> None:
        """tariff_saver.replan: manueller Re-Plan (ohne Hysterese — User will Update jetzt)."""
        if coordinator.store is None:
            return
        target_date = call.data.get("date") or _rolling_target_date()
        if not target_date:
            target_date = dt_util.now().date().isoformat()
        await _compute_and_persist_plans(target_date=str(target_date), reason="manual", hysteresis=False)

    if not hass.services.has_service(DOMAIN, "replan"):
        hass.services.async_register(DOMAIN, "replan", _manual_replan_service)

    async def _manual_update_pv_forecast_service(call: ServiceCall) -> None:
        """tariff_saver.update_pv_forecast: mapped Solcast-Forecast auf alle bekannten Price-Slots."""
        updated = await _update_pv_forecast_in_slots()
        _LOGGER.info("update_pv_forecast service: %d slots updated", updated)

    if not hass.services.has_service(DOMAIN, "update_pv_forecast"):
        hass.services.async_register(DOMAIN, "update_pv_forecast", _manual_update_pv_forecast_service)

    # Initial PV-Forecast für bekannte Slots setzen (ein mal nach Setup)
    async def _initial_pv_forecast_setup(_now=None) -> None:
        try:
            await _update_pv_forecast_in_slots()
        except Exception as err:
            _LOGGER.warning("Initial PV-Forecast setup failed: %s", err)
    hass.async_create_task(_initial_pv_forecast_setup())

    async def _reset_energy_baseline_service(call: ServiceCall) -> None:
        clear_booked = bool(call.data.get("clear_booked", True))
        target_entry_id = call.data.get("entry_id")

        for eid, coord in list(hass.data.get(DOMAIN, {}).items()):
            if not isinstance(eid, str) or not hasattr(coord, "store"):
                continue
            if target_entry_id and eid != target_entry_id:
                continue

            store = getattr(coord, "store", None)
            if store is None:
                continue

            target_entry = hass.config_entries.async_get_entry(eid)
            if target_entry is None:
                continue

            energy_entity = (
                target_entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY)
                or target_entry.data.get(CONF_CONSUMPTION_ENERGY_ENTITY)
            )
            if not isinstance(energy_entity, str) or not energy_entity:
                continue

            state = hass.states.get(energy_entity)
            if state is None:
                continue

            try:
                current_kwh = float(state.state)
            except Exception:
                continue

            now_utc = dt_util.utcnow()
            store.reset_energy_baseline(now_utc, current_kwh, clear_booked=clear_booked)
            await store.async_save()
            try:
                await coord.async_request_refresh()
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_request_refresh", _err)

    async def _mark_consumer_run_service(call: ServiceCall) -> None:
        target_entry_id = call.data.get("entry_id")
        slot = int(call.data.get("slot", 0) or 0)
        if slot <= 0:
            return

        for eid, coord in list(hass.data.get(DOMAIN, {}).items()):
            if not isinstance(eid, str) or not hasattr(coord, "store"):
                continue
            if target_entry_id and eid != target_entry_id:
                continue

            store = getattr(coord, "store", None)
            if store is None:
                continue

            if not hasattr(store, "set_consumer_last_run"):
                continue

            store.set_consumer_last_run(str(slot), dt_util.utcnow())

            energy_kwh = call.data.get("energy_kwh")
            source = call.data.get("source")
            actual_price = call.data.get("actual_price_chf_per_kwh")
            baseline_price = call.data.get("baseline_price_chf_per_kwh")
            if (
                energy_kwh is not None
                and source is not None
                and actual_price is not None
                and baseline_price is not None
                and hasattr(store, "record_consumer_efficiency")
            ):
                try:
                    store.record_consumer_efficiency(
                        str(slot),
                        energy_kwh=float(energy_kwh),
                        source=str(source),
                        actual_price_chf_per_kwh=float(actual_price),
                        baseline_price_chf_per_kwh=float(baseline_price),
                    )
                except Exception as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, ")", _err)

            await store.async_save()
            try:
                await coord.async_request_refresh()
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_request_refresh", _err)

    service_name = "reset_energy_baseline"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _reset_energy_baseline_service)

    service_name = "mark_consumer_run"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _mark_consumer_run_service)

    async def _update_consumer_service(call: ServiceCall) -> None:
        from .const import CONSUMER_COUNT as _CC
        slot = int(call.data.get("slot", 0))
        key = str(call.data.get("key", ""))
        value = call.data.get("value")
        if slot < 1 or slot > _CC or not key:
            return

        # Type conversion
        if key in ("enabled", "pv_required", "tariff_only", "pv_opportunist", "learning_enabled", "skip_next_run", "is_battery_charger", "pause_on_vacation"):
            value = bool(value)
        elif key in ("power_kw", "energy_kwh"):
            value = float(value or 0)
        elif key in ("duration_minutes", "priority", "max_grid_score", "min_days", "max_days", "min_runtime_minutes", "deadline_hours", "run_order"):
            value = int(value or 0)
        else:
            value = str(value or "")

        new_options = dict(entry.options)
        consumers = dict(new_options.get("consumers", {}))
        consumer = dict(consumers.get(str(slot), {}))
        consumer[key] = value
        consumers[str(slot)] = consumer
        new_options["consumers"] = consumers

        hass.config_entries.async_update_entry(entry, options=new_options)
        # Fire state update for config sensor immediately, defer full recalculation
        coordinator.async_set_updated_data(coordinator.data)

    async def _update_setting_service(call: ServiceCall) -> None:
        key = str(call.data.get("key", ""))
        value = call.data.get("value")
        if not key or key == "consumers":
            return
        # Type conversion
        if key in ("battery_enabled", "heating_enabled", "pv_dump_enabled", "pv_dump_pause_on_vacation", "ladeempfehlung_enabled", "standby_watch_enabled", "emergency_pv_run_enabled"):
            value = bool(value)
        elif key in ("feed_in_fixed_price", "battery_capacity_kwh", "battery_min_soc_percent", "battery_pv_full_charge_ratio", "battery_charge_cost_tolerance_pct", "ampel_pv_threshold_kw", "heating_wp_power_kw", "heating_comfort_min", "heating_pv_max", "heating_max_score", "heating_max_score_absolute", "heating_frost_min", "heating_pv_wait_minutes", "heating_wp_min_runtime", "strom_sparen_score", "min_valid_price", "max_valid_price", "battery_hold_enter_score", "battery_hold_exit_score", "flat_tariff_spread_rp", "flat_grid_earliest_hour", "light_auto_off_timeout_minutes", "pool_wp_min_runtime", "pv_dump_on_watts", "pv_dump_off_watts", "pv_dump_dwell_seconds", "pv_dump_min_on_minutes", "ladeempfehlung_max_score", "ladeempfehlung_window_hours", "ladeempfehlung_min_pv_kwh", "standby_watch_max_watts", "standby_watch_default_hours"):
            value = float(value or 0)
        else:
            value = str(value or "")
        new_options = dict(entry.options)
        new_options[key] = value
        hass.config_entries.async_update_entry(entry, options=new_options)
        coordinator.async_set_updated_data(coordinator.data)

    service_name = "update_setting"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _update_setting_service)

    service_name = "update_consumer"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _update_consumer_service)

    async def _update_consumer_status_service(call: ServiceCall) -> None:
        """Set reported_status on a consumer plan (called from automations)."""
        slot = int(call.data.get("slot", 0))
        status = str(call.data.get("status", "")).strip()
        if slot < 1 or slot > 10 or not status:
            return
        ts = dt_util.utcnow().isoformat()
        # Update store copy
        store = getattr(coordinator, "store", None)
        if store is not None and store.consumer_plans:
            plan = store.consumer_plans.get(str(slot)) or store.consumer_plans.get(slot)
            if plan is not None:
                plan["reported_status"] = status
                plan["reported_status_at"] = ts
                store.dirty = True
                await store.async_save()
        # Update coordinator.data copy (separate dict from store)
        data = coordinator.data
        if isinstance(data, dict):
            data_plans = data.get("consumer_plans", {})
            data_plan = data_plans.get(slot) or data_plans.get(str(slot))
            if isinstance(data_plan, dict):
                data_plan["reported_status"] = status
                data_plan["reported_status_at"] = ts
        # Trigger sensor update
        coordinator.async_set_updated_data(coordinator.data)

    hass.services.async_register(DOMAIN, "update_consumer_status", _update_consumer_status_service)

    # ── Session-Consumer Services (kind=session, e.g. Wallbox) ───────────────
    from .const import CONSUMER_COUNT
    async def _session_start_service(call: ServiceCall) -> None:
        slot = int(call.data.get("slot", 0) or 0)
        if slot < 1 or slot > CONSUMER_COUNT:
            _LOGGER.warning("session_start: invalid slot %s", slot)
            return
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        energy_kwh = float(call.data.get("energy_kwh", 0.0) or 0.0)
        deadline_raw = call.data.get("deadline")
        deadline_utc = None
        if isinstance(deadline_raw, str) and deadline_raw:
            try:
                d = dt_util.parse_datetime(deadline_raw)
                deadline_utc = dt_util.as_utc(d) if d else None
            except Exception:
                deadline_utc = None
        elif isinstance(deadline_raw, datetime):
            deadline_utc = dt_util.as_utc(deadline_raw)
        min_power_w = int(call.data.get("min_power_w", 1380) or 1380)
        max_power_w = int(call.data.get("max_power_w", 11000) or 11000)
        prefer_pv = bool(call.data.get("prefer_pv", True))
        store.session_start(
            str(slot),
            energy_kwh=energy_kwh,
            deadline_utc=deadline_utc,
            min_power_w=min_power_w,
            max_power_w=max_power_w,
            prefer_pv=prefer_pv,
        )
        store.log_activity("🔌", f"Session-Start Slot {slot}: {energy_kwh:.1f} kWh, Deadline {deadline_utc}")
        await store.async_save()
        # Session-Plan sofort berechnen (ohne auf 15-min-Tick zu warten)
        await _replan_sessions(reason="session_start")

    async def _session_update_service(call: ServiceCall) -> None:
        slot = int(call.data.get("slot", 0) or 0)
        if slot < 1 or slot > CONSUMER_COUNT:
            return
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        remaining_raw = call.data.get("remaining_kwh")
        remaining = float(remaining_raw) if isinstance(remaining_raw, (int, float, str)) and str(remaining_raw).strip() != "" else None
        result = store.session_update(str(slot), remaining_kwh=remaining)
        if result is None:
            _LOGGER.debug("session_update: no active session for slot %s", slot)
            return
        await store.async_save()
        # Re-Plan wenn remaining_kwh signifikant geändert — sonst nur Heartbeat
        if remaining is not None:
            await _replan_sessions(reason="session_update")
        else:
            try:
                coordinator.async_set_updated_data(coordinator.data)
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "_replan_sessions", _err)

    async def _session_end_service(call: ServiceCall) -> None:
        slot = int(call.data.get("slot", 0) or 0)
        if slot < 1 or slot > CONSUMER_COUNT:
            return
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        reason = str(call.data.get("reason", "ended") or "ended")
        ended = store.session_end(str(slot), reason=reason)
        if ended:
            store.log_activity("🔌", f"Session-Ende Slot {slot}: {reason}")
        # Plan-Eintrag entfernen damit Sensoren auf Default zurückgehen
        if coordinator.store.consumer_plans and str(slot) in coordinator.store.consumer_plans:
            plan = coordinator.store.consumer_plans[str(slot)]
            if isinstance(plan, dict) and plan.get("kind") == "session":
                coordinator.store.consumer_plans.pop(str(slot), None)
        await store.async_save()
        # Bus-Event
        try:
            hass.bus.async_fire("tariff_saver_session_complete", {
                "slot": slot,
                "reason": reason,
                "session": ended if isinstance(ended, dict) else None,
            })
        except Exception as _err:
            _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_save", _err)
        try:
            coordinator.async_set_updated_data(coordinator.data)
        except Exception as _err:
            _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_save", _err)

    if not hass.services.has_service(DOMAIN, "session_start"):
        hass.services.async_register(DOMAIN, "session_start", _session_start_service)
    if not hass.services.has_service(DOMAIN, "session_update"):
        hass.services.async_register(DOMAIN, "session_update", _session_update_service)
    if not hass.services.has_service(DOMAIN, "session_end"):
        hass.services.async_register(DOMAIN, "session_end", _session_end_service)

    # Strom-Sparen Snapshot Services (persistent in storage)
    # Entities werden über 2 HA-Labels konfiguriert:
    #   - strom_sparen_restore: abgeschaltet + beim Restore wieder eingeschaltet (State vor Strom-Sparen)
    #   - strom_sparen_off_only: abgeschaltet, bleibt aus beim Restore
    # Legacy: bestehendes Label "strom_sparen" zählt als strom_sparen_restore.
    STROM_SPAREN_RESTORE_LABELS = {"strom_sparen_restore", "strom_sparen"}
    STROM_SPAREN_OFF_ONLY_LABEL = "strom_sparen_off_only"

    def _get_labeled_entities(labels: set[str]) -> list[str]:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        return [
            entity.entity_id
            for entity in registry.entities.values()
            if labels & (entity.labels or set())
            and entity.disabled_by is None
        ]

    def _get_snapshot_entities() -> list[str]:
        """Entities die beim Restore wieder auf ihren vorherigen State gesetzt werden."""
        return _get_labeled_entities(STROM_SPAREN_RESTORE_LABELS)

    def _get_off_only_entities() -> list[str]:
        """Entities die beim Strom-Sparen abgeschaltet werden, aber nicht wieder restored."""
        return _get_labeled_entities({STROM_SPAREN_OFF_ONLY_LABEL})

    async def _save_strom_sparen_snapshot(call: ServiceCall) -> None:
        """Save current device states to persistent storage."""
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        snapshot = {}
        for entity_id in _get_snapshot_entities():
            state = hass.states.get(entity_id)
            if state and state.state not in ("unavailable", "unknown"):
                snapshot[entity_id] = state.state
                # Save climate/fan attributes for proper restore
                attrs = {}
                if entity_id.startswith("climate."):
                    for attr in ("temperature", "hvac_mode", "preset_mode"):
                        v = state.attributes.get(attr)
                        if v is not None:
                            attrs[attr] = v
                elif entity_id.startswith("fan."):
                    for attr in ("percentage", "preset_mode", "oscillating"):
                        v = state.attributes.get(attr)
                        if v is not None:
                            attrs[attr] = v
                if attrs:
                    snapshot[entity_id + "_attrs"] = attrs
        store.strom_sparen_snapshot = snapshot
        store.dirty = True
        await store.async_save()
        store.log_activity("📸", f"Strom-Sparen Snapshot gespeichert ({len([k for k in snapshot if not k.endswith('_attrs')])} Geräte)")
        _LOGGER.info("Strom-Sparen snapshot saved: %s", list(snapshot.keys()))

    hass.services.async_register(DOMAIN, "save_strom_sparen_snapshot", _save_strom_sparen_snapshot)

    async def _turn_off_entity(entity_id: str) -> None:
        domain = entity_id.split(".")[0]
        try:
            if domain == "climate":
                await hass.services.async_call("climate", "turn_off", {"entity_id": entity_id})
            elif domain == "fan":
                await hass.services.async_call("fan", "turn_off", {"entity_id": entity_id})
            elif domain == "switch":
                await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id})
            elif domain == "light":
                await hass.services.async_call("light", "turn_off", {"entity_id": entity_id})
        except Exception as err:
            _LOGGER.warning("Strom-Sparen: turn_off %s failed: %s", entity_id, err)

    async def _strom_sparen_apply(call: ServiceCall) -> None:
        """Snapshot restore-labeled + schaltet alle gelabelten (restore + off_only) aus.

        Ersetzt die hardcoded-YAML-Liste in der Automation.
        Bei `skip_snapshot=true` (HA-Startup-Fall) wird kein neuer Snapshot gemacht.
        """
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        skip_snapshot = bool(call.data.get("skip_snapshot", False))
        if not skip_snapshot:
            await _save_strom_sparen_snapshot(call)
        restore_entities = _get_snapshot_entities()
        off_only_entities = _get_off_only_entities()
        off_set = set(off_only_entities) - set(restore_entities)
        all_entities = list(restore_entities) + list(off_set)
        for entity_id in all_entities:
            await _turn_off_entity(entity_id)
        store.log_activity(
            "🔌",
            f"Strom-Sparen aus: {len(restore_entities)} Restore + {len(off_set)} Off-Only",
        )
        _LOGGER.info(
            "Strom-Sparen apply: %d restore, %d off-only (total %d off)",
            len(restore_entities), len(off_set), len(all_entities),
        )

    hass.services.async_register(DOMAIN, "strom_sparen_apply", _strom_sparen_apply)

    async def _restore_strom_sparen_snapshot(call: ServiceCall) -> None:
        """Restore device states from persistent storage."""
        store = getattr(coordinator, "store", None)
        if store is None or not store.strom_sparen_snapshot:
            _LOGGER.warning("No strom_sparen_snapshot to restore")
            return
        snapshot = store.strom_sparen_snapshot
        restored = 0
        for entity_id in _get_snapshot_entities():
            saved_state = snapshot.get(entity_id)
            if saved_state is None:
                continue
            attrs = snapshot.get(entity_id + "_attrs", {})
            try:
                domain = entity_id.split(".")[0]
                if domain == "climate":
                    if saved_state == "off":
                        await hass.services.async_call("climate", "turn_off", {"entity_id": entity_id})
                    else:
                        svc_data = {"entity_id": entity_id, "hvac_mode": saved_state}
                        if "temperature" in attrs:
                            svc_data["temperature"] = attrs["temperature"]
                        await hass.services.async_call("climate", "set_hvac_mode", svc_data)
                elif domain == "fan":
                    if saved_state == "off":
                        await hass.services.async_call("fan", "turn_off", {"entity_id": entity_id})
                    else:
                        await hass.services.async_call("fan", "turn_on", {"entity_id": entity_id})
                        if "percentage" in attrs:
                            await hass.services.async_call("fan", "set_percentage", {"entity_id": entity_id, "percentage": attrs["percentage"]})
                elif domain == "switch":
                    svc = "turn_on" if saved_state == "on" else "turn_off"
                    await hass.services.async_call("switch", svc, {"entity_id": entity_id})
                restored += 1
            except Exception as err:
                _LOGGER.error("Failed to restore %s: %s", entity_id, err)
        store.log_activity("♻️", f"Strom-Sparen Snapshot restored ({restored} Geräte)")
        _LOGGER.info("Strom-Sparen snapshot restored: %d devices", restored)

    hass.services.async_register(DOMAIN, "restore_strom_sparen_snapshot", _restore_strom_sparen_snapshot)

    async def _force_refresh() -> None:
        coordinator._last_fetch_date = None
        await coordinator.async_request_refresh()

    async def _daily_refresh(now) -> None:  # noqa: ANN001
        await _force_refresh()
        if not _has_valid_prices(coordinator):
            hass.data[DOMAIN][retry_state_key] = _next_local_midnight(dt_util.now())
        else:
            hass.data[DOMAIN][retry_state_key] = None

    async def _retry_tick(now) -> None:  # noqa: ANN001
        until = hass.data[DOMAIN].get(retry_state_key)
        if not isinstance(until, datetime):
            return
        now_local = dt_util.now()
        if now_local >= until:
            hass.data[DOMAIN][retry_state_key] = None
            return
        if not _has_valid_prices(coordinator):
            await _force_refresh()
        else:
            hass.data[DOMAIN][retry_state_key] = None

    hass.data[DOMAIN][entry.entry_id + "_unsub_daily"] = async_track_time_change(
        hass, _daily_refresh, hour=hour, minute=minute, second=0
    )
    hass.data[DOMAIN][entry.entry_id + "_unsub_retry"] = async_track_time_interval(
        hass, _retry_tick, RETRY_INTERVAL
    )

    async def _daily_base_load_snapshot(now) -> None:
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        consumption_entity = _get_consumption_entity(entry)
        if not consumption_entity:
            return
        consumption_state = hass.states.get(consumption_entity)
        if consumption_state is None:
            return
        try:
            total_kwh = float(consumption_state.state)
        except (ValueError, TypeError):
            return
        season = _get_season(hass, entry)
        today_str = dt_util.now().date().isoformat()

        # Record final reading and compile 30-min profile
        now_local = dt_util.now()
        slot_minute = 0 if now_local.minute < 30 else 30
        slot_start = now_local.replace(minute=slot_minute, second=0, microsecond=0)
        slot_iso = slot_start.isoformat()
        store.record_consumption_slot(slot_iso, total_kwh)

        # Final consumer measurement readings
        from .consumer_helpers import get_consumer_config
        from .const import CONSUMER_COUNT
        for slot in range(1, CONSUMER_COUNT + 1):
            cfg = get_consumer_config(entry, slot)
            if not cfg.enabled or not cfg.measurement_entity:
                continue
            meas_state = hass.states.get(cfg.measurement_entity)
            if meas_state is None:
                continue
            try:
                meas_kwh = float(meas_state.state)
            except (ValueError, TypeError):
                continue
            store.record_consumer_slot(cfg.measurement_entity, slot_iso, meas_kwh)

        profile = store.compile_load_profile(today_str, season)
        store.clear_consumption_slot_buffer()

        # Calculate daily totals from the compiled profile (not from the raw sensor)
        if profile:
            daily_total_kwh = float(profile.get("total_kwh", 0.0))
            consumer_kwh = float(profile.get("consumer_kwh", 0.0))
            base_load_kwh = float(profile.get("base_kwh", 0.0))
        else:
            consumer_kwh = store.compute_consumer_kwh_today()
            daily_total_kwh = 0.0
            base_load_kwh = 0.0

        store.add_base_load_daily(today_str, daily_total_kwh, consumer_kwh, base_load_kwh, season)
        await store.async_save()

    async def _consumption_slot_tick(now) -> None:
        """Sample consumption + consumer measurement sensors every SLOT_MINUTES."""
        from .const import SLOT_MINUTES
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        consumption_entity = _get_consumption_entity(entry)
        if not consumption_entity:
            return
        consumption_state = hass.states.get(consumption_entity)
        if consumption_state is None:
            return
        try:
            total_kwh = float(consumption_state.state)
        except (ValueError, TypeError):
            return
        now_local = dt_util.now()
        slot_minute = (now_local.minute // SLOT_MINUTES) * SLOT_MINUTES
        slot_start = now_local.replace(minute=slot_minute, second=0, microsecond=0)
        slot_iso = slot_start.isoformat()
        store.record_consumption_slot(slot_iso, total_kwh)

        # Sample all consumer measurement entities
        from .consumer_helpers import get_consumer_config
        from .const import CONSUMER_COUNT
        for slot in range(1, CONSUMER_COUNT + 1):
            cfg = get_consumer_config(entry, slot)
            if not cfg.enabled or not cfg.measurement_entity:
                continue
            meas_state = hass.states.get(cfg.measurement_entity)
            if meas_state is None:
                continue
            try:
                meas_kwh = float(meas_state.state)
            except (ValueError, TypeError):
                continue
            store.record_consumer_slot(cfg.measurement_entity, slot_iso, meas_kwh)

        await store.async_save()

    # Create WP Modus input_select if not exists
    if not hass.states.get("input_select.tariff_saver_wp_modus"):
        try:
            is_store = hass.helpers.storage.Store(1, "input_select")
            current = await is_store.async_load() or {}
            items = current.get("items") or []
            if not any(item.get("id") == "tariff_saver_wp_modus" for item in items):
                items.append({
                    "id": "tariff_saver_wp_modus",
                    "name": "Tariff Saver WP Modus",
                    "icon": "mdi:heat-pump",
                    "options": ["Auto", "Heizen", "Aus"],
                    "initial": "Auto",
                })
                current["items"] = items
                await is_store.async_save(current)
                _LOGGER.info("Created input_select.tariff_saver_wp_modus")
        except Exception as ex:
            _LOGGER.debug("Could not create WP Modus helper: %s", ex)

    hass.data[DOMAIN][entry.entry_id + "_unsub_base_load"] = async_track_time_change(
        hass, _daily_base_load_snapshot, hour=23, minute=55, second=0
    )

    # On-demand consumers (trigger_entity → plan single run)
    from .on_demand import async_setup_on_demand
    hass.data[DOMAIN][entry.entry_id + "_unsub_on_demand"] = async_setup_on_demand(
        hass, entry, coordinator
    )

    # Light Auto-Off: Push-basierter Wächter für vergessene Lichter
    from .auto_off import async_setup_auto_off
    hass.data[DOMAIN][entry.entry_id + "_unsub_auto_off"] = async_setup_auto_off(
        hass, entry, coordinator
    )

    async def _light_auto_off_action_service(call: ServiceCall) -> None:
        manager = getattr(coordinator, "light_auto_off", None)
        if manager is None:
            return
        entity_id = str(call.data.get("entity_id") or "").strip()
        action = str(call.data.get("action") or "").strip()
        await manager.handle_action(entity_id, action)

    hass.services.async_register(DOMAIN, "light_auto_off_action", _light_auto_off_action_service)

    # PV-Überschuss Dump-Loads: getaggte Geräte bei Netzeinspeisung einschalten
    from .pv_dump import async_setup_pv_dump
    hass.data[DOMAIN][entry.entry_id + "_unsub_pv_dump"] = async_setup_pv_dump(
        hass, entry, coordinator
    )

    # Standby-Wächter: getaggte Switches abschalten die nur noch dümpeln
    from .standby_watch import async_setup_standby_watch
    hass.data[DOMAIN][entry.entry_id + "_unsub_standby_watch"] = async_setup_standby_watch(
        hass, entry, coordinator
    )

    # Daily reset of skip_next_run flags at 00:00
    async def _midnight_reset_skip_next_run(now) -> None:
        new_options = dict(entry.options)
        consumers_opts = dict(new_options.get("consumers", {}))
        changed = False
        for slot_key, cfg in consumers_opts.items():
            if isinstance(cfg, dict) and cfg.get("skip_next_run"):
                cfg = dict(cfg)
                cfg["skip_next_run"] = False
                consumers_opts[slot_key] = cfg
                changed = True
        if changed:
            new_options["consumers"] = consumers_opts
            hass.config_entries.async_update_entry(entry, options=new_options)
            store = getattr(coordinator, "store", None)
            if store:
                store.log_activity("🔄", "OneShot-Flags zurückgesetzt (Tageswechsel)")
                await store.async_save()

    hass.data[DOMAIN][entry.entry_id + "_unsub_midnight_reset"] = async_track_time_change(
        hass, _midnight_reset_skip_next_run, hour=0, minute=0, second=10
    )
    hass.data[DOMAIN][entry.entry_id + "_unsub_consumption_tick"] = async_track_time_interval(
        hass, _consumption_slot_tick, timedelta(minutes=15)
    )

    # Session Heartbeat-Watchdog: alle 5 min prüfen ob Sessions stale sind (huawei_solar down)
    async def _session_heartbeat_tick(_now) -> None:
        store = getattr(coordinator, "store", None)
        if store is None or not hasattr(store, "session_stale_check"):
            return
        stale_slots = store.session_stale_check(max_stale_seconds=300)
        if not stale_slots:
            return
        for slot_key in stale_slots:
            store.session_end(str(slot_key), reason="heartbeat_stale")
            store.log_activity("⏰", f"Session Slot {slot_key} beendet (kein Heartbeat >5min)")
            if store.consumer_plans:
                plan = store.consumer_plans.get(str(slot_key))
                if isinstance(plan, dict) and plan.get("kind") == "session":
                    store.consumer_plans.pop(str(slot_key), None)
            try:
                hass.bus.async_fire("tariff_saver_session_complete", {
                    "slot": int(slot_key) if str(slot_key).isdigit() else slot_key,
                    "reason": "heartbeat_stale",
                })
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "})", _err)
            try:
                from . import herold as _herold
                await _herold.senden(
                    hass,
                    topic="tariff_saver/wallbox/session_heartbeat_stale",
                    titel="Wallbox: Session-Heartbeat ausgefallen",
                    message=f"Session Slot {slot_key} beendet — huawei_solar hat seit 5+ Min keinen Heartbeat gesendet.",
                    severity="warnung",
                )
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "senden", _err)
        await store.async_save()
        try:
            coordinator.async_set_updated_data(coordinator.data)
        except Exception as _err:
            _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "async_save", _err)

    hass.data[DOMAIN][entry.entry_id + "_unsub_session_heartbeat"] = async_track_time_interval(
        hass, _session_heartbeat_tick, timedelta(minutes=5)
    )

    # 15-min energy reading for long-term billing data
    async def _energy_reading_15m_tick(now) -> None:
        """Record cumulative energy meter reading every 15 minutes (long-term storage)."""
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        consumption_entity = _get_consumption_entity(entry)
        if not consumption_entity:
            return
        consumption_state = hass.states.get(consumption_entity)
        if consumption_state is None:
            return
        try:
            total_kwh = float(consumption_state.state)
        except (ValueError, TypeError):
            return
        now_local = dt_util.now()
        # Align to 15-min boundary
        minute_15 = (now_local.minute // 15) * 15
        slot_start = now_local.replace(minute=minute_15, second=0, microsecond=0)
        slot_utc = dt_util.as_utc(slot_start)
        store.record_energy_reading_15m(slot_utc.isoformat(), total_kwh)
        store.trim_energy_readings_15m(keep_days=400)
        await store.async_save()

    hass.data[DOMAIN][entry.entry_id + "_unsub_energy_15m"] = async_track_time_interval(
        hass, _energy_reading_15m_tick, timedelta(minutes=15)
    )

    async def _heating_tick(now) -> None:
        """Check heating state every 5 minutes."""
        try:
            from .heating import async_heating_tick
            await async_heating_tick(hass, entry, coordinator)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Heating tick failed")

    hass.data[DOMAIN][entry.entry_id + "_unsub_heating_tick"] = async_track_time_interval(
        hass, _heating_tick, timedelta(minutes=5)
    )

    async def _battery_tick(now) -> None:
        """Check battery mode every 5 minutes."""
        from .battery import async_battery_tick
        try:
            await async_battery_tick(hass, entry, coordinator)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Battery tick failed")

    hass.data[DOMAIN][entry.entry_id + "_unsub_battery_tick"] = async_track_time_interval(
        hass, _battery_tick, timedelta(minutes=5)
    )

    # Midnight refresh removed — plans are persisted and only recomputed
    # when new tariff data is published (18:15).

    async def handle_clear_activity_log(call) -> None:
        store = getattr(coordinator, "store", None)
        if store:
            store.activity_log = []
            store.dirty = True
            await store.async_save()
            # Force sensor update by firing event
            hass.bus.async_fire(f"{DOMAIN}_activity_log_updated")

    async def handle_delete_activity_log_entry(call) -> None:
        store = getattr(coordinator, "store", None)
        if store:
            index = int(call.data.get("index", -1))
            if 0 <= index < len(store.activity_log):
                store.activity_log.pop(index)
                store.dirty = True
                await store.async_save()
                # Force sensor update by firing event
                hass.bus.async_fire(f"{DOMAIN}_activity_log_updated")

    async def handle_add_activity_log_entry(call) -> None:
        store = getattr(coordinator, "store", None)
        if store:
            icon = str(call.data.get("icon", "ℹ️"))
            msg = str(call.data.get("msg", "Test"))
            store.log_activity(icon, msg)
            await store.async_save()
            hass.bus.async_fire(f"{DOMAIN}_activity_log_updated")

    hass.services.async_register(DOMAIN, "add_activity_log_entry", handle_add_activity_log_entry)
    hass.services.async_register(DOMAIN, "clear_activity_log", handle_clear_activity_log)
    hass.services.async_register(DOMAIN, "delete_activity_log_entry", handle_delete_activity_log_entry)

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Startup check: process unprocessed EKZ tomorrow data (restart resilience)
    async def _check_ekz_unprocessed() -> None:
        """Check if EKZ has validated tomorrow data we haven't planned yet."""
        if coordinator.store is None:
            return
        from homeassistant.helpers.storage import Store as _Store
        ekz_entries = hass.config_entries.async_entries("ekz_tariff")
        for ekz_entry in ekz_entries:
            try:
                ekz_store = _Store(hass, 2, f"ekz_tariff.{ekz_entry.entry_id}")
                ekz_data = await ekz_store.async_load()
            except Exception:
                continue
            if not isinstance(ekz_data, dict):
                continue
            ekz_tomorrow = ekz_data.get("tomorrow_date")
            ekz_slots = ekz_data.get("slots")
            if not ekz_tomorrow or not ekz_slots:
                continue
            # Already processed?
            if coordinator.store.plans_tariff_date == ekz_tomorrow:
                _LOGGER.debug("EKZ data for %s already processed", ekz_tomorrow)
                continue
            # Still relevant? (not in the past)
            parsed = dt_util.parse_date(ekz_tomorrow)
            if parsed and parsed >= dt_util.now().date():
                _LOGGER.info("Found unprocessed EKZ data for %s on startup — firing event", ekz_tomorrow)
                hass.bus.async_fire("ekz_tariff_new_data", {
                    "date": ekz_tomorrow,
                    "slots": ekz_slots,
                    "entry_id": ekz_entry.entry_id,
                })

    hass.async_create_task(_check_ekz_unprocessed())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    for suffix in ("_unsub_daily", "_unsub_retry", "_unsub_energy_cost", "_unsub_energy_tick", "_unsub_consumer_tick", "_unsub_base_load", "_unsub_consumption_tick", "_unsub_energy_15m", "_unsub_ekz_signal", "_unsub_battery_tick", "_unsub_heating_tick", "_unsub_replan_timer", "_unsub_soc", "_unsub_pv_forecast", "_unsub_vacation", "_unsub_on_demand", "_unsub_auto_off", "_unsub_pv_dump", "_unsub_standby_watch", "_unsub_midnight_reset", "_unsub_session_heartbeat"):
        unsub = hass.data.get(DOMAIN, {}).pop(entry.entry_id + suffix, None)
        if unsub:
            unsub()

    hass.data.get(DOMAIN, {}).pop(f"{entry.entry_id}_retry_until", None)
    if unload_ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        remaining_entries = [
            k
            for k, v in hass.data[DOMAIN].items()
            if isinstance(k, str) and isinstance(v, TariffSaverCoordinator)
        ]
        if not remaining_entries and hass.services.has_service(DOMAIN, "reset_energy_baseline"):
            hass.services.async_remove(DOMAIN, "reset_energy_baseline")
        if not remaining_entries and hass.services.has_service(DOMAIN, "mark_consumer_run"):
            hass.services.async_remove(DOMAIN, "mark_consumer_run")
        if not remaining_entries and hass.services.has_service(DOMAIN, "update_consumer_status"):
            hass.services.async_remove(DOMAIN, "update_consumer_status")
        if not remaining_entries and hass.services.has_service(DOMAIN, "save_strom_sparen_snapshot"):
            hass.services.async_remove(DOMAIN, "save_strom_sparen_snapshot")
        if not remaining_entries and hass.services.has_service(DOMAIN, "restore_strom_sparen_snapshot"):
            hass.services.async_remove(DOMAIN, "restore_strom_sparen_snapshot")
        if not remaining_entries and hass.services.has_service(DOMAIN, "strom_sparen_apply"):
            hass.services.async_remove(DOMAIN, "strom_sparen_apply")
        if not remaining_entries and hass.services.has_service(DOMAIN, "replan"):
            hass.services.async_remove(DOMAIN, "replan")
        for sname in ("session_start", "session_update", "session_end"):
            if not remaining_entries and hass.services.has_service(DOMAIN, sname):
                hass.services.async_remove(DOMAIN, sname)
        if not remaining_entries and hass.services.has_service(DOMAIN, "light_auto_off_action"):
            hass.services.async_remove(DOMAIN, "light_auto_off_action")
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
