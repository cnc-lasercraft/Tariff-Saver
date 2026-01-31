"""Sensor platform for Tariff Saver."""
from __future__ import annotations

from typing import Any
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_GRADE_THRESHOLDS,
    DEFAULT_GRADE_THRESHOLDS,
)
from .coordinator import TariffSaverCoordinator, PriceSlot
from .storage import TariffSaverStore

# Local polling for store-based sensors (no API polling)
SCAN_INTERVAL = timedelta(seconds=30)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _active_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("active", []) if isinstance(data, dict) else []


def _baseline_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("baseline", []) if isinstance(data, dict) else []


def _get_store(hass: HomeAssistant, entry: ConfigEntry) -> TariffSaverStore | None:
    return hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_store")


def _get_grade_thresholds(entry: ConfigEntry) -> list[float]:
    vals = entry.options.get(CONF_GRADE_THRESHOLDS, DEFAULT_GRADE_THRESHOLDS)
    if not isinstance(vals, list) or len(vals) != 4:
        return [float(x) for x in DEFAULT_GRADE_THRESHOLDS]
    try:
        t = [float(x) for x in vals]
        # ensure increasing
        if not (t[0] < t[1] < t[2] < t[3]):
            return [float(x) for x in DEFAULT_GRADE_THRESHOLDS]
        return t
    except Exception:
        return [float(x) for x in DEFAULT_GRADE_THRESHOLDS]


def _current_slot_start_utc(slots: list[PriceSlot]) -> Any:
    """Find the current slot start (UTC) using the active slots list."""
    if not slots:
        return None
    now = dt_util.utcnow()
    current: PriceSlot | None = None
    for s in slots:
        if s.start <= now:
            current = s
        else:
            break
    return (current or slots[0]).start if slots else None


def _grade_from_dev(dev_percent: float, t: list[float]) -> int:
    """Map deviation vs daily avg (percent) to grade 1..5."""
    t1, t2, t3, t4 = t
    if dev_percent <= t1:
        return 1
    if dev_percent <= t2:
        return 2
    if dev_percent < t3:
        return 3
    if dev_percent < t4:
        return 4
    return 5


def _grade_label(g: int) -> str:
    return {
        1: "sehr günstig",
        2: "günstig",
        3: "durchschnitt",
        4: "teuer",
        5: "sehr teuer",
    }.get(g, "unbekannt")


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

            # --- NEW: grade sensor ---
            TariffSaverTariffGradeNowSensor(coordinator, entry),

            # --- NEW: actuals from store ---
            TariffSaverActualCostTodaySensor(hass, coordinator, entry),
            TariffSaverActualBaselineCostTodaySensor(hass, coordinator, entry),
            TariffSaverActualSavingsTodaySensor(hass, coordinator, entry),
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
        active = _active_slots(self.coordinator)
        baseline = _baseline_slots(self.coordinator)
        baseline_map = {s.start: s.price_chf_per_kwh for s in baseline} if baseline else {}

        return {
            "tariff_name": self.coordinator.tariff_name,
            "baseline_tariff_name": self.coordinator.baseline_tariff_name,
            "slot_count": len(active),
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "price_chf_per_kwh": s.price_chf_per_kwh,
                    "baseline_chf_per_kwh": baseline_map.get(s.start),
                }
                for s in active
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

        return (current or slots[0]).price_chf_per_kwh


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
        now = dt_util.utcnow()
        for s in slots:
            if s.start > now:
                return s.price_chf_per_kwh
        return None


class TariffSaverSavingsNext24hSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Estimated savings for next 24h vs baseline (CHF), assuming constant 1 kW load."""

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
    """Cheapest windows for 30m / 1h / 2h / 3h."""

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
        slots = [s for s in slots if s.price_chf_per_kwh > 0]
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
                        if base is not None:
                            save += (base - x.price_chf_per_kwh) * kwh_per_slot
                            matched += 1
                    best_savings = save if matched else None

        avg_chf = best_sum / window_slots
        avg_rp = avg_chf * 100

        result: dict[str, Any] = {
            "start": best_start.isoformat(),
            "end": best_end.isoformat(),
            "avg_chf_per_kwh": round(avg_chf, 6),
            "avg_rp_per_kwh": round(avg_rp, 3),
            "avg_chf_per_kwh_raw": avg_chf,
            "avg_rp_per_kwh_raw": avg_rp,
        }

        if best_savings is not None:
            result["savings_vs_baseline_chf"] = round(best_savings, 2)

        return result

    @property
    def native_value(self) -> float | None:
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return None
        best_1h = self._best_window(slots, {}, 4)
        return best_1h["avg_chf_per_kwh"] if best_1h else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        baseline = _baseline_slots(self.coordinator)
        baseline_map = (
            {s.start: s.price_chf_per_kwh for s in baseline if s.price_chf_per_kwh > 0}
            if baseline
            else {}
        )

        return {
            "tariff_name": self.coordinator.tariff_name,
            "baseline_tariff_name": self.coordinator.baseline_tariff_name,
            "best_30m": self._best_window(slots, baseline_map, 2),
            "best_1h": self._best_window(slots, baseline_map, 4),
            "best_2h": self._best_window(slots, baseline_map, 8),
            "best_3h": self._best_window(slots, baseline_map, 12),
        }


class TariffSaverTariffGradeNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Tariff grade now (1..5) based on deviation vs daily average.

    State = grade for current 15-min slot (now).
    Attributes = outlook grades for next 1/2/3/6 hours.
    """

    _attr_has_entity_name = True
    _attr_name = "Tariff grade"
    _attr_icon = "mdi:school-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tariff_grade"

    def _window_grade(self, hours: int) -> tuple[int | None, float | None, float | None]:
        """Return (grade, dev_percent, avg_window_price) for the next X hours."""
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        avg_day = stats.get("avg_active_chf_per_kwh")
        if not avg_day or avg_day <= 0:
            return None, None, None

        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return None, None, None

        now = dt_util.utcnow()
        end = now + timedelta(hours=hours)

        # Use only published/valid prices (>0)
        window_prices = [
            s.price_chf_per_kwh
            for s in slots
            if s.price_chf_per_kwh > 0 and s.start >= now and s.start < end
        ]
        if not window_prices:
            return None, None, None

        avg_window = sum(window_prices) / len(window_prices)
        dev = (avg_window / float(avg_day) - 1.0) * 100.0

        thresholds = _get_grade_thresholds(self.entry)
        grade = _grade_from_dev(float(dev), thresholds)
        return grade, float(dev), float(avg_window)

    @property
    def native_value(self) -> int | None:
        """Grade for the current 15-min slot."""
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        dev_map = stats.get("dev_vs_avg_percent") or {}

        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        slot_start = _current_slot_start_utc(slots)
        if not slot_start:
            return None

        dev = dev_map.get(slot_start.isoformat())
        if dev is None:
            return None

        thresholds = _get_grade_thresholds(self.entry)
        return _grade_from_dev(float(dev), thresholds)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain now-grade + provide outlook grades."""
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        dev_map = stats.get("dev_vs_avg_percent") or {}
        avg_day = stats.get("avg_active_chf_per_kwh")

        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        slot_start = _current_slot_start_utc(slots)

        dev_now = dev_map.get(slot_start.isoformat()) if slot_start else None
        thresholds = _get_grade_thresholds(self.entry)

        grade_now = None
        label_now = None
        if dev_now is not None:
            grade_now = _grade_from_dev(float(dev_now), thresholds)
            label_now = _grade_label(grade_now)

        # Outlook windows (avg over window, relative to day's avg)
        outlook: dict[str, Any] = {}
        for h in (1, 2, 3, 6):
            g, dev, avgw = self._window_grade(h)
            outlook[f"next_{h}h_grade"] = g
            outlook[f"next_{h}h_dev_vs_avg_percent"] = dev
            outlook[f"next_{h}h_avg_chf_per_kwh"] = avgw

        return {
            "slot_start_utc": slot_start.isoformat() if slot_start else None,
            "avg_active_chf_per_kwh": avg_day,
            "thresholds_percent": thresholds,

            # Now
            "grade_now": grade_now,
            "label_now": label_now,
            "dev_vs_avg_percent_now": dev_now,

            # Outlook
            **outlook,
        }

# -------------------------------------------------------------------
# NEW: Store-based "actual" sensors
# -------------------------------------------------------------------
class _TariffSaverActualBase(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "CHF"
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self.entry = entry

    def _totals(self) -> tuple[float, float, float] | None:
        store = _get_store(self.hass, self.entry)
        if not store:
            return None
        return store.compute_today_totals()


class TariffSaverActualCostTodaySensor(_TariffSaverActualBase):
    _attr_name = "Actual cost today"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(hass, coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actual_cost_today_chf"
        self._attr_icon = "mdi:currency-chf"

    @property
    def native_value(self) -> float | None:
        t = self._totals()
        if not t:
            return None
        dyn, _, _ = t
        return round(dyn, 4)


class TariffSaverActualBaselineCostTodaySensor(_TariffSaverActualBase):
    _attr_name = "Baseline cost today"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(hass, coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actual_baseline_cost_today_chf"
        self._attr_icon = "mdi:currency-chf"

    @property
    def native_value(self) -> float | None:
        t = self._totals()
        if not t:
            return None
        _, base, _ = t
        return round(base, 4)


class TariffSaverActualSavingsTodaySensor(_TariffSaverActualBase):
    _attr_name = "Actual savings today"
    _attr_state_class = None

    def __init__(self, hass: HomeAssistant, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(hass, coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actual_savings_today_chf"
        self._attr_icon = "mdi:piggy-bank-outline"

    @property
    def native_value(self) -> float | None:
        t = self._totals()
        if not t:
            return None
        _, _, savings = t
        return round(savings, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self._totals()
        if not t:
            return {}
        dyn, base, savings = t
        return {
            "actual_cost_today_chf": round(dyn, 4),
            "baseline_cost_today_chf": round(base, 4),
            "actual_savings_today_chf": round(savings, 4),
            "source": "tariff_saver store (finalized 15-min slots)",
        }
