"""PV-Überschuss Dump-Loads — Geräte bei deutlicher Netzeinspeisung einschalten.

Idee: Statt jedes Gerät einzeln als Consumer anzulegen, wird eine Geräteliste
per HA-Label gepflegt. Alle switch/light/input_boolean mit Label
`pv_ueberschuss` werden GEMEINSAM eingeschaltet, sobald real Strom ins Netz
fliesst (= Akku versorgt, echter Überschuss), und wieder ausgeschaltet sobald
wieder Netzbezug nötig ist.

Gate-Signal (statt SOC): die Netz-Ein-/Ausspeiseleistung.
  - `grid_out_entity` (W, Einspeisung ins Netz) ≥ on_watts  → einschalten
  - `grid_in_entity`  (W, Bezug aus Netz)      ≥ off_watts → ausschalten
  - `pv_power_entity` (W, PV-Produktion)  < PV_ZERO_WATTS → ausschalten
Solange der Akku den PV-Überschuss schluckt, ist grid_out ~0 → nichts passiert.
Erst wenn der Akku voll/limitiert ist, steigt grid_out → echter Dump-Überschuss.
Der PV-Produktions-Check ist der Abend-Backstop: nach Sonnenuntergang deckt
der Hausakku die Last, grid_in bleibt ~0 und würde off_watts nie erreichen —
die Dump-Loads würden sonst die ganze Nacht den Akku leersaugen.

Schutzmechanismen:
  - Hysterese: getrennte Ein-/Aus-Schwellen.
  - Dwell: Schwelle muss `dwell_seconds` anhalten (gegen Wolken-Flackern).
  - Min-Laufzeit: einmal ein, bleiben die Geräte `min_on_minutes` an
    (verhindert Flattern bei "alle gleichzeitig", wenn Gerätelast > Überschuss).
  - Nur eigene Geräte: das Modul schaltet nur Geräte AUS, die es selbst
    eingeschaltet hat. Ein manuell eingeschaltetes Gerät wird nie ausgeknipst.
    Aus `managed` entlassen wird ein Gerät nur, wenn es explizit `off` meldet —
    `unavailable` oder eine noch nicht geladene Entity zählen NICHT als aus.
  - Abschalten mit Quittung: `managed` wird erst geleert, wenn die Geräte
    bestätigt aus sind; sonst schaltet `_settle_off` auf den Folgeticks nach.

Persistierter State (store.pv_dump_state):
  {"active": bool, "on_since_iso": str|None, "managed": {entity_id: on_ts_iso},
   "off_attempts": int}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PV_DUMP_DWELL_SECONDS,
    CONF_PV_DUMP_ENABLED,
    CONF_PV_DUMP_GRID_IN_ENTITY,
    CONF_PV_DUMP_GRID_OUT_ENTITY,
    CONF_PV_DUMP_MIN_ON_MINUTES,
    CONF_PV_DUMP_OFF_WATTS,
    CONF_PV_DUMP_ON_WATTS,
    CONF_PV_DUMP_PAUSE_ON_VACATION,
    CONF_PV_DUMP_PV_POWER_ENTITY,
    DEFAULT_PV_DUMP_DWELL_SECONDS,
    DEFAULT_PV_DUMP_ENABLED,
    DEFAULT_PV_DUMP_GRID_IN_ENTITY,
    DEFAULT_PV_DUMP_GRID_OUT_ENTITY,
    DEFAULT_PV_DUMP_MIN_ON_MINUTES,
    DEFAULT_PV_DUMP_OFF_WATTS,
    DEFAULT_PV_DUMP_ON_WATTS,
    DEFAULT_PV_DUMP_PAUSE_ON_VACATION,
    DEFAULT_PV_DUMP_PV_POWER_ENTITY,
    PV_DUMP_LABEL,
    PV_DUMP_OFF_MAX_ATTEMPTS,
    PV_DUMP_PV_ZERO_WATTS,
    PV_DUMP_TICK_SECONDS,
)
from .vacation import is_vacation_active

_LOGGER = logging.getLogger(__name__)


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
    if unit in ("kw", "kwh"):  # kW → W (kWh nur defensiv, sollte nicht vorkommen)
        val *= 1000.0
    return val


def discover_entities(hass: HomeAssistant) -> list[str]:
    """All entity_ids carrying the pv_ueberschuss label (switch/light/input_boolean)."""
    registry = er.async_get(hass)
    found: set[str] = set()
    for rec in registry.entities.values():
        if PV_DUMP_LABEL in (rec.labels or set()):
            found.add(rec.entity_id)
    return sorted(found)


def _is_on(hass: HomeAssistant, entity_id: str) -> bool:
    st = hass.states.get(entity_id)
    return bool(st and st.state == "on")


def _is_off(hass: HomeAssistant, entity_id: str) -> bool:
    """True nur bei explizitem `off`.

    `unavailable`, `unknown` und eine noch gar nicht angelegte Entity (direkt
    nach einem HA-Restart, bevor die Shelly-Integration soweit ist) sind
    KEIN Aus — sie sagen nur, dass wir es gerade nicht wissen. Das
    auseinanderzuhalten ist der ganze Punkt: „kein Signal" als „der User hat
    ausgeschaltet" zu lesen hat Geräte aus `managed` geworfen und damit
    dauerhaft anbleiben lassen.
    """
    st = hass.states.get(entity_id)
    return bool(st and st.state == "off")


class PvDumpManager:
    """Periodic tick that turns labeled devices on/off with grid feed-in surplus."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._unsub: CALLBACK_TYPE | None = None
        self._unsub_registry: CALLBACK_TYPE | None = None
        # In-memory dwell timers (reset on restart → fresh dwell needed, safe)
        self._above_since: datetime | None = None  # grid_out ≥ on_watts continuously
        self._below_since: datetime | None = None  # grid_in ≥ off_watts continuously

    # ---------- config ----------

    def _enabled(self) -> bool:
        return bool(_config_value(self.entry, CONF_PV_DUMP_ENABLED, DEFAULT_PV_DUMP_ENABLED))

    def _grid_out_entity(self) -> str:
        return str(_config_value(self.entry, CONF_PV_DUMP_GRID_OUT_ENTITY, DEFAULT_PV_DUMP_GRID_OUT_ENTITY))

    def _grid_in_entity(self) -> str:
        return str(_config_value(self.entry, CONF_PV_DUMP_GRID_IN_ENTITY, DEFAULT_PV_DUMP_GRID_IN_ENTITY))

    def _on_watts(self) -> float:
        return float(_config_value(self.entry, CONF_PV_DUMP_ON_WATTS, DEFAULT_PV_DUMP_ON_WATTS))

    def _off_watts(self) -> float:
        return float(_config_value(self.entry, CONF_PV_DUMP_OFF_WATTS, DEFAULT_PV_DUMP_OFF_WATTS))

    def _dwell_seconds(self) -> float:
        return float(_config_value(self.entry, CONF_PV_DUMP_DWELL_SECONDS, DEFAULT_PV_DUMP_DWELL_SECONDS))

    def _min_on_minutes(self) -> float:
        return float(_config_value(self.entry, CONF_PV_DUMP_MIN_ON_MINUTES, DEFAULT_PV_DUMP_MIN_ON_MINUTES))

    def _pv_power_entity(self) -> str:
        return str(_config_value(self.entry, CONF_PV_DUMP_PV_POWER_ENTITY, DEFAULT_PV_DUMP_PV_POWER_ENTITY))

    def _vacation_paused(self) -> bool:
        return bool(
            _config_value(self.entry, CONF_PV_DUMP_PAUSE_ON_VACATION, DEFAULT_PV_DUMP_PAUSE_ON_VACATION)
        ) and is_vacation_active(self.hass, self.entry)

    # ---------- persisted state ----------

    def _state(self) -> dict[str, Any]:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return {}
        if not hasattr(store, "pv_dump_state") or store.pv_dump_state is None:
            store.pv_dump_state = {}
        return store.pv_dump_state

    def _save_state(self) -> None:
        store = getattr(self.coordinator, "store", None)
        if store is not None:
            store.dirty = True
            self.hass.async_create_task(store.async_save())

    def _log(self, icon: str, msg: str) -> None:
        store = getattr(self.coordinator, "store", None)
        if store is not None:
            store.log_activity(icon, msg)

    # ---------- switching ----------

    async def _turn_on_all(self, entities: list[str]) -> dict[str, str]:
        """Turn on every labeled device that is currently OFF. Return {entity: on_ts_iso}."""
        managed: dict[str, str] = {}
        to_switch: list[str] = []
        for eid in entities:
            if _is_on(self.hass, eid):
                continue  # bereits an (evtl. vom User) → nicht übernehmen, nicht abschalten
            to_switch.append(eid)
        if to_switch:
            await self.hass.services.async_call(
                "homeassistant", "turn_on", {"entity_id": to_switch}, blocking=False,
            )
            now_iso = dt_util.utcnow().isoformat()
            for eid in to_switch:
                managed[eid] = now_iso
        return managed

    async def _turn_off_managed(self, managed: dict[str, str]) -> int:
        """Turn off devices WE turned on. Return count of commanded entities.

        Angesprochen wird alles, was nicht explizit `off` ist — ein
        `unavailable`-Blip im Moment des Abschaltens darf kein Gerät
        überspringen. `managed` bleibt stehen; erst `_settle_off` entlässt ein
        Gerät, das bestätigt aus ist.
        """
        to_switch = [eid for eid in managed if not _is_off(self.hass, eid)]
        if to_switch:
            await self.hass.services.async_call(
                "homeassistant", "turn_off", {"entity_id": to_switch}, blocking=False,
            )
        return len(to_switch)

    async def _settle_off(self, state: dict[str, Any], managed: dict[str, str]) -> dict[str, str]:
        """Abschaltversuch quittieren: bestätigt aus → raus, Rest nachschalten.

        Läuft auf den Ticks NACH einem Abschaltbefehl. Geräte, die inzwischen
        `off` melden, verlassen `managed` (Zuständigkeit beendet). Alles andere
        bekommt erneut `turn_off`, bis `PV_DUMP_OFF_MAX_ATTEMPTS` erreicht ist —
        dann aufgeben und melden, statt still im Kreis zu laufen.
        """
        remaining = {eid: ts for eid, ts in managed.items() if not _is_off(self.hass, eid)}
        prev_attempts = int(state.get("off_attempts") or 0)
        attempts = prev_attempts
        if remaining:
            attempts += 1
            if attempts > PV_DUMP_OFF_MAX_ATTEMPTS:
                _LOGGER.warning(
                    "pv_dump: %s liess sich in %d Versuchen nicht ausschalten — aufgegeben",
                    ", ".join(sorted(remaining)), PV_DUMP_OFF_MAX_ATTEMPTS,
                )
                self._log(
                    "🛑",
                    f"PV-Überschuss: {len(remaining)} Gerät(e) liessen sich nicht "
                    f"ausschalten — aufgegeben",
                )
                remaining = {}
                attempts = 0
            else:
                await self.hass.services.async_call(
                    "homeassistant", "turn_off", {"entity_id": list(remaining)}, blocking=False,
                )
        else:
            attempts = 0
        if remaining != managed or attempts != prev_attempts:
            state["managed"] = remaining
            state["off_attempts"] = attempts
            self._save_state()
        return remaining

    # ---------- core tick ----------

    async def _evaluate(self) -> None:
        state = self._state()
        active = bool(state.get("active", False))
        managed: dict[str, str] = dict(state.get("managed") or {})

        # Modul aus ODER Ferienmodus: laufende Dump-Loads sauber beenden, dann inert
        vacation_paused = self._vacation_paused()
        if not self._enabled() or vacation_paused:
            if active:
                n = await self._turn_off_managed(managed)
                if vacation_paused:
                    self._log("🏖️", f"PV-Überschuss: Ferienmodus — {n} Gerät(e) aus")
                else:
                    self._log("🛑", f"PV-Überschuss deaktiviert — {n} Gerät(e) aus")
                state.update({"active": False, "on_since_iso": None, "off_attempts": 1})
                self._save_state()
            elif managed:
                # Reste aus einem früheren Abschaltversuch auch im deaktivierten
                # Zustand nachziehen — sonst bliebe ein Gerät ewig hängen.
                await self._settle_off(state, managed)
            self._above_since = None
            self._below_since = None
            return

        grid_out = _read_watts(self.hass, self._grid_out_entity())
        grid_in = _read_watts(self.hass, self._grid_in_entity())
        if grid_out is None and grid_in is None:
            return  # keine Sensordaten → nichts tun

        now = dt_util.utcnow()
        dwell = self._dwell_seconds()
        entities = discover_entities(self.hass)

        if not active:
            # Reste eines Abschaltversuchs quittieren/nachschalten. Erst wenn ein
            # Gerät bestätigt aus ist, endet unsere Zuständigkeit.
            if managed:
                managed = await self._settle_off(state, managed)
            self._below_since = None
            on_watts = self._on_watts()
            if grid_out is not None and grid_out >= on_watts:
                if self._above_since is None:
                    self._above_since = now
                elif (now - self._above_since).total_seconds() >= dwell:
                    # EINSCHALTEN
                    if not entities:
                        return  # nichts getaggt
                    switched_on = await self._turn_on_all(entities)
                    self._above_since = None
                    # Noch nicht bestätigt ausgeschaltete Geräte bleiben unsere —
                    # `_turn_on_all` überspringt sie (schon `on`) und würde sie
                    # sonst aus `managed` fallen lassen.
                    new_managed = {**managed, **switched_on}
                    state.update({
                        "active": True,
                        "on_since_iso": now.isoformat(),
                        "managed": new_managed,
                        "off_attempts": 0,
                    })
                    self._save_state()
                    if switched_on:
                        self._log(
                            "☀️",
                            f"PV-Überschuss: {grid_out/1000:.1f} kW ins Netz → "
                            f"{len(switched_on)} Gerät(e) ein",
                        )
                    # Sonst waren alle getaggten Geräte schon an (User) → aktiv
                    # markieren, aber nicht übernehmen (managed bleibt wie es war).
            else:
                self._above_since = None
            return

        # active == True
        self._above_since = None
        # Manuell ausgeschaltete Geräte aus managed entfernen (nicht wieder anfassen).
        # NUR bei explizitem `off` — `unavailable`/noch nicht geladen heisst nicht
        # "der User hat ausgeschaltet", sondern "gerade unbekannt" (siehe `_is_off`).
        pruned = {eid: ts for eid, ts in managed.items() if not _is_off(self.hass, eid)}
        if pruned != managed:
            managed = pruned
            state["managed"] = managed
            self._save_state()

        # Min-Laufzeit prüfen
        on_since = None
        osi = state.get("on_since_iso")
        if isinstance(osi, str):
            on_since = dt_util.parse_datetime(osi)
        min_on_s = self._min_on_minutes() * 60.0
        if on_since is not None and (now - dt_util.as_utc(on_since)).total_seconds() < min_on_s:
            self._below_since = None  # Min-Laufzeit läuft → Aus-Timer zurücksetzen
            return

        off_watts = self._off_watts()
        pv = _read_watts(self.hass, self._pv_power_entity())
        grid_in_off = grid_in is not None and grid_in >= off_watts
        pv_zero_off = pv is not None and pv < PV_DUMP_PV_ZERO_WATTS
        if grid_in_off or pv_zero_off:
            if self._below_since is None:
                self._below_since = now
            elif (now - self._below_since).total_seconds() >= dwell:
                # AUSSCHALTEN (nur eigene Geräte)
                n = await self._turn_off_managed(managed)
                self._below_since = None
                # `managed` bleibt stehen: die nächsten Ticks quittieren das Aus
                # (`_settle_off`) und schalten nach, falls ein Gerät den Befehl
                # nicht mitbekommen hat.
                state.update({"active": False, "on_since_iso": None, "off_attempts": 1})
                self._save_state()
                if grid_in_off:
                    reason = f"{grid_in/1000:.1f} kW Netzbezug"
                else:
                    reason = f"PV-Produktion {pv:.0f} W"
                self._log("🌥️", f"PV-Überschuss vorbei: {reason} → {n} Gerät(e) aus")
        else:
            self._below_since = None

    # ---------- lifecycle ----------

    @callback
    def _on_registry_update(self, event) -> None:
        """Label hinzugefügt/entfernt → Sensor-Refresh + Re-Evaluation."""
        action = event.data.get("action")
        if action not in ("update", "create", "remove"):
            return
        changes = event.data.get("changes") or {}
        if action == "update" and "labels" not in changes:
            return
        try:
            self.coordinator.async_set_updated_data(self.coordinator.data)
        except Exception as _err:
            _LOGGER.debug("pv_dump: %s in registry refresh: %s", type(_err).__name__, _err)
        self.hass.async_create_task(self._evaluate())

    def start(self) -> None:
        @callback
        def _tick(_now) -> None:
            self.hass.async_create_task(self._evaluate())

        self._unsub = async_track_time_interval(
            self.hass, _tick, timedelta(seconds=PV_DUMP_TICK_SECONDS),
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


def async_setup_pv_dump(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> CALLBACK_TYPE:
    """Initialize the PV-Überschuss Dump-Load subsystem. Returns unsub callable."""
    manager = PvDumpManager(hass, entry, coordinator)
    manager.start()
    coordinator.pv_dump = manager
    return manager.stop
