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

from .const import DOMAIN
from .coordinator import PriceSlot, TariffSaverCoordinator
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


def _pv(coordinator: TariffSaverCoordinator) -> dict[str, Any]:
    data = coordinator.data or {}
    return data.get("pv", {}) if isinstance(data, dict) else {}


def _slot_plan(coordinator: TariffSaverCoordinator) -> list[dict[str, Any]]:
    data = coordinator.data or {}
    return data.get("slot_plan", []) if isinstance(data, dict) else []


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


def _all_in_from_slot(slot: PriceSlot | None) -> float | None:
    if not slot:
        return None
    total = TariffSaverStore.sum_components(slot.components_chf_per_kwh, IMPORT_ALLIN_COMPONENTS)
    if total > 0:
        return total
    comps = slot.components_chf_per_kwh or {}
    for key in ("integrated", "all_in"):
        value = comps.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    return None


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

    entities: list[SensorEntity] = [
        TariffSaverPriceCurveSensor(coordinator, entry),
        TariffSaverPriceNowSensor(coordinator, entry),
        TariffSaverBaselinePriceNowSensor(coordinator, entry),
        TariffSaverNextPriceSensor(coordinator, entry),
        TariffSaverScoreNowSensor(coordinator, entry),
        TariffSaverScoreStarsSensor(coordinator, entry, 10),
        TariffSaverDayScoreSensor(coordinator, entry),
        TariffSaverDayScoreStarsSensor(coordinator, entry, 5),
        TariffSaverPvForecastCurveSensor(coordinator, entry),
        TariffSaverPvForecastRemainingSensor(coordinator, entry),
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

    for hours in WINDOW_HOURS:
        entities.append(BestWindowStartSensor(coordinator, entry, hours))
        entities.append(BestWindowScoreSensor(coordinator, entry, hours))

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
        slot_plan_map = {
            slot.get("start"): slot
            for slot in _slot_plan(self.coordinator)
            if isinstance(slot, dict) and isinstance(slot.get("start"), datetime)
        }
        return {
            "interval_minutes": 30,
            "slot_count": len(active),
            "slots": [
                {
                    "start": slot.start.isoformat(),
                    "price_all_in_chf_per_kwh": _all_in_from_slot(slot),
                    "baseline_chf_per_kwh": _all_in_from_slot(baseline_map.get(slot.start)),
                    "pv_estimate_kw": (slot_plan_map.get(slot.start) or {}).get("pv_estimate_kw", 0.0),
                    "pv_energy_kwh": (slot_plan_map.get(slot.start) or {}).get("pv_energy_kwh", 0.0),
                }
                for slot in active
            ],
        }


class TariffSaverPriceNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Price now 30m"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:cash"

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

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_score_now"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        value = _stats(self.coordinator).get("current_score_0_100")
        return int(value) if isinstance(value, int) else None


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


class TariffSaverPvForecastCurveSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "PV forecast curve"
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pv_forecast_curve"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        value = _pv(self.coordinator).get("slot_count")
        return int(value) if isinstance(value, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pv = _pv(self.coordinator)
        slots = pv.get("slots") or []
        return {
            "configured": pv.get("configured"),
            "entity_id": pv.get("entity_id"),
            "attribute": pv.get("attribute"),
            "interval_minutes": pv.get("interval_minutes"),
            "remaining_energy_kwh": pv.get("remaining_energy_kwh"),
            "slots": [
                {
                    "start": slot.get("start").isoformat() if isinstance(slot.get("start"), datetime) else slot.get("start"),
                    "pv_power_kw": slot.get("pv_power_kw"),
                    "pv_energy_kwh": slot.get("pv_energy_kwh"),
                }
                for slot in slots
            ],
        }


class TariffSaverPvForecastRemainingSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "PV forecast remaining"
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:weather-sunny"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pv_forecast_remaining"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        value = _pv(self.coordinator).get("remaining_energy_kwh")
        return round(float(value), 3) if isinstance(value, (int, float)) and value >= 0 else None


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
