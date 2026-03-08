"""EKZ-backed slot source.

This adapter intentionally does not talk to EKZ directly.
It only reads data already exposed by the separate ha-ekz-tariff integration.
"""
from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from ..const import EKZ_DOMAIN
from ..models import PriceSlot
from .base import SlotSource


class EkzSlotSource(SlotSource):
    """Read slots from the standalone EKZ tariff provider integration."""

    def _find_provider_coordinator(self) -> Any:
        domain_data = self.hass.data.get(EKZ_DOMAIN)
        if not isinstance(domain_data, dict):
            return None
        for value in domain_data.values():
            if hasattr(value, "data") or hasattr(value, "slots_today") or hasattr(value, "baseline_slots"):
                return value
        return None

    @staticmethod
    def _from_provider_slot(slot: Any) -> PriceSlot | None:
        start = getattr(slot, "start", None)
        if start is None:
            return None
        start = dt_util.as_utc(start)

        components = getattr(slot, "components_chf_per_kwh", None)
        if not isinstance(components, dict):
            components = {}

        electricity = getattr(slot, "electricity", None)
        if not isinstance(electricity, (int, float)):
            electricity = getattr(slot, "electricity_chf_per_kwh", None)
        if not isinstance(electricity, (int, float)):
            electricity = float(components.get("electricity", 0.0) or 0.0)

        grid = getattr(slot, "grid", None)
        if not isinstance(grid, (int, float)):
            grid = float(components.get("grid", 0.0) or 0.0)

        regional_fees = getattr(slot, "regional_fees", None)
        if not isinstance(regional_fees, (int, float)):
            regional_fees = float(components.get("regional_fees", 0.0) or 0.0)

        integrated = getattr(slot, "integrated", None)
        if not isinstance(integrated, (int, float)):
            integrated = float(components.get("integrated", 0.0) or 0.0)

        return PriceSlot(
            start=start,
            electricity=float(electricity or 0.0),
            grid=float(grid or 0.0),
            regional_fees=float(regional_fees or 0.0),
            integrated=float(integrated or 0.0),
            components={str(k): float(v) for k, v in components.items() if isinstance(v, (int, float))},
        )

    def _convert_slots(self, raw_slots: Any) -> list[PriceSlot]:
        if not isinstance(raw_slots, list):
            return []
        slots: dict[str, PriceSlot] = {}
        for raw in raw_slots:
            slot = self._from_provider_slot(raw)
            if slot is None:
                continue
            if slot.electricity_chf_per_kwh <= 0 and slot.integrated <= 0:
                continue
            slots[slot.start.isoformat()] = slot
        return [slots[key] for key in sorted(slots)]

    async def async_get_price_slots(self) -> list[PriceSlot]:
        provider = self._find_provider_coordinator()
        if provider is None:
            raise HomeAssistantError("EKZ Tariff provider integration not found")

        data = getattr(provider, "data", None)
        if isinstance(data, dict) and isinstance(data.get("slots_today"), list):
            return self._convert_slots(data.get("slots_today"))

        slots_today = getattr(provider, "slots_today", None)
        if isinstance(slots_today, list):
            return self._convert_slots(slots_today)

        raise HomeAssistantError("EKZ Tariff provider does not expose slots_today")

    async def async_get_baseline_slots(self) -> list[PriceSlot]:
        provider = self._find_provider_coordinator()
        if provider is None:
            return []

        data = getattr(provider, "data", None)
        if isinstance(data, dict) and isinstance(data.get("baseline_slots"), list):
            return self._convert_slots(data.get("baseline_slots"))

        baseline_slots = getattr(provider, "baseline_slots", None)
        if isinstance(baseline_slots, list):
            return self._convert_slots(baseline_slots)

        return []

    async def async_get_metadata(self) -> dict[str, Any]:
        provider = self._find_provider_coordinator()
        return {
            "source_type": "ekz",
            "provider_domain": EKZ_DOMAIN,
            "provider_found": provider is not None,
        }
