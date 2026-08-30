"""Standby-Wächter — Geräte abschalten, die an sind aber nur noch dümpeln.

Erfasst switch-Entities via HA-Label `standby_aus`. Der zugehörige
Leistungssensor wird automatisch über die Device-Registry gefunden (gleiches
Gerät, device_class power; bei Mehrkanal-Geräten via Objekt-ID-Präfix).

Logik pro Tick (alle STANDBY_WATCH_TICK_MINUTES):
  - Switch ist "on" UND Leistung < Schwelle des Geräts (Setting
    `standby_watch_watts` JSON, sonst max_watts) ununterbrochen über die Dauer
    des Geräts (Setting `standby_watch_hours` JSON, sonst default_hours)
    → homeassistant.turn_off + Activity-Log + Herold-Info-Push.
  - Jeder Messwert ≥ Schwelle (oder Sensor unavailable) resettet den Timer.
  - Geräte im pv_dump-managed-Set werden übersprungen (PV-Dump räumt selbst auf).

Timer sind in-memory: nach Restart beginnt die Messung frisch (bei
Stunden-Schwellen verschmerzbar, gleiche Entscheidung wie PV-Dump-Dwell).
Typische Fänge: Kompressor vergessen, Ladegerät auf Erhaltungsladung,
Lötstation über Nacht.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import herold as _herold
from .const import (
    CONF_STANDBY_WATCH_DEFAULT_HOURS,
    CONF_STANDBY_WATCH_ENABLED,
    CONF_STANDBY_WATCH_HOURS,
    CONF_STANDBY_WATCH_MAX_WATTS,
    CONF_STANDBY_WATCH_WATTS,
    DEFAULT_STANDBY_WATCH_DEFAULT_HOURS,
    DEFAULT_STANDBY_WATCH_ENABLED,
    DEFAULT_STANDBY_WATCH_HOURS,
    DEFAULT_STANDBY_WATCH_MAX_WATTS,
    DEFAULT_STANDBY_WATCH_WATTS,
    STANDBY_WATCH_LABEL,
    STANDBY_WATCH_TICK_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

HEROLD_TOPIC = "tariff_saver/standby_off"


def _config_value(entry: ConfigEntry, key: str, default: Any) -> Any:
    src = entry.options if entry.options else entry.data
    val = src.get(key, default)
    if val is None or val == "":
        return default
    return val


def _read_watts(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a power sensor as Watts. Handles kW units. None if unavailable."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None or st.state in ("unavailable", "unknown", ""):
        return None
    try:
        val = float(st.state)
    except (ValueError, TypeError):
        return None
    unit = str(st.attributes.get("unit_of_measurement") or "").lower()
    if unit == "kw":
        val *= 1000.0
    return val


def discover_switches(hass: HomeAssistant) -> list[str]:
    """All switch entity_ids carrying the standby_aus label."""
    registry = er.async_get(hass)
    found: set[str] = set()
    for rec in registry.entities.values():
        if STANDBY_WATCH_LABEL in (rec.labels or set()) and rec.domain == "switch":
            found.add(rec.entity_id)
    return sorted(found)


def find_power_sensor(hass: HomeAssistant, switch_entity_id: str) -> str | None:
    """Find the power sensor belonging to a switch via the device registry.

    Prefers a sensor whose object_id starts with the switch's object_id
    (Shelly-Muster: switch.x → sensor.x_leistung); fällt auf den einzigen
    Power-Sensor des Geräts zurück.
    """
    registry = er.async_get(hass)
    rec = registry.async_get(switch_entity_id)
    if rec is None or not rec.device_id:
        return None
    switch_object_id = switch_entity_id.split(".", 1)[1]
    candidates: list[str] = []
    for other in er.async_entries_for_device(registry, rec.device_id):
        if other.domain != "sensor":
            continue
        st = hass.states.get(other.entity_id)
        device_class = (st.attributes.get("device_class") if st else None) or other.original_device_class
        if device_class != "power":
            continue
        candidates.append(other.entity_id)
    for cand in candidates:
        if cand.split(".", 1)[1].startswith(switch_object_id):
            return cand
    if len(candidates) == 1:
        return candidates[0]
    return None


class StandbyWatchManager:
    """Periodic tick that turns off labeled switches idling below the threshold."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._unsub: CALLBACK_TYPE | None = None
        self._unsub_registry: CALLBACK_TYPE | None = None
        # In-memory: entity_id → seit wann ununterbrochen unter der Schwelle
        self._low_since: dict[str, datetime] = {}

    # ---------- config ----------

    def _enabled(self) -> bool:
        return bool(_config_value(self.entry, CONF_STANDBY_WATCH_ENABLED, DEFAULT_STANDBY_WATCH_ENABLED))

    def _max_watts(self) -> float:
        return float(_config_value(self.entry, CONF_STANDBY_WATCH_MAX_WATTS, DEFAULT_STANDBY_WATCH_MAX_WATTS))

    def _default_hours(self) -> float:
        return float(_config_value(self.entry, CONF_STANDBY_WATCH_DEFAULT_HOURS, DEFAULT_STANDBY_WATCH_DEFAULT_HOURS))

    def _json_overrides(self, conf_key: str, default: str) -> dict[str, float]:
        """Parse a per-device override setting: JSON {entity_id: number > 0}."""
        raw = _config_value(self.entry, conf_key, default)
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            return {}
        result: dict[str, float] = {}
        for eid, num in (parsed or {}).items():
            try:
                val = float(num)
            except (ValueError, TypeError):
                continue
            if val > 0:
                result[str(eid)] = val
        return result

    def hours_overrides(self) -> dict[str, float]:
        return self._json_overrides(CONF_STANDBY_WATCH_HOURS, DEFAULT_STANDBY_WATCH_HOURS)

    def hours_for(self, entity_id: str) -> float:
        return self.hours_overrides().get(entity_id, self._default_hours())

    def watts_overrides(self) -> dict[str, float]:
        return self._json_overrides(CONF_STANDBY_WATCH_WATTS, DEFAULT_STANDBY_WATCH_WATTS)

    def watts_for(self, entity_id: str) -> float:
        return self.watts_overrides().get(entity_id, self._max_watts())

    def _pv_dump_managed(self) -> set[str]:
        store = getattr(self.coordinator, "store", None)
        st = getattr(store, "pv_dump_state", None) if store is not None else None
        return set((st or {}).get("managed") or {})

    def low_since(self, entity_id: str) -> datetime | None:
        return self._low_since.get(entity_id)

    def _log(self, icon: str, msg: str) -> None:
        store = getattr(self.coordinator, "store", None)
        if store is not None:
            store.log_activity(icon, msg)

    # ---------- core tick ----------

    async def _evaluate(self) -> None:
        if not self._enabled():
            self._low_since.clear()
            return

        pv_managed = self._pv_dump_managed()
        now = dt_util.utcnow()
        switches = discover_switches(self.hass)

        # Timer von nicht mehr überwachten Geräten aufräumen
        for eid in list(self._low_since):
            if eid not in switches:
                del self._low_since[eid]

        for eid in switches:
            st = self.hass.states.get(eid)
            if st is None or st.state != "on":
                self._low_since.pop(eid, None)
                continue
            if eid in pv_managed:
                self._low_since.pop(eid, None)
                continue
            power_entity = find_power_sensor(self.hass, eid)
            if power_entity is None:
                continue  # kein Sensor → Gerät kann nicht überwacht werden (Sensor zeigt Warnung)
            threshold = self.watts_for(eid)
            watts = _read_watts(self.hass, power_entity)
            if watts is None or watts >= threshold:
                self._low_since.pop(eid, None)
                continue
            since = self._low_since.get(eid)
            if since is None:
                self._low_since[eid] = now
                continue
            hours = self.hours_for(eid)
            if (now - since).total_seconds() < hours * 3600.0:
                continue
            # Schwelle über volle Dauer unterschritten → abschalten
            self._low_since.pop(eid, None)
            friendly = st.attributes.get("friendly_name") or eid
            await self.hass.services.async_call(
                "homeassistant", "turn_off", {"entity_id": eid}, blocking=False,
            )
            hours_label = f"{hours:g} h"
            self._log("😴", f"Standby-Wächter: {friendly} zog {hours_label} lang < {threshold:g} W → aus")
            await _herold.senden(
                self.hass,
                topic=HEROLD_TOPIC,
                titel="Standby-Wächter",
                message=f"{friendly} war an, zog aber {hours_label} lang unter {threshold:g} W ({watts:.1f} W) — ausgeschaltet.",
            )

    # ---------- lifecycle ----------

    @callback
    def _on_registry_update(self, event) -> None:
        """Label hinzugefügt/entfernt → Sensor-Refresh + Timer-Cleanup."""
        action = event.data.get("action")
        if action not in ("update", "create", "remove"):
            return
        changes = event.data.get("changes") or {}
        if action == "update" and "labels" not in changes:
            return
        try:
            self.coordinator.async_set_updated_data(self.coordinator.data)
        except Exception as _err:
            _LOGGER.debug("standby_watch: %s in registry refresh: %s", type(_err).__name__, _err)
        self.hass.async_create_task(self._evaluate())

    def start(self) -> None:
        @callback
        def _tick(_now) -> None:
            self.hass.async_create_task(self._evaluate())

        self._unsub = async_track_time_interval(
            self.hass, _tick, timedelta(minutes=STANDBY_WATCH_TICK_MINUTES),
        )
        self._unsub_registry = self.hass.bus.async_listen(
            "entity_registry_updated", self._on_registry_update,
        )
        self.hass.async_create_task(self._evaluate())

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._unsub_registry:
            self._unsub_registry()
            self._unsub_registry = None


def async_setup_standby_watch(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> CALLBACK_TYPE:
    """Initialize the Standby-Wächter subsystem. Returns unsub callable."""
    manager = StandbyWatchManager(hass, entry, coordinator)
    manager.start()
    coordinator.standby_watch = manager
    return manager.stop
