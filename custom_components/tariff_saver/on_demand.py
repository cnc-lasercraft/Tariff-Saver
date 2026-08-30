"""On-demand consumers: plan a run when a trigger entity turns on.

Covers appliances (Miele dishwasher, washer, dryer) that are user-initiated and
must run within a deadline. When `trigger_entity` goes off → on, we search for
the cheapest N-minute window in [now, now+deadline_hours], respecting:
  - PV forecast per slot (slot.pv_kw)
  - Base load per slot (seasonal profile)
  - Already-planned consumers (daily + other on_demand) blocking PV

Priority rules:
  - New trigger with priority > existing on_demand plans: re-plan all
  - Same / lower priority: existing plans stay fixed, new finds best remaining window

On on → off before window start, the plan is cancelled.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import CONF_PV_SURPLUS_ENTITY, SLOT_MINUTES, SLOT_HOUR_FRACTION, SLOTS_PER_HOUR
from .slot_helpers import energy_source_label, split_slot_energy, state_to_kw
from .vacation import is_vacation_active

_LOGGER = logging.getLogger(__name__)

DEFAULT_DEADLINE_HOURS = 24

# Opportunistic-now: Start on_demand-Consumer früher wenn echter PV-Surplus reicht
OPPORTUNISTIC_BUFFER_KW = 1.0          # Headroom über consumer.power_kw (Wolken-Reserve)
OPPORTUNISTIC_STABLE_SECONDS = 180     # Surplus muss N Sekunden stabil über Schwelle sein
OPPORTUNISTIC_TICK_SECONDS = 30        # Tick-Frequenz
# Notbremse: so oft darf ein Plan innerhalb EINES Trigger-Ereignisses vorgezogen
# werden. Normal ist genau einmal — danach laeuft das Fenster und der Tick haelt
# ohnehin still. Zaehlt es hoeher, nimmt das Geraet den Startbefehl nicht an.
MAX_OPPORTUNISTIC_ADVANCES = 3


def _episode_consumed(store, slot: str, plan: dict[str, Any]) -> bool:
    """True, wenn seit Beginn des laufenden Trigger-Ereignisses ein Lauf verbucht wurde.

    Miele laesst den Smart-Status nach einem fertigen Programm auf `on` stehen,
    solange die Maschine offen ist. Ohne diese Pruefung liest on_demand das
    dauerhaft als „Auftrag offen" und plant immer weiter — die Start-Automation
    laeuft dann gegen ein fertiges Geraet und scheitert (30.08.: 56 Fehlerzeilen
    am Geschirrspueler). Anker ist `trigger_on_utc` (gesetzt beim echten off→on);
    ein danach verbuchter Lauf verbraucht das Ereignis. Erst das naechste off→on
    macht wieder auf — das entspricht einem neuen Beladen.
    """
    anchor = plan.get("trigger_on_utc")
    if not anchor:
        return False
    anchor_dt = dt_util.parse_datetime(str(anchor))
    if anchor_dt is None:
        return False
    learning = store.get_consumer_learning(str(slot)) or {}
    last_run = learning.get("last_run_end_utc")
    if not last_run:
        return False
    run_dt = dt_util.parse_datetime(str(last_run))
    return run_dt is not None and run_dt >= anchor_dt


def _get_consumers(entry: ConfigEntry) -> dict[str, Any]:
    return dict((entry.options or {}).get("consumers") or (entry.data or {}).get("consumers") or {})


def _on_demand_slots(entry: ConfigEntry) -> list[tuple[str, dict[str, Any]]]:
    result = []
    for slot, cfg in _get_consumers(entry).items():
        if not isinstance(cfg, dict):
            continue
        trig = str(cfg.get("trigger_entity") or "").strip()
        if trig:
            result.append((str(slot), cfg))
    return result


def _slot_price(slot_dict: dict[str, Any]) -> float | None:
    a_total = slot_dict.get("a_total")
    if isinstance(a_total, (int, float)) and a_total > 0:
        return float(a_total)
    a_comp = slot_dict.get("a_comp") or {}
    for key in ("integrated", "electricity"):
        v = a_comp.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _slot_pv_kw(slot_dict: dict[str, Any]) -> float:
    v = slot_dict.get("pv_kw")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _get_season(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Use same logic as __init__._get_season."""
    from .__init__ import _get_season as gs
    return gs(hass, entry)


def _build_commitments(
    plans: dict,
    now: datetime,
    deadline: datetime,
    exclude_slots: set[str],
    base_profile: dict[int, float],
) -> dict[datetime, float]:
    """For each 15-min slot in [now, deadline], return already-committed power in kW.

    Includes:
      - Base load (from seasonal profile, kWh → kW via /SLOT_HOUR_FRACTION)
      - Daily + on_demand consumer plans that overlap this slot (excluding `exclude_slots`)
    """
    committed: dict[datetime, float] = {}

    # Base load
    slot_dt = now.replace(second=0, microsecond=0)
    slot_dt = slot_dt - timedelta(minutes=slot_dt.minute % SLOT_MINUTES)
    while slot_dt < deadline:
        slot_idx = slot_dt.hour * SLOTS_PER_HOUR + slot_dt.minute // SLOT_MINUTES
        base_kwh = base_profile.get(slot_idx, 0.0)
        committed[slot_dt] = base_kwh / SLOT_HOUR_FRACTION  # kWh per 15min → avg kW
        slot_dt += timedelta(minutes=SLOT_MINUTES)

    # Consumer plans
    for slot_key, plan in (plans or {}).items():
        if str(slot_key) in exclude_slots:
            continue
        if not isinstance(plan, dict):
            continue
        cs = plan.get("chosen_start")
        ce = plan.get("chosen_end")
        power = plan.get("power_kw")
        if not (cs and ce and isinstance(power, (int, float))):
            continue
        start = dt_util.parse_datetime(cs)
        end = dt_util.parse_datetime(ce)
        if start is None or end is None:
            continue
        start = dt_util.as_local(start)
        end = dt_util.as_local(end)
        for k in list(committed.keys()):
            k_end = k + timedelta(minutes=SLOT_MINUTES)
            # slot overlaps with plan window
            if start < k_end and end > k:
                committed[k] = committed.get(k, 0.0) + float(power)
    return committed


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    s = (s or "").strip()
    if not s or ":" not in s:
        return None
    try:
        hh, mm = s.split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return None


def _is_in_allowed_window(dt_local: datetime, allowed_from: str, allowed_until: str) -> bool:
    ft = _parse_hhmm(allowed_from)
    ut = _parse_hhmm(allowed_until)
    if ft is None and ut is None:
        return True
    mins = dt_local.hour * 60 + dt_local.minute
    fm = ft[0] * 60 + ft[1] if ft else 0
    um = ut[0] * 60 + ut[1] if ut else 24 * 60
    if fm <= um:
        return fm <= mins < um
    return mins >= fm or mins < um


def _candidate_slots(
    price_slots: dict[str, dict[str, Any]],
    now: datetime,
    deadline: datetime,
    allowed_from: str = "",
    allowed_until: str = "",
) -> list[tuple[datetime, float, float]]:
    """Return list of (slot_start_local, price, pv_kw) for slots in [now-15min, deadline]."""
    out = []
    for key, val in price_slots.items():
        if not isinstance(val, dict):
            continue
        dt = dt_util.parse_datetime(key)
        if dt is None:
            continue
        dt_local = dt_util.as_local(dt)
        if dt_local < now - timedelta(minutes=SLOT_MINUTES):
            continue
        if dt_local >= deadline:
            continue
        if not _is_in_allowed_window(dt_local, allowed_from, allowed_until):
            continue
        price = _slot_price(val)
        if price is None:
            continue
        out.append((dt_local, price, _slot_pv_kw(val)))
    out.sort(key=lambda x: x[0])
    return out


def _battery_budget_map(store) -> tuple[dict[datetime, float], set[datetime], float]:
    """Akku-Budget des letzten Scheduler-Laufs: {slot_start_local: budget_kwh}, Lader-Slots, Akkupreis."""
    bb = getattr(store, "battery_budget", {}) or {}
    budget: dict[datetime, float] = {}
    charger: set[datetime] = set()
    for k, v in (bb.get("slots") or {}).items():
        dt = dt_util.parse_datetime(k)
        if dt is None or not isinstance(v, dict):
            continue
        dt = dt_util.as_local(dt)
        budget[dt] = float(v.get("budget_kwh") or 0.0)
        if v.get("charger"):
            charger.add(dt)
    return budget, charger, float(bb.get("battery_price") or 0.25)


def _build_battery_commitments(plans: dict, exclude_slots: set[str]) -> dict[datetime, float]:
    """Bereits verplante Akku-Energie (kWh) pro Slot aus `expected_battery_kwh` anderer Pläne."""
    out: dict[datetime, float] = {}
    for slot_key, plan in (plans or {}).items():
        if str(slot_key) in exclude_slots or not isinstance(plan, dict):
            continue
        b_kwh = plan.get("expected_battery_kwh")
        n = plan.get("slot_count")
        cs = plan.get("chosen_start")
        if not (isinstance(b_kwh, (int, float)) and b_kwh > 0 and n and cs):
            continue
        start = dt_util.parse_datetime(cs)
        if start is None:
            continue
        start = dt_util.as_local(start)
        per_slot = float(b_kwh) / int(n)
        for j in range(int(n)):
            k = start + timedelta(minutes=SLOT_MINUTES * j)
            out[k] = out.get(k, 0.0) + per_slot
    return out


def _plan_best_window(
    slots: list[tuple[datetime, float, float]],
    commitments: dict[datetime, float],
    power_kw: float,
    duration_minutes: int,
    now: datetime,
    battery: tuple[dict[datetime, float], set[datetime], float] | None = None,
    battery_commitments: dict[datetime, float] | None = None,
) -> tuple[datetime, datetime, float, float, dict[str, float]] | None:
    """Find the window with minimal cost (dreistufig PV → Akku → Netz, Audit 2.3).

    `battery` = (budget_map, charger_slots, battery_price) aus dem Store; ohne Budget
    zählt alles jenseits PV als Netz (bisheriges Verhalten). Das Budget wird
    chronologisch verbraucht: bereits verplante Akku-Energie anderer Pläne vor dem
    Slot (`battery_commitments`) und der eigene Verbrauch im Fenster reduzieren es.

    Returns (start, end, cost_chf, avg_price_chf_per_kwh, split) or None if no valid window.
    Ties: earliest start wins (slots are sorted ascending).
    """
    if duration_minutes <= 0 or power_kw <= 0 or not slots:
        return None
    n_slots = max(1, duration_minutes // SLOT_MINUTES)
    if len(slots) < n_slots:
        return None

    budget_map, charger_slots, battery_price = battery or ({}, set(), 0.25)
    b_commit = battery_commitments or {}
    # Kumulierte Fremd-Akkunutzung bis einschliesslich Slot k (chronologisch)
    cum_commit: dict[datetime, float] = {}
    running = 0.0
    for k in sorted(set(budget_map) | set(b_commit)):
        running += b_commit.get(k, 0.0)
        cum_commit[k] = running

    best: tuple[datetime, datetime, float, float, dict[str, float]] | None = None
    for i in range(len(slots) - n_slots + 1):
        window = slots[i:i + n_slots]
        contiguous = all(
            (window[j + 1][0] - window[j][0]).total_seconds() == SLOT_MINUTES * 60
            for j in range(len(window) - 1)
        )
        if not contiguous:
            continue
        start = max(window[0][0], now)
        end = window[-1][0] + timedelta(minutes=SLOT_MINUTES)

        cost = 0.0
        price_sum = 0.0
        own_batt = 0.0
        pv_sum = batt_sum = grid_sum = 0.0
        for slot_start, price, pv_kw in window:
            price_sum += price
            committed = commitments.get(slot_start, 0.0)
            pv_remainder = max(0.0, pv_kw - committed)
            blocked = slot_start in charger_slots
            batt_avail = max(0.0, budget_map.get(slot_start, 0.0) - cum_commit.get(slot_start, 0.0) - own_batt)
            pv_kwh, b_kwh, g_kwh, c = split_slot_energy(
                power_kw, pv_remainder, batt_avail, price, battery_price,
                battery_blocked=blocked, hours=SLOT_HOUR_FRACTION,
            )
            own_batt += b_kwh
            pv_sum += pv_kwh
            batt_sum += b_kwh
            grid_sum += g_kwh
            cost += c
        avg_price = price_sum / n_slots

        if best is None or cost < best[2]:
            best = (start, end, cost, avg_price, {
                "pv_kwh": pv_sum, "battery_kwh": batt_sum, "grid_kwh": grid_sum, "cost_chf": cost,
            })
    return best


def _active_on_demand_plans(store, entry: ConfigEntry) -> dict[str, tuple[dict[str, Any], int]]:
    """Return {slot: (plan, priority)} for all on_demand plans whose window hasn't started."""
    out: dict[str, tuple[dict[str, Any], int]] = {}
    plans = store.consumer_plans or {}
    consumers = _get_consumers(entry)
    now = dt_util.now()
    for slot_key, plan in plans.items():
        if not isinstance(plan, dict) or plan.get("source") != "on_demand":
            continue
        cs = plan.get("chosen_start")
        start = dt_util.parse_datetime(cs) if cs else None
        if start is None or dt_util.as_local(start) <= now:
            continue  # already running — don't bump
        cfg = consumers.get(str(slot_key)) or consumers.get(slot_key) or {}
        prio = int(cfg.get("priority") or plan.get("priority") or 5)
        out[str(slot_key)] = (plan, prio)
    return out


async def _plan_run(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
    slot: str,
    cfg: dict[str, Any],
) -> None:
    """Plan this consumer, potentially re-planning lower-priority on_demand plans."""
    store = getattr(coordinator, "store", None)
    if store is None:
        return

    name = str(cfg.get("name") or f"Consumer {slot}")

    # Ferienmodus: Trigger ignorieren (Overlay, kein Flag-Verbrauch)
    if cfg.get("pause_on_vacation") and is_vacation_active(hass, entry):
        store.log_activity("🏖️", f"{name}: Ferienmodus — Trigger ignoriert")
        await store.async_save()
        return

    # OneShot skip: consume flag, log, don't plan
    if cfg.get("skip_next_run"):
        new_options = dict(entry.options)
        consumers_opts = dict(new_options.get("consumers", {}))
        cc = dict(consumers_opts.get(str(slot), {}))
        cc["skip_next_run"] = False
        consumers_opts[str(slot)] = cc
        new_options["consumers"] = consumers_opts
        hass.config_entries.async_update_entry(entry, options=new_options)
        store.log_activity("⏭️", f"{name}: OneShot — Trigger ausgelassen, Flag zurückgesetzt")
        await store.async_save()
        return

    duration = int(cfg.get("duration_minutes") or 60)
    deadline_hours = int(cfg.get("deadline_hours") or DEFAULT_DEADLINE_HOURS)
    power_kw = float(cfg.get("power_kw") or 0.0)
    priority = int(cfg.get("priority") or 5)

    now = dt_util.now()
    deadline = now + timedelta(hours=deadline_hours)
    season = _get_season(hass, entry)
    base_profile = store.get_seasonal_load_profile(season)

    # Determine which existing on_demand plans to bump: strictly lower priority
    active = _active_on_demand_plans(store, entry)
    to_bump = {s for s, (_, p) in active.items() if p < priority}
    # Always re-plan the triggering slot itself
    to_bump.add(str(slot))

    # Re-plan order: start with the new one, then bumped by priority desc, then trigger-time asc
    consumers = _get_consumers(entry)
    order = [str(slot)]
    for s in to_bump:
        if s == str(slot):
            continue
        order.append(s)
    # Sort bumped by priority desc, then earliest chosen_start (approx fairness)
    def sort_key(s):
        if s == str(slot):
            return (0, 0, 0)  # always first
        c = consumers.get(s) or {}
        p = int(c.get("priority") or 5)
        plan = active.get(s, ({}, 0))[0]
        cs = plan.get("chosen_start")
        start_ts = 0
        try:
            dt = dt_util.parse_datetime(cs)
            if dt:
                start_ts = dt.timestamp()
        except Exception as _err:
            _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "sort_key", _err)
        return (1, -p, start_ts)
    order.sort(key=sort_key)

    # Clear bumped plans from a working copy
    new_plans = dict(store.consumer_plans or {})
    prev_plans = dict(new_plans)   # fuer trigger_on_utc: Anker der nicht-getriggerten Slots erhalten
    for s in to_bump:
        new_plans.pop(s, None)
        new_plans.pop(int(s) if s.isdigit() else s, None)

    # Assign each in order
    price_slots_raw = store.price_slots if hasattr(store, "price_slots") else {}
    log_msgs: list[str] = []
    for s in order:
        if s == str(slot):
            c = consumers.get(s) or cfg
        else:
            c = consumers.get(s, {})
        if not c:
            continue
        c_duration = int(c.get("duration_minutes") or duration)
        c_power = float(c.get("power_kw") or 0.0)
        c_name = str(c.get("name") or f"Consumer {s}")
        c_deadline_h = int(c.get("deadline_hours") or DEFAULT_DEADLINE_HOURS)
        c_deadline = now + timedelta(hours=c_deadline_h)

        # Build commitments using CURRENT new_plans (other bumped already re-assigned will be included)
        commitments = _build_commitments(new_plans, now, c_deadline, {s}, base_profile)
        battery = _battery_budget_map(store)
        battery_commitments = _build_battery_commitments(new_plans, {s})
        candidates = _candidate_slots(
            price_slots_raw, now, c_deadline,
            allowed_from=str(c.get("allowed_from") or ""),
            allowed_until=str(c.get("allowed_until") or ""),
        )
        best = _plan_best_window(candidates, commitments, c_power, c_duration, now,
                                 battery=battery, battery_commitments=battery_commitments)
        if best is None:
            _LOGGER.warning("on_demand slot %s: no valid window", s)
            log_msgs.append(f"⚠️ {c_name}: kein Fenster gefunden")
            continue

        start, end, grid_cost, avg_price, split = best

        # Live-Surplus-Veto: PV-Forecast (slot.pv_kw) kann massiv überschätzen.
        # Wenn ein Plan jetzt starten würde, aber der tatsächliche Live-Surplus
        # die Consumer-Leistung nicht trägt, auf späteren Slot verschieben.
        if (start - now).total_seconds() <= 15 * 60:
            pv_entity = str(
                entry.options.get(CONF_PV_SURPLUS_ENTITY)
                or entry.data.get(CONF_PV_SURPLUS_ENTITY)
                or ""
            ).strip()
            if pv_entity:
                surplus_state = hass.states.get(pv_entity)
                if surplus_state and surplus_state.state not in ("unknown", "unavailable", ""):
                    try:
                        live_surplus = state_to_kw(surplus_state)
                    except Exception as _err:
                        _LOGGER.debug("live-surplus veto: state_to_kw failed: %s", _err)
                        live_surplus = None
                    if live_surplus is not None and live_surplus < c_power - 0.5:
                        min_start = now + timedelta(minutes=30)
                        candidates_later = [cand for cand in candidates if cand[0] >= min_start]
                        best_later = _plan_best_window(candidates_later, commitments, c_power, c_duration, now,
                                                       battery=battery, battery_commitments=battery_commitments)
                        if best_later is not None:
                            _LOGGER.info(
                                "on_demand slot %s: live-surplus veto (live %.2f kW < power %.2f kW), shifting %s → %s",
                                s, live_surplus, c_power,
                                start.strftime("%H:%M"), best_later[0].strftime("%H:%M"),
                            )
                            log_msgs.append(
                                f"⚠️ {c_name}: Live-Surplus {live_surplus:.1f} kW < {c_power:.1f} kW — Start verschoben auf {best_later[0].strftime('%H:%M')}"
                            )
                            best = best_later
                            start, end, grid_cost, avg_price, split = best
                        else:
                            _LOGGER.warning(
                                "on_demand slot %s: live-surplus veto (live %.2f kW) but no later window — keeping original plan",
                                s, live_surplus,
                            )
        plan = {
            "status": "planned",
            "should_run": False,
            "source": "on_demand",
            "chosen_start": start.isoformat(),
            "chosen_end": end.isoformat(),
            "tariff_window_start": start.isoformat(),
            "tariff_window_end": end.isoformat(),
            "tariff_avg_price_chf_per_kwh": round(avg_price, 6),
            "avg_price_chf_per_kwh": round(avg_price, 6),
            "expected_energy_kwh": round(c_power * (c_duration / 60.0), 3),
            "expected_grid_cost_chf": round(grid_cost, 4),
            "expected_cost_chf": round(grid_cost, 4),
            "expected_pv_kwh": round(split["pv_kwh"], 3),
            "expected_battery_kwh": round(split["battery_kwh"], 3),
            "expected_grid_kwh": round(split["grid_kwh"], 3),
            "energy_source": energy_source_label(split["pv_kwh"], split["battery_kwh"], split["grid_kwh"]),
            "power_kw": c_power,
            "priority": int(c.get("priority") or 5),
            "run_order": int(c.get("run_order") or 0),
            "slot_count": max(1, c_duration // SLOT_MINUTES),
        }
        # Trigger-Ereignis: fuer den Slot, dessen Trigger gerade off→on ging, beginnt
        # ein neues; die anderen werden nur mitgeplant und behalten ihren Anker.
        prev = prev_plans.get(str(s)) or prev_plans.get(s) or {}
        if str(s) == str(slot):
            plan["trigger_on_utc"] = dt_util.utcnow().isoformat()
            plan["opportunistic_advances"] = 0
        else:
            if prev.get("trigger_on_utc"):
                plan["trigger_on_utc"] = prev["trigger_on_utc"]
            plan["opportunistic_advances"] = int(prev.get("opportunistic_advances") or 0)
        new_plans[str(s)] = plan
        src_lbl = {"pv": "PV", "battery": "Akku", "grid": "Netz", "mixed": "gemischt"}[plan["energy_source"]]
        kind = "PV" if grid_cost < 0.001 else f"{src_lbl}, {grid_cost*100:.1f} Rp"
        log_msgs.append(f"📋 {c_name} geplant ({start.strftime('%H:%M')}–{end.strftime('%H:%M')}, {kind})")

    store.consumer_plans = new_plans
    store.dirty = True
    for msg in log_msgs:
        icon, _, text = msg.partition(" ")
        store.log_activity(icon, text)
    await store.async_save()
    coordinator.async_set_updated_data(coordinator.data)


async def _cancel_plan(
    hass: HomeAssistant,
    coordinator,
    slot: str,
    cfg: dict[str, Any],
) -> None:
    """Remove plan if window hasn't started yet."""
    store = getattr(coordinator, "store", None)
    if store is None:
        return
    plans = dict(store.consumer_plans or {})
    plan = plans.get(str(slot)) or plans.get(slot)
    if not isinstance(plan, dict):
        return
    start_str = plan.get("chosen_start")
    if not start_str:
        return
    start = dt_util.parse_datetime(start_str)
    if start is None:
        return
    if dt_util.as_local(start) <= dt_util.now():
        _LOGGER.info("on_demand slot %s: window already started, not cancelling", slot)
        return
    plans.pop(str(slot), None)
    plans.pop(slot, None)
    store.consumer_plans = plans
    store.dirty = True
    name = str(cfg.get("name") or f"Consumer {slot}")
    store.log_activity("🚫", f"{name}: Plan abgebrochen (Trigger off)")
    await store.async_save()
    coordinator.async_set_updated_data(coordinator.data)


async def _create_or_advance_plan(
    hass: HomeAssistant,
    coordinator,
    slot: str,
    cfg: dict[str, Any],
    surplus_kw: float,
) -> None:
    """Opportunistic-now: bumpe Plan auf JETZT oder erstelle einen neuen Plan ab JETZT.

    Wenn ein Plan im Store existiert (egal welche Source) → chosen_start auf jetzt bumpen,
    Duration aus Plan übernehmen.
    Wenn KEIN Plan im Store → minimalen Plan erstellen mit duration aus cfg.
    Im Beide-Fälle: source = "opportunistic_now" als Marker.
    """
    store = getattr(coordinator, "store", None)
    if store is None:
        return
    plans = dict(store.consumer_plans or {})
    existing = plans.get(str(slot)) or plans.get(slot) or {}

    now_local = dt_util.now()
    duration_min = int(cfg.get("duration_minutes") or 60)
    power_kw = float(cfg.get("power_kw") or 0.0)

    # Duration ggf. aus existierendem Plan übernehmen (genauere Zeit aus on_demand-Planning)
    original_cs = existing.get("chosen_start")
    if original_cs and existing.get("chosen_end"):
        try:
            old_start = dt_util.parse_datetime(original_cs)
            old_end = dt_util.parse_datetime(existing.get("chosen_end"))
            if old_start and old_end and old_end > old_start:
                duration_min = max(duration_min, int((old_end - old_start).total_seconds() // 60))
        except Exception as _err:
            _LOGGER.debug("silent %s in create_or_advance duration parse: %s", type(_err).__name__, _err)

    new_start = now_local
    new_end = new_start + timedelta(minutes=duration_min)

    new_plan = dict(existing) if existing else {}
    new_plan["status"] = "planned"
    new_plan["should_run"] = False  # binary_sensor leitet aus chosen_start/now ab
    new_plan["source"] = "opportunistic_now"
    new_plan["chosen_start"] = new_start.isoformat()
    new_plan["chosen_end"] = new_end.isoformat()
    new_plan["tariff_window_start"] = new_start.isoformat()
    new_plan["tariff_window_end"] = new_end.isoformat()
    new_plan["power_kw"] = power_kw
    new_plan["priority"] = int(cfg.get("priority") or 5)
    new_plan["run_order"] = int(cfg.get("run_order") or 0)
    new_plan["slot_count"] = max(1, duration_min // SLOT_MINUTES)
    new_plan["expected_energy_kwh"] = round(power_kw * (duration_min / 60.0), 3)
    new_plan["opportunistic_now"] = True
    # Fehlt der Anker (Trigger stand schon vor dem Start von HA auf on), hier setzen —
    # sonst haette das Ereignis keine Bezugszeit und koennte nie verbraucht werden.
    new_plan.setdefault("trigger_on_utc", dt_util.utcnow().isoformat())
    new_plan["opportunistic_advances"] = int(existing.get("opportunistic_advances") or 0) + 1
    new_plan.pop("opportunistic_capped", None)
    if original_cs:
        new_plan["original_chosen_start"] = original_cs

    plans[str(slot)] = new_plan
    store.consumer_plans = plans
    store.dirty = True
    name = str(cfg.get("name") or f"Consumer {slot}")
    action = "vorgezogen" if original_cs else "gestartet"
    store.log_activity(
        "☀️",
        f"{name}: PV-Überschuss {surplus_kw:.1f} kW reicht — {action} ({new_start.strftime('%H:%M')})",
    )
    await store.async_save()
    coordinator.async_set_updated_data(coordinator.data)


def async_setup_on_demand(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> CALLBACK_TYPE:
    slots_cfg = _on_demand_slots(entry)
    if not slots_cfg:
        return lambda: None

    trigger_to_slot: dict[str, tuple[str, dict[str, Any]]] = {
        str(cfg.get("trigger_entity")).strip(): (slot, cfg)
        for slot, cfg in slots_cfg
    }

    @callback
    def _on_state_change(event: Event) -> None:
        entity_id = event.data.get("entity_id")
        mapped = trigger_to_slot.get(entity_id)
        if not mapped:
            return
        slot, cfg = mapped
        if not cfg.get("enabled", True):
            return
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None:
            return
        new_s = new.state
        old_s = old.state if old else None
        # Only react to explicit off↔on transitions. Ignore unavailable/unknown.
        if old_s == "off" and new_s == "on":
            _LOGGER.info("on_demand slot %s: %s off→on, planning", slot, entity_id)
            hass.async_create_task(_plan_run(hass, entry, coordinator, slot, cfg))
        elif old_s == "on" and new_s == "off":
            _LOGGER.info("on_demand slot %s: %s on→off, cancelling if pending", slot, entity_id)
            hass.async_create_task(_cancel_plan(hass, coordinator, slot, cfg))
        # else: unavailable/unknown in either direction — ignore

    unsub_state = async_track_state_change_event(hass, list(trigger_to_slot.keys()), _on_state_change)
    _LOGGER.info("on_demand: tracking %d trigger entities", len(trigger_to_slot))

    # ---- Opportunistic-Now: Tick alle 30 s, Surplus-Check pro Consumer ----
    surplus_stable_since: dict[str, datetime] = {}

    @callback
    def _opportunistic_tick(_now) -> None:
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        config = entry.options if entry.options else entry.data
        pv_surplus_entity = str(config.get(CONF_PV_SURPLUS_ENTITY, "") or "").strip()
        if not pv_surplus_entity:
            return
        surplus_state = hass.states.get(pv_surplus_entity)
        if surplus_state is None or surplus_state.state in ("unknown", "unavailable", ""):
            surplus_stable_since.clear()
            return
        try:
            surplus_kw = state_to_kw(surplus_state)
        except Exception as _err:
            _LOGGER.debug("silent %s in surplus state_to_kw: %s", type(_err).__name__, _err)
            return

        now_local = dt_util.now()
        plans = store.consumer_plans or {}
        for slot, cfg in slots_cfg:
            if not cfg.get("enabled", True):
                surplus_stable_since.pop(slot, None)
                continue
            if cfg.get("pause_on_vacation") and is_vacation_active(hass, entry):
                surplus_stable_since.pop(slot, None)
                continue
            trig_entity = str(cfg.get("trigger_entity") or "").strip()
            if not trig_entity:
                surplus_stable_since.pop(slot, None)
                continue
            trig_state = hass.states.get(trig_entity)
            if trig_state is None or trig_state.state != "on":
                surplus_stable_since.pop(slot, None)
                continue
            power_kw = float(cfg.get("power_kw") or 0.0)
            if power_kw <= 0:
                surplus_stable_since.pop(slot, None)
                continue
            plan = plans.get(str(slot)) or plans.get(slot) or {}
            # Trigger-Ereignis bereits durch einen Lauf verbraucht? Dann steht der
            # Smart-Status nur noch an, weil die Maschine offen ist.
            if _episode_consumed(store, slot, plan):
                surplus_stable_since.pop(slot, None)
                continue
            advances = int(plan.get("opportunistic_advances") or 0)
            if advances >= MAX_OPPORTUNISTIC_ADVANCES:
                if not plan.get("opportunistic_capped"):
                    _LOGGER.warning(
                        "on_demand slot %s: %dx vorgezogen ohne verbuchten Lauf — "
                        "opportunistic_now gestoppt bis zum naechsten off→on des Triggers",
                        slot, advances,
                    )
                    plan["opportunistic_capped"] = True
                    store.consumer_plans = {**plans, str(slot): plan}
                    store.dirty = True
                    store.log_activity(
                        "🛑",
                        f"{cfg.get('name') or f'Consumer {slot}'}: {advances}x vorgezogen, "
                        "aber kein Lauf verbucht — warte auf neuen Trigger",
                    )
                surplus_stable_since.pop(slot, None)
                continue
            # Skip wenn Plan-Window aktuell läuft (start <= now < end)
            cs = plan.get("chosen_start")
            ce = plan.get("chosen_end")
            if cs and ce:
                try:
                    p_start = dt_util.parse_datetime(cs)
                    p_end = dt_util.parse_datetime(ce)
                    if (p_start and p_end
                            and dt_util.as_local(p_start) <= now_local < dt_util.as_local(p_end)):
                        # Plan läuft gerade — Automation hat (hoffentlich) längst getriggert
                        surplus_stable_since.pop(slot, None)
                        continue
                except Exception as _err:
                    _LOGGER.debug("silent %s in opportunistic plan parse: %s", type(_err).__name__, _err)
            threshold_kw = power_kw + OPPORTUNISTIC_BUFFER_KW
            if surplus_kw < threshold_kw:
                surplus_stable_since.pop(slot, None)
                continue
            # Surplus reicht — Hysterese-Tracker
            if slot not in surplus_stable_since:
                surplus_stable_since[slot] = now_local
                _LOGGER.debug("opportunistic_now slot %s: surplus %.2f >= %.2f kW, starting stable timer",
                              slot, surplus_kw, threshold_kw)
                continue
            elapsed = (now_local - surplus_stable_since[slot]).total_seconds()
            if elapsed < OPPORTUNISTIC_STABLE_SECONDS:
                continue
            # Alle Bedingungen erfüllt: Plan erstellen oder vorziehen
            _LOGGER.info(
                "opportunistic_now slot %s: surplus %.2f kW stable %.0fs >= %.2f kW threshold — creating/advancing plan",
                slot, surplus_kw, elapsed, threshold_kw,
            )
            surplus_stable_since.pop(slot, None)
            hass.async_create_task(_create_or_advance_plan(hass, coordinator, slot, cfg, surplus_kw))

    unsub_tick = async_track_time_interval(
        hass, _opportunistic_tick, timedelta(seconds=OPPORTUNISTIC_TICK_SECONDS),
    )
    _LOGGER.info(
        "on_demand: opportunistic_now active (buffer=%.1f kW, stable=%ds, tick=%ds)",
        OPPORTUNISTIC_BUFFER_KW, OPPORTUNISTIC_STABLE_SECONDS, OPPORTUNISTIC_TICK_SECONDS,
    )

    @callback
    def _unsub_all() -> None:
        unsub_state()
        unsub_tick()

    return _unsub_all
