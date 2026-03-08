"""EKZ-backed slot source.

This adapter intentionally does not talk to EKZ directly.
It only reads data already exposed by the separate ha-ekz-tariff integration.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from ..models import PriceSlot
from ..slot_helpers import normalize_slots
from .base import SlotSource


class EkzSlotSource(SlotSource):
    """Read slots from the standalone EKZ tariff provider integration."""

    def _unwrap_payload(self, obj: Any) -> dict[str, Any] | None:
        """Try to unwrap the actual provider payload dict."""
        if obj is None:
            return None

        if isinstance(obj, Mapping):
            if isinstance(obj.get("slots_today"), list) or isinstance(obj.get("baseline_slots"), list):
                return dict(obj)

            for key in ("data", "provider_data", "payload", "coordinator_data"):
                nested = obj.get(key)
                unwrapped = self._unwrap_payload(nested)
                if unwrapped is not None:
                    return unwrapped

        for attr in ("data", "provider_data", "payload", "coordinator_data"):
            nested = getattr(obj, attr, None)
            unwrapped = self._unwrap_payload(nested)
            if unwrapped is not None:
                return unwrapped

        return None

    def _get_provider_payload(self) -> dict[str, Any] | None:
        """Return provider payload from the EKZ integration helper API."""
        try:
            from custom_components.ekz_tariff import (  # type: ignore
                get_first_provider_data,
                get_provider_data,
            )
        except Exception as err:  # pragma: no cover - import depends on runtime HA setup
            raise HomeAssistantError(
                "Could not import EKZ Tariff provider helpers from custom_components.ekz_tariff"
            ) from err

        entry_id = self.config.get("ekz_entry_id")
        provider_obj = None

        if isinstance(entry_id, str) and entry_id.strip():
            provider_obj = get_provider_data(self.hass, entry_id.strip())
        else:
            provider_obj = get_first_provider_data(self.hass)

        return self._unwrap_payload(provider_obj)

    async def async_get_price_slots(self) -> list[PriceSlot]:
        provider_data = self._get_provider_payload()
        if provider_data is None:
            raise HomeAssistantError("No EKZ Tariff provider data available")

        raw_slots = provider_data.get("slots_today")
        if not isinstance(raw_slots, list):
            raise HomeAssistantError(
                "EKZ Tariff provider payload does not contain slots_today"
            )

        slots = normalize_slots(raw_slots, price_scale=1.0, ignore_zero_prices=True)
        if slots:
            return slots

        raise HomeAssistantError("EKZ Tariff slots_today was found but could not be parsed")

    async def async_get_baseline_slots(self) -> list[PriceSlot]:
        provider_data = self._get_provider_payload()
        if provider_data is None:
            return []

        raw_slots = provider_data.get("baseline_slots")
        if not isinstance(raw_slots, list):
            return []

        return normalize_slots(raw_slots, price_scale=1.0, ignore_zero_prices=True)

    async def async_get_metadata(self) -> dict[str, Any]:
        provider_data = self._get_provider_payload()
        if provider_data is None:
            return {
                "source_type": "ekz",
                "provider_available": False,
            }

        return {
            "source_type": "ekz",
            "provider_available": True,
            "slots_today_found": isinstance(provider_data.get("slots_today"), list),
            "baseline_slots_found": isinstance(provider_data.get("baseline_slots"), list),
            "publication_timestamp": provider_data.get("publication_timestamp"),
            "baseline_publication_timestamp": provider_data.get("baseline_publication_timestamp"),
            "link_status": provider_data.get("link_status"),
        }
