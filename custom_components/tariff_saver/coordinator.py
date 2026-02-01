"""Coordinator for Tariff Saver."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi
from .const import DOMAIN, CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME
from .storage import TariffSaverStore

_LOGGER = logging.getLogger(__name__)

# --- Option keys (must match options_flow.py) ---
OPT_PRICE_MODE = "price_mode"  # "fetch" | "import"
OPT_IMPORT_PROVIDER = "import_provider"  # currently only "ekz_api"
OPT_SOURCE_INTERVAL_MIN = "source_interval_minutes"  # 15 | 60
OPT_NORMALIZATION_MODE = "normalization_mode"  # "repeat"
OPT_IMPORT_ENTITY_DYN = "import_entity_dyn"
OPT_BASELINE_MODE = "baseline_mode"  # "fixed" | "entity"
OPT_BASELINE_VALUE = "baseline_value"
OPT_BASELINE_ENTITY = "baseline_entity"
OPT_PRICE_SCALE = "price_scale"
OPT_IGNORE_ZERO_PRICES = "ignore_zero_prices"

# Defaults (keep in sync with options_flow.py)
DEFAULT_PRICE_MODE = "fetch"
DEFAULT_IMPORT_PROVIDER = "ekz_api"
DEFAULT_SOURCE_INTERVAL_MIN = 15
DEFAULT_NORMALIZATION_MODE = "repeat"
DEFAULT_BASELINE_MODE = "fixed"
DEFAULT_BASELINE_VALUE = 0.0
DEFAULT_PRICE_SCALE = 1.0
DEFAULT_IGNORE_ZERO_PRICES = True


@dataclass(frozen=True)
class PriceSlot:
    """A single 15-minute price slot."""

    start: datetime  # UTC, timezone-aware
    price_chf_per_kwh: float


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


class TariffSaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches and stores tariff price curves + daily derived stats."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: EkzTariffApi,
        config: dict[str, Any],
    ) -> None:
        self.hass = hass
        self.api = api

        self.tariff_name: str = config["tariff_name"]
        self.baseline_tariff_name: str | None = config.get("baseline_tariff_name")
        self.publish_time: str = config.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)

        self._last_fetch_date: date | None = None

        # 🔹 Store wird lazy initialisiert
        self.store: TariffSaverStore | None = None

        # 🔹 Bindings to config entry (resolved during lazy init)
        self._entry_id: str | None = None
        self._entry_options: dict[str, Any] = {}

        super().__init__(
            hass,
            _LOGGER,
            name="Tariff Saver",
            update_interval=None,  # daily trigger will call refresh
        )

    async def _async_update_data(self) -> dict[str, Any]:
        # ------------------------------------------------------------------
        # Lazy init of store (binds to entry_id from hass.data[DOMAIN])
        # ------------------------------------------------------------------
        if self.store is None:
            for entry_id, coord in self.hass.data.get(DOMAIN, {}).items():
                if coord is self:
                    self._entry_id = entry_id
                    self.store = TariffSaverStore(self.hass, entry_id)
                    await self.store.async_load()

                    # Load options from the config entry (for import mode, scaling, etc.)
                    entry = self.hass.config_entries.async_get_entry(entry_id)
                    self._entry_options = dict(entry.options) if entry else {}
                    break

        today = dt_util.now().date()
        if self._last_fetch_date == today:
            return self.data or {"active": [], "baseline": [], "stats": {}}

        # ------------------------------------------------------------------
        # Resolve options (with defaults)
        # ------------------------------------------------------------------
        opts = self._entry_options or {}

        price_mode: str = str(opts.get(OPT_PRICE_MODE, DEFAULT_PRICE_MODE))
        _import_provider: str = str(opts.get(OPT_IMPORT_PROVIDER, DEFAULT_IMPORT_PROVIDER))
        source_interval_min: int = int(opts.get(OPT_SOURCE_INTERVAL_MIN, DEFAULT_SOURCE_INTERVAL_MIN))
        normalization_mode: str = str(opts.get(OPT_NORMALIZATION_MODE, DEFAULT_NORMALIZATION_MODE))

        import_entity_dyn: str | None = opts.get(OPT_IMPORT_ENTITY_DYN)

        baseline_mode: str = str(opts.get(OPT_BASELINE_MODE, DEFAULT_BASELINE_MODE))
        baseline_value: float = float(opts.get(OPT_BASELINE_VALUE, DEFAULT_BASELINE_VALUE) or 0.0)
        baseline_entity: str | None = opts.get(OPT_BASELINE_ENTITY)

        price_scale: float = float(opts.get(OPT_PRICE_SCALE, DEFAULT_PRICE_SCALE) or 1.0)
        ignore_zero: bool = bool(opts.get(OPT_IGNORE_ZERO_PRICES, DEFAULT_IGNORE_ZERO_PRICES))

        # ------------------------------------------------------------------
        # Build active curve (either from EKZ API or imported entity)
        # ------------------------------------------------------------------
        active: list[PriceSlot] = []
        baseline: list[PriceSlot] = []

        if price_mode == "import":
            if not import_entity_dyn:
                raise UpdateFailed("Import mode selected but no dynamic price entity configured")

            try:
                raw_dyn = self._read_price_series_from_entity(import_entity_dyn)
                active = self._parse_imported_prices(raw_dyn)
                active = self._normalize_to_15min(active, source_interval_min, normalization_mode)

                # apply scaling + zero filtering
                active = self._apply_scale_and_filter(active, price_scale, ignore_zero)

                if not active:
                    raise UpdateFailed(f"No valid imported price data from '{import_entity_dyn}'")
            except Exception as err:
                raise UpdateFailed(f"Import active tariff failed: {err}") from err

            # Baseline from options (fixed or entity). If not configured, keep empty.
            if baseline_mode == "fixed":
                if active:
                    baseline = [
                        PriceSlot(start=s.start, price_chf_per_kwh=baseline_value)
                        for s in active
                    ]
            elif baseline_mode == "entity":
                if baseline_entity:
                    try:
                        raw_base = self._read_price_series_from_entity(baseline_entity)
                        baseline = self._parse_imported_prices(raw_base)
                        baseline = self._normalize_to_15min(baseline, source_interval_min, normalization_mode)
                        baseline = self._apply_scale_and_filter(baseline, price_scale, ignore_zero)
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to import baseline entity '%s': %s",
                            baseline_entity,
                            err,
                        )
                        baseline = []
                else:
                    baseline = []

        else:
            # ------------------------------------------------------------------
            # Fetch active tariff (full current day)
            # ------------------------------------------------------------------
            try:
                raw_active = await self.api.fetch_prices(self.tariff_name)
                active = self._parse_prices(raw_active)
                if not active:
                    raise UpdateFailed(f"No data returned for active tariff '{self.tariff_name}'")
            except Exception as err:
                raise UpdateFailed(f"Active tariff update failed: {err}") from err

            # ------------------------------------------------------------------
            # Fetch baseline tariff (optional) - EKZ API baseline OR options override
            # ------------------------------------------------------------------
            # If user configured baseline via options, use that first.
            if baseline_mode == "fixed":
                baseline = [
                    PriceSlot(start=s.start, price_chf_per_kwh=baseline_value)
                    for s in active
                ]
            elif baseline_mode == "entity" and baseline_entity:
                try:
                    raw_base = self._read_price_series_from_entity(baseline_entity)
                    baseline = self._parse_imported_prices(raw_base)
                    baseline = self._normalize_to_15min(baseline, source_interval_min, normalization_mode)
                    baseline = self._apply_scale_and_filter(baseline, price_scale, ignore_zero)
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to import baseline entity '%s': %s",
                        baseline_entity,
                        err,
                    )
                    baseline = []
            else:
                # fallback: old EKZ baseline tariff name
                if self.baseline_tariff_name:
                    try:
                        raw_base = await self.api.fetch_prices(self.baseline_tariff_name)
                        baseline = self._parse_prices(raw_base)
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to fetch baseline tariff '%s': %s",
                            self.baseline_tariff_name,
                            err,
                        )
                        baseline = []

            # apply scaling + zero filtering for fetched series as well (scale is no-op by default)
            active = self._apply_scale_and_filter(active, price_scale, ignore_zero)
            baseline = self._apply_scale_and_filter(baseline, price_scale, ignore_zero)

        # ------------------------------------------------------------------
        # Persist price slots (UTC, 15-min)
        # ------------------------------------------------------------------
        if self.store is not None:
            active_map = {s.start: s.price_chf_per_kwh for s in active if s.price_chf_per_kwh > 0}
            base_map = {s.start: s.price_chf_per_kwh for s in baseline if s.price_chf_per_kwh > 0}

            for start_utc, dyn_price in active_map.items():
                base_price = base_map.get(start_utc)
                if base_price is not None:
                    self.store.set_price_slot(start_utc, dyn_price, base_price)

            self.store.trim_price_slots(keep_days=3)
            if self.store.dirty:
                await self.store.async_save()

        # ------------------------------------------------------------------
        # Compute daily stats
        # ------------------------------------------------------------------
        stats = self._compute_daily_stats(active, baseline)
        self._last_fetch_date = today

        return {"active": active, "baseline": baseline, "stats": stats}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_prices(raw_prices: list[dict[str, Any]]) -> list[PriceSlot]:
        """Parse EKZ price items to sorted, de-duplicated UTC slots."""
        slots: list[PriceSlot] = []

        for item in raw_prices:
            start_ts = item.get("start_timestamp")
            if not isinstance(start_ts, str):
                continue

            dt_start = dt_util.parse_datetime(start_ts)
            if dt_start is None:
                continue

            slots.append(
                PriceSlot(
                    start=dt_util.as_utc(dt_start),
                    price_chf_per_kwh=EkzTariffApi.sum_chf_per_kwh(item),
                )
            )

        # de-duplicate by slot start
        return list({s.start: s for s in sorted(slots, key=lambda s: s.start)}.values())

    @staticmethod
    def _compute_daily_stats(
        active: list[PriceSlot],
        baseline: list[PriceSlot],
    ) -> dict[str, Any]:
        """Compute daily averages and per-slot deviations."""
        active_valid = [s for s in active if s.price_chf_per_kwh > 0]
        base_map = {s.start: s.price_chf_per_kwh for s in baseline if s.price_chf_per_kwh > 0}

        avg_active = _avg([s.price_chf_per_kwh for s in active_valid])
        avg_baseline = (
            _avg([base_map[s.start] for s in active_valid if s.start in base_map])
            if base_map
            else None
        )

        dev_vs_avg: dict[str, float] = {}
        dev_vs_baseline: dict[str, float] = {}

        for s in active_valid:
            if avg_active and avg_active > 0:
                dev_vs_avg[s.start.isoformat()] = (s.price_chf_per_kwh / avg_active - 1.0) * 100.0

            base = base_map.get(s.start)
            if base and base > 0:
                dev_vs_baseline[s.start.isoformat()] = (s.price_chf_per_kwh / base - 1.0) * 100.0

        return {
            "calculated_at": dt_util.utcnow().isoformat(),
            "avg_active_chf_per_kwh": avg_active,
            "avg_baseline_chf_per_kwh": avg_baseline,
            "dev_vs_avg_percent": dev_vs_avg,
            "dev_vs_baseline_percent": dev_vs_baseline,
        }

    # ------------------------------------------------------------------
    # Import parsing + normalization (new, but keeps existing functions intact)
    # ------------------------------------------------------------------
    def _read_price_series_from_entity(self, entity_id: str) -> Any:
        """Read a price series from a HA entity state/attributes.

        We try to be flexible:
        - attributes['prices'] / ['data'] / ['raw'] may hold a list of dicts
        - state may be JSON (list) or a numeric (then not enough for a curve)
        """
        st = self.hass.states.get(entity_id)
        if st is None:
            raise ValueError(f"Entity not found: {entity_id}")

        # Prefer attribute series
        attrs = dict(st.attributes or {})
        for key in ("prices", "data", "raw", "today", "tomorrow"):
            if key in attrs and isinstance(attrs[key], (list, tuple)):
                return attrs[key]

        # Try JSON in state
        if isinstance(st.state, str):
            s = st.state.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    return json.loads(s)
                except Exception:
                    pass

        raise ValueError(
            f"Entity '{entity_id}' does not expose a parsable price series "
            "(expected list in attributes like 'prices'/'data' or JSON list state)."
        )

    @staticmethod
    def _parse_imported_prices(raw: Any) -> list[PriceSlot]:
        """Parse imported series into UTC PriceSlots.

        Accepts list of dicts with common keys:
        - start/start_time/start_timestamp/timestamp/time
        - price/value/price_chf_per_kwh
        """
        if not isinstance(raw, (list, tuple)):
            raise ValueError("Imported series must be a list")

        slots: list[PriceSlot] = []
        for item in raw:
            if not isinstance(item, dict):
                continue

            start_any = (
                item.get("start")
                or item.get("start_time")
                or item.get("start_timestamp")
                or item.get("timestamp")
                or item.get("time")
            )
            if not start_any:
                continue

            dt_start = None
            if isinstance(start_any, (int, float)):
                # assume unix seconds
                try:
                    dt_start = dt_util.utc_from_timestamp(float(start_any))
                except Exception:
                    dt_start = None
            elif isinstance(start_any, str):
                dt_start = dt_util.parse_datetime(start_any)

            if dt_start is None:
                continue

            price_any = (
                item.get("price_chf_per_kwh")
                or item.get("price")
                or item.get("value")
            )
            try:
                price = float(price_any)
            except Exception:
                continue

            slots.append(PriceSlot(start=dt_util.as_utc(dt_start), price_chf_per_kwh=price))

        # de-duplicate by slot start
        return list({s.start: s for s in sorted(slots, key=lambda s: s.start)}.values())

    @staticmethod
    def _normalize_to_15min(
        slots: list[PriceSlot],
        source_interval_min: int,
        mode: str,
    ) -> list[PriceSlot]:
        """Normalize source slots (15 or 60 minutes) to 15-minute slots."""
        if source_interval_min == 15:
            return slots

        if source_interval_min != 60:
            # For now we only support 15/60 as agreed (minutes ignored)
            raise ValueError(f"Unsupported source interval: {source_interval_min} minutes")

        if mode != "repeat":
            raise ValueError(f"Unsupported normalization mode: {mode}")

        out: list[PriceSlot] = []
        for s in slots:
            # replicate hour price to 4 quarter-hours
            base = s.start.replace(minute=0, second=0, microsecond=0)
            for i in range(4):
                out.append(
                    PriceSlot(
                        start=base + timedelta(minutes=15 * i),
                        price_chf_per_kwh=s.price_chf_per_kwh,
                    )
                )

        return list({s.start: s for s in sorted(out, key=lambda x: x.start)}.values())

    @staticmethod
    def _apply_scale_and_filter(
        slots: list[PriceSlot],
        scale: float,
        ignore_zero: bool,
    ) -> list[PriceSlot]:
        """Apply multiplicative scaling and optionally filter out zero/negative prices."""
        out: list[PriceSlot] = []
        for s in slots:
            p = s.price_chf_per_kwh * scale
            if ignore_zero and p <= 0:
                continue
            out.append(PriceSlot(start=s.start, price_chf_per_kwh=p))
        return out
