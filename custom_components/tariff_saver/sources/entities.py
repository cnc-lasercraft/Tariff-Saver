"""Entity-backed slot source."""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from ..const import (
    CONF_BASELINE_ATTRIBUTE,
    CONF_BASELINE_ENTITY,
    CONF_IGNORE_ZERO_PRICES,
    CONF_PRICE_ATTRIBUTE,
    CONF_PRICE_ENTITY,
    CONF_PRICE_SCALE,
)
from ..models import PriceSlot
from ..slot_helpers import normalize_slots, slot_list_from_state
from .base import SlotSource


class EntitySlotSource(SlotSource):
    """Read slot lists from existing Home Assistant entities."""

    async def async_get_price_slots(self) -> list[PriceSlot]:
        entity_id = self.config.get(CONF_PRICE_ENTITY)
        if not isinstance(entity_id, str) or not entity_id:
            raise HomeAssistantError("Missing price entity for entity source")
        state = self.hass.states.get(entity_id)
        if state is None:
            raise HomeAssistantError(f"Price entity not found: {entity_id}")
        raw = slot_list_from_state(state, self.config.get(CONF_PRICE_ATTRIBUTE))
        return normalize_slots(
            raw,
            price_scale=float(self.config.get(CONF_PRICE_SCALE, 1.0) or 1.0),
            ignore_zero_prices=bool(self.config.get(CONF_IGNORE_ZERO_PRICES, True)),
        )

    async def async_get_baseline_slots(self) -> list[PriceSlot]:
        entity_id = self.config.get(CONF_BASELINE_ENTITY)
        if not isinstance(entity_id, str) or not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if state is None:
            return []
        raw = slot_list_from_state(state, self.config.get(CONF_BASELINE_ATTRIBUTE))
        return normalize_slots(
            raw,
            price_scale=float(self.config.get(CONF_PRICE_SCALE, 1.0) or 1.0),
            ignore_zero_prices=bool(self.config.get(CONF_IGNORE_ZERO_PRICES, True)),
        )

    async def async_get_metadata(self) -> dict[str, str | None]:
        return {
            "source_type": "entities",
            "price_entity": self.config.get(CONF_PRICE_ENTITY),
            "baseline_entity": self.config.get(CONF_BASELINE_ENTITY),
        }
