"""Tariff Saver integration."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_SEASON_ENTITY,
    CONF_PUBLISH_TIME,
    DEFAULT_PUBLISH_TIME,
    DOMAIN,
)
from .coordinator import TariffSaverCoordinator

PLATFORMS: list[str] = ["sensor", "binary_sensor"]
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



def _get_season(hass, entry) -> str:
    """Get current season from configured entity or derive from date."""
    from .const import CONF_SEASON_ENTITY
    season_entity = str(
        entry.options.get(CONF_SEASON_ENTITY, entry.data.get(CONF_SEASON_ENTITY, "")) or ""
    ).strip()
    if season_entity:
        state = hass.states.get(season_entity)
        if state and state.state not in ("unavailable", "unknown", ""):
            return state.state
    # Fallback: derive from month
    month = dt_util.now().month
    if month in (12, 1, 2):
        return "Winter"
    elif month in (3, 4, 5):
        return "Frühling"
    elif month in (6, 7, 8):
        return "Sommer"
    else:
        return "Herbst"


def _get_consumption_entity(entry) -> str:
    """Get configured consumption energy entity."""
    return str(
        entry.options.get(CONF_CONSUMPTION_ENERGY_ENTITY, entry.data.get(CONF_CONSUMPTION_ENERGY_ENTITY, "")) or ""
    ).strip()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tariff Saver from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    config = dict(entry.data)
    config.update(dict(entry.options))

    coordinator = TariffSaverCoordinator(hass, config=config)
    coordinator.entry = entry
    hass.data[DOMAIN][entry.entry_id] = coordinator

    publish_time = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
    hour, minute = _parse_hhmm(publish_time)

    retry_state_key = f"{entry.entry_id}_retry_until"
    hass.data[DOMAIN][retry_state_key] = None

    # Listen for EKZ Tariff validated data via HA bus event
    async def _on_ekz_bus_event(event) -> None:
        """EKZ Tariff has validated new data — refresh and calculate plans for tomorrow."""
        _LOGGER.info("Received ekz_tariff_new_data event: %s", event.data)
        target_date = event.data.get("date", "")

        # 1. Refresh tariff data from EKZ provider
        coordinator._last_fetch_date = None
        try:
            await coordinator.async_refresh()
        except Exception:
            pass

        # 2. Verify date validity before planning (defensive — bus event should only fire on success)
        data = coordinator.data or {}
        date_validity = data.get("date_validity", {})
        validity_record = date_validity.get(target_date)
        if isinstance(validity_record, dict) and not validity_record.get("valid", True):
            _LOGGER.warning(
                "Received ekz_tariff_new_data for %s but date_validity says invalid: %s — skipping plan",
                target_date, validity_record.get("details"),
            )
            return

        # 3. Calculate consumer plans for tomorrow
        if not hasattr(coordinator, "entry") or coordinator.store is None:
            _LOGGER.warning("Cannot calculate plans — entry or store not ready")
            return

        all_active = data.get("active", [])

        # Filter slots to ONLY tomorrow's date
        tomorrow_date = dt_util.parse_date(target_date)
        if tomorrow_date is None:
            tomorrow_date = (dt_util.now() + timedelta(days=1)).date()
        active_30m = [s for s in all_active if dt_util.as_local(s.start).date() == tomorrow_date]
        _LOGGER.info("Plan calculation: %d total slots, %d for %s", len(all_active), len(active_30m), tomorrow_date)
        if not active_30m:
            _LOGGER.warning("No slots for tomorrow %s — skipping plan calculation", tomorrow_date)
            return

        from .consumer_helpers import get_consumer_config
        from .scheduler import optimize_consumer_plans
        tariff_date_fmt = target_date
        try:
            parsed_date = dt_util.parse_date(target_date)
            if parsed_date:
                tariff_date_fmt = parsed_date.strftime("%d.%m.%Y")
        except Exception:
            pass

        max_grid_kw = float(config.get("max_grid_power_kw", 25.0) or 25.0)
        try:
            plans = optimize_consumer_plans(
                hass=hass,
                entry=coordinator.entry,
                active_slots=active_30m,
                store=coordinator.store,
                max_grid_power_kw=max_grid_kw,
            )
            # Persist plans
            serializable = {}
            for slot, plan in plans.items():
                sp = dict(plan)
                for k in ("start", "end", "chosen_start", "chosen_end", "tariff_window_start", "tariff_window_end"):
                    v = sp.get(k)
                    if hasattr(v, "isoformat"):
                        sp[k] = v.isoformat()
                serializable[slot] = sp
            coordinator.store.consumer_plans = serializable
            coordinator.store.plans_tariff_date = target_date
            coordinator.store.dirty = True
            await coordinator.store.async_save()

            coordinator.store.log_activity("📅", f"Slots für {tariff_date_fmt} berechnet")

            for _slot, _plan in plans.items():
                _status = _plan.get("status", "")
                _source = _plan.get("source", "")
                _start = _plan.get("start") or _plan.get("chosen_start")
                _end = _plan.get("end") or _plan.get("chosen_end")
                try:
                    _cfg = get_consumer_config(coordinator.entry, int(_slot))
                    _name = _cfg.configured_name
                except Exception:
                    _name = f"Consumer {_slot}"
                if _status == "planned" and _start:
                    _time_str = ""
                    try:
                        _s = dt_util.parse_datetime(str(_start))
                        _e = dt_util.parse_datetime(str(_end)) if _end else None
                        if _s:
                            _time_str = dt_util.as_local(_s).strftime("%H:%M")
                            if _e:
                                _time_str += "-" + dt_util.as_local(_e).strftime("%H:%M")
                    except Exception:
                        pass
                    _price = _plan.get("avg_price_chf_per_kwh") or _plan.get("tariff_avg_price_chf_per_kwh")
                    _detail = f"{_source}"
                    if _price and isinstance(_price, (int, float)):
                        _detail += f" {round(_price * 100, 1)} Rp"
                    coordinator.store.log_activity("\U0001f4cb", f"{_name} geplant ({_detail} {_time_str})")

            # Update coordinator data with new plans
            updated_data = dict(coordinator.data) if coordinator.data else {}
            updated_data["consumer_plans"] = plans
            coordinator.async_set_updated_data(updated_data)
            _LOGGER.info("Consumer plans calculated for %s", target_date)

        except Exception as err:
            _LOGGER.error("Consumer plan calculation failed: %s", err)
            if coordinator.store is not None:
                coordinator.store.log_activity("💥", f"Plan-Berechnung fehlgeschlagen: {err}")

    hass.data[DOMAIN][f"{entry.entry_id}_unsub_ekz_signal"] = hass.bus.async_listen(
        "ekz_tariff_new_data", _on_ekz_bus_event
    )

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

    async def _mark_consumer_run_service(call: ServiceCall) -> None:
        target_entry_id = call.data.get("entry_id")
        slot = int(call.data.get("slot", 0) or 0)
        if slot <= 0:
            return

        for eid, coord in list(hass.data.get(DOMAIN, {}).items()):
            if not isinstance(eid, str) or not hasattr(coord, "store"):
                continue
            if target_entry_id and eid != target_entry_id:
                continue

            store = getattr(coord, "store", None)
            if store is None:
                continue

            if not hasattr(store, "set_consumer_last_run"):
                continue

            store.set_consumer_last_run(str(slot), dt_util.utcnow())

            energy_kwh = call.data.get("energy_kwh")
            source = call.data.get("source")
            actual_price = call.data.get("actual_price_chf_per_kwh")
            baseline_price = call.data.get("baseline_price_chf_per_kwh")
            if (
                energy_kwh is not None
                and source is not None
                and actual_price is not None
                and baseline_price is not None
                and hasattr(store, "record_consumer_efficiency")
            ):
                try:
                    store.record_consumer_efficiency(
                        str(slot),
                        energy_kwh=float(energy_kwh),
                        source=str(source),
                        actual_price_chf_per_kwh=float(actual_price),
                        baseline_price_chf_per_kwh=float(baseline_price),
                    )
                except Exception:
                    pass

            await store.async_save()
            try:
                await coord.async_request_refresh()
            except Exception:
                pass

    service_name = "reset_energy_baseline"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _reset_energy_baseline_service)

    service_name = "mark_consumer_run"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _mark_consumer_run_service)

    async def _update_consumer_service(call: ServiceCall) -> None:
        slot = int(call.data.get("slot", 0))
        key = str(call.data.get("key", ""))
        value = call.data.get("value")
        if slot < 1 or slot > 10 or not key:
            return

        # Type conversion
        if key in ("enabled", "pv_required", "tariff_only", "pv_opportunist", "learning_enabled", "min_runtime_minutes"):
            value = bool(value)
        elif key in ("power_kw", "energy_kwh"):
            value = float(value or 0)
        elif key in ("duration_minutes", "priority", "max_grid_score", "min_days", "max_days"):
            value = int(value or 0)
        else:
            value = str(value or "")

        new_options = dict(entry.options)
        consumers = dict(new_options.get("consumers", {}))
        consumer = dict(consumers.get(str(slot), {}))
        consumer[key] = value
        consumers[str(slot)] = consumer
        new_options["consumers"] = consumers

        hass.config_entries.async_update_entry(entry, options=new_options)
        # Fire state update for config sensor immediately, defer full recalculation
        coordinator.async_set_updated_data(coordinator.data)

    async def _update_setting_service(call: ServiceCall) -> None:
        key = str(call.data.get("key", ""))
        value = call.data.get("value")
        if not key or key == "consumers":
            return
        # Type conversion
        if key in ("battery_enabled", "heating_enabled"):
            value = bool(value)
        elif key in ("feed_in_fixed_price", "battery_capacity_kwh", "battery_min_soc_percent", "ampel_pv_threshold_kw", "heating_wp_power_kw", "heating_comfort_min", "heating_pv_max", "heating_max_score", "heating_max_score_absolute", "heating_frost_min", "heating_pv_wait_minutes", "heating_wp_min_runtime", "strom_sparen_score", "min_valid_price", "max_valid_price"):
            value = float(value or 0)
        else:
            value = str(value or "")
        new_options = dict(entry.options)
        new_options[key] = value
        hass.config_entries.async_update_entry(entry, options=new_options)
        coordinator.async_set_updated_data(coordinator.data)

    service_name = "update_setting"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _update_setting_service)

    service_name = "update_consumer"
    if not hass.services.has_service(DOMAIN, service_name):
        hass.services.async_register(DOMAIN, service_name, _update_consumer_service)

    async def _update_consumer_status_service(call: ServiceCall) -> None:
        """Set reported_status on a consumer plan (called from automations)."""
        slot = int(call.data.get("slot", 0))
        status = str(call.data.get("status", "")).strip()
        if slot < 1 or slot > 10 or not status:
            return
        ts = dt_util.utcnow().isoformat()
        # Update store copy
        store = getattr(coordinator, "store", None)
        if store is not None and store.consumer_plans:
            plan = store.consumer_plans.get(str(slot)) or store.consumer_plans.get(slot)
            if plan is not None:
                plan["reported_status"] = status
                plan["reported_status_at"] = ts
                store.dirty = True
                await store.async_save()
        # Update coordinator.data copy (separate dict from store)
        data = coordinator.data
        if isinstance(data, dict):
            data_plans = data.get("consumer_plans", {})
            data_plan = data_plans.get(slot) or data_plans.get(str(slot))
            if isinstance(data_plan, dict):
                data_plan["reported_status"] = status
                data_plan["reported_status_at"] = ts
        # Trigger sensor update
        coordinator.async_set_updated_data(coordinator.data)

    hass.services.async_register(DOMAIN, "update_consumer_status", _update_consumer_status_service)

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

    async def _daily_base_load_snapshot(now) -> None:
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        consumption_entity = _get_consumption_entity(entry)
        if not consumption_entity:
            return
        consumption_state = hass.states.get(consumption_entity)
        if consumption_state is None:
            return
        try:
            total_kwh = float(consumption_state.state)
        except (ValueError, TypeError):
            return
        season = _get_season(hass, entry)
        today_str = dt_util.now().date().isoformat()

        # Record final reading and compile 30-min profile
        now_local = dt_util.now()
        slot_minute = 0 if now_local.minute < 30 else 30
        slot_start = now_local.replace(minute=slot_minute, second=0, microsecond=0)
        slot_iso = slot_start.isoformat()
        store.record_consumption_slot(slot_iso, total_kwh)

        # Final consumer measurement readings
        from .consumer_helpers import get_consumer_config
        from .const import CONSUMER_COUNT
        for slot in range(1, CONSUMER_COUNT + 1):
            cfg = get_consumer_config(entry, slot)
            if not cfg.enabled or not cfg.measurement_entity:
                continue
            meas_state = hass.states.get(cfg.measurement_entity)
            if meas_state is None:
                continue
            try:
                meas_kwh = float(meas_state.state)
            except (ValueError, TypeError):
                continue
            store.record_consumer_slot(cfg.measurement_entity, slot_iso, meas_kwh)

        profile = store.compile_load_profile(today_str, season)
        store.clear_consumption_slot_buffer()

        # Calculate daily totals from the compiled profile (not from the raw sensor)
        if profile:
            daily_total_kwh = float(profile.get("total_kwh", 0.0))
            consumer_kwh = float(profile.get("consumer_kwh", 0.0))
            base_load_kwh = float(profile.get("base_kwh", 0.0))
        else:
            consumer_kwh = store.compute_consumer_kwh_today()
            daily_total_kwh = 0.0
            base_load_kwh = 0.0

        store.add_base_load_daily(today_str, daily_total_kwh, consumer_kwh, base_load_kwh, season)
        await store.async_save()

    async def _consumption_slot_tick(now) -> None:
        """Sample consumption + consumer measurement sensors every 30 minutes."""
        store = getattr(coordinator, "store", None)
        if store is None:
            return
        consumption_entity = _get_consumption_entity(entry)
        if not consumption_entity:
            return
        consumption_state = hass.states.get(consumption_entity)
        if consumption_state is None:
            return
        try:
            total_kwh = float(consumption_state.state)
        except (ValueError, TypeError):
            return
        now_local = dt_util.now()
        slot_minute = 0 if now_local.minute < 30 else 30
        slot_start = now_local.replace(minute=slot_minute, second=0, microsecond=0)
        slot_iso = slot_start.isoformat()
        store.record_consumption_slot(slot_iso, total_kwh)

        # Sample all consumer measurement entities
        from .consumer_helpers import get_consumer_config
        from .const import CONSUMER_COUNT
        for slot in range(1, CONSUMER_COUNT + 1):
            cfg = get_consumer_config(entry, slot)
            if not cfg.enabled or not cfg.measurement_entity:
                continue
            meas_state = hass.states.get(cfg.measurement_entity)
            if meas_state is None:
                continue
            try:
                meas_kwh = float(meas_state.state)
            except (ValueError, TypeError):
                continue
            store.record_consumer_slot(cfg.measurement_entity, slot_iso, meas_kwh)

        await store.async_save()

    # Create WP Sperre input_boolean if not exists
    if not hass.states.get("input_boolean.tariff_saver_wp_sperre"):
        try:
            await hass.services.async_call(
                "input_boolean", "create", {}, blocking=False
            )
        except Exception:
            pass
        # Use the helper API to create it
        from homeassistant.helpers import entity_registry as er
        from homeassistant.components.input_boolean import DOMAIN as IB_DOMAIN
        try:
            current = await hass.helpers.storage.Store(1, "input_boolean").async_load() or {}
            items = current.get("items") or []
            if not any(item.get("id") == "tariff_saver_wp_sperre" for item in items):
                items.append({
                    "id": "tariff_saver_wp_sperre",
                    "name": "Tariff Saver WP Sperre",
                    "icon": "mdi:hand-back-right",
                })
                current["items"] = items
                store = hass.helpers.storage.Store(1, "input_boolean")
                await store.async_save(current)
                _LOGGER.info("Created input_boolean.tariff_saver_wp_sperre")
        except Exception as ex:
            _LOGGER.debug("Could not create WP Sperre helper: %s", ex)

    hass.data[DOMAIN][entry.entry_id + "_unsub_base_load"] = async_track_time_change(
        hass, _daily_base_load_snapshot, hour=23, minute=55, second=0
    )
    hass.data[DOMAIN][entry.entry_id + "_unsub_consumption_tick"] = async_track_time_interval(
        hass, _consumption_slot_tick, timedelta(minutes=30)
    )

    async def _heating_tick(now) -> None:
        """Check heating state every 5 minutes."""
        from .heating import async_heating_tick
        try:
            await async_heating_tick(hass, entry, coordinator)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Heating tick failed")

    hass.data[DOMAIN][entry.entry_id + "_unsub_heating_tick"] = async_track_time_interval(
        hass, _heating_tick, timedelta(minutes=5)
    )

    async def _battery_tick(now) -> None:
        """Check battery mode every 5 minutes."""
        from .battery import async_battery_tick
        try:
            await async_battery_tick(hass, entry, coordinator)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Battery tick failed")

    hass.data[DOMAIN][entry.entry_id + "_unsub_battery_tick"] = async_track_time_interval(
        hass, _battery_tick, timedelta(minutes=5)
    )

    # Midnight refresh removed — plans are persisted and only recomputed
    # when new tariff data is published (18:15).

    async def handle_clear_activity_log(call) -> None:
        store = getattr(coordinator, "store", None)
        if store:
            store.activity_log = []
            store.dirty = True
            await store.async_save()
            # Force sensor update by firing event
            hass.bus.async_fire(f"{DOMAIN}_activity_log_updated")

    async def handle_delete_activity_log_entry(call) -> None:
        store = getattr(coordinator, "store", None)
        if store:
            index = int(call.data.get("index", -1))
            if 0 <= index < len(store.activity_log):
                store.activity_log.pop(index)
                store.dirty = True
                await store.async_save()
                # Force sensor update by firing event
                hass.bus.async_fire(f"{DOMAIN}_activity_log_updated")

    async def handle_add_activity_log_entry(call) -> None:
        store = getattr(coordinator, "store", None)
        if store:
            icon = str(call.data.get("icon", "ℹ️"))
            msg = str(call.data.get("msg", "Test"))
            store.log_activity(icon, msg)
            await store.async_save()
            hass.bus.async_fire(f"{DOMAIN}_activity_log_updated")

    hass.services.async_register(DOMAIN, "add_activity_log_entry", handle_add_activity_log_entry)
    hass.services.async_register(DOMAIN, "clear_activity_log", handle_clear_activity_log)
    hass.services.async_register(DOMAIN, "delete_activity_log_entry", handle_delete_activity_log_entry)

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    for suffix in ("_unsub_daily", "_unsub_retry", "_unsub_energy_cost", "_unsub_energy_tick", "_unsub_consumer_tick", "_unsub_base_load", "_unsub_consumption_tick", "_unsub_ekz_signal", "_unsub_battery_tick", "_unsub_heating_tick"):
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
        if not remaining_entries and hass.services.has_service(DOMAIN, "mark_consumer_run"):
            hass.services.async_remove(DOMAIN, "mark_consumer_run")
        if not remaining_entries and hass.services.has_service(DOMAIN, "update_consumer_status"):
            hass.services.async_remove(DOMAIN, "update_consumer_status")
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
