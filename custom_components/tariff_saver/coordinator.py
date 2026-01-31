"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PriceSlot:
    start: dt_util.dt.datetime
    price_chf_per_kwh: float


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, List[PriceSlot]]]):
    """Fetches and stores tariff price curves."""

    def __init__(self, hass: HomeAssistant, api: EkzTariffApi, config: dict) -> None:
        self.hass = hass
        self.api = api
        self.tariff_name: str = config["tariff_name"]
        self.baseline_tariff_name: str | None = config.get("baseline_tariff_name")

        super().__init__(
            hass,
            _LOGGER,
            name="Tariff Saver",
            update_interval=timedelta(minutes=15),
        )

    async def _async_update_data(self) -> dict[str, List[PriceSlot]]:
        now = dt_util.utcnow()
        start = now.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=24)

        data: dict[str, List[PriceSlot]] = {}

        # Active tariff
        prices = await self.api.fetch_prices(self.tariff_name, start, end)
        data["active"] = self._parse_prices(prices)

        # Baseline tariff (optional)
        if self.baseline_tariff_name:
            try:
                baseline_prices = await self.api.fetch_prices(
                    self.baseline_tariff_name, start, end
                )
                data["baseline"] = self._parse_prices(baseline_prices)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to fetch baseline tariff: %s", err)
                data["baseline"] = []

        return data

    @staticmethod
    def _parse_prices(raw_prices) -> List[PriceSlot]:
        slots: List[PriceSlot] = []
        for item in raw_prices:
            start_ts = item.get("start_timestamp")
            if not start_ts:
                continue

            price = EkzTariffApi.sum_chf_per_kwh(item)
            slots.append(
                PriceSlot(
                    start=dt_util.parse_datetime(start_ts),
                    price_chf_per_kwh=price,
                )
            )

        slots.sort(key=lambda s: s.start)
        return slots
