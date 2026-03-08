"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_SOURCE_TYPE, DOMAIN, SOURCE_EKZ, SOURCE_ENTITIES
from .models import PriceSlot
from .slot_helpers import avg
from .sources.base import SlotSource
from .sources.ekz import EkzSlotSource
from .sources.entities import EntitySlotSource
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch and persist price curves from a selected slot source."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config
        self.source_type: str = str(config.get(CONF_SOURCE_TYPE, SOURCE_EKZ))
        self.source: SlotSource = self._build_source()
        self._last_fetch_date: date | None = None
        self.store: TariffSaverStore | None = None
        self.source_status: str | None = None
        self.source_details: dict[str, Any] = {}

        super().__init__(hass, _LOGGER, name="Tariff Saver", update_interval=None)

    def _build_source(self) -> SlotSource:
        if self.source_type == SOURCE_ENTITIES:
            return EntitySlotSource(self.hass, self.config)
        return EkzSlotSource(self.hass, self.config)

    async def _async_update_data(self) -> dict[str, Any]:
        if self.store is None:
            for entry_id, coord in self.hass.data.get(DOMAIN, {}).items():
                if coord is self:
                    self.store = TariffSaverStore(self.hass, entry_id)
                    await self.store.async_load()
                    break

        today = dt_util.now().date()
        if self._last_fetch_date == today:
            return self.data or {"active": [], "baseline": [], "stats": {}, "source": {}}

        try:
            active = await self.source.async_get_price_slots()
            baseline = await self.source.async_get_baseline_slots()
            self.source_details = await self.source.async_get_metadata()
            self.source_status = "ok"
        except Exception as err:
            self.source_status = "error"
            self.source_details = {"error": str(err), "source_type": self.source_type}
            raise UpdateFailed(f"Slot source update failed: {err}") from err

        if not active:
            raise UpdateFailed("Slot source returned no active price slots")

        if self.store is not None:
            self.store.set_last_api_success(dt_util.utcnow())
            base_map = {
                slot.start: slot.components_chf_per_kwh
                for slot in baseline
                if slot.electricity_chf_per_kwh > 0 or slot.integrated > 0
            }
            for slot in active:
                if slot.electricity_chf_per_kwh <= 0 and slot.integrated <= 0:
                    continue
                self.store.set_price_slot(
                    slot.start,
                    dyn_components_chf_per_kwh=slot.components_chf_per_kwh,
                    base_components_chf_per_kwh=base_map.get(slot.start),
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
            "source": {"status": self.source_status, **self.source_details},
        }

    @staticmethod
    def _compute_daily_stats(active: list[PriceSlot], baseline: list[PriceSlot]) -> dict[str, Any]:
        active_valid = [s for s in active if s.electricity_chf_per_kwh > 0]
        base_map = {s.start: s.electricity_chf_per_kwh for s in baseline if s.electricity_chf_per_kwh > 0}

        avg_active = avg([s.electricity_chf_per_kwh for s in active_valid])
        avg_baseline = (
            avg([base_map[s.start] for s in active_valid if s.start in base_map])
            if base_map
            else None
        )

        dev_vs_avg: dict[str, float] = {}
        dev_vs_baseline: dict[str, float] = {}

        for slot in active_valid:
            if avg_active and avg_active > 0:
                dev_vs_avg[slot.start.isoformat()] = (slot.electricity_chf_per_kwh / avg_active - 1.0) * 100.0
            base = base_map.get(slot.start)
            if base and base > 0:
                dev_vs_baseline[slot.start.isoformat()] = (slot.electricity_chf_per_kwh / base - 1.0) * 100.0

        return {
            "calculated_at": dt_util.utcnow().isoformat(),
            "avg_active_chf_per_kwh": avg_active,
            "avg_baseline_chf_per_kwh": avg_baseline,
            "dev_vs_avg_percent": dev_vs_avg,
            "dev_vs_baseline_percent": dev_vs_baseline,
        }
