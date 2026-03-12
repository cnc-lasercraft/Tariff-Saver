"""Tariff Saver integration."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_PUBLISH_TIME,
    DEFAULT_PUBLISH_TIME,
    DOMAIN,
)
from .coordinator import TariffSaverCoordinator

PLATFORMS: list[str] = ["sensor"]
RETRY_INTERVAL = timedelta(minutes=30)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up Tariff Saver from yaml (unused)."""
    hass.data.setdefault(DOMAIN, {})
    return True


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


def _has_valid_prices(coordinator: TariffSaverCoordinator) -> bool:
    data = coordinator.data or {}
    active = data.get("active") if isinstance(data, dict) else None
    return isinstance(active, list) and len(active) > 0


def _next_local_midnight(now_local: datetime) -> datetime:
    tomorrow = (now_local + timedelta(days=1)).date()
    return dt_util.as_local(
        dt_util.as_utc(datetime.combine(tomorrow, time(0, 0), tzinfo=now_local.tzinfo))
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tariff Saver from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    config = dict(entry.data)
    config.update(dict(entry.options))

    coordinator = TariffSaverCoordinator(hass, config=config)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    publish_time = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
    hour, minute = _parse_hhmm(publish_time)

    retry_state_key = f"{entry.entry_id}_retry_until"
    hass.data[DOMAIN][retry_state_key] = None

    async def _reset_energy_baseline_service(call: ServiceCall) -> None:
        clear_booked = bool(call.data.get("clear_booked", True))
        target_entry_id = call.data.get("entry_id")

        for eid, coord in list(hass.data.get(DOMAIN, {}).items()):
            if not isinstance(eid, str) or not hasattr(coord, "store"):
                continue
            if target_entry_id and eid != target_entry_id:
                continue

            store = getattr(coord, "store", None)
            if store is None:
                continue

            target_entry = hass.config_entries.async_get_entry(eid)
            if target_entry is None:
                continue

            energy_entity = (
                target_entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY)
                or target_entry.data.get(CONF_CONSUMPTION_ENERGY_ENTITY)
            )
            if not isinstance(energy_entity, str) or not energy_entity:
                continue

            state = hass.states.get(energy_entity)
            if state is None:
                continue

            try:
                current_kwh = float(state.state)
            except Exception:
                continue

            now_utc = dt_util.utcnow()
            store.reset_energy_baseline(now_utc, current_kwh, clear_booked=clear_booked)
            await store.async_save()
            try:
                await coord.async_request_refresh()
            except Exception:
                pass

    service_name = "reset_energy_baseline"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _reset_energy_baseline_service)

    async def _force_refresh() -> None:
        coordinator._last_fetch_date = None
        await coordinator.async_request_refresh()

    async def _daily_refresh(now) -> None:  # noqa: ANN001
        await _force_refresh()
        if not _has_valid_prices(coordinator):
            hass.data[DOMAIN][retry_state_key] = _next_local_midnight(dt_util.now())
        else:
            hass.data[DOMAIN][retry_state_key] = None

    async def _retry_tick(now) -> None:  # noqa: ANN001
        until = hass.data[DOMAIN].get(retry_state_key)
        if not isinstance(until, datetime):
            return
        now_local = dt_util.now()
        if now_local >= until:
            hass.data[DOMAIN][retry_state_key] = None
            return
        if not _has_valid_prices(coordinator):
            await _force_refresh()
        else:
            hass.data[DOMAIN][retry_state_key] = None

    hass.data[DOMAIN][entry.entry_id + "_unsub_daily"] = async_track_time_change(
        hass, _daily_refresh, hour=hour, minute=minute, second=0
    )
    hass.data[DOMAIN][entry.entry_id + "_unsub_retry"] = async_track_time_interval(
        hass, _retry_tick, RETRY_INTERVAL
    )

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    for suffix in ("_unsub_daily", "_unsub_retry", "_unsub_energy_cost", "_unsub_energy_tick", "_unsub_consumer_tick"):
        unsub = hass.data.get(DOMAIN, {}).pop(entry.entry_id + suffix, None)
        if unsub:
            unsub()

    hass.data.get(DOMAIN, {}).pop(f"{entry.entry_id}_retry_until", None)
    if unload_ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        remaining_entries = [
            k
            for k, v in hass.data[DOMAIN].items()
            if isinstance(k, str) and isinstance(v, TariffSaverCoordinator)
        ]
        if not remaining_entries and hass.services.has_service(DOMAIN, "reset_energy_baseline"):
            hass.services.async_remove(DOMAIN, "reset_energy_baseline")
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
