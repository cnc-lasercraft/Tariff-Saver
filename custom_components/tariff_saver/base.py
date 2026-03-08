"""Base slot source interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from homeassistant.core import HomeAssistant

from ..models import PriceSlot


class SlotSource(ABC):
    """Abstract source of normalized tariff slots."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

    @abstractmethod
    async def async_get_price_slots(self) -> list[PriceSlot]:
        """Return dynamic price slots."""

    @abstractmethod
    async def async_get_baseline_slots(self) -> list[PriceSlot]:
        """Return baseline price slots."""

    async def async_get_metadata(self) -> dict[str, Any]:
        """Return source-specific diagnostics."""
        return {}
