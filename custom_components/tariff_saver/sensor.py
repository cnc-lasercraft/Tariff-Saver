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


def _grade_from_dev(dev: float) -> int:
    """Map deviation vs daily average to grade 1..5."""
    if dev <= -20:
        return 1
    if dev <= -10:
        return 2
    if dev <= 10:
        return 3
    if dev <= 25:
        return 4
    return 5


def _label_from_grade(grade: int) -> str:
    return {
        1: "sehr günstig",
        2: "günstig",
        3: "durchschnitt",
        4: "teuer",
        5: "sehr teuer",
    }.get(grade, "unbekannt")


def _stars_from_grade(grade: int | None) -> str:
    if grade is None or grade < 1 or grade > 5:
        return "—"
    return "⭐" * (6 - grade)


# -------------------------------------------------------------------
# Setup
# -------------------------------------------------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TariffSaverCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            TariffSaverPriceCurveSensor(coordinator, entry),
            TariffSaverPriceNowSensor(coordinator, entry),
            TariffSaverNextPriceSensor(coordinator, entry),
            TariffSaverSavingsNext24hSensor(coordinator, entry),
            TariffSaverCheapestWindowsSensor(coordinator, entry),
            TariffSaverTariffGradeSensor(coordinator, entry),
            TariffSaverTariffStarsNowSensor(coordinator, entry),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 1),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 2),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 3),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 6),
        ],
        update_before_add=True,
    )


# -------------------------------------------------------------------
# Sensors
# -------------------------------------------------------------------
class TariffSaverPriceCurveSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
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
            "slot_count": len(slots),
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "price_chf_per_kwh": s.price_chf_per_kwh,
                }
                for s in slots
            ],
        }


class TariffSaverPriceNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
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
        current = None
        for s in slots:
            if s.start <= now:
                current = s
            else:
                break

        return (current or slots[0]).price_chf_per_kwh


class TariffSaverNextPriceSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
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
        now = dt_util.utcnow()
        for s in slots:
            if s.start > now:
                return s.price_chf_per_kwh
        return None


class TariffSaverSavingsNext24hSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
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
        kwh_per_slot = 0.25

        savings = 0.0
        matched = 0
        for s in active:
            base = base_map.get(s.start)
            if base is None:
                continue
            savings += (base - s.price_chf_per_kwh) * kwh_per_slot
            matched += 1

        return round(savings, 2) if matched else None


class TariffSaverCheapestWindowsSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Cheapest windows"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cheapest_windows"

    @property
    def native_value(self) -> float | None:
        slots = _active_slots(self.coordinator)
        valid = [s.price_chf_per_kwh for s in slots if s.price_chf_per_kwh > 0]
        return min(valid) if valid else None


class TariffSaverTariffGradeSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Tariff grade"
    _attr_icon = "mdi:school-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tariff_grade"

    def _current_slot(self) -> PriceSlot | None:
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return None

        now = dt_util.utcnow()
        current = None
        for s in slots:
            if s.start <= now:
                current = s
            else:
                break

        return current or slots[0]

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        dev_map = stats.get("dev_vs_avg_percent") or {}

        slot = self._current_slot()
        if not slot:
            return None

        dev = dev_map.get(slot.start.isoformat())
        if dev is None:
            return None

        return _grade_from_dev(float(dev))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        dev_map = stats.get("dev_vs_avg_percent") or {}

        slot = self._current_slot()
        if not slot:
            return {}

        dev = dev_map.get(slot.start.isoformat())
        if dev is None:
            return {}

        grade = _grade_from_dev(float(dev))
        return {
            "slot_start_utc": slot.start.isoformat(),
            "dev_vs_avg_percent_now": float(dev),
            "label_now": _label_from_grade(grade),
        }


class TariffSaverTariffStarsNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Tariff stars now"
    _attr_icon = "mdi:star-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tariff_stars_now"

    @property
    def native_value(self) -> str | None:
        grade = self.coordinator.hass.states.get(self.entity_id.replace("stars", "grade"))
        if grade and grade.state.isdigit():
            return _stars_from_grade(int(grade.state))
        return None


class TariffSaverTariffStarsHorizonSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:star-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, hours: int) -> None:
        super().__init__(coordinator)
        self.hours = hours
        self._attr_name = f"Tariff stars next {hours}h"
        self._attr_unique_id = f"{entry.entry_id}_tariff_stars_next_{hours}h"

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        avg_day = stats.get("avg_active_chf_per_kwh")
        if not avg_day or avg_day <= 0:
            return None

        now = dt_util.utcnow()
        end = now + timedelta(hours=self.hours)

        prices = [
            s.price_chf_per_kwh
            for s in _active_slots(self.coordinator)
            if s.price_chf_per_kwh > 0 and now <= s.start < end
        ]
        if not prices:
            return None

        avg_window = sum(prices) / len(prices)
        dev = (avg_window / avg_day - 1.0) * 100.0
        grade = _grade_from_dev(float(dev))

        return _stars_from_grade(grade)
