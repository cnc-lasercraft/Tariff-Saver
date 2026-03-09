"""EKZ-backed slot source.

This adapter intentionally does not talk to EKZ directly.
It only reads data already exposed by the separate ha-ekz-tariff integration.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..models import PriceSlot
from .base import SlotSource


class EkzSlotSource(SlotSource):
    """Read slots from the standalone EKZ Tariff provider integration."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

    def _get_provider(self) -> dict[str, Any]:
        """Return provider payload from ekz_tariff helper API."""
        try:
            from custom_components.ekz_tariff import (
                get_first_provider_data,
                get_provider_data,
            )
        except Exception as err:
            raise UpdateFailed(
                "Could not import EKZ Tariff provider helpers from custom_components.ekz_tariff"
            ) from err

        entry_id = self.config.get("ekz_entry_id")
        try:
            if isinstance(entry_id, str) and entry_id.strip():
                return get_provider_data(self.hass, entry_id.strip())
            return get_first_provider_data(self.hass)
        except KeyError as err:
            raise UpdateFailed("No loaded EKZ Tariff entry found") from err

    async def async_get_price_slots(self) -> list[PriceSlot]:
        """Return dynamic tariff slots from EKZ provider."""
        provider = self._get_provider()

        raw_slots = provider.get("active_slots")
        if not isinstance(raw_slots, list) or not raw_slots:
            raise UpdateFailed("EKZ Tariff provider returned no active_slots")

        return [self._convert_slot(slot) for slot in raw_slots]

    async def async_get_baseline_slots(self) -> list[PriceSlot]:
        """Return baseline tariff slots from EKZ provider."""
        provider = self._get_provider()

        raw_slots = provider.get("baseline_slots")
        if not isinstance(raw_slots, list) or not raw_slots:
            return []

        return [self._convert_slot(slot) for slot in raw_slots]

    async def async_get_metadata(self) -> dict[str, Any]:
        """Return optional source metadata."""
        provider = self._get_provider()
        return {
            "source_type": "ekz",
            "provider": provider.get("provider"),
            "entry_id": provider.get("entry_id"),
            "active_publication_timestamp": provider.get("active_publication_timestamp"),
            "baseline_publication_timestamp": provider.get("baseline_publication_timestamp"),
            "baseline_tariff_name": provider.get("baseline_tariff_name"),
            "link_status": provider.get("link_status"),
            "linking_url": provider.get("linking_url"),
            "last_api_success_utc": provider.get("last_api_success_utc"),
        }

    def _convert_slot(self, slot: Any) -> PriceSlot:
        """Convert EKZ provider PriceSlot object to Tariff Saver PriceSlot."""
        start = getattr(slot, "start", None)
        if start is None:
            raise UpdateFailed("EKZ Tariff slot is missing start")

        electricity = float(getattr(slot, "electricity_chf_per_kwh", 0.0) or 0.0)

        comps = getattr(slot, "components_chf_per_kwh", None) or {}
        if not isinstance(comps, dict):
            comps = {}

        grid = float(comps.get("grid_incl_fees", comps.get("grid", 0.0)) or 0.0)
        regional_fees = float(comps.get("regional_fees", 0.0) or 0.0)

        integrated = electricity + grid + regional_fees

        return PriceSlot(
            start=start,
            electricity=electricity,
            grid=grid,
            regional_fees=regional_fees,
            integrated=float(integrated),
            components={str(k): float(v) for k, v in comps.items() if isinstance(v, (int, float))},
        )
