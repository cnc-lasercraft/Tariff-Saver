"""Sensor platform for Tariff Saver (prices + costs, incl. component breakdown)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import TariffSaverCoordinator, PriceSlot
from .storage import IMPORT_ALLIN_COMPONENTS, TariffSaverStore

CONF_CONSUMPTION_ENERGY_ENTITY = "consumption_energy_entity"
SIGNAL_STORE_UPDATED = "tariff_saver_store_updated"

COMPONENT_KEYS = [
    "electricity",
    "grid",
    "regional_fees",
    "metering",
    "refund_storage",
    "integrated",
    "feed_in",
]


def _active_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("active", []) if isinstance(data, dict) else []


def _baseline_slots(coordinator: TariffSaverCoordinator) -> list[PriceSlot]:
    data = coordinator.data or {}
    return data.get("baseline", []) if isinstance(data, dict) else []


def _current_slot(slots: list[PriceSlot]) -> PriceSlot | None:
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: TariffSaverCoordinator = hass.data[DOMAIN][entry.entry_id]

    energy_entity = entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY) or entry.data.get(CONF_CONSUMPTION_ENERGY_ENTITY)
    if isinstance(energy_entity, str) and energy_entity:

        @callback
        def _on_energy_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            try:
                kwh_total = float(new_state.state)
            except Exception:
                return
            store = getattr(coordinator, "store", None)
            if store is None:
                return

            now_utc = dt_util.utcnow()
            if not store.add_sample(now_utc, kwh_total):
                return

            newly = store.finalize_due_slots(now_utc)
            if store.dirty:
                hass.async_create_task(store.async_save())
            if newly > 0:
                async_dispatcher_send(hass, f"{SIGNAL_STORE_UPDATED}_{entry.entry_id}")

        unsub = async_track_state_change_event(hass, [energy_entity], _on_energy_change)
        hass.data[DOMAIN][f"{entry.entry_id}_unsub_energy_cost"] = unsub

    entities: list[SensorEntity] = []
    entities += [
        TariffSaverPriceCurveSensor(coordinator, entry),
        TariffSaverPriceNowSensor(coordinator, entry),
        TariffSaverNextPriceSensor(coordinator, entry),
        TariffSaverPriceAllInNowSensor(coordinator, entry),
    ]
    for comp in COMPONENT_KEYS:
        if comp == "electricity":
            continue
        entities.append(TariffSaverPriceComponentNowSensor(coordinator, entry, comp))

    # Existing simple IDs (as you see them): sensor.actual_cost_month etc.
    entities += [
        PeriodCostSensor(entry, coordinator, "today", "dyn", "electricity", "actual_cost_today", "Actual cost today"),
        PeriodCostSensor(entry, coordinator, "today", "base", "electricity", "baseline_cost_today", "Baseline cost today", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "today", "sav", "electricity", "actual_savings_today", "Actual savings today", icon="mdi:piggy-bank", state_class="measurement"),
        PeriodCostSensor(entry, coordinator, "week", "dyn", "electricity", "actual_cost_week", "Actual cost week"),
        PeriodCostSensor(entry, coordinator, "week", "base", "electricity", "baseline_cost_week", "Baseline cost week", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "week", "sav", "electricity", "actual_savings_week", "Actual savings week", icon="mdi:piggy-bank", state_class="measurement"),
        PeriodCostSensor(entry, coordinator, "month", "dyn", "electricity", "actual_cost_month", "Actual cost month"),
        PeriodCostSensor(entry, coordinator, "month", "base", "electricity", "baseline_cost_month", "Baseline cost month", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "month", "sav", "electricity", "actual_savings_month", "Actual savings month", icon="mdi:piggy-bank", state_class="measurement"),
        PeriodCostSensor(entry, coordinator, "year", "dyn", "electricity", "actual_cost_year", "Actual cost year"),
        PeriodCostSensor(entry, coordinator, "year", "base", "electricity", "baseline_cost_year", "Baseline cost year", icon="mdi:cash-multiple"),
        PeriodCostSensor(entry, coordinator, "year", "sav", "electricity", "actual_savings_year", "Actual savings year", icon="mdi:piggy-bank", state_class="measurement"),
    ]

    # All-in totals
    for period in ("today", "week", "month", "year"):
        entities += [
            PeriodCostSensor(entry, coordinator, period, "dyn", "__allin__", f"actual_cost_allin_{period}", f"Actual cost all-in {period}"),
            PeriodCostSensor(entry, coordinator, period, "base", "__allin__", f"baseline_cost_allin_{period}", f"Baseline cost all-in {period}", icon="mdi:cash-multiple"),
            PeriodCostSensor(entry, coordinator, period, "sav", "__allin__", f"actual_savings_allin_{period}", f"Actual savings all-in {period}", icon="mdi:piggy-bank", state_class="measurement"),
        ]

    # Component-wise costs (dyn/base/sav)
    for period in ("today", "week", "month", "year"):
        for comp in COMPONENT_KEYS:
            entities.append(PeriodCostSensor(entry, coordinator, period, "dyn", comp, f"dyn_{comp}_{period}", f"{comp} cost {period}"))
            entities.append(PeriodCostSensor(entry, coordinator, period, "base", comp, f"base_{comp}_{period}", f"{comp} baseline {period}", icon="mdi:cash-multiple"))
            entities.append(PeriodCostSensor(entry, coordinator, period, "sav", comp, f"sav_{comp}_{period}", f"{comp} savings {period}", icon="mdi:piggy-bank", state_class="measurement"))

    entities += [TariffSaverLastApiSuccessSensor(coordinator, entry)]
    async_add_entities(entities, update_before_add=True)


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
        active = _active_slots(self.coordinator)
        baseline = _baseline_slots(self.coordinator)
        baseline_map = {s.start: s.components_chf_per_kwh for s in baseline} if baseline else {}

        return {
            "tariff_name": getattr(self.coordinator, "tariff_name", None),
            "baseline_tariff_name": getattr(self.coordinator, "baseline_tariff_name", None),
            "slot_count": len(active),
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "price_chf_per_kwh": s.electricity_chf_per_kwh,
                    "baseline_chf_per_kwh": (baseline_map.get(s.start, {}) or {}).get("electricity"),
                    "components": s.components_chf_per_kwh,
                    "baseline_components": baseline_map.get(s.start),
                }
                for s in active
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
        slot = _current_slot(_active_slots(self.coordinator))
        return float(slot.electricity_chf_per_kwh) if slot else None


class TariffSaverPriceAllInNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Price all-in now"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_allin_now"

    @property
    def native_value(self) -> float | None:
        slot = _current_slot(_active_slots(self.coordinator))
        if not slot:
            return None
        comps = slot.components_chf_per_kwh or {}
        total = sum(float(comps.get(c, 0.0) or 0.0) for c in IMPORT_ALLIN_COMPONENTS)
        return round(total, 6) if total else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = _current_slot(_active_slots(self.coordinator))
        if not slot:
            return {}
        comps = slot.components_chf_per_kwh or {}
        api_integrated = comps.get("integrated")
        summed = sum(float(comps.get(c, 0.0) or 0.0) for c in IMPORT_ALLIN_COMPONENTS)
        return {
            "slot_start_utc": slot.start.isoformat(),
            "sum_components": round(summed, 6),
            "api_integrated": float(api_integrated) if isinstance(api_integrated, (int, float)) else None,
            "components_used": list(IMPORT_ALLIN_COMPONENTS),
        }


class TariffSaverPriceComponentNowSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "CHF/kWh"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, component: str) -> None:
        super().__init__(coordinator)
        self._component = component
        self._attr_name = f"Price now {component}"
        self._attr_icon = "mdi:currency-chf"
        self._attr_unique_id = f"{entry.entry_id}_price_now_{component}"

    @property
    def native_value(self) -> float | None:
        slot = _current_slot(_active_slots(self.coordinator))
        if not slot:
            return None
        v = (slot.components_chf_per_kwh or {}).get(self._component)
        return float(v) if isinstance(v, (int, float)) else None


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
        slots = sorted(_active_slots(self.coordinator), key=lambda s: s.start)
        if not slots:
            return None
        now = dt_util.utcnow()
        for s in slots:
            if s.start > now:
                return float(s.electricity_chf_per_kwh)
        return None


class PeriodCostSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity, RestoreEntity):
    _attr_native_unit_of_measurement = "CHF"

    def __init__(self, entry: ConfigEntry, coordinator: TariffSaverCoordinator, period: str, flavor: str,
                 key: str, unique_suffix: str, name: str,
                 icon: str = "mdi:cash", state_class: str = "total") -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.period = period
        self.flavor = flavor
        self.key = key
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._attr_unique_id = unique_suffix
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
        bd = self._get_breakdown()
        if not bd:
            return None
        bucket = bd.get(self.flavor) or {}
        if not isinstance(bucket, dict):
            return None

        if self.key == "__allin__":
            total = TariffSaverStore.sum_components(bucket, IMPORT_ALLIN_COMPONENTS)
            return round(total, 2)

        v = bucket.get(self.key)
        if isinstance(v, (int, float)):
            return round(float(v), 2)
        return 0.0


class TariffSaverLastApiSuccessSensor(CoordinatorEntity[TariffSaverCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Last API success"
    _attr_icon = "mdi:cloud-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_api_success"

    @property
    def native_value(self) -> datetime | None:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return None
        ts = getattr(store, "last_api_success_utc", None)
        if isinstance(ts, datetime):
            return dt_util.as_utc(ts)
        return None
