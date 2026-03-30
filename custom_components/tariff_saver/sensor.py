"""Sensor platform for Tariff Saver."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONSUMER_COUNT, CONF_CONSUMERS, DOMAIN
from .coordinator import PriceSlot, TariffSaverCoordinator, _slot_total_price
from .models import ConsumerConfig
from .consumer_helpers import build_consumer_plan, effective_energy_kwh, get_consumer_config
from .storage import IMPORT_ALLIN_COMPONENTS, TariffSaverStore

CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"
SIGNAL_STORE_UPDATED = "tariff_saver_store_updated"
WINDOW_HOURS: tuple[int, ...] = (1, 2, 3, 6)



def _device_info(entry: ConfigEntry) -> dict[str, Any]:
    return {"identifiers": {(DOMAIN, entry.entry_id)}, "name": entry.title}


def _active_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("active", []) if isinstance(data, dict) else []


def _baseline_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("baseline", []) if isinstance(data, dict) else []


def _stats(coordinator: TariffSaverCoordinator) -> dict[str, Any]:
    data = coordinator.data or {}
    return data.get("stats", {}) if isinstance(data, dict) else {}


def _windows(coordinator: TariffSaverCoordinator) -> dict[str, Any]:
    data = coordinator.data or {}
    return data.get("windows", {}) if isinstance(data, dict) else {}


def _feed_in(coordinator: TariffSaverCoordinator) -> dict[str, Any]:
    data = coordinator.data or {}
    return data.get("feed_in", {}) if isinstance(data, dict) else {}


def _current_slot(slots: list[PriceSlot]) -> PriceSlot | None:
    if not slots:
        return None
    now = dt_util.utcnow()
    current: PriceSlot | None = None
    for slot in sorted(slots, key=lambda s: s.start):
        if slot.start <= now:
            current = slot
        else:
            break
    return current or slots[0]


def _next_slot(slots: list[PriceSlot]) -> PriceSlot | None:
    now = dt_util.utcnow()
    for slot in sorted(slots, key=lambda s: s.start):
        if slot.start > now:
            return slot
    return None


def _absolute_score(price: float, hist_min: float, hist_max: float, baseline: float) -> int:
    """Score 0-100 where 0=historical min, 50=baseline, 100=historical max."""
    if not isinstance(price, (int, float)) or price <= 0:
        return 50
    price = float(price)
    if price <= baseline:
        # Below baseline: map to 0-50
        denom = baseline - hist_min
        if denom <= 0:
            return 50
        score = ((price - hist_min) / denom) * 50.0
    else:
        # Above baseline: map to 50-100
        denom = hist_max - baseline
        if denom <= 0:
            return 50
        score = 50.0 + ((price - baseline) / denom) * 50.0
    return max(0, min(100, int(round(score))))


def _get_baseline_price(coordinator) -> float:
    """Get current baseline all-in price."""
    data = coordinator.data or {}
    if not isinstance(data, dict):
        return 0.25
    baseline = data.get("baseline", [])
    if not baseline:
        return 0.25
    from homeassistant.util import dt as dt_util
    now_utc = dt_util.utcnow()
    current = next((s for s in reversed(sorted(baseline, key=lambda s: s.start)) if s.start <= now_utc), None)
    if current is None and baseline:
        current = baseline[0]
    if current:
        p = _slot_total_price(current.components_chf_per_kwh)
        if isinstance(p, (int, float)) and p > 0.10:
            return float(p)
    return 0.25


def _current_score_live(coordinator) -> int | None:
    data = coordinator.data or {}
    if not isinstance(data, dict):
        return None
    active = data.get("active", [])
    if not active:
        return None
    from homeassistant.util import dt as dt_util
    now_utc = dt_util.utcnow()
    current_slot = next((s for s in reversed(sorted(active, key=lambda s: s.start)) if s.start <= now_utc), None)
    if current_slot is None:
        return None
    current_price = _slot_total_price(current_slot.components_chf_per_kwh)
    if not isinstance(current_price, (int, float)):
        return None
    # Get historical min/max and baseline
    store = getattr(coordinator, "store", None)
    if store is None:
        return 50
    hist_min = store.historical_price_min
    hist_max = store.historical_price_max
    if hist_min >= 999.0 or hist_max <= 0.0:
        return 50
    baseline = _get_baseline_price(coordinator)
    return _absolute_score(float(current_price), hist_min, hist_max, baseline)


def _all_in_from_slot(slot: PriceSlot | None) -> float | None:
    if not slot:
        return None
    comps = slot.components_chf_per_kwh or {}
    for key in ("integrated", "all_in"):
        value = comps.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    total = TariffSaverStore.sum_components(comps, IMPORT_ALLIN_COMPONENTS)
    return total if total > 0 else None


def _stars(score: int | None, scale: int) -> int | None:
    if score is None:
        return None
    return max(0, min(scale, int(round(((100 - score) / 100) * scale))))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TariffSaverCoordinator = hass.data[DOMAIN][entry.entry_id]

    energy_entity = entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY) or entry.data.get(
        CONF_CONSUMPTION_ENERGY_ENTITY
    )
    if isinstance(energy_entity, str) and energy_entity:

        @callback
        def _process_kwh(kwh_total: float) -> None:
            store = getattr(coordinator, "store", None)
            if store is None:
                return
            now_utc = dt_util.utcnow()
            changed = store.add_sample(now_utc, kwh_total)
            finalized = store.finalize_due_slots(now_utc)
            if store.dirty:
                hass.async_create_task(store.async_save())
            if changed or finalized > 0:
                async_dispatcher_send(hass, f"{SIGNAL_STORE_UPDATED}_{entry.entry_id}")

        @callback
        def _on_energy_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            try:
                _process_kwh(float(new_state.state))
            except Exception:
                return

        current_state = hass.states.get(energy_entity)
        if current_state is not None:
            try:
                _process_kwh(float(current_state.state))
            except Exception:
                pass

        hass.data[DOMAIN][f"{entry.entry_id}_unsub_energy_cost"] = async_track_state_change_event(
            hass, [energy_entity], _on_energy_change
        )

        @callback
        def _periodic_tick(now: datetime) -> None:
            state = hass.states.get(energy_entity)
            if state is None:
                return
            try:
                _process_kwh(float(state.state))
            except Exception:
                return

        hass.data[DOMAIN][f"{entry.entry_id}_unsub_energy_tick"] = async_track_time_interval(
            hass, _periodic_tick, timedelta(minutes=5)
        )

    @callback
    def _process_consumers() -> None:
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        now_utc = dt_util.utcnow()
        changed = False
        for slot in range(1, CONSUMER_COUNT + 1):
            config = get_consumer_config(entry, slot)
            if not config.enabled or not config.learning_enabled or not config.measurement_entity:
                store._finalize_consumer_run(str(slot))
                continue
            state = hass.states.get(config.measurement_entity)
            if state is None:
                store._finalize_consumer_run(str(slot))
                continue
            try:
                if store.add_consumer_sample(str(slot), now_utc, float(state.state)):
                    changed = True
            except Exception:
                continue
        if store.dirty:
            hass.async_create_task(store.async_save())
        if changed:
            async_dispatcher_send(hass, f"{SIGNAL_STORE_UPDATED}_{entry.entry_id}")

    @callback
    def _periodic_consumer_tick(now: datetime) -> None:
        _process_consumers()

    _process_consumers()
    hass.data[DOMAIN][f"{entry.entry_id}_unsub_consumer_tick"] = async_track_time_interval(
        hass, _periodic_consumer_tick, timedelta(minutes=5)
    )

    entities: list[SensorEntity] = [
        TariffSaverPriceCurveSensor(coordinator, entry),
        TariffSaverPriceNowSensor(coordinator, entry),
        TariffSaverBaselinePriceNowSensor(coordinator, entry),
        TariffSaverNextPriceSensor(coordinator, entry),
        TariffSaverScoreNowSensor(coordinator, entry),
        TariffSaverScoreStarsSensor(coordinator, entry, 10),
        TariffSaverDayScoreSensor(coordinator, entry),
        TariffSaverDayScoreStarsSensor(coordinator, entry, 5),
        TariffSaverFeedInPriceSensor(coordinator, entry),
        PeriodCostSensor(entry, coordinator, "today", "dyn", "actual_cost_today", "Actual cost today"),
        PeriodCostSensor(entry, coordinator, "today", "base", "baseline_cost_today", "Baseline cost today", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "today", "sav", "actual_savings_today", "Savings today", icon="mdi:piggy-bank", state_class="measurement"),
        PeriodCostSensor(entry, coordinator, "week", "dyn", "actual_cost_week", "Actual cost week"),
        PeriodCostSensor(entry, coordinator, "week", "base", "baseline_cost_week", "Baseline cost week", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "week", "sav", "actual_savings_week", "Savings week", icon="mdi:piggy-bank", state_class="measurement"),
        PeriodCostSensor(entry, coordinator, "month", "dyn", "actual_cost_month", "Actual cost month"),
        PeriodCostSensor(entry, coordinator, "month", "base", "baseline_cost_month", "Baseline cost month", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "month", "sav", "actual_savings_month", "Savings month", icon="mdi:piggy-bank", state_class="measurement"),
        PeriodCostSensor(entry, coordinator, "year", "dyn", "actual_cost_year", "Actual cost year"),
        PeriodCostSensor(entry, coordinator, "year", "base", "baseline_cost_year", "Baseline cost year", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "year", "sav", "actual_savings_year", "Savings year", icon="mdi:piggy-bank", state_class="measurement"),
        TariffSaverLinkStatusSensor(coordinator, entry),
        TariffSaverLastApiSuccessSensor(coordinator, entry),
    ]

    entities.append(BaseLoadSensor(coordinator, entry))
    entities.append(ConsumerConfigSensor(coordinator, entry))
    entities.append(StromAmpelSensor(coordinator, entry))
    entities.append(ActivityLogSensor(coordinator, entry))

    for hours in WINDOW_HOURS:
        entities.append(BestWindowStartSensor(coordinator, entry, hours))
        entities.append(BestWindowScoreSensor(coordinator, entry, hours))

    for slot in range(1, CONSUMER_COUNT + 1):
        entities.append(TariffSaverConsumerSensor(coordinator, entry, slot))

    async_add_entities(entities, update_before_add=True)


class TariffSaverPriceCurveSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Price curve 30m"
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_curve"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        slots = _active_slots(self.coordinator)
        return len(slots) if slots else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        active = _active_slots(self.coordinator)
        baseline = _baseline_slots(self.coordinator)
        baseline_map = {slot.start: slot for slot in baseline}
        return {
            "interval_minutes": 30,
            "slot_count": len(active),
            "slots": [
                {
                    "start": slot.start.isoformat(),
                    "price_all_in_chf_per_kwh": _all_in_from_slot(slot),
                    "baseline_chf_per_kwh": _all_in_from_slot(baseline_map.get(slot.start)),
                }
                for slot in active
            ],
        }


class TariffSaverPriceNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Price now 30m"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:cash"
    _attr_should_poll = True

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_now"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        value = _all_in_from_slot(_current_slot(_active_slots(self.coordinator)))
        return round(float(value), 6) if isinstance(value, (int, float)) and value >= 0 else None


class TariffSaverBaselinePriceNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Baseline price now 30m"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:cash-sync"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_baseline_price_now"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        value = _all_in_from_slot(_current_slot(_baseline_slots(self.coordinator)))
        return round(float(value), 6) if isinstance(value, (int, float)) and value >= 0 else None


class TariffSaverNextPriceSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Next price 30m"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_next"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        value = _all_in_from_slot(_next_slot(_active_slots(self.coordinator)))
        return round(float(value), 6) if isinstance(value, (int, float)) and value >= 0 else None


class TariffSaverScoreNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Score now"
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:gauge"
    _attr_should_poll = True

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_score_now"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        return _current_score_live(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        store = getattr(self.coordinator, "store", None)
        baseline = _get_baseline_price(self.coordinator)
        return {
            "historical_min": store.historical_price_min if store else None,
            "historical_max": store.historical_price_max if store else None,
            "baseline": round(baseline, 4),
            "interpretation": "Score 0=hist.günstigster Preis, Score 50=Baseline(Flat), Score 100=hist.teuerster Preis",
        }


class TariffSaverScoreStarsSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:star"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, scale: int) -> None:
        super().__init__(coordinator)
        self.scale = scale
        self._attr_name = f"Score now stars {scale}"
        self._attr_unique_id = f"{entry.entry_id}_score_now_stars_{scale}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        score = _stats(self.coordinator).get("current_score_0_100")
        return _stars(score if isinstance(score, int) else None, self.scale)




class TariffSaverDayScoreSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Day score"
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:calendar-star"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_day_score"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        value = _stats(self.coordinator).get("day_score_0_100")
        return int(value) if isinstance(value, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = _stats(self.coordinator)
        slot_day = stats.get("slot_day_local")
        return {
            "slot_day_local": slot_day.isoformat() if hasattr(slot_day, "isoformat") else slot_day,
            "day_average_price_chf_per_kwh": stats.get("avg_active_chf_per_kwh"),
            "year_min_day_avg_chf_per_kwh": stats.get("year_min_day_avg_chf_per_kwh"),
            "year_max_day_avg_chf_per_kwh": stats.get("year_max_day_avg_chf_per_kwh"),
            "year_day_samples": stats.get("year_day_samples"),
            "tomorrow_avg_chf_per_kwh": stats.get("tomorrow_avg_chf_per_kwh"),
            "tomorrow_score_0_100": stats.get("tomorrow_score_0_100"),
            "tomorrow_slot_count": stats.get("tomorrow_slot_count"),
        }


class TariffSaverDayScoreStarsSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:star"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, scale: int) -> None:
        super().__init__(coordinator)
        self.scale = scale
        self._attr_name = f"Day score stars {scale}"
        self._attr_unique_id = f"{entry.entry_id}_day_score_stars_{scale}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        score = _stats(self.coordinator).get("day_score_0_100")
        return _stars(score if isinstance(score, int) else None, self.scale)


class TariffSaverFeedInPriceSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Feed-in price"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_feed_in_price"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        value = _feed_in(self.coordinator).get("price_chf_per_kwh")
        return round(float(value), 6) if isinstance(value, (int, float)) and value >= 0 else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        feed_in = _feed_in(self.coordinator)
        return {
            "mode": feed_in.get("mode"),
            "entity_id": feed_in.get("entity_id"),
        }


class BestWindowStartSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, hours: int) -> None:
        super().__init__(coordinator)
        self.hours = hours
        self._attr_name = f"Best window {hours}h start"
        self._attr_unique_id = f"{entry.entry_id}_best_window_{hours}h_start"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        value = (_windows(self.coordinator).get(f"{self.hours}h") or {}).get("start")
        return dt_util.as_utc(value) if isinstance(value, datetime) else None


class BestWindowScoreSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, hours: int) -> None:
        super().__init__(coordinator)
        self.hours = hours
        self._attr_name = f"Best window {hours}h score"
        self._attr_unique_id = f"{entry.entry_id}_best_window_{hours}h_score"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        value = (_windows(self.coordinator).get(f"{self.hours}h") or {}).get("score_0_100")
        return int(value) if isinstance(value, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = _windows(self.coordinator).get(f"{self.hours}h") or {}
        start = data.get("start")
        avg_price = data.get("avg_price_chf_per_kwh")
        return {
            "start": start.isoformat() if isinstance(start, datetime) else None,
            "avg_price_chf_per_kwh": avg_price,
        }



class TariffSaverConsumerSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, slot: int) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.slot = slot
        self._attr_name = f"Consumer {slot}"
        self._attr_unique_id = f"{entry.entry_id}_consumer_{slot}"
        self._attr_device_info = _device_info(entry)
        self._prev_should_run: bool | None = None

    @property
    def native_value(self) -> float | None:
        config = get_consumer_config(self.entry, self.slot)
        store = getattr(self.coordinator, "store", None)
        learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
        value = effective_energy_kwh(config, learning)
        return round(float(value), 3) if isinstance(value, (int, float)) and value > 0 else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = get_consumer_config(self.entry, self.slot)
        store = getattr(self.coordinator, "store", None)
        learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
        plan = build_consumer_plan(self.hass, self.entry, config, learning, _active_slots(self.coordinator))

        # Consumer transition logging moved to binary_sensor.py

        return {
            "slot": self.slot,
            "enabled": config.enabled,
            "name": config.configured_name,
            "mode": config.mode,
            "power_kw": config.power_kw,
            "duration_minutes": config.duration_minutes,
            "energy_kwh": config.energy_kwh,
            "measurement_entity": config.measurement_entity,
            "priority": config.priority,
            "pv_required": config.pv_required,
            "learning_enabled": config.learning_enabled,
            "manual_energy_kwh": config.manual_energy_kwh,
            "effective_energy_kwh": effective_energy_kwh(config, learning),
            "sample_count": int(learning.get("sample_count", 0) or 0),
            "avg_energy_kwh": round(float(learning.get("avg_energy_kwh", 0.0) or 0.0), 3),
            "avg_duration_minutes": round(float(learning.get("avg_duration_minutes", 0.0) or 0.0), 1),
            "avg_power_kw": round(float(learning.get("avg_power_kw", 0.0) or 0.0), 3),
            "last_run_end_utc": learning.get("last_run_end_utc"),
            "min_days": config.min_days,
            "max_days": config.max_days,
            "days_since_last_run": plan.get("days_since_last_run"),
            "due_status": plan.get("due_status"),
            "plan_status": plan.get("status"),
            "plan_source": plan.get("source"),
            "should_run": plan.get("should_run"),
            "chosen_start": plan.get("chosen_start"),
            "chosen_end": plan.get("chosen_end"),
            "expected_pv_energy_kwh": plan.get("expected_pv_energy_kwh"),
            "battery_available_kwh": plan.get("battery_available_kwh"),
            "tariff_window_start": plan.get("tariff_window_start"),
            "tariff_window_end": plan.get("tariff_window_end"),
            "tariff_avg_price_chf_per_kwh": plan.get("tariff_avg_price_chf_per_kwh"),
        }


class PeriodCostSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity, RestoreEntity):
    _attr_native_unit_of_measurement = "CHF"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: TariffSaverCoordinator,
        period: str,
        flavor: str,
        unique_suffix: str,
        name: str,
        icon: str = "mdi:cash",
        state_class: str = "total",
    ) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.period = period
        self.flavor = flavor
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._attr_unique_id = unique_suffix
        self._attr_device_info = _device_info(entry)
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_store_update() -> None:
            self.async_write_ha_state()

        self._unsub = async_dispatcher_connect(
            self.hass, f"{SIGNAL_STORE_UPDATED}_{self.entry.entry_id}", _on_store_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    def _get_breakdown(self) -> dict[str, dict[str, float]] | None:
        store: TariffSaverStore | None = getattr(self.coordinator, "store", None)
        if store is None:
            return None
        fn = getattr(store, f"compute_{self.period}_breakdown", None)
        if fn is None:
            return None
        try:
            out = fn()
            return out if isinstance(out, dict) else None
        except Exception:
            return None

    @property
    def native_value(self) -> float | None:
        breakdown = self._get_breakdown()
        if not breakdown:
            return None
        bucket = breakdown.get(self.flavor)
        if not isinstance(bucket, dict):
            return None
        return round(TariffSaverStore.all_in_from_components(bucket), 2)


class TariffSaverLinkStatusSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Link status"
    _attr_icon = "mdi:link-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_link_status"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        value = getattr(self.coordinator, "link_status", None)
        return str(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        url = getattr(self.coordinator, "linking_url", None)
        return {"linking_url": url} if url else {}


class TariffSaverLastApiSuccessSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Last API success"
    _attr_icon = "mdi:cloud-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_api_success"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return None
        ts = getattr(store, "last_api_success_utc", None)
        if isinstance(ts, datetime):
            return dt_util.as_utc(ts)
        return None


class BaseLoadSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Base load daily"
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_state_class = "measurement"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_base_load_daily"
        self._attr_device_info = _device_info(entry)
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_store_update() -> None:
            self.async_write_ha_state()

        self._unsub = async_dispatcher_connect(
            self.hass, f"{SIGNAL_STORE_UPDATED}_{self.entry.entry_id}", _on_store_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    @property
    def native_value(self) -> float | None:
        store = getattr(self.coordinator, "store", None)
        if store is None or not store.base_load_daily:
            return None
        last = store.base_load_daily[-1]
        value = last.get("base_load_kwh")
        return round(float(value), 3) if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return {}
        last = store.base_load_daily[-1] if store.base_load_daily else {}
        averages = store.get_base_load_season_averages()
        # Season from configured entity or auto-detect
        from .const import CONF_SEASON_ENTITY
        season_entity = str(
            self.entry.options.get(CONF_SEASON_ENTITY, self.entry.data.get(CONF_SEASON_ENTITY, "")) or ""
        ).strip()
        current_season = None
        if season_entity:
            ss = self.hass.states.get(season_entity)
            if ss and ss.state not in ("unavailable", "unknown", ""):
                current_season = ss.state
        if not current_season:
            import datetime as _dtmod
            month = _dtmod.datetime.now().month
            current_season = {12:"Winter",1:"Winter",2:"Winter",3:"Frühling",4:"Frühling",5:"Frühling",6:"Sommer",7:"Sommer",8:"Sommer",9:"Herbst",10:"Herbst",11:"Herbst"}.get(month, "Unknown")
        season_profile = store.get_seasonal_load_profile(current_season)
        all_seasons = ("Winter", "Frühling", "Sommer", "Herbst")
        season_profiles = {s: store.get_seasonal_load_profile(s) for s in all_seasons}
        return {
            "last_date": last.get("date"),
            "last_total_kwh": last.get("emma_kwh"),
            "last_consumer_kwh": last.get("consumer_kwh"),
            "last_season": last.get("season"),
            "current_season": current_season,
            "season_averages_kwh": averages,
            "total_records": len(store.base_load_daily),
            "profile_records": len(store.load_profiles),
            "current_season_profile": season_profile,
            "profile_winter": season_profiles.get("Winter", {}),
            "profile_fruehling": season_profiles.get("Frühling", {}),
            "profile_sommer": season_profiles.get("Sommer", {}),
            "profile_herbst": season_profiles.get("Herbst", {}),
        }


class ConsumerConfigSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Exposes consumer configuration for dashboard cards."""
    _attr_has_entity_name = True
    _attr_name = "Consumer config"
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_consumer_config"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        consumers = self.entry.options.get("consumers", {})
        return sum(1 for c in consumers.values() if isinstance(c, dict) and c.get("enabled"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        consumers = self.entry.options.get("consumers", {})
        result = {}
        for slot in range(1, 11):
            cfg = consumers.get(str(slot), {})
            if not isinstance(cfg, dict):
                cfg = {}
            result[f"consumer_{slot}"] = {
                "enabled": cfg.get("enabled", False),
                "name": cfg.get("name", ""),
                "power_kw": cfg.get("power_kw", 0),
                "duration_minutes": cfg.get("duration_minutes", 0),
                "energy_kwh": cfg.get("energy_kwh", 0),
                "priority": cfg.get("priority", 5),
                "mode": cfg.get("mode", "auto"),
                "pv_required": cfg.get("pv_required", False),
                "max_grid_score": cfg.get("max_grid_score", 100),
                "tariff_only": cfg.get("tariff_only", False),
                "pv_opportunist": cfg.get("pv_opportunist", False),
                "min_runtime_minutes": cfg.get("min_runtime_minutes", 0),
                "run_order": cfg.get("run_order", 0),
                "learning_enabled": cfg.get("learning_enabled", True),
                "min_days": cfg.get("min_days", 0),
                "max_days": cfg.get("max_days", 0),
                "measurement_entity": cfg.get("measurement_entity", ""),
            }
        # General settings
        general = {}
        for key in (
            "consumption_energy_entity", "season_entity",
            "ekz_entry_id", "publish_time",
            "pv_forecast_entity", "pv_forecast_attribute",
            "battery_enabled", "battery_capacity_kwh", "battery_min_soc_percent", "battery_soc_entity",
            "feed_in_price_mode", "feed_in_fixed_price", "feed_in_price_entity",
            "ampel_pv_entity", "ampel_pv_threshold_kw", "ampel_score_good", "ampel_score_bad",
            "heating_wp_entity", "heating_wp_power_kw", "heating_temp_sensor_1", "heating_temp_sensor_2", "heating_temp_sensor_3", "heating_comfort_min", "heating_pv_max", "heating_seasons", "heating_wp_off_value", "heating_wp_ww_value", "heating_wp_heat_value", "heating_max_score", "heating_pv_wait_minutes", "heating_enabled", "heating_pv_surplus_entity", "pv_surplus_entity",
        ):
            val = self.entry.options.get(key, self.entry.data.get(key))
            if val is not None:
                general[key] = val
            else:
                general[key] = ""
        return {"consumers": result, "general": general, "entry_id": self.entry.entry_id}


class ActivityLogSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Exposes recent Tariff Saver activity as a sensor."""
    _attr_has_entity_name = True
    _attr_name = "Activity Log"
    _attr_icon = "mdi:history"
    _attr_should_poll = True

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_activity_log"
        self._attr_device_info = _device_info(entry)
        self._update_counter = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_activity_log_updated",
                self._handle_log_update,
            )
        )

    async def _handle_log_update(self, event) -> None:
        self._update_counter += 1
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        store = getattr(self.coordinator, "store", None)
        if not store or not store.activity_log:
            return "Keine Aktivität"
        latest = store.activity_log[0]
        return f"{latest.get('icon', '')} {latest.get('msg', '')}"[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        store = getattr(self.coordinator, "store", None)
        entries = store.activity_log if store else []
        return {
            "entries": entries,
            "count": len(entries),
        }


class StromAmpelSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Universal traffic-light sensor for electricity usage recommendations."""
    _attr_has_entity_name = True
    _attr_name = "Strom Ampel"
    _attr_icon = "mdi:traffic-light"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_strom_ampel"
        self._attr_device_info = _device_info(entry)

    def _get_config(self, key: str, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def native_value(self) -> str:
        return self._compute().get("state", "yellow")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._compute()

    def _compute(self) -> dict[str, Any]:
        from .const import (
            CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD,
            CONF_AMPEL_SCORE_GOOD, DEFAULT_AMPEL_SCORE_GOOD,
            CONF_AMPEL_SCORE_BAD, DEFAULT_AMPEL_SCORE_BAD,
            CONF_AMPEL_PV_ENTITY,
        )

        pv_threshold = float(self._get_config(CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD) or DEFAULT_AMPEL_PV_THRESHOLD)
        score_good = int(self._get_config(CONF_AMPEL_SCORE_GOOD, DEFAULT_AMPEL_SCORE_GOOD) or DEFAULT_AMPEL_SCORE_GOOD)
        score_bad = int(self._get_config(CONF_AMPEL_SCORE_BAD, DEFAULT_AMPEL_SCORE_BAD) or DEFAULT_AMPEL_SCORE_BAD)
        pv_entity = str(self._get_config(CONF_AMPEL_PV_ENTITY, "") or "").strip()

        # Get current PV power (production)
        pv_kw = 0.0
        if pv_entity:
            pv_state = self.hass.states.get(pv_entity)
            if pv_state:
                try:
                    val = float(pv_state.state)
                    pv_kw = val / 1000.0 if abs(val) > 100 else val  # auto-detect W vs kW
                except (ValueError, TypeError):
                    pass

        # Get real PV surplus
        from .const import CONF_PV_SURPLUS_ENTITY
        pv_surplus_entity = str(self._get_config(CONF_PV_SURPLUS_ENTITY, "") or "").strip()
        pv_surplus_kw = 0.0
        if pv_surplus_entity:
            pv_surplus_state = self.hass.states.get(pv_surplus_entity)
            if pv_surplus_state and pv_surplus_state.state not in ("unavailable", "unknown", ""):
                try:
                    val = float(pv_surplus_state.state)
                    pv_surplus_kw = val / 1000.0 if abs(val) > 100 else val
                except (ValueError, TypeError):
                    pass

        # Get current score (absolute: 0=hist.min, 50=baseline, 100=hist.max)
        score = _current_score_live(self.coordinator)
        if score is None:
            score = 50
        stats = self.coordinator.data.get("stats", {}) if isinstance(self.coordinator.data, dict) else {}

        # Get battery SOC
        from .const import CONF_BATTERY_SOC_ENTITY
        soc_entity = str(self._get_config(CONF_BATTERY_SOC_ENTITY, "") or "").strip()
        soc = 0.0
        if soc_entity:
            soc_state = self.hass.states.get(soc_entity)
            if soc_state:
                try:
                    soc = float(soc_state.state)
                except (ValueError, TypeError):
                    pass

        # Get day score for stars
        day_score = stats.get("day_score_0_100")
        if not isinstance(day_score, (int, float)):
            day_score = 50
        day_score = int(day_score)
        rating = round(((100 - day_score) / 100) * 5 * 2) / 2
        full_stars = int(rating)
        half_star = (rating - full_stars) == 0.5
        empty_stars = 5 - full_stars - (1 if half_star else 0)
        stars_str = "★" * full_stars + ("⯪" if half_star else "") + "☆" * empty_stars

        # Find next good window
        next_good = None
        next_good_str = ""
        active = self.coordinator.data.get("active", []) if isinstance(self.coordinator.data, dict) else []
        if active:
            from homeassistant.util import dt as dt_util
            now_utc = dt_util.utcnow()
            all_prices = [float(p) for p in (
                _slot_total_price(s.components_chf_per_kwh) for s in active
            ) if isinstance(p, (int, float)) and p > 0.10]
            if all_prices:
                p_min = min(all_prices)
                p_max = max(all_prices)
                p_range = p_max - p_min
                if p_range > 0:
                    for s in sorted(active, key=lambda x: x.start):
                        if s.start <= now_utc:
                            continue
                        p = _slot_total_price(s.components_chf_per_kwh)
                        if isinstance(p, (int, float)) and p > 0.10:
                            s_score = int(round(((p - p_min) / p_range) * 100))
                            if s_score <= score_good:
                                next_good = dt_util.as_local(s.start)
                                next_good_str = next_good.strftime("%H:%M")
                                break

        # Also check PV forecast for next good PV window
        next_pv_str = ""
        from .const import CONF_PV_FORECAST_ENTITY, CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE
        pv_forecast_entity = str(self._get_config(CONF_PV_FORECAST_ENTITY, "") or "").strip()
        pv_forecast_attr = str(self._get_config(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE) or DEFAULT_PV_FORECAST_ATTRIBUTE).strip()
        if pv_forecast_entity:
            forecast_state = self.hass.states.get(pv_forecast_entity)
            if forecast_state:
                forecast = forecast_state.attributes.get(pv_forecast_attr, [])
                if isinstance(forecast, list):
                    from homeassistant.util import dt as dt_util
                    now_ts = dt_util.utcnow().timestamp()
                    for slot in forecast:
                        if not isinstance(slot, dict):
                            continue
                        period = slot.get("period_start")
                        pv_est = slot.get("pv_estimate", 0)
                        if period and isinstance(pv_est, (int, float)) and pv_est >= pv_threshold:
                            try:
                                slot_ts = dt_util.parse_datetime(str(period))
                                if slot_ts and slot_ts.timestamp() > now_ts:
                                    next_pv_str = dt_util.as_local(slot_ts).strftime("%H:%M")
                                    break
                            except Exception:
                                pass

        # Determine state and lines
        state = "yellow"
        reason = "normal"
        line1 = ""
        line2 = ""

        if pv_surplus_kw >= pv_threshold:
            state = "green"
            reason = "pv_surplus"
            line1 = "Gratis Solarstrom!"
            line2 = str(round(pv_surplus_kw, 1)) + " kW Überschuss"
        elif pv_kw >= pv_threshold * 0.5 and soc >= 90:
            state = "green"
            reason = "pv_battery_full"
            line1 = "Akku voll + PV"
            line2 = "Akku " + str(int(soc)) + "%, PV " + str(round(pv_kw, 1)) + " kW"
        elif score <= score_good:
            state = "green"
            reason = "cheap_grid"
            line1 = "Günstiger Zeitpunkt"
            line2 = "Score " + str(score)
        elif score >= score_bad:
            state = "red"
            reason = "expensive"
            if next_pv_str:
                line1 = "Warten - PV ab " + next_pv_str
                line2 = "Score " + str(score) + " (teuer)"
            elif next_good_str:
                line1 = "Warten - günstig ab " + next_good_str
                line2 = "Score " + str(score) + " (teuer)"
            else:
                line1 = "Strom gerade teuer"
                line2 = "Score " + str(score)
        else:
            state = "yellow"
            reason = "normal"
            if next_pv_str:
                line1 = "PV ab " + next_pv_str + " besser"
            elif next_good_str:
                line1 = "Günstig ab " + next_good_str
            else:
                line1 = "Wenn nötig, jetzt OK"
            line2 = "Score " + str(score)

        line3 = "Score " + str(score) + "  Heute Score " + str(day_score)

        return {
            "state": state,
            "line1": line1,
            "line2": line2,
            "line3": line3,
            "reason": reason,
            "score": score,
            "day_score": day_score,
            "pv_kw": round(pv_kw, 1),
            "soc": round(soc, 0),
            "next_good": next_good_str,
            "next_pv": next_pv_str,
            "stars": stars_str,
        }
