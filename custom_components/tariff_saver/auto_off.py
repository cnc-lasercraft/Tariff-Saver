"""Light Auto-Off — vergessene Lichter abschalten ohne Komforteinbussen.

Erfasst Lichter via:
  (1) Glob-Pattern (Default `input_select.licht_mode_*`) auf Entity-IDs
  (2) UNION mit Entities die ein Label `auto_off_<N>h` tragen

Schwelle pro Licht aus dem Label-Suffix (z.B. `auto_off_4h` = 4h). Ohne Label
werden Pattern-Matches in der Übersicht gezeigt, aber NICHT überwacht.

"An"-Erkennung pro Domain:
  - input_select.* → state != LIGHT_MODE_OFF_VALUE ("Aus")
  - light.* / switch.* → state == "on"

Aus-schalten:
  - Mode-Helper (input_select.licht_mode_*) → script.master_dimmer_skript
  - Label-only auf light.*/switch.* → homeassistant.turn_off

Flow:
  1. Tick alle LIGHT_AUTO_OFF_TICK_MINUTES (5 min)
  2. Pro überwachtem Licht: brennt länger als Schwelle? → Push wenn nicht snoozed
  3. 10 min nach Push ohne Reaktion → Auto-Off + Bestätigungs-Push
  4. User-Klick auf Action-Button → Service tariff_saver.light_auto_off_action
"""
from __future__ import annotations

import fnmatch
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
    CONF_LIGHT_AUTO_OFF_TIMEOUT_MINUTES,
    CONF_LIGHT_HELPER_PATTERN,
    DEFAULT_LIGHT_AUTO_OFF_TIMEOUT_MINUTES,
    DEFAULT_LIGHT_HELPER_PATTERN,
    LIGHT_AUTO_OFF_HOURS,
    LIGHT_AUTO_OFF_LABEL_PREFIX,
    LIGHT_AUTO_OFF_TEST_MINUTES,
    LIGHT_AUTO_OFF_TICK_MINUTES,
    LIGHT_MODE_OFF_VALUE,
)

_LOGGER = logging.getLogger(__name__)


def _config_value(entry: ConfigEntry, key: str, default: Any) -> Any:
    src = entry.options if entry.options else entry.data
    val = src.get(key, default)
    if val is None or val == "":
        return default
    return val


def _label_to_hours(label: str) -> float | None:
    """`auto_off_4h` → 4. `auto_off_5m` → 5/60 (Test). Other labels → None."""
    if not label.startswith(LIGHT_AUTO_OFF_LABEL_PREFIX):
        return None
    suffix = label[len(LIGHT_AUTO_OFF_LABEL_PREFIX):]
    if suffix.endswith("h"):
        try:
            h = int(suffix[:-1])
        except ValueError:
            return None
        if h not in LIGHT_AUTO_OFF_HOURS:
            return None
        return float(h)
    if suffix.endswith("m"):
        try:
            m = int(suffix[:-1])
        except ValueError:
            return None
        if m not in LIGHT_AUTO_OFF_TEST_MINUTES:
            return None
        return m / 60.0
    return None


def _entity_threshold_hours(registry: er.EntityRegistry, entity_id: str) -> int | None:
    """Read the highest matching auto_off_*h label from this entity. None = no label."""
    rec = registry.async_get(entity_id)
    if rec is None:
        return None
    best: int | None = None
    for label in rec.labels or set():
        h = _label_to_hours(label)
        if h is not None and (best is None or h > best):
            best = h
    return best


def _is_on(domain: str, state: str | None) -> bool:
    if state in (None, "unavailable", "unknown"):
        return False
    if domain == "input_select":
        return state != LIGHT_MODE_OFF_VALUE
    return state == "on"


def _gruppe_from_mode_helper(entity_id: str) -> str | None:
    """`input_select.licht_mode_ug_werkstatt` → `ug_werkstatt`."""
    prefix = "input_select.licht_mode_"
    if not entity_id.startswith(prefix):
        return None
    return entity_id[len(prefix):]


def _discover_entities(
    hass: HomeAssistant,
    pattern: str,
) -> list[str]:
    """All entity_ids matching the glob pattern OR carrying an auto_off_*h label."""
    registry = er.async_get(hass)
    found: set[str] = set()
    pattern = pattern or DEFAULT_LIGHT_HELPER_PATTERN

    for rec in registry.entities.values():
        # 1) Glob match on entity_id
        if fnmatch.fnmatchcase(rec.entity_id, pattern):
            found.add(rec.entity_id)
            continue
        # 2) Label-based override (any domain)
        for label in rec.labels or set():
            if _label_to_hours(label) is not None:
                found.add(rec.entity_id)
                break
    return sorted(found)


def _build_actions(entity_id: str) -> list[dict[str, str]]:
    """Mobile-app actionable buttons for the Herold push."""
    return [
        {"action": f"LIGHT_AUTO_OFF:off:{entity_id}", "title": "Aus"},
        {"action": f"LIGHT_AUTO_OFF:1h:{entity_id}",  "title": "+1h"},
        {"action": f"LIGHT_AUTO_OFF:2h:{entity_id}",  "title": "+2h"},
        {"action": f"LIGHT_AUTO_OFF:4h:{entity_id}",  "title": "+4h"},
        {"action": f"LIGHT_AUTO_OFF:6h:{entity_id}",  "title": "+6h"},
    ]


class LightAutoOff:
    """Per-entity in-memory state plus the periodic tick.

    Persisted state is intentionally minimal: snooze_until_iso lives in
    coordinator.store under `light_auto_off_state` so it survives a restart.
    pending_push_ts is in-memory only (a missed timeout after restart is
    benign: next tick triggers a fresh push if the light is still on).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._unsub: CALLBACK_TYPE | None = None
        # entity_id → datetime (in-memory)
        self._pending_push: dict[str, datetime] = {}

    # ---------- helpers ----------

    def _store_state(self) -> dict[str, Any]:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return {}
        if not hasattr(store, "light_auto_off_state") or store.light_auto_off_state is None:
            store.light_auto_off_state = {}
        return store.light_auto_off_state

    def _get_snooze_until(self, entity_id: str) -> datetime | None:
        s = self._store_state().get(entity_id)
        if not isinstance(s, dict):
            return None
        iso = s.get("snooze_until_iso")
        if not iso:
            return None
        dt = dt_util.parse_datetime(iso)
        return dt_util.as_utc(dt) if dt is not None else None

    def _set_snooze_until(self, entity_id: str, until: datetime | None) -> None:
        state = self._store_state()
        rec = dict(state.get(entity_id) or {})
        if until is None:
            rec.pop("snooze_until_iso", None)
        else:
            rec["snooze_until_iso"] = dt_util.as_utc(until).isoformat()
        if rec:
            state[entity_id] = rec
        else:
            state.pop(entity_id, None)
        store = getattr(self.coordinator, "store", None)
        if store is not None:
            store.dirty = True
            self.hass.async_create_task(store.async_save())

    def _pattern(self) -> str:
        return str(_config_value(
            self.entry, CONF_LIGHT_HELPER_PATTERN, DEFAULT_LIGHT_HELPER_PATTERN,
        ))

    def _timeout_minutes(self) -> int:
        return int(_config_value(
            self.entry, CONF_LIGHT_AUTO_OFF_TIMEOUT_MINUTES, DEFAULT_LIGHT_AUTO_OFF_TIMEOUT_MINUTES,
        ))

    # ---------- core logic ----------

    def _on_duration_seconds(self, entity_id: str) -> float | None:
        """Returns seconds the entity has been "on" continuously, or None if not on."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        domain = entity_id.split(".", 1)[0]
        if not _is_on(domain, state.state):
            return None
        last_changed = state.last_changed
        if last_changed is None:
            return 0.0
        return (dt_util.utcnow() - dt_util.as_utc(last_changed)).total_seconds()

    async def _send_push(self, entity_id: str, threshold_h: float) -> None:
        state = self.hass.states.get(entity_id)
        friendly = (state.attributes.get("friendly_name") if state else None) or entity_id
        if threshold_h < 1:
            label = f"{int(round(threshold_h * 60))} min"
        else:
            label = f"{int(threshold_h)} h"
        message = f"{friendly} brennt seit {label}. Aus oder snoozen?"
        actions = _build_actions(entity_id)
        await self.hass.services.async_call(
            _herold.HEROLD_DOMAIN,
            "senden",
            {
                "topic": "tariff_saver/light_auto_off",
                "titel": "Licht brennt noch 💡",
                "message": message,
                "severity": "info",
                "actions": actions,
                "payload": {"data": {"tag": f"auto_off_{entity_id}"}},
            },
            blocking=False,
        )
        self._pending_push[entity_id] = dt_util.utcnow()
        store = getattr(self.coordinator, "store", None)
        if store is not None:
            store.log_activity("💡", f"Licht-Wächter Push: {friendly} ({label})")

    async def _send_confirm(self, entity_id: str) -> None:
        state = self.hass.states.get(entity_id)
        friendly = (state.attributes.get("friendly_name") if state else None) or entity_id
        await self.hass.services.async_call(
            _herold.HEROLD_DOMAIN,
            "senden",
            {
                "topic": "tariff_saver/light_auto_off",
                "titel": "Licht-Wächter",
                "message": f"{friendly} nach Timeout automatisch ausgeschaltet.",
                "severity": "info",
            },
            blocking=False,
        )

    async def _turn_off_entity(self, entity_id: str) -> None:
        """Use master_dimmer_skript when a mode-helper, else generic homeassistant.turn_off."""
        gruppe = _gruppe_from_mode_helper(entity_id)
        if gruppe is not None:
            await self.hass.services.async_call(
                "script", "master_dimmer_skript",
                {"licht_gruppe": gruppe, "normal_hell": LIGHT_MODE_OFF_VALUE},
                blocking=False,
            )
            return
        await self.hass.services.async_call(
            "homeassistant", "turn_off",
            {"entity_id": entity_id},
            blocking=False,
        )

    async def _evaluate(self) -> None:
        registry = er.async_get(self.hass)
        pattern = self._pattern()
        entities = _discover_entities(self.hass, pattern)
        timeout_s = self._timeout_minutes() * 60.0
        now_utc = dt_util.utcnow()

        for entity_id in entities:
            threshold_h = _entity_threshold_hours(registry, entity_id)
            if threshold_h is None:
                # In overview only — not monitored.
                self._pending_push.pop(entity_id, None)
                continue

            on_secs = self._on_duration_seconds(entity_id)
            if on_secs is None:
                # Light is off → reset all per-entity state.
                # Snooze gilt nur für den aktuellen Brennzyklus; bei aus → clearen,
                # damit ein nächster An-Zyklus keinen alten Snooze erbt.
                self._pending_push.pop(entity_id, None)
                if self._get_snooze_until(entity_id) is not None:
                    self._set_snooze_until(entity_id, None)
                continue

            # Snooze active?
            snooze_until = self._get_snooze_until(entity_id)
            if snooze_until is not None and snooze_until > now_utc:
                continue
            elif snooze_until is not None:
                # Snooze expired — clear it
                self._set_snooze_until(entity_id, None)

            threshold_s = threshold_h * 3600.0

            # Already pushed → check timeout
            pending_ts = self._pending_push.get(entity_id)
            if pending_ts is not None:
                if (now_utc - pending_ts).total_seconds() >= timeout_s:
                    _LOGGER.info("light_auto_off: timeout for %s, turning off", entity_id)
                    self._pending_push.pop(entity_id, None)
                    await self._turn_off_entity(entity_id)
                    await self._send_confirm(entity_id)
                continue

            # Threshold reached → push
            if on_secs >= threshold_s:
                _LOGGER.info("light_auto_off: %s on for %.0fs (≥%dh), pushing", entity_id, on_secs, threshold_h)
                await self._send_push(entity_id, threshold_h)

    async def handle_action(self, entity_id: str, action: str) -> None:
        """Service callback: action ∈ {off, 1h, 2h, 4h, 6h}."""
        action = (action or "").strip().lower()
        if not entity_id:
            return
        # Either way the pending push is consumed.
        self._pending_push.pop(entity_id, None)

        if action == "off":
            await self._turn_off_entity(entity_id)
            store = getattr(self.coordinator, "store", None)
            if store is not None:
                state = self.hass.states.get(entity_id)
                friendly = (state.attributes.get("friendly_name") if state else None) or entity_id
                store.log_activity("💡", f"Licht-Wächter: {friendly} aus per Push")
            return

        snooze_map = {"1h": 1, "2h": 2, "4h": 4, "6h": 6}
        hours = snooze_map.get(action)
        if hours is None:
            _LOGGER.warning("light_auto_off: unknown action %r for %s", action, entity_id)
            return
        until = dt_util.utcnow() + timedelta(hours=hours)
        self._set_snooze_until(entity_id, until)
        store = getattr(self.coordinator, "store", None)
        if store is not None:
            state = self.hass.states.get(entity_id)
            friendly = (state.attributes.get("friendly_name") if state else None) or entity_id
            store.log_activity("⏸️", f"Licht-Wächter: {friendly} +{hours}h snoozed")

    # ---------- mobile_app event listener (für die Buttons) ----------

    @callback
    def _on_mobile_action(self, event) -> None:
        action_str = str(event.data.get("action") or "")
        if not action_str.startswith("LIGHT_AUTO_OFF:"):
            return
        try:
            _, action, entity_id = action_str.split(":", 2)
        except ValueError:
            return
        self.hass.async_create_task(self.handle_action(entity_id, action))

    @callback
    def _on_registry_update(self, event) -> None:
        """Refresh coordinator when an auto_off_*h label is added/removed."""
        action = event.data.get("action")
        if action not in ("update", "create", "remove"):
            return
        changes = event.data.get("changes") or {}
        # Only react to label changes (or any action that may affect monitoring)
        if action == "update" and "labels" not in changes:
            return
        # Trigger sensor refresh so the card sees the new threshold immediately
        try:
            self.coordinator.async_set_updated_data(self.coordinator.data)
        except Exception as _err:
            _LOGGER.debug("light_auto_off: %s in registry refresh: %s", type(_err).__name__, _err)
        # Re-evaluate immediately so a removed label clears any pending push
        # before its 10-min timeout could trigger a stale auto-off.
        self.hass.async_create_task(self._evaluate())

    def start(self) -> None:
        @callback
        def _tick(_now) -> None:
            self.hass.async_create_task(self._evaluate())

        self._unsub = async_track_time_interval(
            self.hass, _tick, timedelta(minutes=LIGHT_AUTO_OFF_TICK_MINUTES),
        )
        self._unsub_event = self.hass.bus.async_listen(
            "mobile_app_notification_action", self._on_mobile_action,
        )
        self._unsub_registry = self.hass.bus.async_listen(
            "entity_registry_updated", self._on_registry_update,
        )
        # Run once at startup
        self.hass.async_create_task(self._evaluate())

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if getattr(self, "_unsub_event", None):
            self._unsub_event()
            self._unsub_event = None
        if getattr(self, "_unsub_registry", None):
            self._unsub_registry()
            self._unsub_registry = None


def async_setup_auto_off(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> CALLBACK_TYPE:
    """Initialize the Light Auto-Off subsystem. Returns unsub callable."""
    manager = LightAutoOff(hass, entry, coordinator)
    manager.start()
    # Stash on coordinator for service handler access
    coordinator.light_auto_off = manager
    return manager.stop
