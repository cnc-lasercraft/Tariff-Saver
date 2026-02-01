"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi
from .const import CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSlot:
    """A single 15-minute price slot."""
    start: datetime  # UTC, timezone-aware
    price_chf_per_kwh: float


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches and stores tariff price curves + daily derived stats."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: EkzTariffApi,
        entry: ConfigEntry,
        config: dict[str, Any],
    ) -> None:
        self.hass = hass
        self.api = api
        self.entry = entry

        self.tariff_name: str = config["tariff_name"]
        self.baseline_tariff_name: str | None = config.get("baseline_tariff_name")
        self.publish_time: str = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)

        self._last_fetch_date: date | None = None

        # ✅ Store korrekt an entry gebunden
        self.store = TariffSaverStore(hass, entry.entry_id)

        super().__init__(
            hass,
            _LOGGER,
            name="Tariff Saver",
            update_interval=None,  # daily trigger will call refresh
        )

    async def _async_update_data(self) -> dict[str, Any]:
        today = dt_util.now().date()
        if self._last_fetch_date == today:
            return self.data or {"active": [], "baseline": [], "stats": {}}

        # Store einmal laden
        if not self.store.price_slots:
            await self.store.async_load()

        try:
            raw_active = await self.api.fetch_prices(self.tariff_name)
            active = self._parse_prices(raw_active)
            if not active:
                raise UpdateFailed(f"No data returned for active tariff '{self.tariff_name}'")
        except Exception as err:
            raise UpdateFailed(f"Active tariff update failed: {err}") from err

        baseline: list[PriceSlot] = []
        if self.baseline_tariff_name:
            try:
                raw_base = await self.api.fetch_prices(self.baseline_tariff_name)
                baseline = self._parse_prices(raw_base)
            except Exception as err:
                _LOGGER.warning(
                    "Failed to fetch baseline tariff '%s': %s",
                    self.baseline_tariff_name,
                    err,
                )
                baseline = []

        # 🔹 Preise persistent speichern
        active_map = {s.start: s.price_chf_per_kwh for s in active if s.price_chf_per_kwh > 0}
        base_map = {s.start: s.price_chf_per_kwh for s in baseline if s.price_chf_per_kwh > 0}

        for start_utc, dyn_price in active_map.items():
            base_price = base_map.get(start_utc)
            if base_price is not None:
                self.store.set_price_slot(start_utc, dyn_price, base_price)

        self.store.trim_price_slots(keep_days=3)
        if self.store.dirty:
            await self.store.async_save()

        stats = self._compute_daily_stats(active, baseline)
        self._last_fetch_date = today

        return {"active": active, "baseline": baseline, "stats": stats}

    @staticmethod
    def _parse_prices(raw_prices: list[dict[str, Any]]) -> list[PriceSlot]:
        slots: list[PriceSlot] = []

        for item in raw_prices:
            start_ts = item.get("start_timestamp")
            if not isinstance(start_ts, str):
                continue

            dt_start = dt_util.parse_datetime(start_ts)
            if dt_start is None:
                continue

            slots.append(
                PriceSlot(
                    start=dt_util.as_utc(dt_start),
                    price_chf_per_kwh=EkzTariffApi.sum_chf_per_kwh(item),
                )
            )

        return list({s.start: s for s in sorted(slots, key=lambda s: s.start)}.values())

    @staticmethod
    def _compute_daily_stats(
        active: list[PriceSlot],
        baseline: list[PriceSlot],
    ) -> dict[str, Any]:
        active_valid = [s for s in active if s.price_chf_per_kwh > 0]
        base_map = {s.start: s.price_chf_per_kwh for s in baseline if s.price_chf_per_kwh > 0}

        avg_active = _avg([s.price_chf_per_kwh for s in active_valid])
        avg_baseline = _avg([base_map[s.start] for s in active_valid if s.start in base_map]) if base_map else None

        dev_vs_avg = {}
        dev_vs_baseline = {}

        for s in active_valid:
            if avg_active:
                dev_vs_avg[s.start.isoformat()] = (s.price_chf_per_kwh / avg_active - 1) * 100
            if s.start in base_map:
                dev_vs_baseline[s.start.isoformat()] = (s.price_chf_per_kwh / base_map[s.start] - 1) * 100

        return {
            "calculated_at": dt_util.utcnow().isoformat(),
            "avg_active_chf_per_kwh": avg_active,
            "avg_baseline_chf_per_kwh": avg_baseline,
            "dev_vs_avg_percent": dev_vs_avg,
            "dev_vs_baseline_percent": dev_vs_baseline,
        }
