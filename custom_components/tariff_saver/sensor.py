"""Sensor platform for Tariff Saver."""
from __future__ import annotations

from typing import Any
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import TariffSaverCoordinator, PriceSlot


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _active_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("active", []) if isinstance(data, dict) else []


def _baseline_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("baseline", []) if isinstance(data, dict) else []


# -------------------------------------------------------------------
# Setup
# -------------------------------------------------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator: TariffSaverCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            TariffSaverPriceCurveSensor(coordinator, entry),
            TariffSaverPriceNowSensor(coordinator, entry),
            TariffSaverNextPriceSensor(coordinator, entry),
            TariffSaverSavingsNext24hSensor(coordinator, entry),
            TariffSaverCheapestWindowsSensor(coordinator, entry),
        ],
        update_before_add=True,
    )


# -------------------------------------------------------------------
# Sensors
# -------------------------------------------------------------------
class TariffSaverPriceCurveSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Active price curve as attributes."""

    _attr_has_entity_name = True
    _attr_name = "Price curve"
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_curve"

    @property
    def native_value(self) -> int | None:
        slots = _active_slots(self.coordinator)
        return len(slots) if slots else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = _active_slots(self.coordinator)
        return {
            "tariff_name": self.coordinator.tariff_name,
            "baseline_tariff_name": self.coordinator.baseline_tariff_name,
            "slot_count": len(slots),
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "price_chf_per_kwh": round(s.price_chf_per_kwh, 6),
                }
                for s in slots
            ],
        }


class TariffSaverPriceNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Current electricity price (active tariff)."""

    _attr_has_entity_name = True
    _attr_name = "Price now"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:currency-chf"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_now"

    @property
    def native_value(self) -> float | None:
        slots = _active_slots(self.coordinator)
        if not slots:
            return None

        now = dt_util.utcnow()
        current: PriceSlot | None = None
        for s in slots:
            if s.start <= now:
                current = s
            else:
                break

        return round((current or slots[0]).price_chf_per_kwh, 6)


class TariffSaverNextPriceSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Next electricity price (active tariff)."""

    _attr_has_entity_name = True
    _attr_name = "Next price"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_next"

    @property
    def native_value(self) -> float | None:
        slots = _active_slots(self.coordinator)
        if not slots:
            return None

        now = dt_util.utcnow()
        for s in slots:
            if s.start > now:
                return round(s.price_chf_per_kwh, 6)
        return None


class TariffSaverSavingsNext24hSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Estimated savings for next 24h vs baseline (CHF) assuming constant 1 kW load."""

    _attr_has_entity_name = True
    _attr_name = "Savings next 24h"
    _attr_native_unit_of_measurement = "CHF"
    _attr_icon = "mdi:piggy-bank-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_savings_next24h"

    @property
    def native_value(self) -> float | None:
        active = _active_slots(self.coordinator)
        baseline = _baseline_slots(self.coordinator)
        if not active or not baseline:
            return None

        base_map = {s.start: s.price_chf_per_kwh for s in baseline}
        kwh_per_slot = 0.25  # 15 minutes

        savings = 0.0
        matched = 0
        for s in active:
            base = base_map.get(s.start)
            if base is None:
                continue
            savings += (base - s.price_chf_per_kwh) * kwh_per_slot
            matched += 1

        return round(savings, 2) if matched else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "tariff_name": self.coordinator.tariff_name,
            "baseline_tariff_name": self.coordinator.baseline_tariff_name,
            "assumption": "1 kW constant load (0.25 kWh per 15 min)",
        }


class TariffSaverCheapestWindowsSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Cheapest windows for 30m/1h/2h/3h (based on active price curve)."""

    _attr_has_entity_name = True
    _attr_name = "Cheapest windows"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cheapest_windows"

    @staticmethod
    def _best_window(
        slots: list[PriceSlot],
        baseline_map: dict,
        window_slots: int,
    ) -> dict[str, Any] | None:
        if len(slots) < window_slots:
            return None

        best_sum = float("inf")
        best_start = None
        best_end = None
        best_savings = None

        kwh_per_slot = 0.25

        for i in range(len(slots) - window_slots + 1):
            window = slots[i : i + window_slots]
            window_sum = sum(x.price_chf_per_kwh for x in window)

            if window_sum < best_sum:
                best_sum = window_sum
                best_start = window[0].start
                best_end = window[-1].start + timedelta(minutes=15)

                if baseline_map:
                    save = 0.0
                    matched = 0
                    for x in window:
                        base = baseline_map.get(x.start)
                        if base is None:
                            continue
                        save += (base - x.price_chf_per_kwh) * kwh_per_slot
                        matched += 1
                    best_savings = save if matched else None

        avg_chf = best_sum / window_slots
        avg_rp = avg_chf * 100

        result: dict[str, Any] = {
            "start": best_start.isoformat(),
            "end": best_end.isoformat(),
            "avg_chf_per_kwh": round(avg_chf, 6),
            "avg_rp_per_kwh": round(avg_rp, 2),
        }
        if best_savings is not None:
            result["savings_vs_baseline_chf"] = round(best_savings, 2)

        return result

    @property
    def native_value(self) -> float | None:
        """Use best 1h avg (CHF/kWh) as state so it is never 'unknown' when data exists."""
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return None
        best_1h = self._best_window(slots, {}, 4)  # 4 * 15min = 1h
        return best_1h["avg_chf_per_kwh"] if best_1h else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        baseline = _baseline_slots(self.coordinator)
        baseline_map = {s.start: s.price_chf_per_kwh for s in baseline} if baseline else {}

        return {
            "tariff_name": self.coordinator.tariff_name,
            "baseline_tariff_name": self.coordinator.baseline_tariff_name,
            "best_30m": self._best_window(slots, baseline_map, 2),
            "best_1h": self._best_window(slots, baseline_map, 4),
            "best_2h": self._best_window(slots, baseline_map, 8),
            "best_3h": self._best_window(slots, baseline_map, 12),
        }
