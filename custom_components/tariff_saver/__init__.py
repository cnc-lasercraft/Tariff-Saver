"""Tariff Saver integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from .api import EkzTariffApi
from .const import DOMAIN, CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME
from .coordinator import TariffSaverCoordinator

PLATFORMS: list[str] = ["sensor"]


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' -> (hour, minute). Fallback to default on bad input."""
    try:
        hh, mm = value.strip().split(":")
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    # fallback
    return 18, 15


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tariff Saver from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    api = EkzTariffApi(session)

    coordinator = TariffSaverCoordinator(hass, api, config=dict(entry.data))

    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # --- Daily schedule: refresh only once per day at publish_time ---
    publish_time = entry.data.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
    hour, minute = _parse_hhmm(publish_time)

    async def _daily_refresh(now) -> None:  # noqa: ANN001
        # Prevent double runs in the same day
        await coordinator.async_request_refresh()

    # Keep unsubscribe so unload works
    unsub = async_track_time_change(hass, _daily_refresh, hour=hour, minute=minute, second=0)
    hass.data[DOMAIN][entry.entry_id + "_unsub_daily"] = unsub

    # Optional: first refresh immediately (so entities are not empty after setup)
    # If you want strict "only daily", comment the next two lines out.
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Tariff Saver config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Unsubscribe daily callback
    unsub = hass.data.get(DOMAIN, {}).pop(entry.entry_id + "_unsub_daily", None)
    if unsub:
        unsub()

    if unload_ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
