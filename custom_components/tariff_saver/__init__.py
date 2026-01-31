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
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    api = EkzTariffApi(session)

    coordinator = TariffSaverCoordinator(
        hass, api, dict(entry.data), dict(entry.options)
    )
    hass.data[DOMAIN][entry.entry_id] = coordinator

    publish_time = entry.options.get(
        CONF_PUBLISH_TIME,
        entry.data.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
    )
    hour, minute = _parse_hhmm(publish_time)

    async def _daily_refresh(now):
        await coordinator.async_request_refresh()

    unsub = async_track_time_change(
        hass, _daily_refresh, hour=hour, minute=minute, second=0
    )
    hass.data[DOMAIN][entry.entry_id + "_unsub_daily"] = unsub

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    unsub = hass.data.get(DOMAIN, {}).pop(entry.entry_id + "_unsub_daily", None)
    if unsub:
        unsub()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
