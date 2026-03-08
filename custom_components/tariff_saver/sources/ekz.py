"""EKZ-backed slot source.

This adapter intentionally does not talk to EKZ directly.
It only reads data already exposed by the separate ha-ekz-tariff integration.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from ..const import EKZ_DOMAIN
from ..models import PriceSlot
from ..slot_helpers import normalize_slots
from .base import SlotSource


class EkzSlotSource(SlotSource):
    """Read slots from the standalone EKZ tariff provider integration."""

    def _iter_candidates(self, value: Any, seen: set[int] | None = None, depth: int = 0):
        """Walk nested hass.data structures and yield possible provider objects."""
        if value is None or depth > 6:
            return
        if seen is None:
            seen = set()
        obj_id = id(value)
        if obj_id in seen:
            return
        seen.add(obj_id)

        yield value

        if isinstance(value, Mapping):
            for nested in value.values():
                yield from self._iter_candidates(nested, seen, depth + 1)
            return

        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for nested in value:
                yield from self._iter_candidates(nested, seen, depth + 1)
            return

        for attr in (
            "coordinator",
            "api",
            "client",
            "entry",
            "provider",
            "source",
            "data",
            "runtime_data",
        ):
            if hasattr(value, attr):
                try:
                    nested = getattr(value, attr)
                except Exception:
                    continue
                yield from self._iter_candidates(nested, seen, depth + 1)

    def _extract_raw_slots(self, candidate: Any, field_name: str) -> list[Any] | None:
        """Try many common provider/coordinator layouts."""
        if candidate is None:
            return None

        if isinstance(candidate, Mapping):
            raw = candidate.get(field_name)
            if isinstance(raw, list):
                return raw
            data = candidate.get("data")
            if isinstance(data, Mapping):
                raw = data.get(field_name)
                if isinstance(raw, list):
                    return raw
            coordinator = candidate.get("coordinator")
            if coordinator is not None:
                raw = self._extract_raw_slots(coordinator, field_name)
                if raw is not None:
                    return raw
            return None

        raw = getattr(candidate, field_name, None)
        if isinstance(raw, list):
            return raw

        data = getattr(candidate, "data", None)
        if isinstance(data, Mapping):
            raw = data.get(field_name)
            if isinstance(raw, list):
                return raw

        return None

    def _find_raw_slots(self, field_name: str) -> list[Any] | None:
        domain_data = self.hass.data.get(EKZ_DOMAIN)
        if domain_data is None:
            return None

        for candidate in self._iter_candidates(domain_data):
            raw = self._extract_raw_slots(candidate, field_name)
            if isinstance(raw, list) and raw:
                return raw
        return None

    async def async_get_price_slots(self) -> list[PriceSlot]:
        raw_slots = self._find_raw_slots("slots_today")
        if raw_slots is None:
            raise HomeAssistantError(
                "EKZ Tariff provider does not expose slots_today in hass.data; "
                "check sources/ekz.py against the provider's real runtime structure"
            )

        slots = normalize_slots(raw_slots, price_scale=1.0, ignore_zero_prices=True)
        if slots:
            return slots

        raise HomeAssistantError("EKZ Tariff slots_today was found but could not be parsed")

    async def async_get_baseline_slots(self) -> list[PriceSlot]:
        raw_slots = self._find_raw_slots("baseline_slots")
        if raw_slots is None:
            return []
        return normalize_slots(raw_slots, price_scale=1.0, ignore_zero_prices=True)

    async def async_get_metadata(self) -> dict[str, Any]:
        return {
            "source_type": "ekz",
            "provider_domain": EKZ_DOMAIN,
            "slots_today_found": self._find_raw_slots("slots_today") is not None,
            "baseline_slots_found": self._find_raw_slots("baseline_slots") is not None,
        }
