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
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches and stores tariff price curves + daily derived stats."""

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
            update_interval=None,  # daily trigger will call refresh
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch active & baseline once per day, then compute daily stats."""
        today = dt_util.now().date()
        if self._last_fetch_date == today:
            return self.data or {"active": [], "baseline": [], "stats": {}}

        try:
            # 🔹 KEY CHANGE:
            # Call API WITHOUT start/end → EKZ returns tariffs of the current date (00:00–24:00)
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
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to fetch baseline tariff '%s': %s",
                    self.baseline_tariff_name,
                    err,
                )
                baseline = []

        stats = self._compute_daily_stats(active, baseline)

        self._last_fetch_date = today
        return {"active": active, "baseline": baseline, "stats": stats}

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

    @staticmethod
    def _compute_daily_stats(active: list[PriceSlot], baseline: list[PriceSlot]) -> dict[str, Any]:
        """Compute daily averages and per-slot deviations."""
        # ignore unpublished/invalid 0 prices
        active_valid = [s for s in active if s.price_chf_per_kwh > 0]
        base_map = {s.start: s.price_chf_per_kwh for s in baseline if s.price_chf_per_kwh > 0}

        avg_active = _avg([s.price_chf_per_kwh for s in active_valid])

        avg_baseline = None
        if base_map:
            common = [base_map.get(s.start) for s in active_valid if s.start in base_map]
            common_vals = [v for v in common if isinstance(v, float)]
            avg_baseline = _avg(common_vals)

        dev_vs_avg: dict[str, float] = {}
        dev_vs_baseline: dict[str, float] = {}

        for s in active_valid:
            if avg_active and avg_active > 0:
                dev_vs_avg[s.start.isoformat()] = (s.price_chf_per_kwh / avg_active - 1.0) * 100.0

            base = base_map.get(s.start)
            if base and base > 0:
                dev_vs_baseline[s.start.isoformat()] = (s.price_chf_per_kwh / base - 1.0) * 100.0

        return {
            "calculated_at": dt_util.utcnow().isoformat(),
            "avg_active_chf_per_kwh": avg_active,
            "avg_baseline_chf_per_kwh": avg_baseline,
            "dev_vs_avg_percent": dev_vs_avg,
            "dev_vs_baseline_percent": dev_vs_baseline,
        }
