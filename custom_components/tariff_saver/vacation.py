"""Ferienmodus — zentraler Aktiv-Check.

Der Ferienmodus ist ein Overlay: solange die konfigurierte Entity
(`vacation_entity`, z.B. input_select.system_status) exakt den konfigurierten
State (`vacation_state`, z.B. "Längere Abwesenheit") hat, gelten markierte
Consumer und die PV-Dump-Loads als pausiert. Es wird nichts persistiert und
kein Flag umgeschaltet — der Zustand wird bei jedem Check live aus der Entity
gelesen und ist damit restart-sicher.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_VACATION_ENTITY,
    CONF_VACATION_STATE,
    DEFAULT_VACATION_STATE,
)


def is_vacation_active(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True wenn die Ferien-Entity konfiguriert ist und den Ferien-State hat."""
    src = entry.options if entry.options else entry.data
    entity_id = str(src.get(CONF_VACATION_ENTITY, "") or "").strip()
    if not entity_id:
        return False
    st = hass.states.get(entity_id)
    if st is None or st.state in ("unavailable", "unknown", ""):
        return False
    expected = str(src.get(CONF_VACATION_STATE, DEFAULT_VACATION_STATE) or DEFAULT_VACATION_STATE).strip()
    return st.state == expected
