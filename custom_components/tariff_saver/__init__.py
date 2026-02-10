"""Tariff Saver integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers import config_entry_oauth2_flow

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
    return 18, 15


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tariff Saver from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # IMPORTANT: merge data + options into a single config dict
    config = dict(entry.data)
    config.update(dict(entry.options))

    session = async_get_clientsession(hass)

    # ------------------------------------------------------------------
    # OAuth2Session (myEKZ)
    # ------------------------------------------------------------------
    oauth_session = None
    if config.get("mode") == "myekz":
        # This requires:
        # - manifest.json: "oauth2": true, "application_credentials": true
        # - oauth2.py + application_credentials.py
        oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry)

    api = EkzTariffApi(session, oauth_session=oauth_session)

    coordinator = TariffSaverCoordinator(hass, api, config=config)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Schedule: refresh only once per day at publish_time (options override data)
    publish_time = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
    hour, minute = _parse_hhmm(publish_time)

    async def _daily_refresh(now) -> None:  # noqa: ANN001
        await coordinator.async_request_refresh()

    unsub = async_track_time_change(hass, _daily_refresh, hour=hour, minute=minute, second=0)
    hass.data[DOMAIN][entry.entry_id + "_unsub_daily"] = unsub

    # First refresh immediately so entities are populated
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Tariff Saver config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    unsub = hass.data.get(DOMAIN, {}).pop(entry.entry_id + "_unsub_daily", None)
    if unsub:
        unsub()

    if unload_ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
