"""Sensor platform for Tariff Saver."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, date, datetime
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
    """Return current slot; fallback to first slot if we're between slots."""
    if not slots:
        return None
    slots = sorted(slots, key=lambda s: s.start)
    now = dt_util.utcnow()
    current: PriceSlot | None = None
    for s in slots:
        if s.start <= now:
            current = s
        else:
            break
    return current or slots[0]


def _grade_from_dev(dev: float) -> int:
    """Map deviation vs daily average (percent) to grade 1..5."""
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
    """More stars = better (grade 1 => ⭐⭐⭐⭐⭐, grade 5 => ⭐)."""
    if grade is None or grade < 1 or grade > 5:
        return "—"
    return "⭐" * (6 - grade)


def _to_float(state: Any) -> float | None:
    try:
        if state in (None, "unknown", "unavailable", ""):
            return None
        return float(state)
    except Exception:
        return None


def _floor_15min_utc(ts: datetime) -> datetime:
    ts = dt_util.as_utc(ts)
    minute = (ts.minute // 15) * 15
    return ts.replace(minute=minute, second=0, microsecond=0)


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _avg_future_from_now(slots: list[PriceSlot]) -> float | None:
    """Average active price from now (UTC) until end of available data."""
    now = dt_util.utcnow()
    vals = [s.price_chf_per_kwh for s in slots if s.price_chf_per_kwh > 0 and s.start >= now]
    return _avg(vals)


def _avg_day_from_stats(coordinator: TariffSaverCoordinator) -> float | None:
    data = coordinator.data or {}
    stats = data.get("stats") or {}
    avg_day = stats.get("avg_active_chf_per_kwh")
    if isinstance(avg_day, (int, float)) and avg_day > 0:
        return float(avg_day)
    return None


def _stars_for_horizon(coordinator: TariffSaverCoordinator, minutes: int) -> tuple[str | None, int | None, float | None]:
    """Stars for average price from now until now+minutes, vs today's average."""
    avg_day = _avg_day_from_stats(coordinator)
    if not avg_day:
        return None, None, None

    now = dt_util.utcnow()
    end = now + timedelta(minutes=minutes)

    prices = [
        s.price_chf_per_kwh
        for s in _active_slots(coordinator)
        if s.price_chf_per_kwh > 0 and now <= s.start < end
    ]
    if not prices:
        return None, None, None

    avg_window = sum(prices) / len(prices)
    dev = (avg_window / float(avg_day) - 1.0) * 100.0
    grade = _grade_from_dev(float(dev))
    return _stars_from_grade(grade), grade, float(dev)


# -------------------------------------------------------------------
# Cost tracking (today) based on energy sensor deltas
# -------------------------------------------------------------------
@dataclass
class _CostTracker:
    last_energy_kwh: float | None = None
    active_cost_chf_today: float = 0.0
    baseline_cost_chf_today: float = 0.0
    has_baseline: bool = False
    day: date | None = None


def _tracker_key(entry: ConfigEntry) -> str:
    return f"{entry.entry_id}_cost_tracker"


def _get_tracker(hass: HomeAssistant, entry: ConfigEntry) -> _CostTracker:
    domain = hass.data.setdefault(DOMAIN, {})
    key = _tracker_key(entry)
    if key not in domain:
        domain[key] = _CostTracker()
    return domain[key]


def _reset_if_new_day(tracker: _CostTracker) -> None:
    today_local = dt_util.now().date()
    if tracker.day != today_local:
        tracker.day = today_local
        tracker.active_cost_chf_today = 0.0
        tracker.baseline_cost_chf_today = 0.0
        tracker.has_baseline = False
        # last_energy_kwh is NOT reset (we keep continuity); deltas still valid


def _price_for_now_from_store(coordinator: TariffSaverCoordinator) -> tuple[float | None, float | None]:
    """Return (active_price, baseline_price) for the current 15-min slot from persistent store."""
    store = getattr(coordinator, "store", None)
    if store is None:
        return None, None

    slot_start = _floor_15min_utc(dt_util.utcnow())
    p = store.get_price_slot(slot_start)
    if not p:
        return None, None

    dyn = p.get("dyn")
    base = p.get("base")
    if not isinstance(dyn, (int, float)) or not isinstance(base, (int, float)):
        return None, None

    return float(dyn), float(base)


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

    # cost tracker shared across cost sensors
    tracker = _get_tracker(hass, entry)

    # Track energy-based costs if configured
    energy_entity = entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY) or entry.data.get(CONF_CONSUMPTION_ENERGY_ENTITY)
    if isinstance(energy_entity, str) and energy_entity:

        @callback
        def _on_energy_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if new_state is None:
                return

            new_val = _to_float(new_state.state)
            old_val = _to_float(old_state.state) if old_state is not None else None
            if new_val is None:
                return

            _reset_if_new_day(tracker)

            # Initialize last energy from old_val if first time
            if tracker.last_energy_kwh is None:
                tracker.last_energy_kwh = old_val if old_val is not None else new_val
                return

            delta = new_val - tracker.last_energy_kwh
            # ignore negative spikes/resets
            if delta <= 0:
                tracker.last_energy_kwh = new_val
                return

            # ✅ use persistent store prices (dyn + baseline)
            active_price, baseline_price = _price_for_now_from_store(coordinator)
            if active_price is None or baseline_price is None:
                tracker.last_energy_kwh = new_val
                return

            tracker.active_cost_chf_today += delta * active_price
            tracker.baseline_cost_chf_today += delta * baseline_price
            tracker.has_baseline = True

            tracker.last_energy_kwh = new_val

            # Force entity updates (they read from tracker)
            hass.async_create_task(hass.helpers.entity_component.async_update_entity("sensor.actual_cost_today"))
            hass.async_create_task(hass.helpers.entity_component.async_update_entity("sensor.actual_savings_today"))
            hass.async_create_task(hass.helpers.entity_component.async_update_entity("sensor.baseline_cost_today"))

        unsub = async_track_state_change_event(hass, [energy_entity], _on_energy_change)
        hass.data[DOMAIN][f"{entry.entry_id}_unsub_energy_cost"] = unsub

    async_add_entities(
        [
            # prices & forecast
            TariffSaverPriceCurveSensor(coordinator, entry),
            TariffSaverPriceNowSensor(coordinator, entry),
            TariffSaverNextPriceSensor(coordinator, entry),
            TariffSaverSavingsNext24hSensor(coordinator, entry),
            TariffSaverCheapestWindowsSensor(coordinator, entry),
            # grading
            TariffSaverTariffGradeSensor(coordinator, entry),

            # ⭐ stars (clean: 2 entities)
            TariffSaverTariffStarsNowSensor(coordinator, entry),
            TariffSaverTariffStarsOutlookSensor(coordinator, entry),

            # costs (today)
            TariffSaverActualCostTodaySensor(coordinator, entry),
            TariffSaverBaselineCostTodaySensor(coordinator, entry),
            TariffSaverActualSavingsTodaySensor(coordinator, entry),
        ],
        update_before_add=True,
    )


# -------------------------------------------------------------------
# Sensors
# -------------------------------------------------------------------
class TariffSaverPriceCurveSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Active price curve as attributes (with baseline per slot if available)."""

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
            "tariff_name": getattr(self.coordinator, "tariff_name", None),
            "baseline_tariff_name": getattr(self.coordinator, "baseline_tariff_name", None),
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
        slot = _current_slot(slots)
        return slot.price_chf_per_kwh if slot else None


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
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return None
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
    """Cheapest windows for 30m / 1h / 2h / 3h, enriched with stars vs future average."""

    _attr_has_entity_name = True
    _attr_name = "Cheapest windows"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cheapest_windows"

    @staticmethod
    def _best_window(slots: list[PriceSlot], window_slots: int) -> dict[str, Any] | None:
        slots = [s for s in slots if s.price_chf_per_kwh > 0]
        if len(slots) < window_slots:
            return None

        best_sum = float("inf")
        best_start: datetime | None = None
        best_end: datetime | None = None

        for i in range(len(slots) - window_slots + 1):
            window = slots[i : i + window_slots]
            window_sum = sum(x.price_chf_per_kwh for x in window)
            if window_sum < best_sum:
                best_sum = window_sum
                best_start = window[0].start
                best_end = window[-1].start + timedelta(minutes=15)

        if best_start is None or best_end is None:
            return None

        avg_chf = best_sum / window_slots
        avg_rp = avg_chf * 100

        return {
            "start": best_start.isoformat(),
            "end": best_end.isoformat(),
            "avg_chf_per_kwh": round(avg_chf, 6),
            "avg_rp_per_kwh": round(avg_rp, 3),
            "avg_chf_per_kwh_raw": float(avg_chf),
            "avg_rp_per_kwh_raw": float(avg_rp),
        }

    @staticmethod
    def _decorate_with_stars(window: dict[str, Any] | None, ref_avg: float | None) -> dict[str, Any] | None:
        """Add stars/grade/dev_vs_ref_percent using ref_avg (future avg)."""
        if not window or not ref_avg or ref_avg <= 0:
            return window

        p = window.get("avg_chf_per_kwh_raw") or window.get("avg_chf_per_kwh")
        if not isinstance(p, (int, float)) or p <= 0:
            return window

        dev = (float(p) / float(ref_avg) - 1.0) * 100.0
        grade = _grade_from_dev(dev)

        out = dict(window)
        out["dev_vs_ref_percent"] = round(dev, 2)
        out["grade"] = grade
        out["stars"] = _stars_from_grade(grade)
        return out

    @property
    def native_value(self) -> float | None:
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return None
        best_1h = self._best_window(slots, 4)
        return best_1h["avg_chf_per_kwh"] if best_1h else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)

        ref_avg = _avg_future_from_now(slots)

        best_30m = self._decorate_with_stars(self._best_window(slots, 2), ref_avg)
        best_1h = self._decorate_with_stars(self._best_window(slots, 4), ref_avg)
        best_2h = self._decorate_with_stars(self._best_window(slots, 8), ref_avg)
        best_3h = self._decorate_with_stars(self._best_window(slots, 12), ref_avg)

        return {
            "tariff_name": getattr(self.coordinator, "tariff_name", None),
            "baseline_tariff_name": getattr(self.coordinator, "baseline_tariff_name", None),
            "ref_scope": "future",
            "ref_avg_chf_per_kwh": round(ref_avg, 6) if ref_avg else None,
            "best_30m": best_30m,
            "best_1h": best_1h,
            "best_2h": best_2h,
            "best_3h": best_3h,
        }


class TariffSaverTariffGradeSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Numeric tariff grade now (1..5) based on deviation vs daily average."""

    _attr_has_entity_name = True
    _attr_name = "Tariff grade"
    _attr_icon = "mdi:school-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tariff_grade"

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        dev_map = stats.get("dev_vs_avg_percent") or {}

        slot = _current_slot(_active_slots(self.coordinator))
        if not slot:
            return None

        dev = dev_map.get(slot.start.isoformat())
        if dev is None:
            return None

        return _grade_from_dev(float(dev))


class TariffSaverTariffStarsNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Stars for current 15-min grade (keeps the same unique_id/entity_id)."""

    _attr_has_entity_name = True
    _attr_name = "Tariff stars now"
    _attr_icon = "mdi:star-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        # keep stable
        self._attr_unique_id = f"{entry.entry_id}_tariff_stars_now"

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        dev_map = stats.get("dev_vs_avg_percent") or {}

        slot = _current_slot(_active_slots(self.coordinator))
        if not slot:
            return None

        dev = dev_map.get(slot.start.isoformat())
        if dev is None:
            return None

        grade = _grade_from_dev(float(dev))
        return _stars_from_grade(grade)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        stats = data.get("stats") or {}
        dev_map = stats.get("dev_vs_avg_percent") or {}
        slot = _current_slot(_active_slots(self.coordinator))
        if not slot:
            return {}
        dev = dev_map.get(slot.start.isoformat())
        if dev is None:
            return {}
        grade = _grade_from_dev(float(dev))
        return {
            "slot_start_utc": slot.start.isoformat(),
            "dev_vs_avg_percent": round(float(dev), 2),
            "grade": grade,
            "label": _label_from_grade(grade),
        }


class TariffSaverTariffStarsOutlookSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Outlook stars bundled as attributes; state defaults to next_1h."""

    _attr_has_entity_name = True
    _attr_name = "Tariff stars outlook"
    _attr_icon = "mdi:star-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tariff_stars_outlook"

    @property
    def native_value(self) -> str | None:
        stars, _grade, _dev = _stars_for_horizon(self.coordinator, minutes=60)
        return stars

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for minutes, key in [
            (30, "next_30m"),
            (60, "next_1h"),
            (120, "next_2h"),
            (180, "next_3h"),
            (360, "next_6h"),
        ]:
            stars, grade, dev = _stars_for_horizon(self.coordinator, minutes=minutes)
            out[key] = stars
            out[f"{key}_grade"] = grade
            out[f"{key}_dev_vs_avg_percent"] = round(dev, 2) if isinstance(dev, (int, float)) else None
        return out


# -------------------------------------------------------------------
# Today cost sensors (energy-based)
# -------------------------------------------------------------------
class _BaseTodayCostSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity, RestoreEntity):
    _attr_native_unit_of_measurement = "CHF"
    _attr_icon = "mdi:currency-chf"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        tracker = _get_tracker(self.hass, self.entry)
        _reset_if_new_day(tracker)
        if tracker.day == dt_util.now().date():
            try:
                v = float(last.state)
            except Exception:
                return
            self._restore_value(tracker, v)

    def _restore_value(self, tracker: _CostTracker, v: float) -> None:
        return


class TariffSaverActualCostTodaySensor(_BaseTodayCostSensor):
    _attr_has_entity_name = True
    _attr_name = "Actual cost today"
    _attr_icon = "mdi:cash"
    _attr_state_class = "total"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actual_cost_today"

    @property
    def native_value(self) -> float | None:
        tracker = _get_tracker(self.hass, self.entry)
        _reset_if_new_day(tracker)
        return round(tracker.active_cost_chf_today, 2)

    def _restore_value(self, tracker: _CostTracker, v: float) -> None:
        tracker.active_cost_chf_today = v


class TariffSaverBaselineCostTodaySensor(_BaseTodayCostSensor):
    _attr_has_entity_name = True
    _attr_name = "Baseline cost today"
    _attr_icon = "mdi:cash-multiple"
    _attr_state_class = "total"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_baseline_cost_today"

    @property
    def native_value(self) -> float | None:
        tracker = _get_tracker(self.hass, self.entry)
        _reset_if_new_day(tracker)
        if not tracker.has_baseline:
            return None
        return round(tracker.baseline_cost_chf_today, 2)

    def _restore_value(self, tracker: _CostTracker, v: float) -> None:
        tracker.baseline_cost_chf_today = v
        tracker.has_baseline = True


class TariffSaverActualSavingsTodaySensor(_BaseTodayCostSensor):
    _attr_has_entity_name = True
    _attr_name = "Actual savings today"
    _attr_icon = "mdi:piggy-bank"
    _attr_state_class = "measurement"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actual_savings_today"

    @property
    def native_value(self) -> float | None:
        tracker = _get_tracker(self.hass, self.entry)
        _reset_if_new_day(tracker)
        if not tracker.has_baseline:
            return None
        return round(tracker.baseline_cost_chf_today - tracker.active_cost_chf_today, 2)

    def _restore_value(self, tracker: _CostTracker, v: float) -> None:
        return
