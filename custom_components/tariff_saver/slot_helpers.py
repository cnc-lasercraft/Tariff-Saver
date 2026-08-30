"""Helpers for parsing and normalizing slot data."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .models import PriceSlot


def score_from_prices(current_price: float | None, min_price: float | None, max_price: float | None) -> int | None:
    """Return score where 0 is cheapest and 100 is most expensive."""
    if not isinstance(current_price, (int, float)) or current_price < 0:
        return None
    if not isinstance(min_price, (int, float)) or not isinstance(max_price, (int, float)):
        return None
    if max_price <= min_price:
        return 50
    score = ((float(current_price) - float(min_price)) / (float(max_price) - float(min_price))) * 100.0
    return max(0, min(100, int(round(score))))


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_datetime_any(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return dt_util.as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = dt_util.parse_datetime(value.strip())
    if parsed is None:
        return None
    return dt_util.as_utc(parsed)


def _extract_list(container: Any, attribute: str | None) -> list[Any]:
    if isinstance(container, list):
        return container
    if isinstance(container, str):
        try:
            decoded = json.loads(container)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    if isinstance(container, dict) and attribute:
        value = container.get(attribute)
        return _extract_list(value, None)
    return []


def slot_list_from_state(state: Any, attribute: str | None) -> list[Any]:
    if state is None:
        return []
    attrs = getattr(state, "attributes", {}) or {}
    if attribute and attribute in attrs:
        return _extract_list(attrs.get(attribute), None)
    return _extract_list(getattr(state, "state", None), None)


def price_slot_from_mapping(item: dict[str, Any], *, price_scale: float = 1.0) -> PriceSlot | None:
    start_raw = item.get("start") or item.get("start_timestamp") or item.get("timestamp")
    start = parse_datetime_any(start_raw)
    if start is None:
        return None

    components = item.get("components") if isinstance(item.get("components"), dict) else {}
    provider_components = (
        item.get("components_chf_per_kwh")
        if isinstance(item.get("components_chf_per_kwh"), dict)
        else {}
    )
    baseline_components = (
        item.get("baseline_components") if isinstance(item.get("baseline_components"), dict) else {}
    )

    electricity = _as_float(
        item.get(
            "electricity",
            item.get(
                "electricity_chf_per_kwh",
                item.get("price_chf_per_kwh", components.get("electricity", provider_components.get("electricity", 0.0))),
            ),
        )
    )
    grid = _as_float(
        item.get("grid", components.get("grid", provider_components.get("grid", 0.0)))
    )
    regional_fees = _as_float(
        item.get(
            "regional_fees",
            components.get("regional_fees", provider_components.get("regional_fees", 0.0)),
        )
    )
    integrated = _as_float(
        item.get(
            "integrated",
            item.get(
                "all_in_chf_per_kwh",
                components.get("integrated", provider_components.get("integrated", 0.0)),
            ),
        )
    )

    if integrated <= 0:
        integrated = electricity + grid + regional_fees
    if electricity <= 0 and integrated > 0:
        electricity = integrated

    merged_components: dict[str, float] = {}
    for source in (components, provider_components, baseline_components, item):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if isinstance(value, (int, float)):
                merged_components[str(key)] = float(value) * price_scale

    electricity *= price_scale
    grid *= price_scale
    regional_fees *= price_scale
    integrated *= price_scale

    return PriceSlot(
        start=start,
        electricity=electricity,
        grid=grid,
        regional_fees=regional_fees,
        integrated=integrated,
        components=merged_components,
    )


def normalize_slots(raw_items: list[Any], *, price_scale: float = 1.0, ignore_zero_prices: bool = True) -> list[PriceSlot]:
    slots: dict[datetime, PriceSlot] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        slot = price_slot_from_mapping(item, price_scale=price_scale)
        if slot is None:
            continue
        if ignore_zero_prices and slot.electricity_chf_per_kwh <= 0 and slot.integrated <= 0:
            continue
        slots[slot.start] = slot
    return [slots[key] for key in sorted(slots)]


def avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


_STATE_TO_KW_WARNED: set[str] = set()


def state_to_kw(state_obj) -> float:
    """Convert an HA power entity state to kW using unit_of_measurement.
    No unit → assume Watts (residential power sensors are almost always in W).
    """
    if state_obj is None or state_obj.state in ("unavailable", "unknown", ""):
        return 0.0
    try:
        val = float(state_obj.state)
    except (ValueError, TypeError):
        return 0.0
    unit = (state_obj.attributes.get("unit_of_measurement") or "").strip()
    if unit == "W":
        return val / 1000.0
    if unit == "kW":
        return val
    eid = getattr(state_obj, "entity_id", "?")
    if eid not in _STATE_TO_KW_WARNED:
        import logging
        logging.getLogger(__name__).warning(
            "state_to_kw: %s hat keine unit_of_measurement — interpretiere als W. Setze Unit W/kW am Sensor um Warnung zu vermeiden.",
            eid,
        )
        _STATE_TO_KW_WARNED.add(eid)
    return val / 1000.0


# --- Dreistufiges Energiemodell PV → Akku → Netz (Audit 2026-08-26, Cluster 2 / S5) ---

def split_slot_energy(
    load_kw: float,
    pv_avail_kw: float,
    battery_avail_kwh: float,
    price: float,
    battery_price: float,
    *,
    ignore_pv: bool = False,
    battery_blocked: bool = False,
    hours: float = 0.25,
) -> tuple[float, float, float, float]:
    """Teilt die Last eines Slots auf PV, Akku und Netz auf.

    Reihenfolge: PV-Überschuss (0 CHF) → Akku-Budget (Wiederbeschaffungspreis) → Netz
    (Slot-Preis). `ignore_pv` = tariff_only (PV wird nicht angerechnet, Akku schon —
    nachts liefert physisch der Akku). `battery_blocked` = Akku lädt gerade aus dem
    Netz (C9-Fenster) → keine Akku-Deckung möglich.

    Rückgabe: (pv_kwh, battery_kwh, grid_kwh, cost_chf).
    """
    if load_kw <= 0:
        return 0.0, 0.0, 0.0, 0.0
    need_kwh = load_kw * hours
    pv_kwh = 0.0 if ignore_pv else min(need_kwh, max(0.0, pv_avail_kw) * hours)
    rest = need_kwh - pv_kwh
    batt_kwh = 0.0 if battery_blocked else min(rest, max(0.0, battery_avail_kwh))
    grid_kwh = rest - batt_kwh
    cost = batt_kwh * battery_price + grid_kwh * price
    return pv_kwh, batt_kwh, grid_kwh, cost


def energy_source_label(pv_kwh: float, battery_kwh: float, grid_kwh: float) -> str:
    """Dominante Quelle (≥ 80 %) oder 'mixed'."""
    total = pv_kwh + battery_kwh + grid_kwh
    if total <= 0:
        return "pv"
    for name, val in (("pv", pv_kwh), ("battery", battery_kwh), ("grid", grid_kwh)):
        if val >= total * 0.8:
            return name
    return "mixed"
