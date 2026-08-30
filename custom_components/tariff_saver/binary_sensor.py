
"""Binary sensors for Tariff Saver consumer run recommendations."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
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
    entities.append(TariffSaverTariffFlatBinarySensor(coordinator, entry))
    entities.append(TariffSaverHygieneOverdueBinarySensor(coordinator, entry))
    entities.append(TariffSaverPoolFilterFreigabe(coordinator, entry))
    entities.append(TariffSaverPoolWpFreigabe(coordinator, entry))
    entities.append(TariffSaverLadeempfehlungBinarySensor(coordinator, entry))
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


class TariffSaverTariffFlatBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity):
    """On when tariff spread over active slots is below the configured threshold (EKZ lieferte flat)."""

    _attr_has_entity_name = True
    _attr_name = "Tariff flat"
    _attr_icon = "mdi:approximately-equal"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tariff_flat"
        self._attr_device_info = _device_info(entry)

    def _compute(self) -> tuple[bool, float | None, float | None, float | None]:
        from .const import (
            CONF_FLAT_TARIFF_SPREAD_RP, DEFAULT_FLAT_TARIFF_SPREAD_RP,
        )
        from .coordinator import _slot_total_price
        active = _active_slots(self.coordinator) or []
        prices = []
        for s in active:
            p = _slot_total_price(getattr(s, "components_chf_per_kwh", None))
            if isinstance(p, (int, float)) and p > 0.10:
                prices.append(float(p))
        if not prices:
            return False, None, None, None
        pmin = min(prices)
        pmax = max(prices)
        spread_rp = (pmax - pmin) * 100.0
        threshold_rp = float(self.entry.options.get(CONF_FLAT_TARIFF_SPREAD_RP, self.entry.data.get(CONF_FLAT_TARIFF_SPREAD_RP, DEFAULT_FLAT_TARIFF_SPREAD_RP)) or DEFAULT_FLAT_TARIFF_SPREAD_RP)
        return spread_rp < threshold_rp, pmin, pmax, spread_rp

    @property
    def is_on(self) -> bool:
        return self._compute()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import (
            CONF_FLAT_TARIFF_SPREAD_RP, DEFAULT_FLAT_TARIFF_SPREAD_RP,
            CONF_FLAT_GRID_EARLIEST_HOUR, DEFAULT_FLAT_GRID_EARLIEST_HOUR,
        )
        _, pmin, pmax, spread_rp = self._compute()
        return {
            "price_min_chf_per_kwh": round(pmin, 6) if pmin is not None else None,
            "price_max_chf_per_kwh": round(pmax, 6) if pmax is not None else None,
            "spread_rp_per_kwh": round(spread_rp, 3) if spread_rp is not None else None,
            "threshold_rp": float(self.entry.options.get(CONF_FLAT_TARIFF_SPREAD_RP, self.entry.data.get(CONF_FLAT_TARIFF_SPREAD_RP, DEFAULT_FLAT_TARIFF_SPREAD_RP)) or DEFAULT_FLAT_TARIFF_SPREAD_RP),
            "grid_earliest_hour": int(self.entry.options.get(CONF_FLAT_GRID_EARLIEST_HOUR, self.entry.data.get(CONF_FLAT_GRID_EARLIEST_HOUR, DEFAULT_FLAT_GRID_EARLIEST_HOUR)) or DEFAULT_FLAT_GRID_EARLIEST_HOUR),
        }


class TariffSaverStromSparenBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity, RestoreEntity):
    """On when current score exceeds the configured threshold — non-essential devices should be turned off.

    Debounce-Logik (seit 2026-04-24):
    - 30 s stabiler raw-True-State vor OFF→ON-Übergang (verhindert <1 s Flicker)
    - 15 min Min-Dwell-Time zwischen State-Changes
    - Hysterese: ON bei score > threshold+1, OFF bei score < threshold-1
    - Grid-Needed-Check: bei PV-Surplus oder Akku-reicht-bis-PV immer off
    - RestoreEntity: State + last_changed überleben Reload/Restart (Min-Dwell bleibt konsistent)
    """

    _attr_has_entity_name = True
    _attr_name = "Strom sparen"
    _attr_icon = "mdi:power-plug-off"

    ON_DEBOUNCE_SEC = 30
    MIN_DWELL_SEC = 15 * 60
    TICK_SEC = 10

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_strom_sparen"
        self._attr_device_info = _device_info(entry)
        self._attr_is_on: bool = False
        self._raw_on_since_utc: datetime | None = None
        self._last_state_change_utc: datetime | None = None
        self._unsub_tick = None

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
            from .slot_helpers import state_to_kw
            pv_kw = state_to_kw(self.hass.states.get(pv_surplus_entity))
            if pv_kw >= pv_threshold:
                return False

        # 2. Battery covers demand until PV → no grid needed
        energy_until_pv = self.hass.states.get(f"sensor.tariff_saver_energy_until_pv")
        if energy_until_pv and energy_until_pv.state not in ("unavailable", "unknown", ""):
            missing = energy_until_pv.attributes.get("missing_kwh", None)
            if isinstance(missing, (int, float)) and missing <= 0:
                return False

        return True

    def _raw_target(self) -> bool | None:
        """Was DER State sein sollte aktuell (ohne Debounce/Dwell)? None = unbekannt."""
        from .sensor import _current_score_live
        score = _current_score_live(self.coordinator)
        if score is None:
            return None
        threshold = self._get_threshold()
        if self._attr_is_on:
            # Currently ON: bleib on wenn score >= threshold-1 UND grid_needed
            return score >= threshold - 1 and self._grid_needed()
        # Currently OFF: off → on wenn score >= threshold+1 UND grid_needed
        return score >= threshold + 1 and self._grid_needed()

    @callback
    def _evaluate(self) -> None:
        """Berechnet Ziel-State + wendet Debounce + Min-Dwell an. Schreibt state nur bei Änderung."""
        raw = self._raw_target()
        if raw is None:
            return
        now = dt_util.utcnow()
        current = bool(self._attr_is_on)

        # Min-Dwell: 15 min in aktuellem State bleiben
        if self._last_state_change_utc is not None:
            elapsed = (now - self._last_state_change_utc).total_seconds()
            if elapsed < self.MIN_DWELL_SEC:
                return

        # OFF → ON nur nach 30 s stabilem raw=True (verhindert Flicker)
        if not current and raw:
            if self._raw_on_since_utc is None:
                self._raw_on_since_utc = now
                return
            if (now - self._raw_on_since_utc).total_seconds() < self.ON_DEBOUNCE_SEC:
                return
            self._attr_is_on = True
            self._last_state_change_utc = now
            self._raw_on_since_utc = None
            self._log_state_change(True)
            self.async_write_ha_state()
            return

        # OFF bleibt OFF — reset debounce falls raw schwankte
        if not current and not raw:
            self._raw_on_since_utc = None
            return

        # ON → OFF sofort (kein Debounce für Off)
        if current and not raw:
            self._attr_is_on = False
            self._last_state_change_utc = now
            self._log_state_change(False)
            self.async_write_ha_state()

    @callback
    def _log_state_change(self, new_on: bool) -> None:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return
        from .sensor import _current_score_live
        score = _current_score_live(self.coordinator)
        threshold = self._get_threshold()
        if new_on:
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
            grid_needed = self._grid_needed()
            if score is not None and score <= threshold:
                reason = f"Score {score} ≤ {threshold}"
            elif not grid_needed:
                reason = "Kein Grid-Bezug nötig"
            else:
                reason = f"Score {score}"
            store.log_activity("🟢", f"Strom sparen AUS — {reason}")

    @callback
    def _handle_coordinator_update(self) -> None:
        self._evaluate()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore state + last_changed damit Min-Dwell über Reload/Restart überlebt.
        # Sonst: nach jedem Reload ist _last_state_change_utc=None → Min-Dwell-Skip → Flicker.
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = (last.state == "on")
            if last.last_changed is not None:
                self._last_state_change_utc = last.last_changed

        # Periodic re-evaluation (10 s) für ON-Debounce und Min-Dwell-Expiry
        from homeassistant.helpers.event import async_track_time_interval
        from datetime import timedelta
        self._unsub_tick = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=self.TICK_SEC),
        )
        self._evaluate()

    @callback
    def _async_tick(self, _now) -> None:
        """Wrapper als @callback, damit HA den Tick im Event-Loop ausführt
        (sonst landet er im Executor-Thread und async_write_ha_state crasht)."""
        self._evaluate()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        await super().async_will_remove_from_hass()

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


class TariffSaverHygieneOverdueBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity, RestoreEntity):
    """ON wenn ein Daily-Hygiene-Consumer überfällig + kein gültiger Plan.

    Hygiene-Consumer = enabled, max_days > 0, nicht pv_opportunist, nicht trigger_entity, nicht session.
    Überfällig = days_since_last_run > max_days UND kein Plan mit status='planned' und chosen_start ≥ today.
    Tick alle 15 min mit @callback. Push via Herold beim off→on Übergang (nicht repetitiv).
    """

    _attr_has_entity_name = True
    _attr_name = "Hygiene überfällig"
    _attr_icon = "mdi:alert-circle-outline"

    TICK_SEC = 15 * 60

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_hygiene_overdue"
        self._attr_device_info = _device_info(entry)
        self._attr_is_on: bool = False
        self._overdue_consumers: list[str] = []
        self._unsub_tick = None

    def _evaluate_state(self) -> tuple[bool, list[str]]:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return False, []
        plans = store.consumer_plans or {}
        today = dt_util.now().date()
        overdue: list[str] = []
        for slot in range(1, CONSUMER_COUNT + 1):
            try:
                cfg = get_consumer_config(self._entry, slot)
            except Exception as _err:
                _LOGGER.debug("silent %s in get_consumer_config slot=%s: %s", type(_err).__name__, slot, _err)
                continue
            if not cfg.enabled or cfg.pv_opportunist or cfg.trigger_entity or cfg.kind == "session":
                continue
            if cfg.max_days <= 0:
                continue
            # Bedarfs-Consumer (demand_entity, z.B. C1 mit ww_bedarf) sind nicht tage-, sondern
            # bedarfsgesteuert — ohne Bedarf gibt es bewusst keinen Plan (Audit 9.1). Kein Watchdog.
            if str(getattr(cfg, "demand_entity", "") or "").strip():
                continue
            learning = store.get_consumer_learning(str(slot)) or {}
            lr = learning.get("last_run_end_utc")
            last_run_dt = dt_util.parse_datetime(lr) if isinstance(lr, str) else None
            if last_run_dt is None:
                days_since = 999
            else:
                days_since = max(0, (today - dt_util.as_local(last_run_dt).date()).days)
            if days_since <= cfg.max_days:
                continue
            plan = plans.get(str(slot)) or plans.get(slot) or {}
            plan_valid = False
            if plan.get("status") == "planned":
                cs = plan.get("chosen_start")
                if cs:
                    try:
                        cs_dt = dt_util.parse_datetime(str(cs))
                        if cs_dt and dt_util.as_local(cs_dt).date() >= today:
                            plan_valid = True
                    except Exception as _err:
                        _LOGGER.debug("silent %s in chosen_start parse: %s", type(_err).__name__, _err)
            if not plan_valid:
                overdue.append(cfg.configured_name)
        return bool(overdue), overdue

    @callback
    def _evaluate(self) -> None:
        is_on, overdue = self._evaluate_state()
        prev_on = bool(self._attr_is_on)
        prev_list = list(self._overdue_consumers)
        self._overdue_consumers = overdue
        if is_on != prev_on:
            self._attr_is_on = is_on
            self.async_write_ha_state()
            if is_on:
                self.hass.async_create_task(self._on_overdue(overdue))
            else:
                store = getattr(self.coordinator, "store", None)
                if store:
                    store.log_activity("✅", "Hygiene-Watchdog: alle Consumer wieder im Plan")
        elif overdue != prev_list:
            self.async_write_ha_state()

    async def _on_overdue(self, overdue: list[str]) -> None:
        store = getattr(self.coordinator, "store", None)
        if store:
            store.log_activity("⚠️", f"Consumer überfällig: {', '.join(overdue)}")
        # Der Titel nennt die Namen: der Watchdog deckt ALLE intervallgesteuerten
        # Consumer ab, nicht nur die Boiler-Hygiene. Der alte Titel
        # "Hygiene-Consumer überfällig" liess eine Meldung über "Boiler daily"
        # wie einen Hygiene-Ausfall aussehen (26.08.).
        names = ", ".join(overdue[:2]) + (f" +{len(overdue) - 2}" if len(overdue) > 2 else "")
        try:
            from . import herold as _herold
            await _herold.senden(
                self.hass,
                topic="tariff_saver/hygiene_overdue",
                titel=f"Consumer überfällig: {names}",
                message=(
                    f"Diese Consumer sind überfällig und haben keinen gültigen Plan "
                    f"für heute/morgen: {', '.join(overdue)}"
                ),
                severity="warnung",
            )
        except Exception as _err:
            _LOGGER.debug("silent %s in herold hygiene_overdue: %s", type(_err).__name__, _err)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._evaluate()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = (last.state == "on")
        from homeassistant.helpers.event import async_track_time_interval
        from datetime import timedelta
        self._unsub_tick = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=self.TICK_SEC),
        )
        self._evaluate()

    @callback
    def _async_tick(self, _now) -> None:
        self._evaluate()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "overdue_consumers": list(self._overdue_consumers),
            "count": len(self._overdue_consumers),
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
        self._unsub_emergency_tick = None
        self._started_at: datetime | None = None  # set on transition False→True, cleared on True→False
        self._preflight_rejected_for: str | None = None  # chosen_start of plan that failed preflight (avoid replan spam)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from datetime import timedelta

        @callback
        def _emergency_tick(_now) -> None:
            # PV-Notlauf-Re-Evaluation: nur relevant wenn der Plan wegen
            # fehlender Tarif-Daten tot ist oder gerade ein Notlauf aktiv ist.
            store = getattr(self.coordinator, "store", None)
            if store is None:
                return
            running = getattr(store, f"_emrg_start_{self.slot}", None) is not None
            if not running:
                plans = (self.coordinator.data or {}).get("consumer_plans", {})
                plan = plans.get(self.slot) or plans.get(str(self.slot))
                if not (isinstance(plan, dict) and plan.get("status") == "error_no_data"):
                    return
            self.async_write_ha_state()

        self._unsub_emergency_tick = async_track_time_interval(
            self.hass, _emergency_tick, timedelta(minutes=3)
        )

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
        if self._unsub_emergency_tick:
            self._unsub_emergency_tick()
            self._unsub_emergency_tick = None

    def _get_plan(self) -> dict:
        """Effektiven Plan liefern UND fuer die Anzeige-Sensoren veroeffentlichen.

        `sensor.tariff_saver_consumer_X` darf `_emergency_pv_plan` nicht selbst
        aufrufen: das ist eine Zustandsmaschine mit Timern und einer globalen
        Notlauf-Sperre, ein zweiter Aufrufer wuerde sie verfaelschen. Stattdessen
        legen wir das Ergebnis am Coordinator ab; der Sensor liest es von dort
        und zeigt damit denselben Zustand wie dieser binary_sensor.

        Bewusst NICHT in coordinator.data: das wird bei jedem Refresh neu
        gebaut, und der Sensor schreibt seinen Zustand rund eine Millisekunde
        VOR diesem binary_sensor - er faende dort also nie einen Eintrag und
        fiele immer auf den Rohplan zurueck (am 30.08. live nachgemessen).
        Am Coordinator ueberlebt der Eintrag den Refresh; der Sensor zeigt
        damit hoechstens einen Zyklus Verzoegerung, was fuer eine Anzeige
        genuegt. Rein fluechtig, nichts davon wird persistiert.
        """
        plan = self._compute_plan()
        plans = getattr(self.coordinator, "effective_plans", None)
        if plans is None:
            plans = {}
            self.coordinator.effective_plans = plans
        plans[self.slot] = plan
        return plan

    def _compute_plan(self) -> dict:
        """Return pre-computed coordinated plan, or compute on-the-fly as fallback."""
        config = get_consumer_config(self.entry, self.slot)
        # Disabled consumer: leer + ruhig (kein Plan erwartet)
        if not config.enabled:
            return {"status": "disabled", "should_run": False}
        # PV-Opportunist: always compute fresh (real-time PV check, no caching)
        if config.pv_opportunist:
            store = getattr(self.coordinator, "store", None)
            learning = store.get_consumer_learning(str(self.slot)) if store is not None else {}
            return build_consumer_plan(self.hass, self.entry, config, learning, _active_slots(self.coordinator))
        plans = (self.coordinator.data or {}).get("consumer_plans", {})
        plan = plans.get(self.slot) or plans.get(str(self.slot))
        if plan is not None:
            # PV-Notlauf: keine Tarif-Daten → fälliger Consumer darf bei
            # echtem PV-Überschuss trotzdem laufen (Sicherheitsnetz, s. 19.–23.08.).
            if isinstance(plan, dict) and plan.get("status") == "error_no_data":
                emergency = self._emergency_pv_plan(config)
                if emergency is not None:
                    return emergency
            # Kein (oder kein weiterer) Notlauf → evtl. aktiven Notlauf-Zustand freigeben
            self._emergency_release()
            return plan
        # on_demand Consumer: Plan entsteht erst wenn Trigger-Entity feuert → kein Fehler
        if config.trigger_entity:
            return {"status": "waiting_trigger", "should_run": False, "source": "on_demand"}
        # Session Consumer (Wallbox): Plan entsteht erst wenn huawei_solar session_start ruft → kein Fehler
        if config.kind == "session":
            return {"status": "waiting_session", "should_run": False, "source": "session"}
        _LOGGER.error(
            "Consumer %d: Kein Plan vorhanden! Plans-Keys: %s",
            self.slot, list(plans.keys()) if plans else "leer",
        )
        return {"status": "error_no_plan", "should_run": False}

    def _emergency_pv_plan(self, config) -> dict | None:
        """PV-Notlauf: Plan-Status error_no_data, aber der Consumer ist fällig
        und echter PV-Überschuss steht an → should_run trotzdem on.

        Hysterese 120%/80% der Consumer-Leistung (wie pv_opportunist),
        min_runtime + Cooldown gegen Flattern, Duration-Cap als Obergrenze.
        Global läuft maximal EIN Notlauf gleichzeitig (geteilte Ressourcen
        wie WP-Kreis und Power-Budget werden nicht koordiniert).
        Rückgabe None = kein Notlauf; Aufräumen macht _emergency_release().
        """
        from .const import (
            CONF_EMERGENCY_PV_RUN_ENABLED,
            CONF_PV_SURPLUS_ENTITY,
            DEFAULT_EMERGENCY_PV_RUN_ENABLED,
            EMERGENCY_PV_START_FACTOR,
            EMERGENCY_PV_STOP_FACTOR,
        )

        enabled = self.entry.options.get(
            CONF_EMERGENCY_PV_RUN_ENABLED,
            self.entry.data.get(CONF_EMERGENCY_PV_RUN_ENABLED, DEFAULT_EMERGENCY_PV_RUN_ENABLED),
        )
        if not bool(enabled):
            return None
        # Nur Scheduler-Consumer: Opportunisten/on_demand/Session haben eigene
        # Pfade, Akku-Lader (tariff_only) will Grid-Preise, nicht PV.
        if config.pv_opportunist or config.trigger_entity or config.is_battery_charger or config.tariff_only:
            return None
        if config.kind == "session":
            return None
        if config.skip_next_run:
            return None
        power = float(config.power_kw or 0.0)
        if power <= 0:
            return None
        if config.pause_on_vacation:
            from .vacation import is_vacation_active
            if is_vacation_active(self.hass, self.entry):
                return None

        now_local = dt_util.now()
        from .on_demand import _is_in_allowed_window
        if not _is_in_allowed_window(now_local, config.allowed_from, config.allowed_until):
            return None

        store = getattr(self.coordinator, "store", None)
        if store is None:
            return None

        # Fällig? Kalendertage seit letztem Lauf ≥ max(min_days, 1)
        learning = store.get_consumer_learning(str(self.slot)) or {}
        last_run = learning.get("last_run_end_utc")
        if last_run:
            lr = dt_util.parse_datetime(str(last_run))
            if lr is not None:
                days = (now_local.date() - dt_util.as_local(lr).date()).days
                if days < max(int(config.min_days or 0), 1):
                    return None

        pv_entity = str(
            self.entry.options.get(CONF_PV_SURPLUS_ENTITY, self.entry.data.get(CONF_PV_SURPLUS_ENTITY, "")) or ""
        ).strip()
        if not pv_entity:
            return None
        from .slot_helpers import state_to_kw
        surplus = state_to_kw(self.hass.states.get(pv_entity))

        now = dt_util.utcnow()
        start_key = f"_emrg_start_{self.slot}"
        started = getattr(store, start_key, None)

        def _plan_dict() -> dict:
            return {
                "status": "pv_notlauf",
                "should_run": True,
                "source": "pv_notlauf",
                "chosen_start": None,
                "chosen_end": None,
                "power_kw": power,
                "pv_surplus_kw": round(surplus, 2),
                "reason": "error_no_data",
            }

        if started is not None:
            # Läuft bereits: Duration-Cap, dann min_runtime/Hysterese
            elapsed_min = (now - started).total_seconds() / 60.0
            max_dur = float(config.duration_minutes or 0)
            if max_dur > 0 and elapsed_min >= max_dur:
                return None
            min_rt = float(config.min_runtime_minutes or 0)
            if elapsed_min < min_rt or surplus >= power * EMERGENCY_PV_STOP_FACTOR:
                return _plan_dict()
            return None

        # Nicht laufend: global nur ein Notlauf, Start-Schwelle, Cooldown
        active = getattr(store, "_emrg_active_slot", None)
        if active is not None and active != self.slot:
            return None
        if surplus < power * EMERGENCY_PV_START_FACTOR:
            return None
        stopped = getattr(store, f"_emrg_stop_{self.slot}", None)
        cooldown_min = max(float(config.min_runtime_minutes or 0), 3.0)
        if stopped is not None and (now - stopped).total_seconds() / 60.0 < cooldown_min:
            return None

        setattr(store, start_key, now)
        setattr(store, "_emrg_active_slot", self.slot)
        name = config.configured_name or f"Consumer {self.slot}"
        store.log_activity("🚑", f"{name}: PV-Notlauf — keine Tarifdaten, Überschuss {surplus:.1f} kW")
        return _plan_dict()

    def _emergency_release(self) -> None:
        """Aktiven Notlauf-Zustand dieses Slots freigeben (Stop-Zeit für Cooldown merken)."""
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return
        start_key = f"_emrg_start_{self.slot}"
        if getattr(store, start_key, None) is not None:
            setattr(store, start_key, None)
            setattr(store, f"_emrg_stop_{self.slot}", dt_util.utcnow())
        if getattr(store, "_emrg_active_slot", None) == self.slot:
            setattr(store, "_emrg_active_slot", None)

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
                            except Exception as _err:
                                _LOGGER.debug("silent %s in %s: %s", type(_err).__name__, "time_str = f' {dt_util.as_local(s).strft", _err)
                        store.log_activity("🟢", f"{name} gestartet ({source}{time_str})")
                    else:
                        store.log_activity("⏹️", f"{name} beendet")
                        from homeassistant.util import dt as dt_util
                        # Hygiene-Consumer (max_days>0): last_run NICHT hier setzen.
                        # Deren Automation markiert via mark_consumer_run (nur bei Erfolg).
                        # Sonst würde ein abgebrochener/administrativ (Re-Plan) beendeter
                        # Lauf bei should_run on→off fälschlich als erledigt zählen
                        # (Bug entdeckt 2026-06-13).
                        if config.max_days <= 0:
                            store.set_consumer_last_run(str(self.slot), dt_util.utcnow())
                    self.hass.async_create_task(store.async_save())
        self._prev_is_on = current

    def _reject_plan(self, name: str, reason: str) -> None:
        """Preflight-Reject (Audit 7.2): Plan auf `rejected` setzen, damit weder Pin
        (`_pinned_window` pinnt nur `planned`) noch Hysterese (Consumer-Menge ändert
        sich → Vergleich übersprungen) das abgelehnte Fenster festhalten; dann Re-Plan
        ohne Hysterese mit Live-SOC/PV."""
        store = getattr(self.coordinator, "store", None)
        for plans in (getattr(store, "consumer_plans", None), (self.coordinator.data or {}).get("consumer_plans")):
            if not isinstance(plans, dict):
                continue
            p = plans.get(str(self.slot)) or plans.get(self.slot)
            if isinstance(p, dict):
                p["status"] = "rejected"
                p["reject_reason"] = reason
        if store:
            store.log_activity("⏭️", f"{name}: Pre-flight — {reason} — Re-Plan")
            store.dirty = True
        self.hass.async_create_task(self.hass.services.async_call("tariff_saver", "replan", {}))

    def _preflight_ok(self, config, plan: dict) -> bool:
        """Beim Consumer-Start prüfen, ob die Planungsannahmen noch stimmen (Audit 7.1).

        Der Plan deklariert seit Cluster 2 seine Quellen (`expected_pv/battery/grid_kwh`).
        Geprüft wird nur, was der Plan annimmt:
        - PV-Anteil: Live-Surplus ≥ power × pv_share − 0.5 kW (Plan `grid`/`battery` → kein PV-Veto, WW1).
        - Akku-Anteil: Live-Reserve (SOC − min_soc − Bedarf bis PV-Start ohne diesen Consumer)
          ≥ 80 % von expected_battery_kwh — sonst würde der Lauf real Netzbezug am Morgen erzeugen (A1).
        - Akku-Lader (C9): SOC noch unter target_soc_pct.
        Bei Fehlschlag: Plan `rejected` + Re-Plan (ohne Hysterese).
        """
        from .const import CONF_PV_SURPLUS_ENTITY, CONF_BATTERY_SOC_ENTITY
        from .slot_helpers import state_to_kw
        name = config.configured_name or f"Consumer {self.slot}"

        if getattr(config, "is_battery_charger", False):
            target = plan.get("battery_target_soc_pct")
            if isinstance(target, (int, float)) and target > 0:
                soc_entity = str(self.entry.options.get(CONF_BATTERY_SOC_ENTITY, self.entry.data.get(CONF_BATTERY_SOC_ENTITY, "")) or "").strip()
                if soc_entity:
                    st = self.hass.states.get(soc_entity)
                    if st and st.state not in ("unavailable", "unknown", ""):
                        try:
                            soc = float(st.state)
                        except (ValueError, TypeError):
                            soc = None
                        if soc is not None and soc >= float(target) - 1.0:
                            _LOGGER.info("Pre-flight %s: SOC %.0f%% ≥ target %.0f%% → skip", name, soc, float(target))
                            self._reject_plan(name, f"SOC {soc:.0f}% ≥ Ziel {float(target):.0f}%")
                            return False
            return True

        if config.pv_opportunist or config.trigger_entity:
            return True  # andere Pfade

        power = float(config.power_kw or 0.0)
        if power <= 0:
            return True
        pv_kwh = float(plan.get("expected_pv_kwh") or 0.0)
        batt_kwh = float(plan.get("expected_battery_kwh") or 0.0)
        grid_kwh = float(plan.get("expected_grid_kwh") or 0.0)
        total = pv_kwh + batt_kwh + grid_kwh
        # Alt-Plan ohne Split (vor Cluster 2): bisheriges Verhalten = voller PV-Check
        pv_share = (pv_kwh / total) if total > 0 else (0.0 if config.tariff_only else 1.0)

        # --- PV-Annahme prüfen ---
        if pv_share > 0.05:
            pv_entity = str(self.entry.options.get(CONF_PV_SURPLUS_ENTITY, self.entry.data.get(CONF_PV_SURPLUS_ENTITY, "")) or "").strip()
            if pv_entity:
                pv_surplus = state_to_kw(self.hass.states.get(pv_entity))
                needed = power * pv_share - 0.5
                if pv_surplus < needed:
                    _LOGGER.info("Pre-flight %s: PV-Surplus %.2f kW < %.2f kW (Plan %.0f%% PV) → skip, Re-Plan",
                                 name, pv_surplus, needed, pv_share * 100)
                    self._reject_plan(name, f"PV {pv_surplus:.1f} < {needed:.1f} kW (Plan {pv_share*100:.0f}% PV)")
                    return False

        # --- Akku-Annahme prüfen (A1: Reserve bis PV-Start muss bleiben) ---
        if batt_kwh > 0.1:
            eup = self.hass.states.get("sensor.tariff_saver_energy_until_pv")
            if eup and eup.attributes:
                try:
                    a = eup.attributes
                    own = float(plan.get("expected_energy_kwh") or 0.0)
                    other_consumers = max(0.0, float(a.get("consumer_kwh") or 0.0) - own)
                    reserve_need = float(a.get("base_load_kwh") or 0.0) + float(a.get("heating_kwh") or 0.0) + other_consumers
                    spare = float(a.get("available_kwh") or 0.0) - reserve_need
                except (TypeError, ValueError):
                    spare = None
                if spare is not None and spare < batt_kwh * 0.8:
                    _LOGGER.info("Pre-flight %s: Akku-Reserve %.1f kWh < Plan-Akkuanteil %.1f kWh → skip, Re-Plan",
                                 name, spare, batt_kwh)
                    self._reject_plan(name, f"Akku frei {spare:.1f} < {batt_kwh:.1f} kWh")
                    return False
        return True

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
                if getattr(config, "is_battery_charger", False):
                    from .const import CONF_PV_SURPLUS_ENTITY
                    from .slot_helpers import state_to_kw
                    pv_entity = str(self.entry.options.get(CONF_PV_SURPLUS_ENTITY, self.entry.data.get(CONF_PV_SURPLUS_ENTITY, "")) or "").strip()
                    if pv_entity and state_to_kw(self.hass.states.get(pv_entity)) > 0.5:
                        self._log_transition(False)
                        return False
                # Pre-Flight-Check: nur beim ersten is_on=True (Consumer noch nicht gestartet)
                if self._started_at is None:
                    cs_str = str(chosen_start)
                    if self._preflight_rejected_for != cs_str:
                        from .preflight_dispatcher import check_slot as _preflight_dispatch
                        if not _preflight_dispatch(self.hass, self.entry, self.coordinator, self.slot):
                            self._preflight_rejected_for = cs_str
                            name = config.configured_name or f"Consumer {self.slot}"
                            self._reject_plan(name, "Dispatcher abgelehnt (run_order/PV-Share/Grid-Cap)")
                            self._log_transition(False)
                            return False
                        if not self._preflight_ok(config, plan):
                            self._preflight_rejected_for = cs_str
                            self._log_transition(False)
                            return False
                    else:
                        # Preflight für dieses Plan-Fenster schon abgelehnt
                        self._log_transition(False)
                        return False
                result = True
        if not result:
            result = bool(plan.get("should_run", False))

        # Pure-PV-Consumer: bei PV-Verlust stoppen — aber Mindestlaufzeit respektieren.
        # Phase 1b (Rolling Plan): `_started_at` wird im Log-Transition-Hook gesetzt.
        config = get_consumer_config(self.entry, self.slot)
        if config.pv_opportunist and not result and self._started_at is not None:
            from homeassistant.util import dt as dt_util
            min_runtime = int(config.min_runtime_minutes or 0)
            if min_runtime > 0:
                elapsed = (dt_util.utcnow() - self._started_at).total_seconds() / 60.0
                if elapsed < min_runtime:
                    _LOGGER.debug(
                        "Consumer %d pv_opportunist: stop suppressed, min_runtime %d min not reached (%.1f min)",
                        self.slot, min_runtime, elapsed,
                    )
                    result = True  # weiterlaufen lassen bis min_runtime erreicht

        self._log_transition(result)
        # Track start time for min_runtime
        if result and self._started_at is None:
            from homeassistant.util import dt as dt_util
            self._started_at = dt_util.utcnow()
        elif not result and self._started_at is not None:
            self._started_at = None
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
            "expected_pv_kwh": plan.get("expected_pv_kwh"),
            "expected_battery_kwh": plan.get("expected_battery_kwh"),
            "expected_grid_kwh": plan.get("expected_grid_kwh"),
            "expected_cost_chf": plan.get("expected_cost_chf"),
            "chosen_start": plan.get("chosen_start"),
            "chosen_end": plan.get("chosen_end"),
            "tariff_window_start": plan.get("tariff_window_start"),
            "tariff_window_end": plan.get("tariff_window_end"),
            "tariff_avg_price_chf_per_kwh": plan.get("tariff_avg_price_chf_per_kwh"),
            "last_run_end_utc": learning.get("last_run_end_utc"),
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


class _PoolFreigabeBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity):
    """Base class for pool PV-surplus release sensors with hysteresis and dwell time."""

    _attr_has_entity_name = True

    PV_DEBOUNCE_SAMPLES = 5  # 5 aufeinanderfolgende Samples unter Schwelle → off (Spike-Schutz)
    PV_ON_DEBOUNCE_SEC = 90  # Überschuss muss ≥ N s über Schwelle bleiben → on (Spike-Schutz beim Einschalten)

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device_info(entry)
        self._prev_is_on: bool | None = None
        self._last_change: datetime | None = None
        self._below_count: int = 0
        self._above_since: datetime | None = None  # seit wann ununterbrochen über Schwelle
        self._pv_ok_state: bool = False  # entprellte PV-Überschuss-Entscheidung

    def _cfg(self, key: str, default):
        return self._entry.options.get(key, self._entry.data.get(key, default)) or default

    def _debounce_pv_ok(self, raw_pv_ok: bool) -> bool:
        """Symmetrischer Spike-Schutz für die PV-Überschuss-Entscheidung.

        Einschalten (zeitbasiert): Überschuss muss ununterbrochen ≥ PV_ON_DEBOUNCE_SEC
        über der Schwelle liegen — ein einzelner Mess-Spike startet nichts mehr.
        Ausschalten (sample-basiert, unverändert): erst nach PV_DEBOUNCE_SAMPLES
        aufeinanderfolgenden Below-Samples wirklich off (hält über kurze PV-Dips)."""
        if raw_pv_ok:
            self._below_count = 0
            if not self._pv_ok_state:
                now = dt_util.utcnow()
                if self._above_since is None:
                    self._above_since = now
                elif (now - self._above_since).total_seconds() >= self.PV_ON_DEBOUNCE_SEC:
                    self._pv_ok_state = True
        else:
            self._above_since = None
            if self._pv_ok_state:
                self._below_count += 1
                if self._below_count >= self.PV_DEBOUNCE_SAMPLES:
                    self._pv_ok_state = False
        return self._pv_ok_state

    def _pv_surplus_kw(self) -> float:
        from .const import CONF_PV_SURPLUS_ENTITY
        from .slot_helpers import state_to_kw
        pv_entity = str(self._cfg(CONF_PV_SURPLUS_ENTITY, "")).strip()
        if not pv_entity:
            return 0.0
        return state_to_kw(self.hass.states.get(pv_entity))

    def _apply_dwell(self, target: bool) -> bool:
        """Prevent flapping: stay in current state for at least dwell_minutes."""
        from .const import CONF_POOL_DWELL_MINUTES, DEFAULT_POOL_DWELL_MINUTES
        dwell = int(float(self._cfg(CONF_POOL_DWELL_MINUTES, DEFAULT_POOL_DWELL_MINUTES)))
        currently_on = bool(self._prev_is_on)
        if self._last_change is not None:
            elapsed = (dt_util.utcnow() - self._last_change).total_seconds()
            if elapsed < dwell * 60:
                return currently_on
        if target != currently_on:
            self._last_change = dt_util.utcnow()
        return target


class TariffSaverPoolFilterFreigabe(_PoolFreigabeBinarySensor):
    """ON when live PV surplus covers the filter pump.

    Reines PV-Signal. Der frühere Score-Fallback (Freigabe bei günstigem
    Netz-Score ohne PV) wurde 2026-08-26 entfernt: er liess die Pumpe nachts
    aus dem Akku laufen, obwohl das Tagesziel erreicht war (Audit Cluster 1).
    Die tägliche Pflicht-Filterung ohne PV übernimmt ausschliesslich
    Consumer 4 über den Scheduler (runtime_sensor + allowed_from/until).
    """

    _attr_name = "Pool Filter Freigabe"
    _attr_icon = "mdi:pump"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pool_filter_freigabe"

    @property
    def is_on(self) -> bool:
        from .const import CONF_POOL_FILTER_MIN_SURPLUS_KW, DEFAULT_POOL_FILTER_MIN_SURPLUS_KW
        threshold = float(self._cfg(CONF_POOL_FILTER_MIN_SURPLUS_KW, DEFAULT_POOL_FILTER_MIN_SURPLUS_KW))

        pv = self._pv_surplus_kw()
        currently_on = bool(self._prev_is_on)

        # Hysteresis: ON at threshold, OFF at 80% of threshold
        if currently_on:
            raw_pv_ok = pv >= threshold * 0.8
        else:
            raw_pv_ok = pv >= threshold
        pv_ok = self._debounce_pv_ok(raw_pv_ok)

        result = self._apply_dwell(pv_ok)
        self._prev_is_on = result
        return result

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import CONF_POOL_FILTER_MIN_SURPLUS_KW, DEFAULT_POOL_FILTER_MIN_SURPLUS_KW
        return {
            "pv_surplus_kw": round(self._pv_surplus_kw(), 2),
            "threshold_kw": float(self._cfg(CONF_POOL_FILTER_MIN_SURPLUS_KW, DEFAULT_POOL_FILTER_MIN_SURPLUS_KW)),
            "pv_ok": self._pv_ok_state,
            "reason": "pv" if self.is_on else "off",
        }


class TariffSaverPoolWpFreigabe(_PoolFreigabeBinarySensor):
    """ON when PV surplus covers filter + WP AND filter is running."""

    _attr_name = "Pool WP Freigabe"
    _attr_icon = "mdi:heat-pump-outline"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pool_wp_freigabe"
        self._on_since: datetime | None = None

    @property
    def is_on(self) -> bool:
        from .const import (
            CONF_POOL_WP_MIN_SURPLUS_KW, DEFAULT_POOL_WP_MIN_SURPLUS_KW,
            CONF_POOL_WP_MIN_RUNTIME, DEFAULT_POOL_WP_MIN_RUNTIME,
        )

        # Prerequisite: filter must be running (no water flow → no heating, override min-runtime)
        filter_state = self.hass.states.get("binary_sensor.tariff_saver_pool_filter_freigabe")
        if not filter_state or filter_state.state != "on":
            self._prev_is_on = False
            self._on_since = None
            return False

        threshold = float(self._cfg(CONF_POOL_WP_MIN_SURPLUS_KW, DEFAULT_POOL_WP_MIN_SURPLUS_KW))
        pv = self._pv_surplus_kw()
        currently_on = bool(self._prev_is_on)

        # Hysteresis: ON at threshold, OFF at 80%
        if currently_on:
            raw_ok = pv >= threshold * 0.8
        else:
            raw_ok = pv >= threshold
        target = self._debounce_pv_ok(raw_ok)

        # Min runtime: once started, hold ON for at least N minutes (compressor protection)
        min_runtime = int(float(self._cfg(CONF_POOL_WP_MIN_RUNTIME, DEFAULT_POOL_WP_MIN_RUNTIME)))
        if currently_on and not target and self._on_since is not None and min_runtime > 0:
            elapsed = (dt_util.utcnow() - self._on_since).total_seconds() / 60.0
            if elapsed < min_runtime:
                target = True

        result = self._apply_dwell(target)
        if result and not currently_on:
            self._on_since = dt_util.utcnow()
        elif not result:
            self._on_since = None
        self._prev_is_on = result
        return result

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import (
            CONF_POOL_WP_MIN_SURPLUS_KW, DEFAULT_POOL_WP_MIN_SURPLUS_KW,
            CONF_POOL_WP_MIN_RUNTIME, DEFAULT_POOL_WP_MIN_RUNTIME,
        )
        filter_state = self.hass.states.get("binary_sensor.tariff_saver_pool_filter_freigabe")
        attrs = {
            "pv_surplus_kw": round(self._pv_surplus_kw(), 2),
            "threshold_kw": float(self._cfg(CONF_POOL_WP_MIN_SURPLUS_KW, DEFAULT_POOL_WP_MIN_SURPLUS_KW)),
            "filter_running": filter_state.state == "on" if filter_state else False,
            "min_runtime_minutes": int(float(self._cfg(CONF_POOL_WP_MIN_RUNTIME, DEFAULT_POOL_WP_MIN_RUNTIME))),
            "pv_ok": self._pv_ok_state,
        }
        if self._on_since is not None:
            elapsed = (dt_util.utcnow() - self._on_since).total_seconds() / 60.0
            attrs["on_since"] = self._on_since.isoformat()
            attrs["elapsed_minutes"] = round(elapsed, 1)
        return attrs


class TariffSaverLadeempfehlungBinarySensor(CoordinatorEntity[TariffSaverCoordinator], BinarySensorEntity):
    """ON wenn Anstecken der EV-Fahrzeuge lohnt (Ladeermahnung-Signal).

    Zwei Kriterien (OR), Attribut "reason" = tarif / pv / beide:
    - tarif: im kommenden Nachtfenster (21:00-07:00 lokal) existiert ein
      zusammenhängendes Fenster von window_hours Länge, dessen Durchschnittspreis
      auf der Jahres-Skala (wie day_score) einen Score <= max_score ergibt.
    - pv: PV-Forecast des auf die Nacht folgenden Tages liefert nach Abzug der
      Saison-Grundlast >= min_pv_kwh Überschuss.
    Die Ermahnungs-Pushes sind HA-Automationen pro Fahrzeug (nicht hier).
    """

    _attr_has_entity_name = True
    _attr_name = "Ladeempfehlung"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: TariffSaverCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ladeempfehlung"
        self._attr_device_info = _device_info(entry)
        self._result: dict[str, Any] = {}
        self._cache_marker: Any = None
        self._profile_cache_day = None
        self._profile_cache: dict[int, float] = {}

    def _cfg(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _get_result(self) -> dict[str, Any]:
        # Einmal pro Coordinator-Update rechnen; is_on + Attribute lesen den Cache.
        data = self.coordinator.data or {}
        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        marker = stats.get("calculated_at")
        if marker is not None and marker == self._cache_marker and self._result:
            return self._result
        try:
            self._result = self._compute()
        except Exception as err:
            _LOGGER.warning("Ladeempfehlung compute failed: %s", err)
            self._result = {"is_on": False, "reason": None, "error": str(err)}
        self._cache_marker = marker
        return self._result

    def _seasonal_profile(self) -> dict[int, float]:
        # Profil ändert sich nur täglich (23:55-Snapshot) → pro Kalendertag cachen,
        # get_seasonal_load_profile ist ein Full-Scan über load_profiles.
        from .__init__ import _get_season
        today = dt_util.now().date()
        if self._profile_cache_day == today:
            return self._profile_cache
        store = getattr(self.coordinator, "store", None)
        season = _get_season(self.hass, self.entry)
        self._profile_cache = store.get_seasonal_load_profile(season) if store else {}
        self._profile_cache_day = today
        return self._profile_cache

    def _compute(self) -> dict[str, Any]:
        from datetime import timedelta
        from .const import (
            CONF_LADEEMPFEHLUNG_ENABLED, DEFAULT_LADEEMPFEHLUNG_ENABLED,
            CONF_LADEEMPFEHLUNG_MAX_SCORE, DEFAULT_LADEEMPFEHLUNG_MAX_SCORE,
            CONF_LADEEMPFEHLUNG_WINDOW_HOURS, DEFAULT_LADEEMPFEHLUNG_WINDOW_HOURS,
            CONF_LADEEMPFEHLUNG_MIN_PV_KWH, DEFAULT_LADEEMPFEHLUNG_MIN_PV_KWH,
            LADEEMPFEHLUNG_NIGHT_START_HOUR, LADEEMPFEHLUNG_NIGHT_END_HOUR,
            SLOTS_PER_HOUR, SLOT_MINUTES,
        )
        from .coordinator import _slot_total_price
        from .slot_helpers import score_from_prices

        enabled_raw = self._cfg(CONF_LADEEMPFEHLUNG_ENABLED, DEFAULT_LADEEMPFEHLUNG_ENABLED)
        enabled = DEFAULT_LADEEMPFEHLUNG_ENABLED if enabled_raw in (None, "") else bool(enabled_raw)
        max_score = int(float(self._cfg(CONF_LADEEMPFEHLUNG_MAX_SCORE, DEFAULT_LADEEMPFEHLUNG_MAX_SCORE) or DEFAULT_LADEEMPFEHLUNG_MAX_SCORE))
        window_hours = int(float(self._cfg(CONF_LADEEMPFEHLUNG_WINDOW_HOURS, DEFAULT_LADEEMPFEHLUNG_WINDOW_HOURS) or DEFAULT_LADEEMPFEHLUNG_WINDOW_HOURS))
        min_pv_kwh = float(self._cfg(CONF_LADEEMPFEHLUNG_MIN_PV_KWH, DEFAULT_LADEEMPFEHLUNG_MIN_PV_KWH) or DEFAULT_LADEEMPFEHLUNG_MIN_PV_KWH)

        now_local = dt_util.now()
        now_utc = dt_util.utcnow()

        # Kommendes Nachtfenster: vor 07:00 sind wir mitten drin (gestern 21:00 -
        # heute 07:00), sonst heute 21:00 - morgen 07:00.
        if now_local.hour < LADEEMPFEHLUNG_NIGHT_END_HOUR:
            night_start_local = (now_local - timedelta(days=1)).replace(hour=LADEEMPFEHLUNG_NIGHT_START_HOUR, minute=0, second=0, microsecond=0)
        else:
            night_start_local = now_local.replace(hour=LADEEMPFEHLUNG_NIGHT_START_HOUR, minute=0, second=0, microsecond=0)
        night_end_local = (night_start_local + timedelta(days=1)).replace(hour=LADEEMPFEHLUNG_NIGHT_END_HOUR)
        night_start_utc = dt_util.as_utc(night_start_local)
        night_end_utc = dt_util.as_utc(night_end_local)
        pv_day = night_end_local.date()  # der Tag, dessen PV nach der Nacht kommt

        data = self.coordinator.data or {}
        active = data.get("active", []) if isinstance(data, dict) else []
        stats = data.get("stats", {}) if isinstance(data, dict) else {}

        # --- Tarif-Kriterium: bestes zusammenhängendes Fenster im Nachtbereich ---
        night_slots = sorted(
            (s for s in active if night_start_utc <= s.start < night_end_utc and s.start >= now_utc),
            key=lambda s: s.start,
        )
        priced = []
        for s in night_slots:
            p = _slot_total_price(s.components_chf_per_kwh)
            if isinstance(p, (int, float)) and p > 0.10:  # Dummy-Preise filtern
                priced.append((s.start, float(p)))

        n_slots = max(1, window_hours * SLOTS_PER_HOUR)
        best_avg = None
        best_start = None
        span = timedelta(minutes=SLOT_MINUTES * (n_slots - 1))
        for i in range(0, len(priced) - n_slots + 1):
            if priced[i + n_slots - 1][0] - priced[i][0] != span:
                continue  # Lücke im Fenster
            avg = sum(p for _, p in priced[i : i + n_slots]) / n_slots
            if best_avg is None or avg < best_avg:
                best_avg = avg
                best_start = priced[i][0]

        year_min = stats.get("year_min_day_avg_chf_per_kwh")
        year_max = stats.get("year_max_day_avg_chf_per_kwh")
        window_score = score_from_prices(best_avg, year_min, year_max)
        tarif_ok = window_score is not None and window_score <= max_score

        # --- PV-Kriterium: Überschuss (Forecast - Grundlast) am pv_day ---
        day_start_utc = dt_util.as_utc(night_end_local.replace(hour=0, minute=0))
        day_end_utc = dt_util.as_utc(night_end_local.replace(hour=0, minute=0) + timedelta(days=1))
        profile = self._seasonal_profile()
        fallback_kwh = 0.15  # kWh pro 15-min-Slot ohne Profil-Daten
        pv_kwh = 0.0
        surplus_kwh = 0.0
        for s in active:
            if not (day_start_utc <= s.start < day_end_utc):
                continue
            pv_kw = float(getattr(s, "pv_kw", 0.0) or 0.0)
            if pv_kw <= 0:
                continue
            local = dt_util.as_local(s.start)
            idx = local.hour * SLOTS_PER_HOUR + local.minute // SLOT_MINUTES
            base_kw = float(profile.get(idx, fallback_kwh)) * SLOTS_PER_HOUR
            pv_kwh += pv_kw * 0.25
            surplus_kwh += max(0.0, pv_kw - base_kw) * 0.25
        pv_ok = surplus_kwh >= min_pv_kwh

        is_on = bool(enabled and (tarif_ok or pv_ok))
        reason = None
        if tarif_ok and pv_ok:
            reason = "beide"
        elif tarif_ok:
            reason = "tarif"
        elif pv_ok:
            reason = "pv"

        return {
            "is_on": is_on,
            "enabled": enabled,
            "reason": reason if is_on else None,
            "best_window_start": dt_util.as_local(best_start).isoformat() if best_start else None,
            "best_window_end": dt_util.as_local(best_start + timedelta(minutes=SLOT_MINUTES * n_slots)).isoformat() if best_start else None,
            "best_window_avg_chf_per_kwh": round(best_avg, 4) if best_avg is not None else None,
            "best_window_avg_rp_per_kwh": round(best_avg * 100.0, 1) if best_avg is not None else None,
            "best_window_score": window_score,
            "max_score": max_score,
            "window_hours": window_hours,
            "pv_day": pv_day.isoformat(),
            "pv_forecast_kwh": round(pv_kwh, 1),
            "pv_surplus_kwh": round(surplus_kwh, 1),
            "min_pv_kwh": min_pv_kwh,
            "night_window": f"{night_start_local.strftime('%d.%m. %H:%M')} - {night_end_local.strftime('%d.%m. %H:%M')}",
        }

    @property
    def is_on(self) -> bool:
        return bool(self._get_result().get("is_on"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = dict(self._get_result())
        result.pop("is_on", None)
        return result
