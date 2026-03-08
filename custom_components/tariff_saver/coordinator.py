"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_EKZ_ENTRY_ID, CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME, DOMAIN
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSlot:
    """A single 15-minute price slot."""

    start: datetime  # UTC, timezone-aware
    electricity_chf_per_kwh: float
    components_chf_per_kwh: dict[str, float]


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


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Reads tariff curves from the separate EKZ provider integration."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

        self.tariff_name: str = "EKZ active"
        self.baseline_tariff_name: str | None = "EKZ baseline"
        self.publish_time: str = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
        self.ekz_entry_id: str | None = config.get(CONF_EKZ_ENTRY_ID)

        self._last_fetch_date: date | None = None
        self.store: TariffSaverStore | None = None

        self.link_status: str | None = None
        self.linking_url: str | None = None
        self.last_api_success_utc: datetime | None = None

        super().__init__(hass, _LOGGER, name="Tariff Saver", update_interval=None)

    async def _async_update_data(self) -> dict[str, Any]:
        if self.store is None:
            for entry_id, coord in self.hass.data.get(DOMAIN, {}).items():
                if coord is self:
                    self.store = TariffSaverStore(self.hass, entry_id)
                    await self.store.async_load()
                    break

        today = dt_util.now().date()
        if self._last_fetch_date == today and self.data:
            return self.data

        provider_data = self._get_provider_data()
        active_raw = provider_data.get("active_slots") or []
        baseline_raw = provider_data.get("baseline_slots") or []

        active = [self._convert_slot(slot) for slot in active_raw]
        baseline = [self._convert_slot(slot) for slot in baseline_raw]

        if not active:
            raise UpdateFailed("EKZ Tariff provider returned no active_slots")

        self.link_status = provider_data.get("link_status")
        self.linking_url = provider_data.get("linking_url")
        self.last_api_success_utc = provider_data.get("last_api_success_utc")

        if self.store is not None:
            if isinstance(self.last_api_success_utc, datetime):
                self.store.set_last_api_success(self.last_api_success_utc)
            base_map = {s.start: s.components_chf_per_kwh for s in baseline}
            for s in active:
                self.store.set_price_slot(
                    s.start,
                    dyn_components_chf_per_kwh=s.components_chf_per_kwh,
                    base_components_chf_per_kwh=base_map.get(s.start),
                )
            self.store.trim_price_slots(keep_days=7)

        stats = self._compute_daily_stats(active, baseline)
        if self.store is not None:
            slot_day = stats.get("slot_day_local")
            avg_active = stats.get("avg_active_chf_per_kwh")
            if isinstance(slot_day, date) and isinstance(avg_active, (int, float)) and avg_active > 0:
                self.store.set_day_average_price(slot_day, float(avg_active))
                self.store.trim_day_average_prices(keep_days=400)
            if self.store.dirty:
                await self.store.async_save()

        windows = self._compute_best_windows(active)
        self._last_fetch_date = today
        return {
            "active": active,
            "baseline": baseline,
            "stats": stats,
            "windows": windows,
            "provider": {
                "entry_id": provider_data.get("entry_id"),
                "provider": provider_data.get("provider"),
                "active_publication_timestamp": provider_data.get("active_publication_timestamp"),
                "baseline_publication_timestamp": provider_data.get("baseline_publication_timestamp"),
            },
        }

    def _get_provider_data(self) -> dict[str, Any]:
        try:
            from custom_components.ekz_tariff import get_first_provider_data, get_provider_data
        except ImportError as err:
            raise UpdateFailed("Could not import custom_components.ekz_tariff provider helpers") from err

        try:
            if isinstance(self.ekz_entry_id, str) and self.ekz_entry_id.strip():
                return get_provider_data(self.hass, self.ekz_entry_id.strip())
            return get_first_provider_data(self.hass)
        except KeyError as err:
            raise UpdateFailed("No loaded EKZ Tariff entry found") from err

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

    def _compute_daily_stats(self, active: list[PriceSlot], baseline: list[PriceSlot]) -> dict[str, Any]:
        active_map = {s.start: _slot_total_price(s.components_chf_per_kwh) for s in active}
        active_values = [float(v) for v in active_map.values() if isinstance(v, (int, float)) and v >= 0]

        base_map = {s.start: _slot_total_price(s.components_chf_per_kwh) for s in baseline}
        base_values = [float(v) for v in base_map.values() if isinstance(v, (int, float)) and v >= 0]

        min_active = min(active_values) if active_values else None
        max_active = max(active_values) if active_values else None
        avg_active = _avg(active_values)
        avg_baseline = _avg(base_values)

        current_slot = next((s for s in reversed(sorted(active, key=lambda s: s.start)) if s.start <= dt_util.utcnow()), None)
        if current_slot is None and active:
            current_slot = active[0]
        current_price = _slot_total_price(current_slot.components_chf_per_kwh) if current_slot else None
        current_baseline = _slot_total_price(base_map and next((b.components_chf_per_kwh for b in baseline if current_slot and b.start == current_slot.start), None)) if current_slot else None

        slot_day_local = dt_util.as_local(active[0].start).date() if active else None
        year_prices = self.store.get_year_day_average_prices(slot_day_local) if self.store and isinstance(slot_day_local, date) else []
        year_min = min(year_prices) if year_prices else None
        year_max = max(year_prices) if year_prices else None
        day_score = score_from_prices(avg_active, year_min, year_max)

        dev_vs_baseline_percent = None
        if isinstance(current_price, (int, float)) and isinstance(current_baseline, (int, float)) and current_baseline > 0:
            dev_vs_baseline_percent = ((float(current_price) / float(current_baseline)) - 1.0) * 100.0

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
            slot_count = hours * 4
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

            best_start, best_avg = min(candidates, key=lambda item: (item[1], item[0]))
            out[key] = {
                "start": best_start,
                "avg_price_chf_per_kwh": round(best_avg, 6),
                "score_0_100": score_from_prices(best_avg, min_price, max_price),
            }

        return out
