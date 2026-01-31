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

# Sampling / persistence tuning for fast-updating energy sensors
SAMPLE_MIN_INTERVAL = timedelta(seconds=10)   # only keep one sample every 10s
FLUSH_INTERVAL = timedelta(seconds=30)        # persist at most every 30s (if dirty)
KEEP_SAMPLES_HOURS = 48

# Slot finalize schedule
SLOT_MINUTES = [0, 15, 30, 45]
SLOT_FINALIZE_SECOND = 5  # a small buffer after the quarter boundary


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' -> (hour, minute). Fallback to DEFAULT_PUBLISH_TIME on bad input."""
    try:
        hh, mm = value.strip().split(":")
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass

    try:
        hh, mm = DEFAULT_PUBLISH_TIME.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return 18, 15


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up domain (YAML not used)."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to the latest version."""
    _LOGGER.debug(
        "Migrating %s entry %s from version %s.%s",
        DOMAIN,
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )

    data = dict(entry.data)
    options = dict(entry.options)

    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
            version=CONFIG_VERSION,
            minor_version=CONFIG_MINOR_VERSION,
        )
        _LOGGER.info("Migrated %s entry %s to version %s.%s", DOMAIN, entry.entry_id, CONFIG_VERSION, CONFIG_MINOR_VERSION)
        return True

    if entry.version == CONFIG_VERSION:
        return True

    if entry.version > CONFIG_VERSION:
        _LOGGER.error(
            "Cannot migrate %s entry %s from future version %s (supports up to %s)",
            DOMAIN,
            entry.entry_id,
            entry.version,
            CONFIG_VERSION,
        )
        return False

    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
    )
    _LOGGER.warning(
        "Unexpected old %s entry version %s for %s; force-bumped to %s.%s",
        DOMAIN,
        entry.version,
        entry.entry_id,
        CONFIG_VERSION,
        CONFIG_MINOR_VERSION,
    )
    return True


def _get_energy_entity_id(entry: ConfigEntry) -> str | None:
    """Where the user selected their energy entity. Adjust if you store it differently."""
    # Your OptionsFlow uses: consumption_energy_entity
    return entry.options.get("consumption_energy_entity") or entry.data.get("consumption_energy_entity")


def _round_down_to_quarter(dt_local):
    minute = (dt_local.minute // 15) * 15
    return dt_local.replace(minute=minute, second=0, microsecond=0)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tariff Saver from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # --- API + price coordinator (your existing logic) ---
    session = async_get_clientsession(hass)
    api = EkzTariffApi(session)
    coordinator = TariffSaverCoordinator(hass, api, config=dict(entry.data))
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # --- Storage (samples + booked slots) ---
    store = TariffSaverStore(hass, entry.entry_id)
    await store.async_load()
    store.trim_samples(KEEP_SAMPLES_HOURS)
    hass.data[DOMAIN][f"{entry.entry_id}_store"] = store

    # --- Daily price refresh only once per day at publish_time ---
    publish_time = entry.data.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
    hour, minute = _parse_hhmm(publish_time)

    async def _daily_refresh(now) -> None:  # noqa: ANN001
        await coordinator.async_request_refresh()

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_daily"] = async_track_time_change(
        hass, _daily_refresh, hour=hour, minute=minute, second=0
    )

    # Initial refresh (so prices are available)
    await coordinator.async_config_entry_first_refresh()

    # --- Energy sampling (fast sensor) ---
    energy_entity = _get_energy_entity_id(entry)
    if energy_entity:
        _LOGGER.info("Tariff Saver using energy entity: %s", energy_entity)
    else:
        _LOGGER.warning("Tariff Saver: no consumption_energy_entity selected; actual cost/savings will stay empty")

    # Keep last sample time for throttling in RAM
    last_saved_ts_utc = store.last_sample_ts

    @callback
    def _energy_event(event) -> None:
        nonlocal last_saved_ts_utc
        new_state = event.data.get("new_state")
        if not new_state or new_state.state in (None, "unknown", "unavailable"):
            return

        try:
            kwh_total = float(new_state.state)
        except ValueError:
            return

        now_utc = dt_util.utcnow()
        if last_saved_ts_utc and (now_utc - last_saved_ts_utc) < SAMPLE_MIN_INTERVAL:
            return

        did_add = store.add_sample(now_utc, kwh_total, min_interval_s=int(SAMPLE_MIN_INTERVAL.total_seconds()))
        if did_add:
            last_saved_ts_utc = store.last_sample_ts
            store.trim_samples(KEEP_SAMPLES_HOURS)

    unsub_energy = None
    if energy_entity:
        unsub_energy = async_track_state_change_event(hass, [energy_entity], _energy_event)
        hass.data[DOMAIN][f"{entry.entry_id}_unsub_energy"] = unsub_energy

    # --- Periodic flush to disk (only if dirty) ---
    async def _flush_store(now) -> None:  # noqa: ANN001
        if store.dirty:
            await store.async_save()

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_flush"] = async_track_time_interval(
        hass, _flush_store, FLUSH_INTERVAL
    )

    # --- Slot finalization every quarter hour ---
    async def _finalize_last_full_slot() -> None:
        if not energy_entity:
            return

        now_local = dt_util.now()
        current_q = _round_down_to_quarter(now_local)

        # finalize the slot that ended at the previous quarter boundary
        slot_end = current_q
        # because we run at second=5, slot_end is the current boundary,
        # so the last fully completed slot ends at slot_end
        slot_start = slot_end - timedelta(minutes=15)

        if store.is_slot_booked(slot_end):
            return

        delta = store.delta_kwh(slot_start, slot_end)
        if delta is None:
            store.book_slot_status(slot_end, "missing_samples")
            return

        # reset / rollover guard
        if delta < 0:
            store.book_slot_status(slot_end, "invalid")
            return

        # ---- PRICE LOOKUP (YOU MAY NEED TO ADAPT THIS BLOCK ONCE) ----
        # We need CHF/kWh for the slot. This must come from your coordinator data.
        #
        # Implement these two helper calls to match your coordinator structure.
        dyn_price = _get_slot_price_chf_per_kwh(coordinator, slot_start, slot_end, baseline=False)
        base_price = _get_slot_price_chf_per_kwh(coordinator, slot_start, slot_end, baseline=True)

        # 0/None -> unpriced/invalid placeholder
        if dyn_price is None or dyn_price <= 0:
            store.book_slot_status(slot_end, "unpriced")
            return

        # baseline might be None if not configured; treat as 0
        if base_price is None or base_price <= 0:
            base_price = 0.0

        dyn_chf = delta * dyn_price
        base_chf = delta * base_price

        store.book_slot_ok(slot_end, delta, dyn_chf, base_chf)

        # persist immediately after booking (important)
        await store.async_save()

    @callback
    def _slot_tick(now) -> None:  # noqa: ANN001
        hass.async_create_task(_finalize_last_full_slot())

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_slot"] = async_track_time_change(
        hass, _slot_tick, minute=SLOT_MINUTES, second=SLOT_FINALIZE_SECOND
    )

    # Forward platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _get_slot_price_chf_per_kwh(
    coordinator: TariffSaverCoordinator,
    slot_start_local,
    slot_end_local,
    baseline: bool,
) -> float | None:
    """
    Return CHF/kWh for a given slot.

    IMPORTANT: This is the only place you may need to adapt, depending on how
    your coordinator stores the slot prices.

    Current assumption (common pattern):
    - coordinator.data contains a list of slots under "slots"
    - each slot has: start (iso str), end (iso str), price_chf_per_kwh
    - baseline prices either in same structure or a different key.
    """
    data = getattr(coordinator, "data", None) or {}
    slots = data.get("slots") or data.get("price_slots") or []
    # You may also have baseline in data.get("baseline_slots") etc.
    if baseline:
        slots = data.get("baseline_slots") or slots

    # Normalize target boundaries to iso for comparison
    targ
