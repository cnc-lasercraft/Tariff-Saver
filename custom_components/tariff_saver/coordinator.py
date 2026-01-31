"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi
from .const import CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSlot:
    """A single 15-minute price slot."""
    start: datetime  # UTC, timezone-aware
    price_chf_per_kwh: float


def _align_to_15min(dt: datetime) -> datetime:
    """Align datetime down to 15-min boundary."""
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, list[PriceSlot]]]):
    """Fetches and stores tariff price curves."""

    def __init__(self, hass: HomeAssistant, api: EkzTariffApi, config: dict[str, Any]) -> None:
        self.hass = hass
        self.api = api
        self.tariff_name: str = config["tariff_name"]
        self.baseline_tariff_name: str | None = config.get("baseline_tariff_name")
        self.publish_time: str = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)

        self._last_fetch_date: date | None = None

        super().__init__(
            hass,
            _LOGGER,
            name="Tariff Saver",
            update_interval=None,  # 👈 only manual/daily trigger refreshes
        )

    async def _async_update_data(self) -> dict[str, list[PriceSlot]]:
        """Fetch active and optional baseline price curves (once per day)."""
        today = dt_util.now().date()
        if self._last_fetch_date == today:
            # Already fetched today; return existing data
            return self.data or {"active": [], "baseline": []}

        now = dt_util.utcnow()
        start = _align_to_15min(now)
        end = start + timedelta(hours=24)

        try:
            raw_active = await self.api.fetch_prices(self.tariff_name, start, end)
            active = self._parse_prices(raw_active)
            if not active:
                raise UpdateFailed(f"No data returned for active tariff '{self.tariff_name}'")
        except Exception as err:
            raise UpdateFailed(f"Active tariff update failed: {err}") from err

        data: dict[str, list[PriceSlot]] = {"active": active}

        if self.baseline_tariff_name:
            try:
                raw_base = await self.api.fetch_prices(self.baseline_tariff_name, start, end)
                data["baseline"] = self._parse_prices(raw_base)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to fetch baseline tariff '%s': %s", self.baseline_tariff_name, err)
                data["baseline"] = []
        else:
            data["baseline"] = []

        self._last_fetch_date = today
        return data

    @staticmethod
    def _parse_prices(raw_prices: list[dict[str, Any]]) -> list[PriceSlot]:
        """Parse EKZ price items to sorted, de-duplicated UTC slots."""
        slots: list[PriceSlot] = []

        for item in raw_prices:
            start_ts = item.get("start_timestamp")
            if not isinstance(start_ts, str):
                continue

            dt_start = dt_util.parse_datetime(start_ts)
            if dt_start is None:
                continue

            dt_start_utc = dt_util.as_utc(dt_start)

            price = EkzTariffApi.sum_chf_per_kwh(item)
            slots.append(PriceSlot(start=dt_start_utc, price_chf_per_kwh=price))

        slots.sort(key=lambda s: s.start)
        dedup: dict[datetime, PriceSlot] = {s.start: s for s in slots}
        return list(dedup.values())
