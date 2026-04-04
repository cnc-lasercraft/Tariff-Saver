
"""Binary sensors for Tariff Saver consumer run recommendations."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

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
    entities = [TariffSaverConsumerShouldRunBinarySensor(coordinator, entry, slot) for slot in range(1, CONSUMER_COUNT + 1)]
    entities.append(TariffSaverTomorrowAvailableBinarySensor(coordinator, entry))
    entities.append(TariffSaverStromSparenBinarySensor(coordinator, entry))
    async_add_entities(entities, update_before_add=True)

class TariffSaverTomorrowAvailableBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity):
    """On when tomorrow's tariff data is available (>= 24 slots) AND validated."""

    _attr_has_entity_name = True
    _attr_name = "Tomorrow available"
    _attr_icon = "mdi:calendar-arrow-right"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tomorrow_available"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        has_slots = int(stats.get("tomorrow_slot_count", 0) or 0) >= 24
        is_valid = stats.get("tomorrow_data_valid", True)
        return has_slots and is_valid

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        return {
            "tomorrow_slot_count": stats.get("tomorrow_slot_count", 0),
            "tomorrow_data_valid": stats.get("tomorrow_data_valid"),
            "tomorrow_validity_error": stats.get("tomorrow_validity_error"),
            "tomorrow_validity_details": stats.get("tomorrow_validity_details"),
        }


class TariffSaverStromSparenBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity):
    """On when current score exceeds the configured threshold — non-essential devices should be turned off."""

    _attr_has_entity_name = True
    _attr_name = "Strom sparen"
    _attr_icon = "mdi:power-plug-off"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_strom_sparen"
        self._attr_device_info = _device_info(entry)
        self._prev_is_on: bool | None = None

    def _get_threshold(self) -> int:
        from .const import CONF_STROM_SPAREN_SCORE, DEFAULT_STROM_SPAREN_SCORE
        return int(float(
            self._entry.options.get(CONF_STROM_SPAREN_SCORE,
                self._entry.data.get(CONF_STROM_SPAREN_SCORE, DEFAULT_STROM_SPAREN_SCORE))
            or DEFAULT_STROM_SPAREN_SCORE
        ))

    def _grid_needed(self) -> bool:
        """Check if grid power is needed — False if PV surplus or battery covers demand until PV."""
        from .const import (
            CONF_PV_SURPLUS_ENTITY, CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD,
        )
        config = self._entry.options if self._entry.options else self._entry.data

        # 1. PV surplus right now → no grid needed
        pv_surplus_entity = str(config.get(CONF_PV_SURPLUS_ENTITY, "") or "").strip()
        pv_threshold = float(config.get(CONF_AMPEL_PV_THRESHOLD, DEFAULT_AMPEL_PV_THRESHOLD) or DEFAULT_AMPEL_PV_THRESHOLD)
        if pv_surplus_entity:
            pv_state = self.hass.states.get(pv_surplus_entity)
            if pv_state and pv_state.state not in ("unavailable", "unknown", ""):
                try:
                    val = float(pv_state.state)
                    pv_kw = val / 1000.0 if abs(val) > 100 else val
                    if pv_kw >= pv_threshold:
                        return False
                except (ValueError, TypeError):
                    pass

        # 2. Battery covers demand until PV → no grid needed
        energy_until_pv = self.hass.states.get(f"sensor.tariff_saver_energy_until_pv")
        if energy_until_pv and energy_until_pv.state not in ("unavailable", "unknown", ""):
            missing = energy_until_pv.attributes.get("missing_kwh", None)
            if isinstance(missing, (int, float)) and missing <= 0:
                return False

        return True

    @property
    def is_on(self) -> bool:
        from .sensor import _current_score_live
        score = _current_score_live(self.coordinator)
        if score is None:
            return False
        if score <= self._get_threshold():
            return False
        # Score is high, but only activate if grid power is actually needed
        return self._grid_needed()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Log state changes to activity log with explanation."""
        current = self.is_on
        if self._prev_is_on is not None and current != self._prev_is_on:
            store = getattr(self.coordinator, "store", None)
            if store is not None:
                from .sensor import _current_score_live
                score = _current_score_live(self.coordinator)
                threshold = self._get_threshold()
                grid_needed = self._grid_needed()
                if current:
                    # Build reason
                    energy_pv = self.hass.states.get("sensor.tariff_saver_energy_until_pv")
                    missing = None
                    available = None
                    if energy_pv and energy_pv.state not in ("unavailable", "unknown", ""):
                        missing = energy_pv.attributes.get("missing_kwh")
                        available = energy_pv.attributes.get("available_kwh")
                    reason = f"Score {score} > {threshold}"
                    if isinstance(missing, (int, float)) and isinstance(available, (int, float)):
                        reason += f", Akku {round(available, 1)} kWh reicht nicht ({round(missing, 1)} kWh fehlen)"
                    store.log_activity("🔴", f"Strom sparen EIN — {reason}")
                else:
                    if score is not None and score <= threshold:
                        reason = f"Score {score} ≤ {threshold}"
                    elif not grid_needed:
                        reason = "Kein Grid-Bezug nötig"
                    else:
                        reason = f"Score {score}"
                    store.log_activity("🟢", f"Strom sparen AUS — {reason}")
        self._prev_is_on = current
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .sensor import _current_score_live
        score = _current_score_live(self.coordinator)
        threshold = self._get_threshold()
        grid_needed = self._grid_needed()
        return {
            "current_score": score,
            "threshold": threshold,
            "grid_needed": grid_needed,
        }


class TariffSaverConsumerShouldRunBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry, slot: int) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.slot = slot
        self._attr_name = f"Consumer {slot} should run"
        self._attr_unique_id = f"{entry.entry_id}_consumer_{slot}_should_run"
        self._prev_is_on: bool | None = None
        self._attr_device_info = _device_info(entry)
        self._unsub_start = None
        self._unsub_end = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Coordinator hat neue Daten — Übergangszeitpunkte neu planen."""
        self._schedule_transitions()
        super()._handle_coordinator_update()

    def _schedule_transitions(self) -> None:
        """Registriert point-in-time Callbacks bei chosen_start und chosen_end.

        So wechselt der Binary-Sensor-State exakt zum geplanten Zeitpunkt,
        ohne auf ein Coordinator-Update warten zu müssen.
        """
        # Bestehende Callbacks canceln
        if self._unsub_start:
            self._unsub_start()
            self._unsub_start = None
        if self._unsub_end:
            self._unsub_end()
            self._unsub_end = None

        plan = self._get_plan()
        chosen_start_str = plan.get("chosen_start")
        chosen_end_str = plan.get("chosen_end")
        if not chosen_start_str or not chosen_end_str:
            return

        start = dt_util.parse_datetime(str(chosen_start_str))
        end = dt_util.parse_datetime(str(chosen_end_str))
        if not start or not end:
            return

        now = dt_util.as_local(dt_util.utcnow())
        if end <= now:
            return  # Fenster bereits vorbei

        @callback
        def _write_state(point_in_time) -> None:
            self.async_write_ha_state()

        if start > now:
            self._unsub_start = async_track_point_in_time(self.hass, _write_state, start)

        self._unsub_end = async_track_point_in_time(self.hass, _write_state, end)
        _LOGGER.debug(
            "Consumer %d: Übergänge geplant start=%s end=%s",
            self.slot, start.isoformat(), end.isoformat(),
        )

    async def async_will_remove_from_hass(self) -> None:
        """Entity wird entfernt — Callbacks aufräumen."""
        if self._unsub_start:
            self._unsub_start()
        if self._unsub_end:
            self._unsub_end()

    def _get_plan(self) -> dict:
        """Return pre-computed coordinated plan, or compute on-the-fly as fallback."""
        config = get_consumer_config(self.entry, self.slot)
        # PV-Opportunist: always compute fresh (real-time PV check, no caching)
        if config.pv_opportunist:
            store = getattr(self.coordinator, "store", None)
            learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
            return build_consumer_plan(self.hass, self.entry, config, learning, _active_slots(self.coordinator))
        plans = (self.coordinator.data or {}).get("consumer_plans", {})
        plan = plans.get(self.slot) or plans.get(str(self.slot))
        if plan is not None:
            return plan
        # Kein Plan vorhanden — Fehler, kein stilles Neuberechnen
        _LOGGER.error(
            "Consumer %d: Kein Plan vorhanden! Plans-Keys: %s",
            self.slot, list(plans.keys()) if plans else "leer",
        )
        return {"status": "error_no_plan", "should_run": False}

    @property
    def should_poll(self) -> bool:
        config = get_consumer_config(self.entry, self.slot)
        return config.pv_opportunist

    def _log_transition(self, current: bool) -> None:
        if self._prev_is_on is not None and current != self._prev_is_on:
            store = getattr(self.coordinator, "store", None)
            if store:
                config = get_consumer_config(self.entry, self.slot)
                if config.enabled:
                    name = config.configured_name or f"Consumer {self.slot}"
                    if current:
                        plan = self._get_plan()
                        source = plan.get("source") or ""
                        start = plan.get("chosen_start", "")
                        end = plan.get("chosen_end", "")
                        time_str = ""
                        if start and end:
                            try:
                                from homeassistant.util import dt as dt_util
                                s = dt_util.parse_datetime(str(start))
                                e = dt_util.parse_datetime(str(end))
                                if s and e:
                                    time_str = f" {dt_util.as_local(s).strftime('%H:%M')}-{dt_util.as_local(e).strftime('%H:%M')}"
                            except Exception:
                                pass
                        store.log_activity("🟢", f"{name} gestartet ({source}{time_str})")
                    else:
                        store.log_activity("⏹️", f"{name} beendet")
                        from homeassistant.util import dt as dt_util
                        store.set_consumer_last_run(str(self.slot), dt_util.utcnow())
                    self.hass.async_create_task(store.async_save())
        self._prev_is_on = current

    @property
    def is_on(self) -> bool:
        plan = self._get_plan()
        chosen_start = plan.get("chosen_start")
        chosen_end = plan.get("chosen_end")
        result = False
        if chosen_start and chosen_end:
            from homeassistant.util import dt as dt_util
            now = dt_util.as_local(dt_util.utcnow())
            start = dt_util.parse_datetime(str(chosen_start))
            end = dt_util.parse_datetime(str(chosen_end))
            if start and end and start <= now < end:
                # For tariff_only: block if PV surplus available
                config = get_consumer_config(self.entry, self.slot)
                if config.tariff_only:
                    from .const import CONF_PV_SURPLUS_ENTITY
                    pv_entity = str(self.entry.options.get(CONF_PV_SURPLUS_ENTITY, self.entry.data.get(CONF_PV_SURPLUS_ENTITY, "")) or "").strip()
                    if pv_entity:
                        pv_state = self.hass.states.get(pv_entity)
                        if pv_state and pv_state.state not in ("unavailable", "unknown", ""):
                            try:
                                val = float(pv_state.state)
                                pv_kw = val / 1000.0 if val > 100 else val
                                if pv_kw > 0.5:
                                    self._log_transition(False)
                                    return False
                            except (ValueError, TypeError):
                                pass
                result = True
        if not result:
            result = bool(plan.get("should_run", False))
        self._log_transition(result)
        return result

    def _compute_status(self, plan: dict) -> str:
        """Compute dynamic status from plan, time window, and reported feedback."""
        # 1. Reported status from automation has highest priority
        reported = plan.get("reported_status")
        if reported:
            return str(reported)

        # 2. Auto-derive from time window
        chosen_start = plan.get("chosen_start")
        chosen_end = plan.get("chosen_end")
        plan_status = plan.get("status", "")

        if not chosen_start or not chosen_end:
            return plan_status or "kein_plan"

        now = dt_util.as_local(dt_util.utcnow())
        start = dt_util.parse_datetime(str(chosen_start))
        end = dt_util.parse_datetime(str(chosen_end))
        if not start or not end:
            return plan_status

        if now < start:
            return "geplant"
        if start <= now < end:
            return "aktiv" if self.is_on else "blockiert"
        # now >= end
        return "beendet"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self._get_plan()
        config = get_consumer_config(self.entry, self.slot)
        store = getattr(self.coordinator, "store", None)
        learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
        return {
            "slot": self.slot,
            "enabled": config.enabled,
            "name": config.configured_name,
            "status": self._compute_status(plan),
            "reported_status": plan.get("reported_status"),
            "reported_status_at": plan.get("reported_status_at"),
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
            "min_days": plan.get("min_days"),
            "max_days": plan.get("max_days"),
            "days_since_last_run": plan.get("days_since_last_run"),
            "due_status": plan.get("due_status"),
            "pv_runs": learning.get("pv_runs", 0),
            "grid_runs": learning.get("grid_runs", 0),
            "pv_kwh": learning.get("pv_kwh", 0.0),
            "grid_kwh": learning.get("grid_kwh", 0.0),
            "total_savings_chf": learning.get("total_savings_chf", 0.0),
        }
