"""Coordinator for Tariff Saver (Public + myEKZ linking).

Fix:
- Parse EKZ list-form component fields (unit CHF_kWh) into components map.
- Electricity (CHF/kWh) is used for existing "price now" and grading.
- Components are persisted in store for cost breakdown.

IMPORTANT:
- No entity renames.
- Public behavior intact.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi
from .const import (
    DOMAIN,
    CONF_PUBLISH_TIME,
    DEFAULT_PUBLISH_TIME,
    CONF_MODE,
    MODE_MYEKZ,
    CONF_EMS_INSTANCE_ID,
    CONF_REDIRECT_URI,
)
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
    """Fetches and stores tariff price curves + derived stats."""

    def __init__(self, hass: HomeAssistant, api: EkzTariffApi, config: dict[str, Any]) -> None:
        self.hass = hass
        self.api = api

        self.tariff_name: str = config.get("tariff_name", "myEKZ")
        self.baseline_tariff_name: str | None = config.get("baseline_tariff_name")
        self.publish_time: str = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
        self.mode: str = config.get(CONF_MODE, "public")

        self.ems_instance_id: str | None = config.get(CONF_EMS_INSTANCE_ID)
        self.redirect_uri: str | None = config.get(CONF_REDIRECT_URI)

        self._last_fetch_date: date | None = None
        self.store: TariffSaverStore | None = None

        super().__init__(hass, _LOGGER, name="Tariff Saver", update_interval=None)

    async def _async_update_data(self) -> dict[str, Any]:
        # Lazy init store (bind to entry_id)
        if self.store is None:
            for entry_id, coord in self.hass.data.get(DOMAIN, {}).items():
                if coord is self:
                    self.store = TariffSaverStore(self.hass, entry_id)
                    await self.store.async_load()
                    break

        today = dt_util.now().date()
        if self._last_fetch_date == today:
            return self.data or {"active": [], "baseline": [], "stats": {}, "myekz": {}}

        # myEKZ mode: only linking status for now
        if self.mode == MODE_MYEKZ:
            if not self.ems_instance_id or not self.redirect_uri:
                raise UpdateFailed("myEKZ mode requires ems_instance_id and redirect_uri.")

            try:
                status = await self.api.fetch_ems_link_status(
                    ems_instance_id=self.ems_instance_id,
                    redirect_uri=self.redirect_uri,
                )
            except Exception as err:
                raise UpdateFailed(f"myEKZ emsLinkStatus failed: {err}") from err

            self._last_fetch_date = today
            return {"active": [], "baseline": [], "stats": {}, "myekz": status}

        # Public mode
        try:
            raw_active = await self.api.fetch_prices(self.tariff_name)
            active = self._parse_prices(raw_active)
            if not active:
                raise UpdateFailed(f"No data returned for active tariff '{self.tariff_name}'")
        except Exception as err:
            raise UpdateFailed(f"Active tariff update failed: {err}") from err

        if self.store is not None:
            self.store.set_last_api_success(dt_util.utcnow())

        baseline: list[PriceSlot] = []
        if self.baseline_tariff_name:
            try:
                raw_base = await self.api.fetch_prices(self.baseline_tariff_name)
                baseline = self._parse_prices(raw_base)
            except Exception as err:
                _LOGGER.warning("Failed to fetch baseline tariff '%s': %s", self.baseline_tariff_name, err)
                baseline = []

        # Persist price slots (per-component)
        if self.store is not None:
            base_map = {s.start: s.components_chf_per_kwh for s in baseline if s.electricity_chf_per_kwh > 0}

            for s in active:
                if s.electricity_chf_per_kwh <= 0:
                    continue
                base_comps = base_map.get(s.start)
                api_integrated = None
                if isinstance(s.components_chf_per_kwh.get("integrated"), (int, float)):
                    api_integrated = float(s.components_chf_per_kwh["integrated"])
                self.store.set_price_slot(
                    s.start,
                    dyn_components_chf_per_kwh=s.components_chf_per_kwh,
                    base_components_chf_per_kwh=base_comps,
                    api_integrated=api_integrated,
                )

            self.store.trim_price_slots(keep_days=7)
            if self.store.dirty:
                await self.store.async_save()

        stats = self._compute_daily_stats(active, baseline)
        self._last_fetch_date = today
        return {"active": active, "baseline": baseline, "stats": stats, "myekz": {}}

    # ---------------- Helpers ----------------
    def _parse_prices(self, raw_prices: list[dict[str, Any]]) -> list[PriceSlot]:
        slots: list[PriceSlot] = []
        for item in raw_prices:
            start_ts = item.get("start_timestamp")
            if not isinstance(start_ts, str):
                continue
            dt_start = dt_util.parse_datetime(start_ts)
            if dt_start is None:
                continue

            comps = EkzTariffApi.parse_components_chf_per_kwh(item)
            elec = float(comps.get("electricity", 0.0) or 0.0)

            slots.append(
                PriceSlot(
                    start=dt_util.as_utc(dt_start),
                    electricity_chf_per_kwh=elec,
                    components_chf_per_kwh=comps,
                )
            )

        # de-duplicate by slot start
        out = {s.start: s for s in sorted(slots, key=lambda s: s.start)}
        return list(out.values())

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
