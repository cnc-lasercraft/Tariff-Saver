
"""Binary sensors for Tariff Saver consumer run recommendations."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONSUMER_COUNT, DOMAIN
from .consumer_helpers import build_consumer_plan, get_consumer_config
from .coordinator import TariffSaverCoordinator

def _device_info(entry: ConfigEntry) -> dict[str, Any]:
    return {"identifiers": {(DOMAIN, entry.entry_id)}, "name": entry.title}

def _active_slots(coordinator: TariffSaverCoordinator):
    data = coordinator.data or {}
    return data.get("active", []) if isinstance(data, dict) else []

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TariffSaverCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TariffSaverConsumerShouldRunBinarySensor(coordinator, entry, slot) for slot in range(1, CONSUMER_COUNT + 1)], update_before_add=True)

class TariffSaverConsumerShouldRunBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, slot: int) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.slot = slot
        self._attr_name = f"Consumer {slot} should run"
        self._attr_unique_id = f"{entry.entry_id}_consumer_{slot}_should_run"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool:
        config = get_consumer_config(self.entry, self.slot)
        store = getattr(self.coordinator, "store", None)
        learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
        plan = build_consumer_plan(self.hass, self.entry, config, learning, _active_slots(self.coordinator))
        return bool(plan.get("should_run"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = get_consumer_config(self.entry, self.slot)
        store = getattr(self.coordinator, "store", None)
        learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
        plan = build_consumer_plan(self.hass, self.entry, config, learning, _active_slots(self.coordinator))
        return {
            "slot": self.slot,
            "enabled": config.enabled,
            "name": config.configured_name,
            "status": plan.get("status"),
            "source": plan.get("source"),
            "required_energy_kwh": plan.get("required_energy_kwh"),
            "power_kw": plan.get("power_kw"),
            "duration_minutes": plan.get("duration_minutes"),
            "battery_available_kwh": plan.get("battery_available_kwh"),
            "expected_pv_energy_kwh": plan.get("expected_pv_energy_kwh"),
            "chosen_start": plan.get("chosen_start"),
            "chosen_end": plan.get("chosen_end"),
            "tariff_window_start": plan.get("tariff_window_start"),
            "tariff_window_end": plan.get("tariff_window_end"),
            "tariff_avg_price_chf_per_kwh": plan.get("tariff_avg_price_chf_per_kwh"),
            "last_run_end_utc": plan.get("last_run_end_utc"),
        }
