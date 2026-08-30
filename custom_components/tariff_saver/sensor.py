"""Sensor platform for Tariff Saver."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
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

from .const import CONSUMER_COUNT, CONF_CONSUMERS, DOMAIN, SLOT_MINUTES, SLOTS_PER_HOUR, SLOTS_PER_DAY
from .coordinator import PriceSlot, TariffSaverCoordinator, _slot_total_price
from .models import ConsumerConfig
from .consumer_helpers import build_consumer_plan, effective_energy_kwh, get_consumer_config
from .storage import IMPORT_ALLIN_COMPONENTS, TariffSaverStore

_LOGGER = logging.getLogger(__name__)

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
    """Get baseline price for current season from config settings (netto CHF/kWh)."""
    from homeassistant.util import dt as dt_util
    from .const import (
        CONF_BASELINE_WINTER, CONF_BASELINE_FRUEHLING,
        CONF_BASELINE_SOMMER, CONF_BASELINE_HERBST,
        DEFAULT_BASELINE_WINTER, DEFAULT_BASELINE_FRUEHLING,
        DEFAULT_BASELINE_SOMMER, DEFAULT_BASELINE_HERBST,
    )
    config = coordinator.config or {}
    month = dt_util.now().month
    if month in (12, 1, 2):
        return float(config.get(CONF_BASELINE_WINTER, DEFAULT_BASELINE_WINTER))
    elif month in (3, 4, 5):
        return float(config.get(CONF_BASELINE_FRUEHLING, DEFAULT_BASELINE_FRUEHLING))
    elif month in (6, 7, 8):
        return float(config.get(CONF_BASELINE_SOMMER, DEFAULT_BASELINE_SOMMER))
    else:
        return float(config.get(CONF_BASELINE_HERBST, DEFAULT_BASELINE_HERBST))


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


def _stars_display_from_value(value: int | float, scale: int = 5) -> str:
    """Return HTML string with MDI star icons for a given star value (e.g. 3.5 of 5)."""
    value = max(0.0, min(float(scale), float(value)))
    full = int(value)
    has_half = (value - full) >= 0.5
    empty = scale - full - (1 if has_half else 0)
    sf = '<font color="#FFD700"><ha-icon icon="mdi:star"></ha-icon></font>'
    sh = '<font color="#FFD700"><ha-icon icon="mdi:star-half-full"></ha-icon></font>'
    se = '<font color="#555"><ha-icon icon="mdi:star-outline"></ha-icon></font>'
    return (sf * full) + (sh if has_half else "") + (se * empty)


def _stars_display(score: int | None, scale: int = 5) -> str | None:
    """Return HTML string with MDI star icons (full, half, empty) colored gold/grey."""
    if score is None:
        return None
    rating = round(((100 - score) / 100) * scale * 2) / 2  # half-star resolution
    rating = max(0.0, min(float(scale), rating))
    full = int(rating)
    has_half = (rating - full) == 0.5
    empty = scale - full - (1 if has_half else 0)
    sf = '<font color="#FFD700"><ha-icon icon="mdi:star"></ha-icon></font>'
    sh = '<font color="#FFD700"><ha-icon icon="mdi:star-half-full"></ha-icon></font>'
    se = '<font color="#555"><ha-icon icon="mdi:star-outline"></ha-icon></font>'
    return (sf * full) + (sh if has_half else "") + (se * empty)


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
                # Gedrosselt: Samples kommen ~alle 30 s, ein Voll-Save des
                # Multi-MB-Stores pro Sample stallt Loop + Disk.
                store.schedule_save()
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
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "_on_energy_change", _err)

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
        """Poll measurement_entities — aber NUR wenn Consumer aktuell laufen soll.

        Sonst misst ein gemeinsam genutzter Sensor (z.B. WP-Kreis Shelly für Slot 1+10)
        permanent Energy vom Haupt-WP → Fake-Runs → Learning zumüllt →
        ``last_run_end_utc`` fälschlich gesetzt → Consumer wird „waiting_window" obwohl
        heute noch gar nicht gelaufen. Fix seit 2026-04-21: samplen nur wenn
        ``binary_sensor.tariff_saver_consumer_X_should_run == on``.
        """
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
            # Nur samplen während der Consumer aktiv laufen soll (oder gerade PV-Opp ist)
            should_run_state = hass.states.get(f"binary_sensor.tariff_saver_consumer_{slot}_should_run")
            if should_run_state is None or should_run_state.state != "on":
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
            store.schedule_save()
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
    entities.append(EnergyUntilPvSensor(coordinator, entry))
    entities.append(PvUeberschussVerfuegbarSensor(coordinator, entry))
    entities.append(StromAmpelSensor(coordinator, entry))
    entities.append(BatteryManagerSensor(coordinator, entry))
    entities.append(ActivityLogSensor(coordinator, entry))
    entities.append(BillingSensor(coordinator, entry))
    entities.append(WallboxTargetMaxSensor(coordinator, entry))
    entities.append(WallboxPvReserveSensor(coordinator, entry))
    entities.append(WallboxSessionModeSensor(coordinator, entry))
    entities.append(LightAutoOffStatusSensor(coordinator, entry))
    entities.append(PvDumpStatusSensor(coordinator, entry))
    entities.append(StandbyWatchStatusSensor(coordinator, entry))

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
        baseline = _get_baseline_price(self.coordinator)
        return {
            "interval_minutes": SLOT_MINUTES,
            "slot_count": len(active),
            "baseline_chf_per_kwh": round(baseline, 6) if baseline else None,
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
        value = _get_baseline_price(self.coordinator)
        return round(float(value), 6) if isinstance(value, (int, float)) and value > 0 else None


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
            "stars_display": _stars_display(_current_score_live(self.coordinator), 5),
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
            "stars_display": _stars_display(stats.get("day_score_0_100"), 5),
            "tomorrow_avg_chf_per_kwh": stats.get("tomorrow_avg_chf_per_kwh"),
            "tomorrow_score_0_100": stats.get("tomorrow_score_0_100"),
            "tomorrow_slot_count": stats.get("tomorrow_slot_count"),
            "tomorrow_stars_display": _stars_display(stats.get("tomorrow_score_0_100"), 5),
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

    def _get_plan(self, config, learning) -> dict[str, Any]:
        """Vorberechneten Plan aus dem Store lesen (gleiche Quelle wie der
        binary_sensor). Nur pv_opportunist-Consumer brauchen eine Live-Berechnung
        (Echtzeit-PV-Check); alle anderen lesen das Scheduler-Ergebnis aus
        coordinator.data['consumer_plans'] — sonst würde dieser Display-Sensor
        bei JEDEM State-Write die volle O(n²)-Scheduler-Optimierung neu rechnen
        (~0,5 s im Event-Loop)."""
        # Bevorzugt den effektiven Plan, den binary_sensor._get_plan am Coordinator
        # ablegt: nur der kennt den PV-Notlauf (Zustandsmaschine mit globaler Sperre,
        # darf hier nicht ein zweites Mal laufen). Erst nach einem Neustart, bevor der
        # binary_sensor das erste Mal gerechnet hat, fehlt der Eintrag - dann gilt
        # unveraendert der bisherige Weg.
        effective = getattr(self.coordinator, "effective_plans", None) or {}
        published = effective.get(self.slot) or effective.get(str(self.slot))
        if published is not None:
            return published
        if not config.enabled:
            return {"status": "disabled", "should_run": False}
        if config.pv_opportunist:
            return build_consumer_plan(
                self.hass, self.entry, config, learning, _active_slots(self.coordinator)
            )
        plans = (self.coordinator.data or {}).get("consumer_plans", {})
        plan = plans.get(self.slot) or plans.get(str(self.slot))
        if plan is not None:
            return plan
        if config.trigger_entity:
            return {"status": "waiting_trigger", "should_run": False, "source": "on_demand"}
        if config.kind == "session":
            return {"status": "waiting_session", "should_run": False, "source": "session"}
        return {"status": "error_no_plan", "should_run": False}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = get_consumer_config(self.entry, self.slot)
        store = getattr(self.coordinator, "store", None)
        learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
        plan = self._get_plan(config, learning)

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
            "expected_pv_energy_kwh": plan.get("expected_pv_energy_kwh", plan.get("expected_energy_kwh")),
            "battery_available_kwh": plan.get("battery_available_kwh"),
            "tariff_window_start": plan.get("tariff_window_start"),
            "tariff_window_end": plan.get("tariff_window_end"),
            "tariff_avg_price_chf_per_kwh": plan.get("tariff_avg_price_chf_per_kwh"),
            "pv_windows": plan.get("pv_windows"),
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
        # Alle Saison-Profile in EINEM Scan über load_profiles bauen, statt
        # get_seasonal_load_profile() 5× aufzurufen (je ein Full-Scan).
        season_profiles = store.get_all_seasonal_load_profiles()
        season_profile = season_profiles.get(current_season, {})
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
        for slot in range(1, CONSUMER_COUNT + 1):
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
                "kind": cfg.get("kind", "daily_fixed"),
                "skip_next_run": cfg.get("skip_next_run", False),
                "pause_on_vacation": cfg.get("pause_on_vacation", False),
                "allowed_from": cfg.get("allowed_from", ""),
                "allowed_until": cfg.get("allowed_until", ""),
                "runtime_sensor": cfg.get("runtime_sensor", ""),
                "demand_entity": cfg.get("demand_entity", ""),
            }
        # General settings
        general = {}
        for key in (
            "consumption_energy_entity", "season_entity",
            "ekz_entry_id", "publish_time",
            "pv_forecast_entity", "pv_forecast_attribute",
            "battery_enabled", "battery_capacity_kwh", "battery_min_soc_percent", "battery_soc_entity", "battery_soc_margin_percent",
            "battery_round_trip_loss_percent", "battery_max_charge_kw", "battery_pv_charge_threshold_kw", "battery_pv_full_charge_ratio",
            "battery_charge_cost_tolerance_pct", "battery_device_id", "battery_hold_enter_score", "battery_hold_exit_score",
            "feed_in_price_mode", "feed_in_fixed_price", "feed_in_price_entity",
            "min_valid_price", "max_valid_price",
            "ampel_pv_entity", "ampel_pv_threshold_kw", "ampel_score_good", "ampel_score_bad",
            "heating_wp_entity", "heating_wp_power_kw", "heating_temp_sensor_1", "heating_temp_sensor_2", "heating_temp_sensor_3", "heating_comfort_min", "heating_pv_max", "heating_seasons", "heating_wp_off_value", "heating_wp_ww_value", "heating_wp_heat_value", "heating_max_score", "heating_max_score_absolute", "heating_frost_min", "heating_wp_min_runtime", "heating_pv_wait_minutes", "heating_enabled", "heating_pv_surplus_entity", "heating_block_entity", "pv_surplus_entity", "strom_sparen_score",
            "pool_filter_min_surplus_kw", "pool_wp_min_surplus_kw", "pool_dwell_minutes", "pool_wp_min_runtime",
            "flat_tariff_spread_rp", "flat_grid_earliest_hour",
            "pv_dump_enabled", "pv_dump_grid_out_entity", "pv_dump_grid_in_entity", "pv_dump_pv_power_entity", "pv_dump_on_watts", "pv_dump_off_watts", "pv_dump_dwell_seconds", "pv_dump_min_on_minutes", "pv_dump_pause_on_vacation",
            "standby_watch_enabled", "standby_watch_max_watts", "standby_watch_default_hours", "standby_watch_hours", "standby_watch_watts",
            "vacation_entity", "vacation_state",
            "light_helper_pattern", "light_auto_off_timeout_minutes",
            "ladeempfehlung_enabled", "ladeempfehlung_max_score", "ladeempfehlung_window_hours", "ladeempfehlung_min_pv_kwh",
            "emergency_pv_run_enabled",
        ):
            val = self.entry.options.get(key, self.entry.data.get(key))
            if val is not None:
                general[key] = val
            else:
                general[key] = ""
        # Effektive Defaults für Keys deren Default nicht falsy ist (Card zeigt sonst "aus"/"")
        if general.get("pv_dump_pause_on_vacation") == "":
            general["pv_dump_pause_on_vacation"] = True
        if general.get("vacation_state") == "":
            general["vacation_state"] = "on"
        if general.get("ladeempfehlung_enabled") == "":
            general["ladeempfehlung_enabled"] = True
        if general.get("emergency_pv_run_enabled") == "":
            general["emergency_pv_run_enabled"] = True
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


class PvUeberschussVerfuegbarSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """PV-Überschuss der für externe Verbraucher (z.B. Wallbox) verfügbar ist.

    Liest den konfigurierten PV-Surplus-Wert (normalerweise Netz-Einspeisung) —
    dieser reflektiert bereits alle aktuell laufenden Haus-Verbraucher inklusive
    aller von Tariff Saver gesteuerten Consumer (Heizboost, Akku-Grid-Laden,
    Motorrad-Erhaltung). Dient als Eingabe für loose gekoppelte Wallbox-Logik
    (huawei_solar) — Wallbox darf diesen Wert gefahrlos ziehen ohne andere
    Verbraucher zu stören.

    Einheit: Watt. Wert ≥ 0.
    """

    _attr_has_entity_name = True
    _attr_name = "PV Ueberschuss verfuegbar"
    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:solar-power-variant"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_should_poll = False

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_pv_ueberschuss_verfuegbar"
        self._attr_device_info = _device_info(entry)
        self._unsub_state: Any = None
        self._unsub_timer: Any = None

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _source_entity(self) -> str:
        from .const import CONF_PV_SURPLUS_ENTITY
        return str(self._cfg(CONF_PV_SURPLUS_ENTITY, "") or "").strip()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        src = self._source_entity()
        if src:
            self._unsub_state = async_track_state_change_event(
                self.hass, [src], self._on_source_changed
            )
        # Fallback-Timer: spätestens alle 15 Minuten neu auswerten
        self._unsub_timer = async_track_time_interval(
            self.hass, self._on_timer, timedelta(minutes=15)
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        await super().async_will_remove_from_hass()

    @callback
    def _on_source_changed(self, event: Event) -> None:
        self.async_write_ha_state()

    @callback
    def _on_timer(self, _now) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        src = self._source_entity()
        if not src:
            return None
        state = self.hass.states.get(src)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            val = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = str(state.attributes.get("unit_of_measurement", "") or "").lower()
        if unit == "kw":
            watts = val * 1000.0
        elif unit == "w":
            watts = val
        else:
            # Heuristik: |val| > 100 → wohl schon Watt, sonst Kilowatt
            watts = val if abs(val) > 100 else val * 1000.0
        return max(0, int(round(watts)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        src = self._source_entity()
        return {
            "source_entity": src or None,
            "note": (
                "Netto-Überschuss aus der konfigurierten PV-Surplus-Entity. "
                "Laufende TS-Consumer sind bereits im Hausverbrauch enthalten, "
                "somit direkt als Headroom für externe Verbraucher (z.B. Wallbox) nutzbar."
            ),
        }


class EnergyUntilPvSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Estimates total energy demand until PV production starts, including heat pump."""

    _attr_has_entity_name = True
    _attr_name = "Energy until PV"
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:battery-charging"
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_energy_until_pv"
        self._attr_device_info = _device_info(entry)

    def _cfg(self, key: str, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _compute(self) -> dict[str, Any]:
        from .const import (
            CONF_PV_FORECAST_ENTITY, CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE,
            CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD,
            CONF_HEATING_ENABLED, CONF_HEATING_SEASONS, DEFAULT_HEATING_SEASONS,
            CONF_HEATING_WP_POWER_KW, DEFAULT_HEATING_WP_POWER_KW,
            CONF_HEATING_MAX_SCORE, DEFAULT_HEATING_MAX_SCORE,
            CONF_BATTERY_ENABLED, CONF_BATTERY_CAPACITY_KWH,
            CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT,
            CONF_BATTERY_SOC_ENTITY, CONF_BATTERY_SOC_MARGIN, DEFAULT_BATTERY_SOC_MARGIN,
        )
        from .__init__ import _get_season

        now_utc = dt_util.utcnow()
        now_local = dt_util.as_local(now_utc)
        result = {
            "base_load_kwh": 0.0, "heating_kwh": 0.0, "consumer_kwh": 0.0,
            "total_kwh": 0.0, "pv_start_time": None, "slots_until_pv": 0,
            "heating_slots": 0, "available_kwh": 0.0, "missing_kwh": 0.0,
            "recommended_target_soc": 0,
        }

        # --- PV start time ---
        pv_threshold = float(self._cfg(CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD) or DEFAULT_AMPEL_PV_THRESHOLD)
        pv_entity = str(self._cfg(CONF_PV_FORECAST_ENTITY, "") or "").strip()
        pv_attr = str(self._cfg(CONF_PV_FORECAST_ATTRIBUTE, DEFAULT_PV_FORECAST_ATTRIBUTE) or DEFAULT_PV_FORECAST_ATTRIBUTE).strip()
        pv_start_utc = None

        # Try today forecast first, then tomorrow if no future PV slot found
        forecast_entities = [pv_entity]
        if pv_entity:
            # Derive tomorrow entity: replace "heute" with "morgen" or "_today" with "_tomorrow"
            tomorrow_entity = pv_entity.replace("heute", "morgen").replace("_today", "_tomorrow")
            if tomorrow_entity != pv_entity:
                forecast_entities.append(tomorrow_entity)

        for fc_entity in forecast_entities:
            if not fc_entity or pv_start_utc is not None:
                break
            state = self.hass.states.get(fc_entity)
            if not state:
                continue
            forecast = state.attributes.get(pv_attr, [])
            if not isinstance(forecast, list):
                continue
            for slot in forecast:
                if not isinstance(slot, dict):
                    continue
                period = slot.get("period_start")
                pv_est = slot.get("pv_estimate", 0)
                if period and isinstance(pv_est, (int, float)) and pv_est >= pv_threshold:
                    try:
                        slot_dt = dt_util.parse_datetime(str(period))
                        if slot_dt and slot_dt > now_utc:
                            pv_start_utc = slot_dt
                            break
                    except Exception as _err:
                        _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "break", _err)

        if pv_start_utc is None:
            # No PV in any forecast — use noon tomorrow as fallback
            noon_local = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
            if noon_local <= now_local:
                noon_local += timedelta(days=1)
            pv_start_utc = dt_util.as_utc(noon_local)

        pv_start_local = dt_util.as_local(pv_start_utc)
        result["pv_start_time"] = pv_start_local.isoformat()

        # --- Slot range: from now until PV start (SLOT_MINUTES granularity) ---
        now_idx = now_local.hour * SLOTS_PER_HOUR + now_local.minute // SLOT_MINUTES
        pv_idx = pv_start_local.hour * SLOTS_PER_HOUR + pv_start_local.minute // SLOT_MINUTES
        if pv_idx <= now_idx:
            pv_idx += SLOTS_PER_DAY
        slots_until_pv = pv_idx - now_idx
        result["slots_until_pv"] = slots_until_pv

        # --- Base load from season profile ---
        store = getattr(self.coordinator, "store", None)
        current_season = _get_season(self.hass, self.entry)
        profile = store.get_seasonal_load_profile(current_season) if store else {}
        # Fallback per slot = 0.3 kWh war Default für 30-min Slots → bei 15-min entsprechend halbiert
        fallback_per_slot = 0.3 / (SLOTS_PER_HOUR / 2)  # 0.15 bei 15-min
        base_load_kwh = 0.0
        for i in range(now_idx, now_idx + slots_until_pv):
            idx = i % SLOTS_PER_DAY
            base_load_kwh += float(profile.get(idx, fallback_per_slot))
        result["base_load_kwh"] = round(base_load_kwh, 1)

        # --- Heating (WP) consumption based on score per slot ---
        heating_kwh = 0.0
        heating_slots = 0
        heating_enabled = self._cfg(CONF_HEATING_ENABLED, False)
        seasons_str = str(self._cfg(CONF_HEATING_SEASONS, DEFAULT_HEATING_SEASONS) or DEFAULT_HEATING_SEASONS)
        active_seasons = [s.strip() for s in seasons_str.split(",") if s.strip()]
        wp_power_kw = float(self._cfg(CONF_HEATING_WP_POWER_KW, DEFAULT_HEATING_WP_POWER_KW) or DEFAULT_HEATING_WP_POWER_KW)
        max_score = int(float(self._cfg(CONF_HEATING_MAX_SCORE, DEFAULT_HEATING_MAX_SCORE) or DEFAULT_HEATING_MAX_SCORE))

        heating_active = bool(heating_enabled) and current_season == "Winter"

        if heating_active:
            # Get price data and historical min/max for score calculation
            data = self.coordinator.data or {}
            active_price_slots = data.get("active", []) if isinstance(data, dict) else []
            hist_min = store.historical_price_min if store else 999.0
            hist_max = store.historical_price_max if store else 0.0
            baseline = _get_baseline_price(self.coordinator)

            for i in range(now_idx, now_idx + slots_until_pv):
                idx = i % SLOTS_PER_DAY
                # Find the price slot matching this slot index
                slot_hour = idx // SLOTS_PER_HOUR
                slot_minute = (idx % SLOTS_PER_HOUR) * SLOT_MINUTES
                slot_start_local = now_local.replace(hour=slot_hour, minute=slot_minute, second=0, microsecond=0)
                if i >= SLOTS_PER_DAY:
                    slot_start_local += timedelta(days=1)
                slot_start_utc = dt_util.as_utc(slot_start_local)

                # Find matching price slot
                matching_price = None
                for ps in active_price_slots:
                    if ps.start <= slot_start_utc < ps.start + timedelta(minutes=15):
                        matching_price = _slot_total_price(ps.components_chf_per_kwh)
                        break
                    # Also check next 15-min slot (our slots are 30 min, price slots are 15 min)
                    if ps.start <= slot_start_utc + timedelta(minutes=15) < ps.start + timedelta(minutes=15):
                        if matching_price is None:
                            matching_price = _slot_total_price(ps.components_chf_per_kwh)

                if matching_price is not None and hist_min < 999.0 and hist_max > 0.0:
                    slot_score = _absolute_score(float(matching_price), hist_min, hist_max, baseline)
                else:
                    slot_score = 50  # Unknown → assume neutral

                # WP runs if score <= max_score (Teuer-Grenze)
                if slot_score <= max_score:
                    heating_kwh += wp_power_kw * (1.0 / SLOTS_PER_HOUR)
                    heating_slots += 1

        result["heating_kwh"] = round(heating_kwh, 1)
        result["heating_slots"] = heating_slots

        # --- Planned consumers running before PV ---
        consumer_kwh = 0.0
        pv_start_ts = pv_start_utc.timestamp()
        now_ts = now_utc.timestamp()
        for slot_nr in range(1, CONSUMER_COUNT + 1):
            config = get_consumer_config(self.entry, slot_nr)
            if not config.enabled:
                continue
            # Check if consumer has a plan that runs before PV
            bs = self.hass.states.get(f"binary_sensor.tariff_saver_consumer_{slot_nr}_should_run")
            if not bs or not bs.attributes:
                continue
            chosen_start = bs.attributes.get("chosen_start")
            chosen_end = bs.attributes.get("chosen_end")
            if not chosen_start or not chosen_end:
                continue
            try:
                cs = dt_util.parse_datetime(str(chosen_start))
                ce = dt_util.parse_datetime(str(chosen_end))
                if cs and ce and cs.timestamp() < pv_start_ts and ce.timestamp() > now_ts:
                    learning = store.get_consumer_learning(str(slot_nr)) if store else {}
                    energy = effective_energy_kwh(config, learning)
                    if energy and energy > 0:
                        consumer_kwh += energy
            except Exception as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "consumer_kwh += energy", _err)
        result["consumer_kwh"] = round(consumer_kwh, 1)

        # --- Totals ---
        total_kwh = base_load_kwh + heating_kwh + consumer_kwh
        result["total_kwh"] = round(total_kwh, 1)

        # --- Battery ---
        battery_enabled = self._cfg(CONF_BATTERY_ENABLED, False)
        capacity = float(self._cfg(CONF_BATTERY_CAPACITY_KWH, 0.0) or 0.0)
        min_soc = float(self._cfg(CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT) or DEFAULT_BATTERY_MIN_SOC_PERCENT)
        margin = float(self._cfg(CONF_BATTERY_SOC_MARGIN, DEFAULT_BATTERY_SOC_MARGIN) or DEFAULT_BATTERY_SOC_MARGIN)

        current_soc = 0.0
        soc_entity = str(self._cfg(CONF_BATTERY_SOC_ENTITY, "") or "").strip()
        if soc_entity:
            soc_state = self.hass.states.get(soc_entity)
            if soc_state:
                try:
                    current_soc = float(soc_state.state)
                except (ValueError, TypeError) as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "current_soc = float(soc_state.state)", _err)

        available_kwh = max(0.0, (current_soc - min_soc) / 100.0 * capacity) if battery_enabled and capacity > 0 else 0.0
        missing_kwh = max(0.0, total_kwh - available_kwh)
        result["available_kwh"] = round(available_kwh, 1)
        result["missing_kwh"] = round(missing_kwh, 1)

        # Target SOC
        if battery_enabled and capacity > 0:
            target = current_soc + (missing_kwh / capacity * 100.0) + margin
            target = max(12, min(100, int(round(target))))
        else:
            target = 0
        result["recommended_target_soc"] = target

        return result

    @property
    def native_value(self) -> float | None:
        return self._compute().get("total_kwh", 0.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._compute()


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
                except (ValueError, TypeError) as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "pv_kw = val / 1000.0 if abs(val) > 100 e", _err)

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
                except (ValueError, TypeError) as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "pv_surplus_kw = val / 1000.0 if abs(val)", _err)

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
                except (ValueError, TypeError) as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "soc = float(soc_state.state)", _err)

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
                            except Exception as _err:
                                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "break", _err)

        # Check strom_sparen
        strom_sparen = self.hass.states.get("binary_sensor.tariff_saver_strom_sparen")
        is_strom_sparen = strom_sparen and strom_sparen.state == "on"

        # Get energy_until_pv data for battery assessment
        from .const import CONF_AMPEL_BATTERY_MARGIN_KWH, DEFAULT_AMPEL_BATTERY_MARGIN_KWH
        battery_margin = float(self._get_config(CONF_AMPEL_BATTERY_MARGIN_KWH, DEFAULT_AMPEL_BATTERY_MARGIN_KWH) or DEFAULT_AMPEL_BATTERY_MARGIN_KWH)
        eup_state = self.hass.states.get("sensor.tariff_saver_energy_until_pv")
        available_kwh = 0.0
        total_need_kwh = 0.0
        battery_comfortable = False  # Akku reicht inkl. Marge
        battery_tight = False        # Akku reicht knapp (ohne Marge)
        if eup_state and eup_state.state not in ("unavailable", "unknown", ""):
            try:
                available_kwh = float(eup_state.attributes.get("available_kwh", 0))
                total_need_kwh = float(eup_state.attributes.get("total_kwh", 0))
                missing_kwh = float(eup_state.attributes.get("missing_kwh", 0))
                battery_comfortable = available_kwh >= (total_need_kwh + battery_margin)
                battery_tight = missing_kwh <= 0 and not battery_comfortable
            except (ValueError, TypeError) as _err:
                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "battery_tight = missing_kwh <= 0 and not", _err)

        # Build next-hint suffix
        next_hint = ""
        if next_pv_str:
            next_hint = " · PV ab " + next_pv_str
        elif next_good_str:
            next_hint = " · günstig ab " + next_good_str

        # Determine ampel state, reason, and ampel_stars (0-5)
        # Priority order: strom_sparen > pv_surplus > pv+battery_full > cheap > battery_comfortable > expensive+grid > expensive+battery > normal
        state = "yellow"
        reason = "normal"
        line1 = ""
        line2 = ""
        ampel_stars = 3  # default: neutral

        if is_strom_sparen:
            state = "red"
            reason = "strom_sparen"
            ampel_stars = 0
            line1 = "Strom sparen aktiv!"
            line2 = "Unwichtige Geräte aus"
        elif pv_surplus_kw >= pv_threshold:
            state = "green"
            reason = "pv_surplus"
            ampel_stars = 5
            line1 = "Gratis Solarstrom!"
            line2 = str(round(pv_surplus_kw, 1)) + " kW Überschuss"
        elif pv_kw >= pv_threshold * 0.5 and soc >= 90:
            state = "green"
            reason = "pv_battery_full"
            ampel_stars = 5
            line1 = "Akku voll + PV"
            line2 = "Akku " + str(int(soc)) + "%, PV " + str(round(pv_kw, 1)) + " kW"
        elif score <= score_good:
            state = "green"
            reason = "cheap_grid"
            ampel_stars = 5 if battery_comfortable else 4
            line1 = "Günstiger Zeitpunkt"
            line2 = "Score " + str(score) + next_hint
        elif battery_comfortable and score < score_bad:
            state = "green"
            reason = "battery_comfortable"
            ampel_stars = 4
            line1 = "Akku reicht — freie Fahrt"
            line2 = "Akku " + str(int(soc)) + "%" + next_hint
        elif battery_comfortable and score >= score_bad:
            state = "yellow"
            reason = "battery_expensive"
            ampel_stars = 3
            line1 = "Akku reicht, aber teuer"
            line2 = "Score " + str(score) + next_hint
        elif battery_tight and score < score_bad:
            state = "yellow"
            reason = "battery_tight"
            ampel_stars = 3
            line1 = "Akku reicht knapp"
            line2 = "Score " + str(score) + next_hint
        elif score >= score_bad:
            state = "orange" if battery_tight else "red"
            reason = "expensive_grid" if not battery_tight else "expensive_tight"
            ampel_stars = 2 if battery_tight else 1
            if next_pv_str:
                line1 = "Warten — PV ab " + next_pv_str
            elif next_good_str:
                line1 = "Warten — günstig ab " + next_good_str
            else:
                line1 = "Strom gerade teuer"
            line2 = "Score " + str(score)
        else:
            state = "yellow"
            reason = "normal"
            ampel_stars = 3
            if next_pv_str:
                line1 = "PV ab " + next_pv_str + " besser"
            elif next_good_str:
                line1 = "Günstig ab " + next_good_str
            else:
                line1 = "Wenn nötig, jetzt OK"
            line2 = "Score " + str(score) + next_hint

        # Ampel stars display (HTML with MDI icons)
        ampel_stars_display = _stars_display_from_value(ampel_stars, 5)

        # line3: ampel stars
        line3 = ampel_stars_display

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
            "available_kwh": round(available_kwh, 1),
            "total_need_kwh": round(total_need_kwh, 1),
            "battery_comfortable": battery_comfortable,
            "next_good": next_good_str,
            "next_pv": next_pv_str,
            "ampel_stars": ampel_stars,
            "ampel_stars_display": ampel_stars_display,
            "tariff_stars_display": _stars_display(score, 5),
            "day_stars_display": _stars_display(day_score, 5),
        }


class BatteryManagerSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Exposes the current battery management mode and state."""

    _attr_has_entity_name = True
    _attr_name = "Battery Manager"
    _attr_icon = "mdi:battery-sync"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_battery_manager"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str:
        state = self._get_battery_state()
        return state.get("mode", "normal")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._get_battery_state()

        # Read live values
        from .const import CONF_BATTERY_SOC_ENTITY
        config = self.entry.options if self.entry.options else self.entry.data
        soc_entity = str(config.get(CONF_BATTERY_SOC_ENTITY, "") or "").strip()
        current_soc = None
        if soc_entity:
            soc_state = self.hass.states.get(soc_entity)
            if soc_state and soc_state.state not in ("unavailable", "unknown", ""):
                try:
                    current_soc = round(float(soc_state.state), 1)
                except (ValueError, TypeError) as _err:
                    _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "extra_state_attributes", _err)

        score = _current_score_live(self.coordinator)

        # Forcible charge status
        forcible = self.hass.states.get("sensor.batteries_forcible_charge")
        forcible_state = forcible.state if forcible else "unknown"

        # Consumer 9 plan
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        plans = data.get("consumer_plans", {})
        plan_9 = plans.get(9, plans.get("9", {}))

        # Battery stats
        store = getattr(self.coordinator, "store", None)
        today_str = dt_util.now().strftime("%Y-%m-%d")
        today_stats = {}
        stats_summary = {}
        if store:
            today_stats = store.battery_stats.get(today_str, {})
            stats_summary = store.get_battery_stats_summary()

        return {
            "mode": state.get("mode", "normal"),
            "reason": state.get("reason", ""),
            "mode_since": state.get("mode_since", ""),
            "soc": current_soc,
            "score": score,
            "forcible_charge": forcible_state,
            "grid_charge_plan": plan_9.get("status", ""),
            "grid_charge_window": plan_9.get("chosen_start", ""),
            "battery_case": plan_9.get("battery_case", plan_9.get("source", "")),
            "today_grid_kwh": today_stats.get("grid_charge_kwh", 0.0),
            "today_grid_cost_chf": today_stats.get("grid_charge_cost_chf", 0.0),
            "today_hold_minutes": today_stats.get("hold_minutes", 0),
            "today_case": today_stats.get("case", ""),
            "stats_days": stats_summary.get("days", 0),
            "stats_total_grid_kwh": stats_summary.get("total_grid_kwh", 0.0),
            "stats_total_grid_cost": stats_summary.get("total_grid_cost", 0.0),
            "stats_avg_price": stats_summary.get("avg_price", 0.0),
            "stats_total_hold_min": stats_summary.get("total_hold_min", 0),
        }

    def _get_battery_state(self) -> dict:
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        return data.get("battery_state", {})


class BillingSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Billing sensor: current period costs with MWST, fixed costs, and comparisons."""

    _attr_has_entity_name = True
    _attr_name = "Billing"
    _attr_native_unit_of_measurement = "CHF"
    _attr_icon = "mdi:receipt-text"
    _attr_should_poll = True

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_billing"
        self._attr_device_info = _device_info(entry)

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _get_period_dates(self) -> tuple[date, date]:
        """Calculate current billing period start and end dates."""
        from .const import CONF_BILLING_PERIOD_MONTHS, DEFAULT_BILLING_PERIOD_MONTHS, CONF_BILLING_START_DATE
        period_months = int(self._cfg(CONF_BILLING_PERIOD_MONTHS, DEFAULT_BILLING_PERIOD_MONTHS) or DEFAULT_BILLING_PERIOD_MONTHS)
        start_str = str(self._cfg(CONF_BILLING_START_DATE, "") or "").strip()

        today = dt_util.now().date()

        if start_str:
            parsed = dt_util.parse_date(start_str)
            if parsed:
                # Roll forward to current period
                period_start = parsed
                while True:
                    if period_months == 1:
                        if period_start.month == 12:
                            next_start = period_start.replace(year=period_start.year + 1, month=1)
                        else:
                            next_start = period_start.replace(month=period_start.month + 1)
                    else:
                        month = period_start.month + period_months
                        year = period_start.year
                        while month > 12:
                            month -= 12
                            year += 1
                        next_start = period_start.replace(year=year, month=month)
                    if next_start > today:
                        return period_start, next_start
                    period_start = next_start
        # Fallback: current quarter
        quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        period_start = today.replace(month=quarter_start_month, day=1)
        month = quarter_start_month + period_months
        year = period_start.year
        while month > 12:
            month -= 12
            year += 1
        period_end = date(year, month, 1)
        return period_start, period_end

    def _compute_fixed_costs(self, period_start: date, today: date, period_end: date) -> tuple[float, list]:
        """Compute fixed costs proportional to elapsed days."""
        from .const import CONF_BILLING_FIXED_COSTS
        import json as _json
        raw = self._cfg(CONF_BILLING_FIXED_COSTS, "[]")
        if isinstance(raw, str):
            try:
                items = _json.loads(raw)
            except Exception:
                items = []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []

        total = 0.0
        details = []
        months_in_period = (period_end.year - period_start.year) * 12 + period_end.month - period_start.month

        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            amount = float(item.get("amount", 0.0) or 0.0)
            period_type = str(item.get("period", "monthly")).strip()
            if not label or amount <= 0:
                continue

            if period_type == "monthly":
                # Full amount per month × number of months in period
                cost = amount * months_in_period
            else:
                # Once per billing period
                cost = amount

            total += cost
            details.append({"label": label, "amount_chf": round(cost, 2), "period": period_type})

        return round(total, 2), details

    @property
    def native_value(self) -> float | None:
        store = self.coordinator.store
        if store is None:
            return None
        from .const import CONF_MWST_PERCENT, DEFAULT_MWST_PERCENT
        period_start, period_end = self._get_period_dates()
        today = dt_util.now().date()
        start_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(period_start, time(0, 0), tzinfo=dt_util.now().tzinfo)))
        end_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(min(today + timedelta(days=1), period_end), time(0, 0), tzinfo=dt_util.now().tzinfo)))
        summary = store.compute_period_summary(start_dt, end_dt)
        fixed, _ = self._compute_fixed_costs(period_start, today, period_end)
        subtotal = summary["dyn_chf"] + fixed
        mwst_pct = float(self._cfg(CONF_MWST_PERCENT, DEFAULT_MWST_PERCENT) or DEFAULT_MWST_PERCENT)
        total = subtotal * (1 + mwst_pct / 100)
        return round(total, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        store = self.coordinator.store
        if store is None:
            return {}
        from .const import (
            CONF_MWST_PERCENT, DEFAULT_MWST_PERCENT,
            CONF_BILLING_PERIOD_MONTHS, DEFAULT_BILLING_PERIOD_MONTHS,
        )

        period_start, period_end = self._get_period_dates()
        today = dt_util.now().date()
        period_days = (period_end - period_start).days
        elapsed_days = (today - period_start).days + 1

        # Current period
        start_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(period_start, time(0, 0), tzinfo=dt_util.now().tzinfo)))
        end_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(min(today + timedelta(days=1), period_end), time(0, 0), tzinfo=dt_util.now().tzinfo)))
        summary = store.compute_period_summary(start_dt, end_dt)

        fixed, fixed_details = self._compute_fixed_costs(period_start, today, period_end)
        subtotal = summary["dyn_chf"] + fixed
        mwst_pct = float(self._cfg(CONF_MWST_PERCENT, DEFAULT_MWST_PERCENT) or DEFAULT_MWST_PERCENT)
        mwst_chf = subtotal * (mwst_pct / 100)
        total = subtotal + mwst_chf

        # Projection
        if elapsed_days > 0:
            projected_total = total * (period_days / elapsed_days)
            projected_kwh = summary["kwh"] * (period_days / elapsed_days)
        else:
            projected_total = 0.0
            projected_kwh = 0.0

        # Previous year same month
        prev_year_start = today.replace(year=today.year - 1, month=today.month, day=1)
        if prev_year_start.month == 12:
            prev_year_end = prev_year_start.replace(year=prev_year_start.year + 1, month=1)
        else:
            prev_year_end = prev_year_start.replace(month=prev_year_start.month + 1)
        prev_start_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(prev_year_start, time(0, 0), tzinfo=dt_util.now().tzinfo)))
        prev_end_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(prev_year_end, time(0, 0), tzinfo=dt_util.now().tzinfo)))
        prev_summary = store.compute_period_summary(prev_start_dt, prev_end_dt)
        prev_days = (prev_year_end - prev_year_start).days
        prev_avg_kwh = prev_summary["kwh"] / prev_days if prev_days > 0 and prev_summary["slots"] > 0 else None
        prev_avg_cost = prev_summary["dyn_chf"] / prev_days if prev_days > 0 and prev_summary["slots"] > 0 else None

        # Year average (last 365 days)
        year_start = today - timedelta(days=365)
        year_start_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(year_start, time(0, 0), tzinfo=dt_util.now().tzinfo)))
        year_end_dt = dt_util.as_local(dt_util.as_utc(datetime.combine(today + timedelta(days=1), time(0, 0), tzinfo=dt_util.now().tzinfo)))
        year_summary = store.compute_period_summary(year_start_dt, year_end_dt)
        year_avg_kwh = year_summary["kwh"] / 365 if year_summary["slots"] > 0 else None
        year_avg_cost = year_summary["dyn_chf"] / 365 if year_summary["slots"] > 0 else None

        return {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "period_day": elapsed_days,
            "period_days": period_days,
            "consumption_kwh": summary["kwh"],
            "cost_netto_chf": round(summary["dyn_chf"], 2),
            "baseline_netto_chf": round(summary["base_chf"], 2),
            "savings_netto_chf": round(summary["savings_chf"], 2),
            "savings_percent": summary["savings_percent"],
            "fixed_costs_netto_chf": fixed,
            "fixed_costs_details": fixed_details,
            "subtotal_netto_chf": round(subtotal, 2),
            "mwst_percent": mwst_pct,
            "mwst_chf": round(mwst_chf, 2),
            "total_brutto_chf": round(total, 2),
            "projected_consumption_kwh": round(projected_kwh, 1),
            "projected_total_brutto_chf": round(projected_total, 2),
            "prev_year_month_avg_kwh_per_day": round(prev_avg_kwh, 1) if prev_avg_kwh else None,
            "prev_year_month_avg_cost_per_day_chf": round(prev_avg_cost, 2) if prev_avg_cost else None,
            "year_avg_kwh_per_day": round(year_avg_kwh, 1) if year_avg_kwh else None,
            "year_avg_cost_per_day_chf": round(year_avg_cost, 2) if year_avg_cost else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Wallbox Session Sensors (TS v2 — Phase 2 Sub-Phase 2c)
#  Status-Sensoren die huawei_solar v2 liest. Bis Scheduler v2 (2d) live ist
#  geben sie Default-Werte zurück — keine echte Plan-Berechnung.
# ─────────────────────────────────────────────────────────────────────────────


class _WallboxSensorBase(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, key: str, name: str) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = _device_info(entry)

    def _active_session(self) -> dict[str, Any] | None:
        store = getattr(self.coordinator, "store", None)
        if store is None or not hasattr(store, "get_active_sessions"):
            return None
        sessions = store.get_active_sessions()
        for s in sessions.values():
            if isinstance(s, dict) and s.get("active"):
                return s
        return None

    def _active_session_plan(self) -> dict[str, Any] | None:
        """Liefert den Scheduler-v2-Plan für die aktive Session (aus store.consumer_plans)."""
        store = getattr(self.coordinator, "store", None)
        if store is None or not store.consumer_plans:
            return None
        for slot_key, plan in store.consumer_plans.items():
            if isinstance(plan, dict) and plan.get("kind") == "session":
                return plan
        return None


class WallboxTargetMaxSensor(_WallboxSensorBase):
    """Aktueller Power-Cap für die Wallbox in W. 0 = nicht laden.

    Bis Scheduler v2 live ist:
    - Session aktiv mit Deadline → max_power_w (TS plant noch nicht, gibt nur das Limit weiter)
    - Session aktiv ohne Deadline → 0 (huawei_solar v1 fällt auf eigene PV-Logik zurück)
    - Keine Session → 0
    """

    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:ev-station"
    # Spec (ladeplanung_design.md 5.5.7) nutzt `_w`-Suffix — HA strippt das sonst wegen unit=W
    entity_id = "sensor.tariff_saver_wallbox_target_max_w"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "wallbox_target_max_w", "Wallbox Target Max")

    @property
    def native_value(self) -> int | None:
        s = self._active_session()
        if not s:
            return 0
        if not s.get("deadline_utc"):
            return 0  # pure PV-only-Modus → huawei_solar v1
        # Scheduler v2: target_now_w aus Plan lesen
        plan = self._active_session_plan()
        if plan is not None:
            return int(plan.get("target_now_w", 0) or 0)
        # Fallback (kein Plan berechnet noch): max_power_w
        return int(s.get("max_power_w", 0) or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._active_session() or {}
        plan = self._active_session_plan() or {}
        return {
            "session_active": bool(s.get("active", False)),
            "session_slot": s.get("slot"),
            "session_remaining_kwh": s.get("energy_needed_kwh"),
            "session_deadline": s.get("deadline_utc"),
            "session_min_power_w": s.get("min_power_w"),
            "session_max_power_w": s.get("max_power_w"),
            "session_prefer_pv": s.get("prefer_pv"),
            "feasible": plan.get("feasible", True),
            "delivered_kwh": plan.get("delivered_kwh"),
            "energy_needed_kwh": plan.get("energy_needed_kwh"),
            "total_cost_chf": plan.get("total_cost_chf"),
            "chosen_windows": plan.get("chosen_windows", []),
            "current_window": plan.get("current_window"),
            "next_window": plan.get("next_window"),
            "scheduler_version": "v2",
        }


class WallboxPvReserveSensor(_WallboxSensorBase):
    """PV-Leistung die die Wallbox frei lassen muss für andere TS-Consumer (W).

    huawei_solar zieht das vom rohen PV-Surplus ab.
    Bis Scheduler v2 live ist: 0 (kein Reserve).
    """

    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:solar-power"
    # Spec-konformer entity_id (sonst würde HA `_w` wegen unit=W strippen)
    entity_id = "sensor.tariff_saver_wallbox_pv_reserve_w"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "wallbox_pv_reserve_w", "Wallbox PV Reserve")

    @property
    def native_value(self) -> int:
        from .scheduler import compute_pv_reserve_for_wallbox
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return 0
        return compute_pv_reserve_for_wallbox(store.consumer_plans or {}, dt_util.utcnow())


class WallboxSessionModeSensor(_WallboxSensorBase):
    """Mode-Indikator für huawei_solar.

    `pv_only`: Default — huawei_solar nutzt v1-PV-Logik
    `pv_plus_tariff`: Session aktiv mit Deadline, TS plant Slots (nach 2d)
    `tariff_only`: Notladung ohne PV — TS hart-Plan (nach 2d)
    """

    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "wallbox_session_mode", "Wallbox Session Mode")

    @property
    def native_value(self) -> str:
        s = self._active_session()
        if not s:
            return "pv_only"
        if not s.get("deadline_utc"):
            return "pv_only"
        if s.get("prefer_pv", True):
            return "pv_plus_tariff"
        return "tariff_only"


class LightAutoOffStatusSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Übersicht aller vom Light-Auto-Off-Modul erfassten Lichter.

    State: Anzahl überwachter Lichter (mit auto_off_*h-Label).
    Attribute `lights`: Liste von Dicts mit pro Licht:
      - entity_id, friendly_name, domain, state ("on"/"off")
      - on_since_iso (UTC ISO wenn an, sonst null)
      - threshold_h (vom Label, sonst null)
      - snooze_until_iso (UTC ISO wenn snoozed)
      - source ("pattern" | "label" | "both")
    """

    _attr_has_entity_name = True
    _attr_name = "Light Auto-Off Status"
    _attr_icon = "mdi:lightbulb-alert"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_light_auto_off_status"
        self._attr_device_info = _device_info(entry)

    def _build(self) -> tuple[int, list[dict[str, Any]]]:
        from .auto_off import (
            _discover_entities, _entity_threshold_hours, _is_on,
            _label_to_hours,
        )
        from .const import (
            CONF_LIGHT_HELPER_PATTERN,
            DEFAULT_LIGHT_HELPER_PATTERN,
            LIGHT_AUTO_OFF_LABEL_PREFIX,
        )
        import fnmatch

        from homeassistant.helpers import entity_registry as er

        src = self.entry.options if self.entry.options else self.entry.data
        pattern = str(src.get(CONF_LIGHT_HELPER_PATTERN, DEFAULT_LIGHT_HELPER_PATTERN) or DEFAULT_LIGHT_HELPER_PATTERN)
        registry = er.async_get(self.hass)
        entity_ids = _discover_entities(self.hass, pattern)
        store_state = (getattr(self.coordinator, "store", None) and self.coordinator.store.light_auto_off_state) or {}

        out: list[dict[str, Any]] = []
        monitored = 0
        for eid in entity_ids:
            rec = registry.async_get(eid)
            domain = eid.split(".", 1)[0]
            state_obj = self.hass.states.get(eid)
            state = state_obj.state if state_obj else None
            on = _is_on(domain, state)
            on_since_iso = None
            if on and state_obj and state_obj.last_changed:
                on_since_iso = dt_util.as_utc(state_obj.last_changed).isoformat()
            threshold_h = _entity_threshold_hours(registry, eid)
            in_pattern = fnmatch.fnmatchcase(eid, pattern)
            has_label = False
            if rec is not None:
                for label in rec.labels or set():
                    if _label_to_hours(label) is not None:
                        has_label = True
                        break
            if in_pattern and has_label:
                source = "both"
            elif has_label:
                source = "label"
            else:
                source = "pattern"
            snooze_until = None
            entry_state = store_state.get(eid) if isinstance(store_state, dict) else None
            if isinstance(entry_state, dict):
                snooze_until = entry_state.get("snooze_until_iso")
            friendly = (state_obj.attributes.get("friendly_name") if state_obj else None) or eid
            if threshold_h is not None:
                monitored += 1
            out.append({
                "entity_id": eid,
                "friendly_name": friendly,
                "domain": domain,
                "state": "on" if on else "off",
                "on_since_iso": on_since_iso,
                "threshold_h": threshold_h,
                "snooze_until_iso": snooze_until,
                "source": source,
            })
        out.sort(key=lambda r: (0 if r["threshold_h"] is None else 1, r["friendly_name"]))
        return monitored, out

    @property
    def native_value(self) -> int:
        try:
            return self._build()[0]
        except Exception as _err:
            _LOGGER.debug("LightAutoOffStatusSensor: %s in native_value: %s", type(_err).__name__, _err)
            return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        try:
            monitored, lights = self._build()
        except Exception as _err:
            _LOGGER.debug("LightAutoOffStatusSensor: %s in attrs: %s", type(_err).__name__, _err)
            return {"monitored_count": 0, "lights": []}
        return {
            "monitored_count": monitored,
            "total_count": len(lights),
            "lights": lights,
        }


class StandbyWatchStatusSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Status des Standby-Wächters.

    State: "aus" (Modul deaktiviert) / "aktiv".
    Attribute:
      - enabled, max_watts, default_hours, hour_choices
      - devices: alle getaggten Switches {entity_id, friendly_name, state,
        watts, power_entity, hours, effective_hours, watts_limit,
        effective_watts, low_since_iso, pv_dump_managed}
      - total_count
    """

    _attr_has_entity_name = True
    _attr_name = "Standby-Wächter Status"
    _attr_icon = "mdi:power-sleep"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_standby_watch_status"
        self._attr_device_info = _device_info(entry)

    def _build(self) -> dict[str, Any]:
        from .standby_watch import (
            _config_value, _read_watts, discover_switches, find_power_sensor,
        )
        from .const import (
            CONF_STANDBY_WATCH_ENABLED, DEFAULT_STANDBY_WATCH_ENABLED,
            CONF_STANDBY_WATCH_MAX_WATTS, DEFAULT_STANDBY_WATCH_MAX_WATTS,
            CONF_STANDBY_WATCH_DEFAULT_HOURS, DEFAULT_STANDBY_WATCH_DEFAULT_HOURS,
            STANDBY_WATCH_HOUR_CHOICES,
        )

        enabled = bool(_config_value(self.entry, CONF_STANDBY_WATCH_ENABLED, DEFAULT_STANDBY_WATCH_ENABLED))
        max_watts = float(_config_value(self.entry, CONF_STANDBY_WATCH_MAX_WATTS, DEFAULT_STANDBY_WATCH_MAX_WATTS))
        default_hours = float(_config_value(self.entry, CONF_STANDBY_WATCH_DEFAULT_HOURS, DEFAULT_STANDBY_WATCH_DEFAULT_HOURS))

        manager = getattr(self.coordinator, "standby_watch", None)
        overrides = manager.hours_overrides() if manager is not None else {}
        watt_overrides = manager.watts_overrides() if manager is not None else {}
        pv_managed: set[str] = set()
        store = getattr(self.coordinator, "store", None)
        if store is not None and getattr(store, "pv_dump_state", None):
            pv_managed = set((store.pv_dump_state or {}).get("managed") or {})

        devices = []
        for eid in discover_switches(self.hass):
            state_obj = self.hass.states.get(eid)
            friendly = (state_obj.attributes.get("friendly_name") if state_obj else None) or eid
            power_entity = find_power_sensor(self.hass, eid)
            watts = _read_watts(self.hass, power_entity) if power_entity else None
            low_since = manager.low_since(eid) if manager is not None else None
            devices.append({
                "entity_id": eid,
                "friendly_name": friendly,
                "state": state_obj.state if state_obj else "unavailable",
                "power_entity": power_entity,
                "watts": round(watts, 1) if watts is not None else None,
                "hours": overrides.get(eid),
                "effective_hours": overrides.get(eid, default_hours),
                "watts_limit": watt_overrides.get(eid),
                "effective_watts": watt_overrides.get(eid, max_watts),
                "low_since_iso": low_since.isoformat() if low_since else None,
                "pv_dump_managed": eid in pv_managed,
            })
        devices.sort(key=lambda r: r["friendly_name"])

        return {
            "enabled": enabled,
            "max_watts": max_watts,
            "default_hours": default_hours,
            "hour_choices": list(STANDBY_WATCH_HOUR_CHOICES),
            "total_count": len(devices),
            "devices": devices,
        }

    @property
    def native_value(self) -> str:
        try:
            d = self._build()
        except Exception as _err:
            _LOGGER.debug("StandbyWatchStatusSensor: %s in native_value: %s", type(_err).__name__, _err)
            return "unbekannt"
        return "aktiv" if d["enabled"] else "aus"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        try:
            return self._build()
        except Exception as _err:
            _LOGGER.debug("StandbyWatchStatusSensor: %s in attrs: %s", type(_err).__name__, _err)
            return {"enabled": False, "devices": []}


class PvDumpStatusSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    """Status der PV-Überschuss Dump-Loads.

    State: "aktiv" / "inaktiv" / "aus" (Modul deaktiviert).
    Attribute:
      - enabled, active
      - grid_out_w (Netzeinspeisung), grid_in_w (Netzbezug)
      - on_watts / off_watts / dwell_seconds / min_on_minutes (Settings)
      - devices: Liste aller getaggten Geräte {entity_id, friendly_name, state, managed}
      - managed_count, total_count, on_since_iso
    """

    _attr_has_entity_name = True
    _attr_name = "PV-Überschuss Status"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_pv_dump_status"
        self._attr_device_info = _device_info(entry)

    def _build(self) -> dict[str, Any]:
        from .pv_dump import _config_value, _read_watts, discover_entities, _is_on
        from .const import (
            CONF_PV_DUMP_ENABLED, DEFAULT_PV_DUMP_ENABLED,
            CONF_PV_DUMP_GRID_OUT_ENTITY, DEFAULT_PV_DUMP_GRID_OUT_ENTITY,
            CONF_PV_DUMP_GRID_IN_ENTITY, DEFAULT_PV_DUMP_GRID_IN_ENTITY,
            CONF_PV_DUMP_ON_WATTS, DEFAULT_PV_DUMP_ON_WATTS,
            CONF_PV_DUMP_OFF_WATTS, DEFAULT_PV_DUMP_OFF_WATTS,
            CONF_PV_DUMP_DWELL_SECONDS, DEFAULT_PV_DUMP_DWELL_SECONDS,
            CONF_PV_DUMP_MIN_ON_MINUTES, DEFAULT_PV_DUMP_MIN_ON_MINUTES,
            CONF_PV_DUMP_PV_POWER_ENTITY, DEFAULT_PV_DUMP_PV_POWER_ENTITY,
            PV_DUMP_PV_ZERO_WATTS,
        )

        from .const import CONF_PV_DUMP_PAUSE_ON_VACATION, DEFAULT_PV_DUMP_PAUSE_ON_VACATION
        from .vacation import is_vacation_active

        enabled = bool(_config_value(self.entry, CONF_PV_DUMP_ENABLED, DEFAULT_PV_DUMP_ENABLED))
        vacation_paused = bool(
            _config_value(self.entry, CONF_PV_DUMP_PAUSE_ON_VACATION, DEFAULT_PV_DUMP_PAUSE_ON_VACATION)
        ) and is_vacation_active(self.hass, self.entry)
        grid_out = _read_watts(self.hass, str(_config_value(self.entry, CONF_PV_DUMP_GRID_OUT_ENTITY, DEFAULT_PV_DUMP_GRID_OUT_ENTITY)))
        grid_in = _read_watts(self.hass, str(_config_value(self.entry, CONF_PV_DUMP_GRID_IN_ENTITY, DEFAULT_PV_DUMP_GRID_IN_ENTITY)))
        pv = _read_watts(self.hass, str(_config_value(self.entry, CONF_PV_DUMP_PV_POWER_ENTITY, DEFAULT_PV_DUMP_PV_POWER_ENTITY)))

        store = getattr(self.coordinator, "store", None)
        st = (store.pv_dump_state if store is not None and getattr(store, "pv_dump_state", None) else {}) or {}
        active = bool(st.get("active", False))
        managed = st.get("managed") or {}

        devices = []
        for eid in discover_entities(self.hass):
            state_obj = self.hass.states.get(eid)
            friendly = (state_obj.attributes.get("friendly_name") if state_obj else None) or eid
            devices.append({
                "entity_id": eid,
                "friendly_name": friendly,
                "state": "on" if _is_on(self.hass, eid) else "off",
                "managed": eid in managed,
            })
        devices.sort(key=lambda r: r["friendly_name"])

        return {
            "enabled": enabled,
            "active": active,
            "vacation_paused": vacation_paused,
            "grid_out_w": round(grid_out) if grid_out is not None else None,
            "grid_in_w": round(grid_in) if grid_in is not None else None,
            "pv_w": round(pv) if pv is not None else None,
            "pv_zero_watts": PV_DUMP_PV_ZERO_WATTS,
            "on_watts": float(_config_value(self.entry, CONF_PV_DUMP_ON_WATTS, DEFAULT_PV_DUMP_ON_WATTS)),
            "off_watts": float(_config_value(self.entry, CONF_PV_DUMP_OFF_WATTS, DEFAULT_PV_DUMP_OFF_WATTS)),
            "dwell_seconds": float(_config_value(self.entry, CONF_PV_DUMP_DWELL_SECONDS, DEFAULT_PV_DUMP_DWELL_SECONDS)),
            "min_on_minutes": float(_config_value(self.entry, CONF_PV_DUMP_MIN_ON_MINUTES, DEFAULT_PV_DUMP_MIN_ON_MINUTES)),
            "managed_count": len(managed),
            "total_count": len(devices),
            "on_since_iso": st.get("on_since_iso"),
            "devices": devices,
        }

    @property
    def native_value(self) -> str:
        try:
            d = self._build()
        except Exception as _err:
            _LOGGER.debug("PvDumpStatusSensor: %s in native_value: %s", type(_err).__name__, _err)
            return "unbekannt"
        if not d["enabled"]:
            return "aus"
        if d.get("vacation_paused"):
            return "ferien"
        return "aktiv" if d["active"] else "inaktiv"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        try:
            return self._build()
        except Exception as _err:
            _LOGGER.debug("PvDumpStatusSensor: %s in attrs: %s", type(_err).__name__, _err)
            return {"enabled": False, "active": False, "devices": []}
