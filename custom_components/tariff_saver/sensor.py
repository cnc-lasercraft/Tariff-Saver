"""Sensor platform for Tariff Saver."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import TariffSaverCoordinator, PriceSlot


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"


def _active_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("active", []) if isinstance(data, dict) else []


def _baseline_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("baseline", []) if isinstance(data, dict) else []


def _current_slot(slots: list[PriceSlot]) -> PriceSlot | None:
    if not slots:
        return None
    now = dt_util.utcnow()
    slots = sorted(slots, key=lambda s: s.start)
    current = None
    for s in slots:
        if s.start <= now:
            current = s
        else:
            break
    return current


def _grade_from_dev(dev: float) -> int:
    if dev <= -20:
        return 1
    if dev <= -10:
        return 2
    if dev <= 10:
        return 3
    if dev <= 25:
        return 4
    return 5


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
    store = coordinator.store

    # ---------------------------------------------------------------
    # Energy sampling → store → slot finalization
    # ---------------------------------------------------------------
    energy_entity = entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY) or entry.data.get(
        CONF_CONSUMPTION_ENERGY_ENTITY
    )

    if isinstance(energy_entity, str) and energy_entity:

        @callback
        def _on_energy_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            if not new_state:
                return

            try:
                kwh_total = float(new_state.state)
            except Exception:
                return

            now_utc = dt_util.utcnow()

            stored = store.add_sample(now_utc, kwh_total)
            if not stored:
                return

            # finalize any finished 15-min slots
            store.finalize_due_slots(now_utc)

            # persist if needed
            if store.dirty:
                hass.async_create_task(store.async_save())

            # update cost sensors
            for ent in (
                "sensor.actual_cost_today",
                "sensor.baseline_cost_today",
                "sensor.actual_savings_today",
            ):
                hass.async_create_task(
                    hass.helpers.entity_component.async_update_entity(ent)
                )

        async_track_state_change_event(hass, [energy_entity], _on_energy_change)

    # ---------------------------------------------------------------
    # Add entities
    # ---------------------------------------------------------------
    async_add_entities(
        [
            TariffSaverPriceCurveSensor(coordinator, entry),
            TariffSaverPriceNowSensor(coordinator, entry),
            TariffSaverNextPriceSensor(coordinator, entry),
            TariffSaverCheapestWindowsSensor(coordinator, entry),
            TariffSaverTariffStarsNowSensor(coordinator, entry),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 1),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 2),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 3),
            TariffSaverTariffStarsHorizonSensor(coordinator, entry, 6),
            TariffSaverActualCostTodaySensor(coordinator, entry),
            TariffSaverBaselineCostTodaySensor(coordinator, entry),
            TariffSaverActualSavingsTodaySensor(coordinator, entry),
        ],
        update_before_add=True,
    )


# -------------------------------------------------------------------
# Sensors
# -------------------------------------------------------------------
class TariffSaverPriceCurveSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "Price curve"
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_curve"

    @property
    def native_value(self) -> int | None:
        slots = _active_slots(self.coordinator)
        return len(slots) if slots else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        active = _active_slots(self.coordinator)
        baseline = _baseline_slots(self.coordinator)
        base_map = {s.start: s.price_chf_per_kwh for s in baseline}
        return {
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "price": s.price_chf_per_kwh,
                    "baseline": base_map.get(s.start),
                }
                for s in active
            ]
        }


class TariffSaverPriceNowSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "Price now"
    _attr_native_unit_of_measurement = "CHF/kWh"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_now"

    @property
    def native_value(self) -> float | None:
        slot = _current_slot(_active_slots(self.coordinator))
        return slot.price_chf_per_kwh if slot else None


class TariffSaverNextPriceSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "Next price"
    _attr_native_unit_of_measurement = "CHF/kWh"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_next"

    @property
    def native_value(self) -> float | None:
        now = dt_util.utcnow()
        for s in sorted(_active_slots(self.coordinator), key=lambda s: s.start):
            if s.start > now:
                return s.price_chf_per_kwh
        return None


class TariffSaverCheapestWindowsSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "Cheapest windows"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cheapest_windows"

    @property
    def native_value(self) -> None:
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return {}

        avg_all = sum(s.price_chf_per_kwh for s in slots) / len(slots)

        def best(n: int):
            best_sum = None
            best_start = None
            for i in range(len(slots) - n + 1):
                window = slots[i : i + n]
                s = sum(x.price_chf_per_kwh for x in window)
                if best_sum is None or s < best_sum:
                    best_sum = s
                    best_start = window[0].start
            if best_start is None:
                return None
            avg = best_sum / n
            dev = (avg / avg_all - 1) * 100
            return {
                "start": best_start.isoformat(),
                "stars": _stars_from_grade(_grade_from_dev(dev)),
            }

        return {
            "best_30m": best(2),
            "best_1h": best(4),
            "best_2h": best(8),
            "best_3h": best(12),
        }


# -------------------------------------------------------------------
# Stars
# -------------------------------------------------------------------
class TariffSaverTariffStarsNowSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "Tariff stars now"
    _attr_icon = "mdi:star"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tariff_stars_now"

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        stats = data.get("stats", {})
        devs = stats.get("dev_vs_avg_percent", {})
        slot = _current_slot(_active_slots(self.coordinator))
        if not slot:
            return None
        dev = devs.get(slot.start.isoformat())
        if dev is None:
            return None
        return _stars_from_grade(_grade_from_dev(dev))


class TariffSaverTariffStarsHorizonSensor(CoordinatorEntity, SensorEntity):
    _attr_icon = "mdi:star-outline"

    def __init__(self, coordinator, entry, hours: int):
        super().__init__(coordinator)
        self.hours = hours
        self._attr_name = f"Tariff stars next {hours}h"
        self._attr_unique_id = f"{entry.entry_id}_tariff_stars_next_{hours}h"

    @property
    def native_value(self) -> str | None:
        now = dt_util.utcnow()
        end = now + timedelta(hours=self.hours)
        prices = [
            s.price_chf_per_kwh
            for s in _active_slots(self.coordinator)
            if now <= s.start < end
        ]
        if not prices:
            return None
        avg_all = sum(prices) / len(prices)
        dev = (avg_all / avg_all - 1) * 100
        return _stars_from_grade(_grade_from_dev(dev))


# -------------------------------------------------------------------
# Cost sensors (from booked slots)
# -------------------------------------------------------------------
class _BaseCostTodaySensor(CoordinatorEntity, SensorEntity, RestoreEntity):
    _attr_native_unit_of_measurement = "CHF"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self.entry = entry

    def _totals(self) -> tuple[float, float, float]:
        return self.coordinator.store.compute_today_totals()


class TariffSaverActualCostTodaySensor(_BaseCostTodaySensor):
    _attr_name = "Actual cost today"
    _attr_state_class = "total"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actual_cost_today"

    @property
    def native_value(self) -> float:
        dyn, _, _ = self._totals()
        return round(dyn, 2)


class TariffSaverBaselineCostTodaySensor(_BaseCostTodaySensor):
    _attr_name = "Baseline cost today"
    _attr_state_class = "total"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_baseline_cost_today"

    @property
    def native_value(self) -> float:
        _, base, _ = self._totals()
        return round(base, 2)


class TariffSaverActualSavingsTodaySensor(_BaseCostTodaySensor):
    _attr_name = "Actual savings today"
    _attr_state_class = "measurement"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actual_savings_today"

    @property
    def native_value(self) -> float:
        _, _, savings = self._totals()
        return round(savings, 2)
