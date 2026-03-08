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


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


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
            base_map = {s.start: s.components_chf_per_kwh for s in baseline if s.electricity_chf_per_kwh > 0}
            for s in active:
                if s.electricity_chf_per_kwh <= 0:
                    continue
                self.store.set_price_slot(
                    s.start,
                    dyn_components_chf_per_kwh=s.components_chf_per_kwh,
                    base_components_chf_per_kwh=base_map.get(s.start),
                )
            self.store.trim_price_slots(keep_days=7)
            if self.store.dirty:
                await self.store.async_save()

        stats = self._compute_daily_stats(active, baseline)
        self._last_fetch_date = today
        return {
            "active": active,
            "baseline": baseline,
            "stats": stats,
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

        electricity = float(getattr(slot, "electricity_chf_per_kwh", 0.0) or 0.0)
        raw_components = getattr(slot, "components_chf_per_kwh", {}) or {}
        components = {
            str(key): float(value)
            for key, value in raw_components.items()
            if isinstance(value, (int, float))
        }
        if electricity > 0 and "electricity" not in components:
            components["electricity"] = electricity
        if "integrated" not in components:
            total = sum(float(v) for v in components.values() if isinstance(v, (int, float)))
            if total > 0:
                components["integrated"] = total

        return PriceSlot(
            start=dt_util.as_utc(start),
            electricity_chf_per_kwh=electricity,
            components_chf_per_kwh=components,
        )

    @staticmethod
    def _compute_daily_stats(active: list[PriceSlot], baseline: list[PriceSlot]) -> dict[str, Any]:
        active_valid = [s for s in active if s.electricity_chf_per_kwh > 0]
        base_map = {s.start: s.electricity_chf_per_kwh for s in baseline if s.electricity_chf_per_kwh > 0}

        avg_active = _avg([s.electricity_chf_per_kwh for s in active_valid])
        avg_baseline = (
            _avg([base_map[s.start] for s in active_valid if s.start in base_map])
            if base_map
            else None
        )

        dev_vs_avg: dict[str, float] = {}
        dev_vs_baseline: dict[str, float] = {}

        for s in active_valid:
            if avg_active and avg_active > 0:
                dev_vs_avg[s.start.isoformat()] = (s.electricity_chf_per_kwh / avg_active - 1.0) * 100.0
            base = base_map.get(s.start)
            if base and base > 0:
                dev_vs_baseline[s.start.isoformat()] = (s.electricity_chf_per_kwh / base - 1.0) * 100.0

        return {
            "calculated_at": dt_util.utcnow().isoformat(),
            "avg_active_chf_per_kwh": avg_active,
            "avg_baseline_chf_per_kwh": avg_baseline,
            "dev_vs_avg_percent": dev_vs_avg,
            "dev_vs_baseline_percent": dev_vs_baseline,
        }
