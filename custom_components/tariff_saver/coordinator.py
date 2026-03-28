"""Coordinator for Tariff Saver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EKZ_ENTRY_ID,
    CONF_FEED_IN_FIXED_PRICE,
    CONF_FEED_IN_PRICE_ENTITY,
    CONF_FEED_IN_PRICE_MODE,
    CONF_PUBLISH_TIME,
    DEFAULT_FEED_IN_FIXED_PRICE,
    DEFAULT_PUBLISH_TIME,
    FEED_IN_PRICE_MODE_ENTITY,
    DOMAIN,
)
from .consumer_helpers import build_all_consumer_plans
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSlot:
    """A normalized price slot."""

    start: datetime  # UTC, timezone-aware
    electricity_chf_per_kwh: float
    components_chf_per_kwh: dict[str, float]


WINDOW_HOURS: tuple[int, ...] = (1, 2, 3, 6)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _slot_total_price(comps: dict[str, float] | None) -> float | None:
    if not comps:
        return None
    total = 0.0
    found = False
    for key in ("electricity", "grid", "regional_fees"):
        value = comps.get(key)
        if isinstance(value, (int, float)):
            total += float(value)
            found = True
    if found and total > 0:
        return total
    for key in ("integrated", "all_in"):
        value = comps.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    return None




def _aggregate_slots_30m(slots: list[PriceSlot]) -> list[PriceSlot]:
    """Aggregate 15-minute price slots into 30-minute slots."""
    buckets: dict[datetime, list[PriceSlot]] = {}
    for slot in sorted(slots, key=lambda s: s.start):
        local_start = dt_util.as_local(slot.start)
        bucket_minute = 0 if local_start.minute < 30 else 30
        bucket_local = local_start.replace(minute=bucket_minute, second=0, microsecond=0)
        bucket_start = dt_util.as_utc(bucket_local)
        buckets.setdefault(bucket_start, []).append(slot)

    aggregated: list[PriceSlot] = []
    for bucket_start in sorted(buckets):
        bucket_slots = buckets[bucket_start]
        components: dict[str, float] = {}
        counts: dict[str, int] = {}
        electricity_values: list[float] = []

        for slot in bucket_slots:
            if isinstance(slot.electricity_chf_per_kwh, (int, float)):
                electricity_values.append(float(slot.electricity_chf_per_kwh))
            for key, value in (slot.components_chf_per_kwh or {}).items():
                if isinstance(value, (int, float)):
                    components[key] = components.get(key, 0.0) + float(value)
                    counts[key] = counts.get(key, 0) + 1

        averaged_components = {
            key: round(total / counts[key], 6)
            for key, total in components.items()
            if counts.get(key)
        }
        electricity = round(sum(electricity_values) / len(electricity_values), 6) if electricity_values else 0.0
        if electricity > 0:
            averaged_components["electricity"] = electricity

        aggregated.append(
            PriceSlot(
                start=bucket_start,
                electricity_chf_per_kwh=electricity,
                components_chf_per_kwh=averaged_components,
            )
        )

    return aggregated

def score_from_prices(current_price: float | None, min_price: float | None, max_price: float | None) -> int | None:
    """Return score where 0 is cheapest and 100 is most expensive."""
    if not isinstance(current_price, (int, float)) or current_price < 0:
        return None
    if not isinstance(min_price, (int, float)) or not isinstance(max_price, (int, float)):
        return None
    if max_price <= min_price:
        return 50
    score = ((float(current_price) - float(min_price)) / (float(max_price) - float(min_price))) * 100.0
    return max(0, min(100, int(round(score))))


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Reads tariff curves from the separate EKZ provider integration."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

        self.tariff_name: str = "EKZ active"
        self.baseline_tariff_name: str | None = "EKZ baseline"
        self.publish_time: str = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
        self.ekz_entry_id: str | None = config.get(CONF_EKZ_ENTRY_ID)
        self.feed_in_price_mode: str = str(config.get(CONF_FEED_IN_PRICE_MODE) or "fixed")
        self.feed_in_fixed_price: float = float(config.get(CONF_FEED_IN_FIXED_PRICE, DEFAULT_FEED_IN_FIXED_PRICE) or 0.0)
        self.feed_in_price_entity: str | None = config.get(CONF_FEED_IN_PRICE_ENTITY)

        self._last_fetch_date: date | None = None
        self.store: TariffSaverStore | None = None

        self.link_status: str | None = None
        self.linking_url: str | None = None
        self.last_api_success_utc: datetime | None = None

        super().__init__(hass, _LOGGER, name="Tariff Saver", update_interval=timedelta(minutes=15))

    async def _async_update_data(self) -> dict[str, Any]:
        if self.store is None:
            for entry_id, coord in self.hass.data.get(DOMAIN, {}).items():
                if coord is self:
                    self.store = TariffSaverStore(self.hass, entry_id)
                    await self.store.async_load()
                    break

        now_local = dt_util.now()
        today = now_local.date()
        tomorrow = today + timedelta(days=1)

        # Once per day fetch; EKZ signal resets _last_fetch_date to force re-fetch
        if self._last_fetch_date == today and self.data:
            return self.data

        provider_data = self._get_provider_data()
        active_raw = provider_data.get("active_slots") or []
        baseline_raw = provider_data.get("baseline_slots") or []

        active = [self._convert_slot(slot) for slot in active_raw]
        baseline = [self._convert_slot(slot) for slot in baseline_raw]
        active_30m = _aggregate_slots_30m(active)
        baseline_30m = _aggregate_slots_30m(baseline)

        if not active:
            raise UpdateFailed("EKZ Tariff provider returned no active_slots")

        self.link_status = provider_data.get("link_status")
        self.linking_url = provider_data.get("linking_url")
        self.last_api_success_utc = provider_data.get("last_api_success_utc")

        if self.store is not None:
            if isinstance(self.last_api_success_utc, datetime):
                self.store.set_last_api_success(self.last_api_success_utc)
            base_map = {s.start: s.components_chf_per_kwh for s in baseline}
            for s in active:
                self.store.set_price_slot(
                    s.start,
                    dyn_components_chf_per_kwh=s.components_chf_per_kwh,
                    base_components_chf_per_kwh=base_map.get(s.start),
                )
            self.store.trim_price_slots(keep_days=7)

        stats = self._compute_daily_stats(active_30m, baseline_30m)

        # Update historical min/max prices
        if self.store is not None:
            all_prices = [_slot_total_price(s.components_chf_per_kwh) for s in active_30m]
            self.store.update_historical_prices(all_prices)
            if self.store.dirty:
                self.hass.async_create_task(self.store.async_save())
        feed_in_price = self._get_feed_in_price()

        if self.store is not None:
            slot_day = stats.get("slot_day_local")
            avg_active = stats.get("avg_active_chf_per_kwh")
            if isinstance(slot_day, date) and isinstance(avg_active, (int, float)) and avg_active > 0:
                self.store.set_day_average_price(slot_day, float(avg_active))
                self.store.trim_day_average_prices(keep_days=400)
            if self.store.dirty:
                await self.store.async_save()

        windows = self._compute_best_windows(active_30m)
        self._last_fetch_date = today

        # ── Consumer Plans: einmal berechnen, stabil speichern ──
        # Plans werden für ein bestimmtes Tarif-Datum berechnet und wiederverwendet.
        # Neue Berechnung NUR wenn Tarife für einen neuen Tag eintreffen.
        consumer_plans: dict = {}

        # Tarif-Datum bestimmen: welchen Tag decken die Slots ab?
        slot_dates = [dt_util.as_local(s.start).date() for s in active_30m]
        from collections import Counter
        tariff_date = str(Counter(slot_dates).most_common(1)[0][0]) if slot_dates else str(today)

        if self.store is not None and self.store.consumer_plans and self.store.plans_tariff_date == tariff_date:
            # Plans für dieses Tarif-Datum vorhanden — laden, nicht neu berechnen
            consumer_plans = {
                int(k) if str(k).isdigit() else k: v
                for k, v in self.store.consumer_plans.items()
            }
            _LOGGER.info("Plans geladen für %s", tariff_date)
        elif hasattr(self, "entry"):
            # Keine Plans für dieses Datum — neu berechnen
            tariff_date_fmt = dt_util.parse_date(tariff_date).strftime("%d.%m.%Y") if dt_util.parse_date(tariff_date) else tariff_date
            try:
                consumer_plans = build_all_consumer_plans(
                    hass=self.hass,
                    entry=self.entry,
                    active_slots=active_30m,
                    store=self.store,
                )
                # Persist plans
                if self.store is not None:
                    serializable = {}
                    for slot, plan in consumer_plans.items():
                        sp = dict(plan)
                        for k in ("start", "end", "chosen_start", "chosen_end", "tariff_window_start", "tariff_window_end"):
                            v = sp.get(k)
                            if hasattr(v, "isoformat"):
                                sp[k] = v.isoformat()
                        serializable[slot] = sp
                    self.store.consumer_plans = serializable
                    self.store.plans_tariff_date = tariff_date
                    self.store.dirty = True

                _LOGGER.info("Slots für %s berechnet", tariff_date_fmt)
                self.store.log_activity("📅", f"Slots für {tariff_date_fmt} berechnet")

                # Log geplante Consumer
                from .consumer_helpers import get_consumer_config
                for _slot, _plan in consumer_plans.items():
                    _status = _plan.get("status", "")
                    _source = _plan.get("source", "")
                    _start = _plan.get("start") or _plan.get("chosen_start")
                    _end = _plan.get("end") or _plan.get("chosen_end")
                    try:
                        _cfg = get_consumer_config(self.entry, int(_slot))
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
                        self.store.log_activity("\U0001f4cb", f"{_name} geplant ({_detail} {_time_str})")
            except Exception as _err:
                _LOGGER.error("Consumer-Plan-Berechnung fehlgeschlagen: %s", _err)
        else:
            if self.store is not None and self.store.plans_tariff_date:
                _LOGGER.error(
                    "Keine Plans für heute (%s)! Gespeicherte Plans sind für %s.",
                    tariff_date, self.store.plans_tariff_date,
                )

        return {
            "active": active_30m,
            "baseline": baseline_30m,
            "active_raw": active,
            "baseline_raw": baseline,
            "stats": stats,
            "feed_in": {
                "mode": self.feed_in_price_mode,
                "price_chf_per_kwh": feed_in_price,
                "entity_id": self.feed_in_price_entity,
            },
            "windows": windows,
            "provider": {
                "entry_id": provider_data.get("entry_id"),
                "provider": provider_data.get("provider"),
                "active_publication_timestamp": provider_data.get("active_publication_timestamp"),
                "baseline_publication_timestamp": provider_data.get("baseline_publication_timestamp"),
            },
            "consumer_plans": consumer_plans,
        }

    def _get_provider_data(self) -> dict[str, Any]:
        try:
            from custom_components.ekz_tariff import get_first_provider_data, get_provider_data
        except ImportError as err:
            raise UpdateFailed("Could not import custom_components.ekz_tariff provider helpers") from err

        try:
            if isinstance(self.ekz_entry_id, str) and self.ekz_entry_id.strip():
                return get_provider_data(self.hass, self.ekz_entry_id.strip())
            return get_first_provider_data(self.hass)
        except KeyError as err:
            raise UpdateFailed("No loaded EKZ Tariff entry found") from err

    def _get_feed_in_price(self) -> float | None:
        if self.feed_in_price_mode == FEED_IN_PRICE_MODE_ENTITY and isinstance(self.feed_in_price_entity, str) and self.feed_in_price_entity:
            state = self.hass.states.get(self.feed_in_price_entity)
            if state is not None:
                try:
                    value = float(state.state)
                    return value if value >= 0 else None
                except Exception:
                    return None
            return None
        return self.feed_in_fixed_price if self.feed_in_fixed_price >= 0 else None

    @staticmethod
    def _convert_slot(slot: Any) -> PriceSlot:
        start = getattr(slot, "start", None)
        if not isinstance(start, datetime):
            raise UpdateFailed("EKZ Tariff slot is missing start")

        raw_components = getattr(slot, "components_chf_per_kwh", {}) or {}
        components = {
            str(key): float(value)
            for key, value in raw_components.items()
            if isinstance(value, (int, float))
        }

        electricity = float(getattr(slot, "electricity_chf_per_kwh", 0.0) or 0.0)
        if electricity <= 0:
            electricity = float(components.get("electricity") or 0.0)
        if electricity > 0:
            components["electricity"] = electricity

        return PriceSlot(
            start=dt_util.as_utc(start),
            electricity_chf_per_kwh=electricity,
            components_chf_per_kwh=components,
        )

    def _compute_daily_stats(self, active: list[PriceSlot], baseline: list[PriceSlot]) -> dict[str, Any]:
        # Filter to today only for daily averages/scores
        today_local = dt_util.now().date()
        today_active = [s for s in active if dt_util.as_local(s.start).date() == today_local]
        today_baseline = [s for s in baseline if dt_util.as_local(s.start).date() == today_local]

        active_map = {s.start: _slot_total_price(s.components_chf_per_kwh) for s in today_active}
        active_values = [float(v) for v in active_map.values() if isinstance(v, (int, float)) and v >= 0]

        baseline_by_start = {s.start: s for s in today_baseline}
        base_values = [
            float(v)
            for v in (_slot_total_price(s.components_chf_per_kwh) for s in today_baseline)
            if isinstance(v, (int, float)) and v >= 0
        ]

        min_active = min(active_values) if active_values else None
        max_active = max(active_values) if active_values else None
        avg_active = _avg(active_values)
        avg_baseline = _avg(base_values)

        current_slot = next((s for s in reversed(sorted(active, key=lambda s: s.start)) if s.start <= dt_util.utcnow()), None)
        if current_slot is None and active:
            current_slot = active[0]
        current_price = _slot_total_price(current_slot.components_chf_per_kwh) if current_slot else None
        all_baseline_map = {s.start: s for s in baseline}
        current_base_slot = all_baseline_map.get(current_slot.start) if current_slot else None
        current_baseline = _slot_total_price(current_base_slot.components_chf_per_kwh) if current_base_slot else None

        slot_day_local = today_local if today_active else None
        year_prices = self.store.get_year_day_average_prices(slot_day_local) if self.store and isinstance(slot_day_local, date) else []
        year_min = min(year_prices) if year_prices else None
        year_max = max(year_prices) if year_prices else None
        day_score = score_from_prices(avg_active, year_min, year_max)

        dev_vs_baseline_percent = None
        if isinstance(current_price, (int, float)) and isinstance(current_baseline, (int, float)) and current_baseline > 0:
            dev_vs_baseline_percent = ((float(current_price) / float(current_baseline)) - 1.0) * 100.0

        # Tomorrow stats (available after ~18:15 when EKZ publishes)
        from datetime import timedelta as _td
        tomorrow_local = today_local + _td(days=1)
        tomorrow_active = [s for s in active if dt_util.as_local(s.start).date() == tomorrow_local]
        tomorrow_values = [
            float(v) for v in
            (_slot_total_price(s.components_chf_per_kwh) for s in tomorrow_active)
            if isinstance(v, (int, float)) and v >= 0
        ]
        avg_tomorrow = _avg(tomorrow_values)
        tomorrow_score = score_from_prices(avg_tomorrow, year_min, year_max) if avg_tomorrow else None

        return {
            "calculated_at": dt_util.utcnow().isoformat(),
            "slot_day_local": slot_day_local,
            "min_active_chf_per_kwh": min_active,
            "max_active_chf_per_kwh": max_active,
            "avg_active_chf_per_kwh": avg_active,
            "avg_baseline_chf_per_kwh": avg_baseline,
            "current_price_chf_per_kwh": current_price,
            "current_baseline_chf_per_kwh": current_baseline,
            "current_score_0_100": score_from_prices(current_price, min_active, max_active),
            "price_vs_baseline_percent": dev_vs_baseline_percent,
            "day_score_0_100": day_score,
            "year_min_day_avg_chf_per_kwh": year_min,
            "year_max_day_avg_chf_per_kwh": year_max,
            "year_day_samples": len(year_prices),
            "tomorrow_avg_chf_per_kwh": avg_tomorrow,
            "tomorrow_score_0_100": tomorrow_score,
            "tomorrow_slot_count": len(tomorrow_values),
        }

    @staticmethod
    def _compute_best_windows(active: list[PriceSlot]) -> dict[str, Any]:
        ordered = sorted(active, key=lambda s: s.start)
        prices = [_slot_total_price(s.components_chf_per_kwh) for s in ordered]
        valid_prices = [float(v) for v in prices if isinstance(v, (int, float)) and v >= 0]
        min_price = min(valid_prices) if valid_prices else None
        max_price = max(valid_prices) if valid_prices else None

        out: dict[str, Any] = {}
        now_utc = dt_util.utcnow()

        for hours in WINDOW_HOURS:
            slot_count = hours * 2
            candidates: list[tuple[datetime, float]] = []
            for idx in range(0, len(ordered) - slot_count + 1):
                window_slots = ordered[idx : idx + slot_count]
                if window_slots[-1].start < now_utc:
                    continue
                window_prices = [_slot_total_price(s.components_chf_per_kwh) for s in window_slots]
                if any(not isinstance(v, (int, float)) or float(v) < 0 for v in window_prices):
                    continue
                avg_price = sum(float(v) for v in window_prices) / slot_count
                candidates.append((window_slots[0].start, avg_price))

            key = f"{hours}h"
            if not candidates:
                out[key] = {"start": None, "avg_price_chf_per_kwh": None, "score_0_100": None}
                continue

            best_start, best_price = min(candidates, key=lambda item: item[1])
            out[key] = {
                "start": best_start,
                "avg_price_chf_per_kwh": round(best_price, 6),
                "score_0_100": score_from_prices(best_price, min_price, max_price),
            }
        return out
