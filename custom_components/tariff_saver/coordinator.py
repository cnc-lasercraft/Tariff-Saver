"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSlot:
    """A single 15-minute price slot."""
    start: datetime
    price_chf_per_kwh: float


class TariffSaverCoordinator(DataUpdateCoordinator[list[PriceSlot]]):
    """Fetch and store EKZ 15-min prices."""

    def __init__(self, hass: HomeAssistant, tariff_name: str) -> None:
        self.hass = hass
        self.tariff_name = tariff_name
        self.api = EkzTariffApi(async_get_clientsession(hass))

        super().__init__(
            hass,
            _LOGGER,
            name="tariff_saver_prices",
            update_interval=timedelta(minutes=15),
        )

    async def _async_update_data(self) -> list[PriceSlot]:
        """Fetch data from EKZ."""
        try:
            now = dt_util.utcnow()

            # Fetch next 24 hours (enough for "now", charts, and window search later)
            start = now - timedelta(minutes=15)  # small buffer
            end = now + timedelta(hours=24)

            raw_items: list[dict[str, Any]] = await self.api.fetch_prices(
                tariff_name=self.tariff_name,
                start=start,
                end=end,
            )

            slots: list[PriceSlot] = []
            for item in raw_items:
                start_ts = item.get("start_timestamp")
                if not isinstance(start_ts, str):
                    continue

                dt_start = dt_util.parse_datetime(start_ts)
                if dt_start is None:
                    continue

                # Ensure timezone-aware in UTC
                dt_start_utc = dt_util.as_utc(dt_start)

                price = self.api.sum_chf_per_kwh(item)
                slots.append(PriceSlot(start=dt_start_utc, price_chf_per_kwh=price))

            # Sort and de-duplicate by start timestamp (API may return overlapping ranges)
            slots.sort(key=lambda s: s.start)
            dedup: dict[datetime, PriceSlot] = {s.start: s for s in slots}

            return list(dedup.values())

        except Exception as err:
            raise UpdateFailed(f"EKZ update failed: {err}") from err
