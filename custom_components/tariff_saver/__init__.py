"""Tariff Saver integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi
from .const import DOMAIN, CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME
from .coordinator import TariffSaverCoordinator
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

CONFIG_VERSION = 2
CONFIG_MINOR_VERSION = 0

# ---- tuning for fast-updating energy sensors ----
SAMPLE_MIN_INTERVAL = timedelta(seconds=10)   # store at most every 10s
FLUSH_INTERVAL = timedelta(seconds=30)        # persist to disk at most every 30s
KEEP_SAMPLES_HOURS = 48

# ---- slot scheduling ----
SLOT_MINUTES = [0, 15, 30, 45]
SLOT_FINALIZE_SECOND = 5


# ------------------------------------------------
# helpers
# ------------------------------------------------
def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hh, mm = value.strip().split(":")
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass

    hh, mm = DEFAULT_PUBLISH_TIME.split(":")
    return int(hh), int(mm)


def _round_down_to_quarter(dt_local):
    minute = (dt_local.minute // 15) * 15
    return dt_local.replace(minute=minute, second=0, microsecond=0)


def _get_energy_entity_id(entry: ConfigEntry) -> str | None:
    # stored in options (preferred) or data
    return entry.options.get("consumption_energy_entity") or entry.data.get(
        "consumption_energy_entity"
    )


def _get_slot_price_chf_per_kwh(
    coordinator: TariffSaverCoordinator,
    slot_start_local,
    baseline: bool,
) -> float | None:
    """
    Price lookup for a 15-min slot.

    Coordinator stores:
      data["active"]   -> list[PriceSlot(start=UTC, price_chf_per_kwh)]
      data["baseline"] -> list[PriceSlot(...)]
    """
    data = coordinator.data or {}
    slots = data.get("baseline" if baseline else "active") or []

    slot_start_utc = dt_util.as_utc(slot_start_local)

    for s in slots:
        if s.start == slot_start_utc:
            return float(s.price_chf_per_kwh)

    return None


# ------------------------------------------------
# HA setup
# ------------------------------------------------
async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """YAML not used."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version == CONFIG_VERSION:
        return True

    hass.config_entries.async_update_entry(
        entry,
        data=dict(entry.data),
        options=dict(entry.options),
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tariff Saver."""
    hass.data.setdefault(DOMAIN, {})

    # --- coordinator (prices) ---
    session = async_get_clientsession(hass)
    api = EkzTariffApi(session)
    coordinator = TariffSaverCoordinator(hass, api, dict(entry.data))
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await coordinator.async_config_entry_first_refresh()

    # --- storage ---
    store = TariffSaverStore(hass, entry.entry_id)
    await store.async_load()
    store.trim_samples(KEEP_SAMPLES_HOURS)
    hass.data[DOMAIN][f"{entry.entry_id}_store"] = store

    # --- daily price refresh ---
    publish_time = entry.data.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
    hour, minute = _parse_hhmm(publish_time)

    async def _daily_refresh(now) -> None:  # noqa: ANN001
        await coordinator.async_request_refresh()

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_daily"] = async_track_time_change(
        hass, _daily_refresh, hour=hour, minute=minute, second=0
    )

    # ------------------------------------------------
    # energy sampling (fast sensor)
    # ------------------------------------------------
    energy_entity = _get_energy_entity_id(entry)
    if not energy_entity:
        _LOGGER.warning(
            "Tariff Saver: no consumption_energy_entity configured – actual costs disabled"
        )

    last_saved_ts = store.last_sample_ts

    @callback
    def _energy_event(event) -> None:
        nonlocal last_saved_ts

        new_state = event.data.get("new_state")
        if not new_state or new_state.state in ("unknown", "unavailable"):
            return

        try:
            kwh_total = float(new_state.state)
        except ValueError:
            return

        now_utc = dt_util.utcnow()
        if last_saved_ts and (now_utc - last_saved_ts) < SAMPLE_MIN_INTERVAL:
            return

        if store.add_sample(
            now_utc,
            kwh_total,
            min_interval_s=int(SAMPLE_MIN_INTERVAL.total_seconds()),
        ):
            last_saved_ts = store.last_sample_ts
            store.trim_samples(KEEP_SAMPLES_HOURS)

    if energy_entity:
        hass.data[DOMAIN][f"{entry.entry_id}_unsub_energy"] = (
            async_track_state_change_event(
                hass, [energy_entity], _energy_event
            )
        )

    # --- periodic flush ---
    async def _flush_store(now) -> None:  # noqa: ANN001
        if store.dirty:
            await store.async_save()

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_flush"] = async_track_time_interval(
        hass, _flush_store, FLUSH_INTERVAL
    )

    # ------------------------------------------------
    # slot finalization (15-min)
    # ------------------------------------------------
    async def _finalize_last_full_slot() -> None:
        if not energy_entity:
            return

        now_local = dt_util.now()
        slot_end = _round_down_to_quarter(now_local)
        slot_start = slot_end - timedelta(minutes=15)

        if store.is_slot_booked(slot_end):
            return

        delta_kwh = store.delta_kwh(slot_start, slot_end)
        if delta_kwh is None:
            store.book_slot_status(slot_end, "missing_samples")
            return

        if delta_kwh < 0:
            store.book_slot_status(slot_end, "invalid")
            return

        dyn_price = _get_slot_price_chf_per_kwh(
            coordinator, slot_start, baseline=False
        )
        base_price = _get_slot_price_chf_per_kwh(
            coordinator, slot_start, baseline=True
        )

        if dyn_price is None or dyn_price <= 0:
            store.book_slot_status(slot_end, "unpriced")
            return

        if base_price is None or base_price <= 0:
            base_price = 0.0

        dyn_chf = delta_kwh * dyn_price
        base_chf = delta_kwh * base_price

        store.book_slot_ok(slot_end, delta_kwh, dyn_chf, base_chf)
        await store.async_save()

        _LOGGER.debug(
            "Booked slot %s | %.5f kWh | dyn %.5f CHF | base %.5f CHF",
            slot_end.isoformat(),
            delta_kwh,
            dyn_chf,
            base_chf,
        )

    @callback
    def _slot_tick(now) -> None:  # noqa: ANN001
        hass.async_create_task(_finalize_last_full_slot())

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_slot"] = async_track_time_change(
        hass,
        _slot_tick,
        minute=SLOT_MINUTES,
        second=SLOT_FINALIZE_SECOND,
    )

    # --- forward sensors ---
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    for suffix in (
        "_unsub_daily",
        "_unsub_energy",
        "_unsub_flush",
        "_unsub_slot",
    ):
        unsub = hass.data.get(DOMAIN, {}).pop(f"{entry.entry_id}{suffix}", None)
        if unsub:
            unsub()

    store: TariffSaverStore | None = hass.data.get(DOMAIN, {}).pop(
        f"{entry.entry_id}_store", None
    )
    if store and store.dirty:
        await store.async_save()

    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
