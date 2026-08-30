"""Herold integration helper — optional dependency.

Herold (custom_components.herold) ist ein Meldungs-Broker. Diese CC läuft auch
ohne Herold; wenn er vorhanden ist, laufen Benachrichtigungen über ihn statt
direkt via notify.*.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

HEROLD_DOMAIN = "herold"
QUELLE = "custom_components.tariff_saver"

# Topics pro Ereignis-Typ. default_rollen lassen wir leer — Zuordnung erfolgt
# im Herold-UI. default_severity ist nur Fallback, der Producer setzt sie i.d.R.
# explizit beim senden().
TOPICS: dict[str, dict[str, Any]] = {
    "tariff_saver/boiler/hygiene_ueberfaellig": {
        "name": "Boiler Hygiene überfällig",
        "beschreibung": "Consumer-10-Zyklus (Legionellen-Schutz) konnte nicht im Plan-Fenster laufen und ist überfällig.",
        "default_severity": "warnung",
    },
    "tariff_saver/boiler/uebertemperatur": {
        "name": "Boiler Übertemperatur",
        "beschreibung": "Boiler über Soll — Watchdog meldet hängende Hygiene-Freigabe oder Defekt.",
        "default_severity": "kritisch",
    },
    "tariff_saver/boiler/60_grad_nicht_erreicht": {
        "name": "Boiler 60°C nicht erreicht",
        "beschreibung": "Boiler-Hygiene konnte in 2h keine 60°C erreichen. Elektroeinsatz defekt?",
        "default_severity": "warnung",
    },
    "tariff_saver/ww/cleanup_triggered": {
        "name": "WW Cleanup getriggert",
        "beschreibung": "Cleanup-Automation musste Select zurücksetzen weil Main-Automation es versäumt hat.",
        "default_severity": "info",
    },
    "tariff_saver/consumer/run_aborted": {
        "name": "Consumer-Lauf abgebrochen",
        "beschreibung": "Ein Consumer-Lauf wurde vorzeitig abgebrochen (PV-Verlust, Cost-based-Abort).",
        "default_severity": "info",
    },
    "tariff_saver/flat_tarif_heute": {
        "name": "Flat-Tarif erkannt",
        "beschreibung": "EKZ lieferte einen Tag mit flachem Tarif (Spread < Schwelle). Grid-Consumer werden nach hinten verschoben.",
        "default_severity": "info",
    },
    "tariff_saver/strom_sparen/aktiv": {
        "name": "Strom sparen aktiv",
        "beschreibung": "Score über Schwellwert — nicht-essentielle Geräte wurden abgeschaltet.",
        "default_severity": "info",
    },
    "tariff_saver/watchdog": {
        "name": "Tariff Saver Watchdog",
        "beschreibung": "Coordinator-Ausfall erkannt — Integration wurde automatisch neu geladen.",
        "default_severity": "warnung",
    },
    "tariff_saver/light_auto_off": {
        "name": "Licht-Wächter",
        "beschreibung": "Ein Raumlicht brennt länger als erlaubt. Push mit Schnoozen oder Auto-Off nach Timeout.",
        "default_severity": "info",
    },
    "tariff_saver/plan_no_data": {
        "name": "Keine Tarif-Daten — Pläne invalidiert",
        "beschreibung": "EKZ hat keine validen Slots für den Plan-Zieltag geliefert. Alle Consumer-Pläne wurden auf 'error_no_data' gesetzt.",
        "default_severity": "warnung",
    },
    "tariff_saver/hygiene_overdue": {
        "name": "Hygiene-Consumer überfällig",
        "beschreibung": "Ein Daily-Hygiene-Consumer (z.B. Boiler) ist überfällig und es existiert kein gültiger Plan für heute/morgen.",
        "default_severity": "warnung",
    },
    "tariff_saver/standby_off": {
        "name": "Standby-Wächter",
        "beschreibung": "Ein Gerät war eingeschaltet, zog aber über Stunden nur Standby-Leistung — es wurde automatisch abgeschaltet.",
        "default_severity": "info",
    },
    "tariff_saver/ladeermahnung": {
        "name": "EV-Ladeermahnung",
        "beschreibung": "Fahrzeug (Zeekr/WN7) zu Hause, nicht eingesteckt, SOC unter Schwelle — und Anstecken lohnt (günstiges Nachtfenster oder PV-Überschuss morgen).",
        "default_severity": "info",
    },
}


def available(hass: HomeAssistant) -> bool:
    """True wenn Herold geladen ist."""
    return HEROLD_DOMAIN in hass.data


async def register_topics(hass: HomeAssistant) -> None:
    """Alle Topics einmalig beim Setup registrieren. No-op wenn Herold fehlt."""
    if not available(hass):
        return
    for topic_id, meta in TOPICS.items():
        try:
            await hass.services.async_call(
                HEROLD_DOMAIN,
                "topic_registrieren",
                {"topic": topic_id, "quelle": QUELLE, **meta},
                blocking=False,
            )
        except Exception as e:
            _LOGGER.warning("Herold topic_registrieren failed for %s: %s", topic_id, e)


async def senden(
    hass: HomeAssistant,
    *,
    topic: str,
    titel: str,
    message: str,
    severity: str = "info",
    actions: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Meldung an Herold senden. No-op wenn Herold fehlt (Aufrufer entscheidet Fallback)."""
    if not available(hass):
        _LOGGER.debug("Herold nicht verfügbar, skip senden: %s", topic)
        return
    data: dict[str, Any] = {
        "topic": topic,
        "titel": titel,
        "message": message,
        "severity": severity,
    }
    if actions:
        data["actions"] = actions
    if payload:
        data["payload"] = payload
    try:
        await hass.services.async_call(HEROLD_DOMAIN, "senden", data, blocking=True)
    except Exception as e:
        _LOGGER.warning("Herold senden failed for %s: %s", topic, e)
