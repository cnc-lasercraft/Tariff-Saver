"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EKZ_ENTRY_ID,
    CONF_FEED_IN_FIXED_PRICE,
    CONF_FEED_IN_PRICE_ENTITY,
    CONF_FEED_IN_PRICE_MODE,
    CONF_PUBLISH_TIME,
    DEFAULT_FEED_IN_FIXED_PRICE,
    DEFAULT_PUBLISH_TIME,
    FEED_IN_PRICE_MODE_ENTITY,
    DOMAIN,
    SLOTS_PER_HOUR,
)
from .consumer_helpers import build_all_consumer_plans
from .slot_helpers import score_from_prices
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSlot:
    """A normalized price slot.

    pv_kw: prognostizierte PV-Leistung in kW für diesen Slot (aus Solcast o.ä.),
    bereits auf Slot-Granularität (SLOT_MINUTES) gemappt. 0.0 = kein Forecast verfügbar.
    """

    start: datetime  # UTC, timezone-aware
    electricity_chf_per_kwh: float
    components_chf_per_kwh: dict[str, float]
    pv_kw: float = 0.0


WINDOW_HOURS: tuple[int, ...] = (1, 2, 3, 6)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _slot_total_price(comps: dict[str, float] | None) -> float | None:
    if not comps:
        return None
    total = 0.0
    found = False
    for key in ("electricity", "grid", "regional_fees"):
        value = comps.get(key)
        if isinstance(value, (int, float)):
            total += float(value)
            found = True
    if found and total > 0:
        return total
    for key in ("integrated", "all_in"):
        value = comps.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    return None




class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Reads tariff curves from the separate EKZ provider integration."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

        self.tariff_name: str = "EKZ active"
        self.baseline_tariff_name: str | None = "EKZ baseline"
        self.publish_time: str = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
        self.ekz_entry_id: str | None = config.get(CONF_EKZ_ENTRY_ID)
        self.feed_in_price_mode: str = str(config.get(CONF_FEED_IN_PRICE_MODE) or "fixed")
        self.feed_in_fixed_price: float = float(config.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE) or 0.0)
        self.feed_in_price_entity: str | None = config.get(CONF_FEED_IN_PRICE_ENTITY)

        self._last_fetch_date: date | None = None
        self.store: TariffSaverStore | None = None

        self.link_status: str | None = None
        self.linking_url: str | None = None
        self.last_api_success_utc: datetime | None = None

        super().__init__(hass, _LOGGER, name="Tariff Saver", update_interval=timedelta(minutes=15))

    async def _async_update_data(self) -> dict[str, Any]:
        if self.store is None:
            for entry_id, coord in self.hass.data.get(DOMAIN, {}).items():
                if coord is self:
                    self.store = TariffSaverStore(self.hass, entry_id)
                    await self.store.async_load()
                    break

        # Build active slots from own store (populated by _on_ekz_bus_event)
        active = self._build_active_from_store()
        if not active:
            if self.data:
                return self.data
            raise UpdateFailed("Keine Preisdaten im Store")

        self.store.trim_price_slots(keep_days=400)

        # Update historical min/max prices BEFORE computing stats (scores depend on these)
        if self.store is not None:
            from .const import CONF_MIN_VALID_PRICE, DEFAULT_MIN_VALID_PRICE, CONF_MAX_VALID_PRICE, DEFAULT_MAX_VALID_PRICE
            min_valid = float(self.config.get(CONF_MIN_VALID_PRICE, DEFAULT_MIN_VALID_PRICE) or DEFAULT_MIN_VALID_PRICE)
            max_valid = float(self.config.get(CONF_MAX_VALID_PRICE, DEFAULT_MAX_VALID_PRICE) or DEFAULT_MAX_VALID_PRICE)
            all_prices = [_slot_total_price(s.components_chf_per_kwh) for s in active]
            self.store.update_historical_prices(all_prices, min_valid=min_valid, max_valid=max_valid)
            if self.store.dirty:
                # Gedrosselt: _async_update_data läuft via should_poll-Sensoren alle
                # ~30 s, und dirty kann (Sample-Debounce) minutenlang stehen — ein
                # direkter Save hier hiesse Voll-Serialize des Multi-MB-Stores pro Poll.
                self.store.schedule_save()

        stats = self._compute_daily_stats(active, [], {})
        feed_in_price = self._get_feed_in_price()

        if self.store is not None:
            slot_day = stats.get("slot_day_local")
            avg_active = stats.get("avg_active_chf_per_kwh")
            if isinstance(slot_day, date) and isinstance(avg_active, (int, float)) and avg_active > 0:
                self.store.set_day_average_price(slot_day, float(avg_active))
                self.store.trim_day_average_prices(keep_days=400)
            if self.store.dirty:
                self.store.schedule_save()

        windows = self._compute_best_windows(active)

        # Consumer Plans: nur aus Storage laden
        consumer_plans: dict = {}
        if self.store is not None and self.store.consumer_plans:
            consumer_plans = {
                int(k) if str(k).isdigit() else k: v
                for k, v in self.store.consumer_plans.items()
            }

        return {
            "active": active,
            "baseline": [],
            "active_raw": [],
            "baseline_raw": [],
            "stats": stats,
            "feed_in": {
                "mode": self.feed_in_price_mode,
                "price_chf_per_kwh": feed_in_price,
                "entity_id": self.feed_in_price_entity,
            },
            "windows": windows,
            "provider": {},
            "consumer_plans": consumer_plans,
            "date_validity": {},
        }

    def _build_active_from_store(self) -> list[PriceSlot]:
        """Build active PriceSlot list from own stored price_slots."""
        if self.store is None or not self.store.price_slots:
            return []
        slots_15m: list[PriceSlot] = []
        for ts_key, slot_data in self.store.price_slots.items():
            parsed = dt_util.parse_datetime(ts_key)
            if parsed is None:
                continue
            # price_slots store format: a_comp = dynamic components, b_comp = baseline
            a_comp = slot_data.get("a_comp") or {}
            if not a_comp:
                continue
            components = {str(k): float(v) for k, v in a_comp.items() if isinstance(v, (int, float))}
            electricity = float(components.get("electricity", 0.0))
            pv_kw_raw = slot_data.get("pv_kw")
            try:
                pv_kw = float(pv_kw_raw) if pv_kw_raw is not None else 0.0
            except (TypeError, ValueError):
                pv_kw = 0.0
            slots_15m.append(PriceSlot(
                start=dt_util.as_utc(parsed),
                electricity_chf_per_kwh=electricity,
                components_chf_per_kwh=components,
                pv_kw=pv_kw,
            ))
        return sorted(slots_15m, key=lambda s: s.start)

    def _get_feed_in_price(self) -> float | None:
        if self.feed_in_price_mode == FEED_IN_PRICE_MODE_ENTITY and isinstance(self.feed_in_price_entity, str) and self.feed_in_price_entity:
            state = self.hass.states.get(self.feed_in_price_entity)
            if state is not None:
                try:
                    value = float(state.state)
                    return value if value >= 0 else None
                except Exception:
                    return None
            return None
        return self.feed_in_fixed_price if self.feed_in_fixed_price >= 0 else None

    @staticmethod
    def _convert_slot(slot: Any) -> PriceSlot:
        start = getattr(slot, "start", None)
        if not isinstance(start, datetime):
            raise UpdateFailed("EKZ Tariff slot is missing start")

        raw_components = getattr(slot, "components_chf_per_kwh", {}) or {}
        components = {
            str(key): float(value)
            for key, value in raw_components.items()
            if isinstance(value, (int, float))
        }

        electricity = float(getattr(slot, "electricity_chf_per_kwh", 0.0) or 0.0)
        if electricity <= 0:
            electricity = float(components.get("electricity") or 0.0)
        if electricity > 0:
            components["electricity"] = electricity

        return PriceSlot(
            start=dt_util.as_utc(start),
            electricity_chf_per_kwh=electricity,
            components_chf_per_kwh=components,
        )

    def _compute_daily_stats(self, active: list[PriceSlot], baseline: list[PriceSlot], date_validity: dict[str, Any] | None = None) -> dict[str, Any]:
        # Filter to today only for daily averages/scores
        today_local = dt_util.now().date()
        today_active = [s for s in active if dt_util.as_local(s.start).date() == today_local]
        today_baseline = [s for s in baseline if dt_util.as_local(s.start).date() == today_local]

        active_map = {s.start: _slot_total_price(s.components_chf_per_kwh) for s in today_active}
        active_values = [float(v) for v in active_map.values() if isinstance(v, (int, float)) and v >= 0]

        baseline_by_start = {s.start: s for s in today_baseline}
        base_values = [
            float(v)
            for v in (_slot_total_price(s.components_chf_per_kwh) for s in today_baseline)
            if isinstance(v, (int, float)) and v >= 0
        ]

        min_active = min(active_values) if active_values else None
        max_active = max(active_values) if active_values else None
        avg_active = _avg(active_values)
        avg_baseline = _avg(base_values)

        current_slot = next((s for s in reversed(sorted(active, key=lambda s: s.start)) if s.start <= dt_util.utcnow()), None)
        if current_slot is None and active:
            current_slot = active[0]
        current_price = _slot_total_price(current_slot.components_chf_per_kwh) if current_slot else None
        all_baseline_map = {s.start: s for s in baseline}
        current_base_slot = all_baseline_map.get(current_slot.start) if current_slot else None
        current_baseline = _slot_total_price(current_base_slot.components_chf_per_kwh) if current_base_slot else None

        slot_day_local = today_local if today_active else None
        year_prices = self.store.get_year_day_average_prices(today_local) if self.store else []
        year_min = min(year_prices) if year_prices else None
        year_max = max(year_prices) if year_prices else None
        day_score = score_from_prices(avg_active, year_min, year_max)

        dev_vs_baseline_percent = None
        if isinstance(current_price, (int, float)) and isinstance(current_baseline, (int, float)) and current_baseline > 0:
            dev_vs_baseline_percent = ((float(current_price) / float(current_baseline)) - 1.0) * 100.0

        # Tomorrow stats (available after ~18:15 when EKZ publishes)
        from datetime import timedelta as _td
        tomorrow_local = today_local + _td(days=1)
        tomorrow_active = [s for s in active if dt_util.as_local(s.start).date() == tomorrow_local]
        tomorrow_values = [
            float(v) for v in
            (_slot_total_price(s.components_chf_per_kwh) for s in tomorrow_active)
            if isinstance(v, (int, float)) and v >= 0
        ]
        avg_tomorrow = _avg(tomorrow_values)
        tomorrow_score = score_from_prices(avg_tomorrow, year_min, year_max) if avg_tomorrow else None

        # Date validity from EKZ provider
        dv = date_validity or {}
        today_val = dv.get(today_local.isoformat())
        tomorrow_val = dv.get(tomorrow_local.isoformat())
        today_data_valid = today_val.get("valid", True) if isinstance(today_val, dict) else True
        tomorrow_data_valid = tomorrow_val.get("valid", True) if isinstance(tomorrow_val, dict) else True

        return {
            "calculated_at": dt_util.utcnow().isoformat(),
            "slot_day_local": slot_day_local,
            "min_active_chf_per_kwh": min_active,
            "max_active_chf_per_kwh": max_active,
            "avg_active_chf_per_kwh": avg_active,
            "avg_baseline_chf_per_kwh": avg_baseline,
            "current_price_chf_per_kwh": current_price,
            "current_baseline_chf_per_kwh": current_baseline,
            "current_score_0_100": score_from_prices(current_price, min_active, max_active),
            "price_vs_baseline_percent": dev_vs_baseline_percent,
            "day_score_0_100": day_score,
            "year_min_day_avg_chf_per_kwh": year_min,
            "year_max_day_avg_chf_per_kwh": year_max,
            "year_day_samples": len(year_prices),
            "tomorrow_avg_chf_per_kwh": avg_tomorrow,
            "tomorrow_score_0_100": tomorrow_score,
            "tomorrow_slot_count": len(tomorrow_values),
            "today_data_valid": today_data_valid,
            "today_validity_error": today_val.get("error") if isinstance(today_val, dict) else None,
            "today_validity_details": today_val.get("details") if isinstance(today_val, dict) else None,
            "tomorrow_data_valid": tomorrow_data_valid,
            "tomorrow_validity_error": tomorrow_val.get("error") if isinstance(tomorrow_val, dict) else None,
            "tomorrow_validity_details": tomorrow_val.get("details") if isinstance(tomorrow_val, dict) else None,
        }

    @staticmethod
    def _compute_best_windows(active: list[PriceSlot]) -> dict[str, Any]:
        ordered = sorted(active, key=lambda s: s.start)
        prices = [_slot_total_price(s.components_chf_per_kwh) for s in ordered]
        valid_prices = [float(v) for v in prices if isinstance(v, (int, float)) and v >= 0]
        min_price = min(valid_prices) if valid_prices else None
        max_price = max(valid_prices) if valid_prices else None

        out: dict[str, Any] = {}
        now_utc = dt_util.utcnow()

        for hours in WINDOW_HOURS:
            slot_count = hours * SLOTS_PER_HOUR
            candidates: list[tuple[datetime, float]] = []
            for idx in range(0, len(ordered) - slot_count + 1):
                window_slots = ordered[idx : idx + slot_count]
                if window_slots[-1].start < now_utc:
                    continue
                window_prices = [_slot_total_price(s.components_chf_per_kwh) for s in window_slots]
                if any(not isinstance(v, (int, float)) or float(v) < 0 for v in window_prices):
                    continue
                avg_price = sum(float(v) for v in window_prices) / slot_count
                candidates.append((window_slots[0].start, avg_price))

            key = f"{hours}h"
            if not candidates:
                out[key] = {"start": None, "avg_price_chf_per_kwh": None, "score_0_100": None}
                continue

            best_start, best_price = min(candidates, key=lambda item: item[1])
            out[key] = {
                "start": best_start,
                "avg_price_chf_per_kwh": round(best_price, 6),
                "score_0_100": score_from_prices(best_price, min_price, max_price),
            }
        return out
