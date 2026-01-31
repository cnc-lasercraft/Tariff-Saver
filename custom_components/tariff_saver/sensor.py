"""Sensor platform for Tariff Saver."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import TariffSaverCoordinator, PriceSlot


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator: TariffSaverCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TariffSaverPriceNowSensor(coordinator, entry)], update_before_add=True)


class TariffSaverPriceNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Shows the current electricity price (CHF/kWh)."""

    _attr_has_entity_name = True
    _attr_name = "Price now"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:currency-chf"
    _attr_translation_key = None  # keep simple for now

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_price_now"

    @property
    def native_value(self) -> float | None:
        """Return the current price based on the latest slot <= now."""
        slots = self.coordinator.data or []
        if not slots:
            return None

        now = dt_util.utcnow()
        # pick the latest slot starting at or before now
        current: PriceSlot | None = None
        for s in slots:
            if s.start <= now:
                current = s
            else:
                break

        return round(current.price_chf_per_kwh, 6) if current else round(slots[0].price_chf_per_kwh, 6)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose useful attributes for charts/analysis."""
        slots = self.coordinator.data or []
        now = dt_util.utcnow()

        next_slot: PriceSlot | None = None
        for s in slots:
            if s.start > now:
                next_slot = s
                break

        # Provide the next 24h as a compact list for dashboards
        slots_attr = [
            {
                "start": s.start.isoformat(),
                "price_chf_per_kwh": round(s.price_chf_per_kwh, 6),
            }
            for s in slots
        ]

        return {
            "tariff_name": self.coordinator.tariff_name,
            "next_start": next_slot.start.isoformat() if next_slot else None,
            "next_price_chf_per_kwh": round(next_slot.price_chf_per_kwh, 6) if next_slot else None,
            "slots": slots_attr,
            "slot_count": len(slots),
            "updated_at": dt_util.utcnow().isoformat(),
        }
