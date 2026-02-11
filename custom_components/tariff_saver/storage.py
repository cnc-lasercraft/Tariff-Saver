"""Persistent storage for Tariff Saver (migration-safe).

You are hitting:
  NotImplementedError (helpers/storage.py:_async_migrate_func)

That ONLY happens when Home Assistant needs to migrate a Store file
(version changed) but the Store instance was created WITHOUT a migrate callback.

This file fixes that by:
- Detecting the correct keyword argument name for the current HA version
  (async_migrate_func vs migrate_func) using inspect.signature
- Registering the migration callback reliably.

Legacy supported:
v2: price_slots {iso: {"dyn": float, "base": float}}, samples [[iso,kwh]], booked_slots {iso:{...}}
v4: price_slots {iso: {"dyn":{...},"base":{...},"api_integrated":...}}, samples [{"ts":epoch,"kwh":...}], booked [ {...} ]
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable
import inspect

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

# Components included in "all-in" import cost (feed_in is export; excluded)
IMPORT_ALLIN_COMPONENTS = [
    "electricity",
    "grid",
    "regional_fees",
    "metering",
    "refund_storage",
]


class TariffSaverStore:
    STORAGE_VERSION = 4
    STORAGE_MINOR_VERSION = 1
    STORAGE_KEY = "tariff_saver"

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id

        self._store = self._create_store()

        self.price_slots: dict[str, dict[str, Any]] = {}
        self.samples: list[dict[str, float]] = []
        self.booked: list[dict[str, Any]] = []
        self.last_api_success_utc: datetime | None = None
        self.dirty: bool = False

    def _create_store(self) -> Store:
        key = f"{self.STORAGE_KEY}.{self.entry_id}"

        sig = inspect.signature(Store.__init__)
        kwargs: dict[str, Any] = {"minor_version": self.STORAGE_MINOR_VERSION}

        # HA core (modern) uses async_migrate_func. Some forks/older builds used migrate_func.
        if "async_migrate_func" in sig.parameters:
            kwargs["async_migrate_func"] = self._async_migrate
        elif "migrate_func" in sig.parameters:
            kwargs["migrate_func"] = self._async_migrate

        return Store(self.hass, self.STORAGE_VERSION, key, **kwargs)

    # -------------------------
    # Migration callback
    # -------------------------
    async def _async_migrate(self, old_version: int, old_minor: int, old_data: dict[str, Any]) -> dict[str, Any]:
        data = dict(old_data or {})

        # If stored is newer than our code, don't down-migrate.
        if old_version > self.STORAGE_VERSION:
            return data

        # Already v4+
        if old_version >= 4:
            data.setdefault("price_slots", {})
            data.setdefault("samples", [])
            data.setdefault("booked", [])
            return data

        # ---- price_slots ----
        new_price_slots: dict[str, dict[str, Any]] = {}
        raw_price_slots = data.get("price_slots") or {}
        if isinstance(raw_price_slots, dict):
            for k, v in raw_price_slots.items():
                if not isinstance(k, str) or not isinstance(v, dict):
                    continue
                if isinstance(v.get("dyn"), (int, float)):
                    dyn = float(v.get("dyn", 0.0))
                    base = v.get("base")
                    base_f = float(base) if isinstance(base, (int, float)) else None
                    new_price_slots[k] = {
                        "dyn": {"electricity": dyn},
                        "base": {"electricity": base_f} if base_f is not None else None,
                        "api_integrated": None,
                    }
                elif isinstance(v.get("dyn"), dict):
                    new_price_slots[k] = dict(v)

        # ---- samples ----
        new_samples: list[dict[str, float]] = []
        raw_samples = data.get("samples") or []
        if isinstance(raw_samples, list):
            for item in raw_samples:
                if isinstance(item, dict) and "ts" in item and "kwh" in item:
                    try:
                        new_samples.append({"ts": float(item["ts"]), "kwh": float(item["kwh"])})
                    except Exception:
                        continue
                elif isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
                    dtp = dt_util.parse_datetime(item[0])
                    if dtp is None:
                        continue
                    try:
                        kwh = float(item[1])
                    except Exception:
                        continue
                    new_samples.append({"ts": dt_util.as_utc(dtp).timestamp(), "kwh": kwh})

        # ---- booked ----
        new_booked: list[dict[str, Any]] = []
        raw_booked = data.get("booked")
        raw_booked_slots = data.get("booked_slots")

        if isinstance(raw_booked, list):
            for b in raw_booked:
                if not isinstance(b, dict) or "start" not in b:
                    continue
                if "dyn" not in b and ("dyn_chf" in b or "base_chf" in b or "savings_chf" in b):
                    dyn = float(b.get("dyn_chf", 0.0) or 0.0)
                    base = float(b.get("base_chf", 0.0) or 0.0)
                    sav = float(b.get("savings_chf", 0.0) or 0.0)
                    nb = dict(b)
                    nb.pop("dyn_chf", None)
                    nb.pop("base_chf", None)
                    nb.pop("savings_chf", None)
                    nb["dyn"] = {"electricity": dyn}
                    nb["base"] = {"electricity": base}
                    nb["sav"] = {"electricity": sav}
                    new_booked.append(nb)
                else:
                    new_booked.append(dict(b))
        elif isinstance(raw_booked_slots, dict):
            for start_iso, payload in raw_booked_slots.items():
                if not isinstance(start_iso, str) or not isinstance(payload, dict):
                    continue
                kwh = float(payload.get("kwh", 0.0) or 0.0)
                dyn = float(payload.get("dyn_chf", payload.get("dyn", 0.0)) or 0.0)
                base = float(payload.get("base_chf", payload.get("base", 0.0)) or 0.0)
                sav = float(payload.get("savings_chf", payload.get("sav", 0.0)) or 0.0)
                status = str(payload.get("status", "ok" if dyn or base else "unpriced"))
                new_booked.append(
                    {
                        "start": start_iso,
                        "kwh": kwh,
                        "status": status,
                        "dyn": {"electricity": dyn},
                        "base": {"electricity": base},
                        "sav": {"electricity": sav},
                    }
                )
            new_booked.sort(key=lambda x: str(x.get("start", "")))

        return {
            "price_slots": new_price_slots,
            "samples": new_samples,
            "booked": new_booked,
            "last_api_success_utc": data.get("last_api_success_utc"),
        }

    # -------------------------
    # Load / Save
    # -------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}

        self.price_slots = dict(data.get("price_slots") or {}) if isinstance(data.get("price_slots"), dict) else {}

        self.samples = []
        raw_samples = data.get("samples") or []
        if isinstance(raw_samples, list):
            for item in raw_samples:
                if isinstance(item, dict) and "ts" in item and "kwh" in item:
                    try:
                        self.samples.append({"ts": float(item["ts"]), "kwh": float(item["kwh"])})
                    except Exception:
                        continue

        self.booked = []
        raw_booked = data.get("booked") or []
        if isinstance(raw_booked, list):
            for b in raw_booked:
                if isinstance(b, dict) and "start" in b:
                    self.booked.append(dict(b))

        ts = data.get("last_api_success_utc")
        if isinstance(ts, str):
            dtp = dt_util.parse_datetime(ts)
            self.last_api_success_utc = dt_util.as_utc(dtp) if dtp else None
        else:
            self.last_api_success_utc = None

        self.dirty = False

    async def async_save(self) -> None:
        await self._store.async_save(self._as_dict())
        self.dirty = False

    def _as_dict(self) -> dict[str, Any]:
        return {
            "price_slots": self.price_slots,
            "samples": self.samples,
            "booked": self.booked,
            "last_api_success_utc": self.last_api_success_utc.isoformat() if self.last_api_success_utc else None,
        }

    # -------------------------
    # Public helpers used by coordinator/sensors
    # -------------------------
    def set_last_api_success(self, when_utc: datetime) -> None:
        self.last_api_success_utc = dt_util.as_utc(when_utc)
        self.dirty = True

    def set_price_slot(
        self,
        start_utc: datetime,
        dyn_components_chf_per_kwh: dict[str, float],
        base_components_chf_per_kwh: dict[str, float] | None = None,
        api_integrated: float | None = None,
    ) -> None:
        start_utc = dt_util.as_utc(start_utc)
        key = start_utc.isoformat()

        dyn = {k: float(v) for k, v in (dyn_components_chf_per_kwh or {}).items() if isinstance(v, (int, float))}
        base = None
        if base_components_chf_per_kwh:
            base = {k: float(v) for k, v in base_components_chf_per_kwh.items() if isinstance(v, (int, float))}

        self.price_slots[key] = {
            "dyn": dyn,
            "base": base,
            "api_integrated": float(api_integrated) if isinstance(api_integrated, (int, float)) else None,
        }
        self.dirty = True

    def get_price_components(self, start_utc: datetime) -> tuple[dict[str, float] | None, dict[str, float] | None, float | None]:
        key = dt_util.as_utc(start_utc).isoformat()
        slot = self.price_slots.get(key)
        if not isinstance(slot, dict):
            return None, None, None
        dyn = slot.get("dyn")
        base = slot.get("base")
        api_int = slot.get("api_integrated")
        return (
            dict(dyn) if isinstance(dyn, dict) else None,
            dict(base) if isinstance(base, dict) else None,
            float(api_int) if isinstance(api_int, (int, float)) else None,
        )

    def trim_price_slots(self, keep_days: int = 7) -> None:
        cutoff_iso = (dt_util.utcnow() - timedelta(days=keep_days)).isoformat()
        before = len(self.price_slots)
        self.price_slots = {k: v for k, v in self.price_slots.items() if k >= cutoff_iso}
        if len(self.price_slots) != before:
            self.dirty = True

    def add_sample(self, ts_utc: datetime, kwh_total: float) -> bool:
        ts_utc = dt_util.as_utc(ts_utc)
        if not isinstance(kwh_total, (int, float)):
            return False
        epoch = ts_utc.timestamp()
        if self.samples and abs(self.samples[-1]["ts"] - epoch) < 1e-6:
            return False
        self.samples.append({"ts": epoch, "kwh": float(kwh_total)})
        cutoff = (dt_util.utcnow() - timedelta(days=14)).timestamp()
        self.samples = [s for s in self.samples if float(s.get("ts", 0.0)) >= cutoff]
        self.dirty = True
        return True

    @staticmethod
    def _slot_start_utc(ts_utc: datetime) -> datetime:
        ts_utc = dt_util.as_utc(ts_utc)
        minute = (ts_utc.minute // 15) * 15
        return ts_utc.replace(minute=minute, second=0, microsecond=0)

    def finalize_due_slots(self, now_utc: datetime) -> int:
        # Keep the booking algorithm identical to the last working version.
        from datetime import timedelta as _td

        now_utc = dt_util.as_utc(now_utc)
        cutoff = now_utc - _td(minutes=1)
        if len(self.samples) < 2:
            return 0

        last_booked_start: datetime | None = None
        if self.booked:
            dtp = dt_util.parse_datetime(str(self.booked[-1].get("start", "")))
            last_booked_start = dt_util.as_utc(dtp) if dtp else None

        sample_points: list[tuple[datetime, float]] = []
        for s in self.samples:
            try:
                dtp = dt_util.as_utc(datetime.fromtimestamp(float(s["ts"])))
                sample_points.append((dtp, float(s["kwh"])))
            except Exception:
                continue
        sample_points.sort(key=lambda x: x[0])
        if not sample_points:
            return 0

        def kwh_at(t: datetime) -> float | None:
            prev = None
            for dtp, kwh in sample_points:
                if dtp <= t:
                    prev = kwh
                else:
                    break
            return prev

        cursor = self._slot_start_utc(sample_points[0][0])
        if last_booked_start:
            cursor = last_booked_start + _td(minutes=15)
        end_slot = self._slot_start_utc(cutoff)

        newly = 0
        while cursor < end_slot:
            slot_end = cursor + _td(minutes=15)
            if slot_end > cutoff:
                break

            kwh_start = kwh_at(cursor)
            kwh_end = kwh_at(slot_end)
            if kwh_start is None or kwh_end is None:
                self.booked.append({"start": cursor.isoformat(), "kwh": 0.0, "status": "missing_samples", "dyn": {}, "base": {}, "sav": {}})
                newly += 1
                cursor += _td(minutes=15)
                continue

            delta = float(kwh_end - kwh_start)
            if delta < 0:
                self.booked.append({"start": cursor.isoformat(), "kwh": 0.0, "status": "invalid", "dyn": {}, "base": {}, "sav": {}})
                newly += 1
                cursor += _td(minutes=15)
                continue

            dyn_prices, base_prices, _api_int = self.get_price_components(cursor)
            if not dyn_prices:
                self.booked.append({"start": cursor.isoformat(), "kwh": delta, "status": "unpriced", "dyn": {}, "base": {}, "sav": {}})
                newly += 1
                cursor += _td(minutes=15)
                continue

            dyn_cost: dict[str, float] = {c: delta * float(p) for c, p in dyn_prices.items() if isinstance(p, (int, float)) and float(p) != 0.0}
            base_cost: dict[str, float] = {}
            if base_prices:
                base_cost = {c: delta * float(p) for c, p in base_prices.items() if isinstance(p, (int, float)) and float(p) != 0.0}
            sav_cost: dict[str, float] = {c: base_cost[c] - dyn_cost[c] for c in base_cost if c in dyn_cost}

            self.booked.append({"start": cursor.isoformat(), "kwh": delta, "status": "ok" if dyn_cost else "unpriced", "dyn": dyn_cost, "base": base_cost, "sav": sav_cost})
            newly += 1
            cursor += _td(minutes=15)

        if newly:
            cutoff_b = dt_util.utcnow() - timedelta(days=400)
            out = []
            for b in self.booked:
                dtp = dt_util.parse_datetime(str(b.get("start", "")))
                if dtp and dt_util.as_utc(dtp) >= cutoff_b:
                    out.append(b)
            self.booked = out
            self.dirty = True

        return newly

    def compute_period_breakdown(self, period: str) -> dict[str, dict[str, float]]:
        now = dt_util.now()
        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        elif period == "week":
            start = (now - timedelta(days=now.isoweekday() - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=7)
        elif period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        elif period == "year":
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = start.replace(year=start.year + 1)
        else:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)

        start_utc = dt_util.as_utc(start)
        end_utc = dt_util.as_utc(end)

        out: dict[str, dict[str, float]] = {"dyn": {}, "base": {}, "sav": {}}
        for b in self.booked:
            dtp = dt_util.parse_datetime(str(b.get("start", "")))
            if not dtp:
                continue
            s_utc = dt_util.as_utc(dtp)
            if not (start_utc <= s_utc < end_utc):
                continue
            for bucket in ("dyn", "base", "sav"):
                m = b.get(bucket)
                if not isinstance(m, dict):
                    continue
                for comp, val in m.items():
                    if isinstance(val, (int, float)):
                        out[bucket][comp] = out[bucket].get(comp, 0.0) + float(val)
        return out

    def compute_today_breakdown(self) -> dict[str, dict[str, float]]:
        return self.compute_period_breakdown("today")

    def compute_week_breakdown(self) -> dict[str, dict[str, float]]:
        return self.compute_period_breakdown("week")

    def compute_month_breakdown(self) -> dict[str, dict[str, float]]:
        return self.compute_period_breakdown("month")

    def compute_year_breakdown(self) -> dict[str, dict[str, float]]:
        return self.compute_period_breakdown("year")

    @staticmethod
    def sum_components(m: dict[str, float], components: list[str]) -> float:
        return sum(float(m.get(c, 0.0) or 0.0) for c in components)
