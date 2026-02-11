"""Persistent storage for Tariff Saver (with HA Store migration).

Why this exists:
Home Assistant's Store requires a migrate_func when STORAGE_VERSION changes.
Without it you get: NotImplementedError during _async_migrate_func().

This file:
- Implements migrate_func to move v2/v3 -> v4 safely.
- Keeps backwards-compatible parsing even after migration.

v2 examples you posted:
- samples: list[[iso_ts, kwh_total], ...]
- booked_slots: dict[slot_start_iso -> {kwh,dyn_chf,base_chf,status,...}]
- price_slots: dict[slot_start_iso -> {"dyn": float, "base": float}]  (electricity-only)

v4:
- price_slots: dict[iso -> {"dyn": {comp: val}, "base": {comp: val}|None, "api_integrated": float|None}]
- samples: list[{"ts": epoch_seconds, "kwh": float}]
- booked:  list[{"start": iso, "kwh": float, "status": str, "dyn":{comp:chf}, "base":{comp:chf}, "sav":{comp:chf}}]
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

# Components included in "all-in" (import) sum for consumption costs.
# (feed_in is export tariff; we keep it available but exclude from import sum)
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

        self._store = Store(
            hass,
            self.STORAGE_VERSION,
            f"{self.STORAGE_KEY}.{entry_id}",
            minor_version=self.STORAGE_MINOR_VERSION,
            migrate_func=self._async_migrate,
        )

        self.price_slots: dict[str, dict[str, Any]] = {}
        self.samples: list[dict[str, float]] = []
        self.booked: list[dict[str, Any]] = []
        self.last_api_success_utc: datetime | None = None

        self.dirty: bool = False

    # -------------------------
    # Migration (required by HA)
    # -------------------------
    async def _async_migrate(self, old_version: int, old_minor: int, old_data: dict[str, Any]) -> dict[str, Any]:
        """Migrate older stored schema to current v4.

        Home Assistant calls this automatically when stored version/minor differs.
        """
        data = dict(old_data or {})

        # If already new enough, just pass through
        if old_version >= 4:
            # Still ensure keys exist
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
                    # Already dict-based (future proof)
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
            # v3 list
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
            # v2 dict
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

        # ---- last_api_success ----
        last_api = data.get("last_api_success_utc")

        # Build new data (drop legacy booked_slots key)
        return {
            "price_slots": new_price_slots,
            "samples": new_samples,
            "booked": new_booked,
            "last_api_success_utc": last_api,
        }

    # -------------------------
    # Load / Save
    # -------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}

        self.price_slots = dict(data.get("price_slots") or {}) if isinstance(data.get("price_slots"), dict) else {}

        # samples in v4 form
        self.samples = []
        raw_samples = data.get("samples") or []
        if isinstance(raw_samples, list):
            for item in raw_samples:
                if isinstance(item, dict) and "ts" in item and "kwh" in item:
                    try:
                        self.samples.append({"ts": float(item["ts"]), "kwh": float(item["kwh"])})
                    except Exception:
                        continue

        # booked in v4 form
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

        # If HA migrated, it already wrote the new version; we don't need to mark dirty here.
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
    # API success timestamp
    # -------------------------
    def set_last_api_success(self, when_utc: datetime) -> None:
        self.last_api_success_utc = dt_util.as_utc(when_utc)
        self.dirty = True

    # -------------------------
    # Price slots (v4)
    # -------------------------
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
        cutoff = dt_util.utcnow() - timedelta(days=keep_days)
        cutoff_iso = cutoff.isoformat()
        before = len(self.price_slots)
        self.price_slots = {k: v for k, v in self.price_slots.items() if k >= cutoff_iso}
        if len(self.price_slots) != before:
            self.dirty = True

    # -------------------------
    # Samples
    # -------------------------
    def add_sample(self, ts_utc: datetime, kwh_total: float) -> bool:
        ts_utc = dt_util.as_utc(ts_utc)
        if not isinstance(kwh_total, (int, float)):
            return False
        kwh_total = float(kwh_total)

        epoch = ts_utc.timestamp()
        if self.samples and abs(self.samples[-1]["ts"] - epoch) < 1e-6:
            return False

        self.samples.append({"ts": epoch, "kwh": kwh_total})
        self._trim_samples(keep_days=14)
        self.dirty = True
        return True

    def _trim_samples(self, keep_days: int = 14) -> None:
        cutoff = (dt_util.utcnow() - timedelta(days=keep_days)).timestamp()
        self.samples = [s for s in self.samples if float(s.get("ts", 0)) >= cutoff]

    # -------------------------
    # Booking
    # -------------------------
    @staticmethod
    def _slot_start_utc(ts_utc: datetime) -> datetime:
        ts_utc = dt_util.as_utc(ts_utc)
        minute = (ts_utc.minute // 15) * 15
        return ts_utc.replace(minute=minute, second=0, microsecond=0)

    def _append_booked(
        self,
        start_utc: datetime,
        kwh: float,
        status: str,
        dyn: dict[str, float] | None = None,
        base: dict[str, float] | None = None,
        sav: dict[str, float] | None = None,
    ) -> None:
        self.booked.append(
            {
                "start": dt_util.as_utc(start_utc).isoformat(),
                "kwh": float(kwh),
                "status": str(status),
                "dyn": dict(dyn or {}),
                "base": dict(base or {}),
                "sav": dict(sav or {}),
            }
        )

    def _trim_booked(self, keep_days: int = 400) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=keep_days)
        out: list[dict[str, Any]] = []
        for b in self.booked:
            dtp = dt_util.parse_datetime(str(b.get("start", "")))
            if dtp is None:
                continue
            if dt_util.as_utc(dtp) >= cutoff:
                out.append(b)
        self.booked = out

    def finalize_due_slots(self, now_utc: datetime) -> int:
        now_utc = dt_util.as_utc(now_utc)
        cutoff = now_utc - timedelta(minutes=1)

        if len(self.samples) < 2:
            return 0

        last_booked_start: datetime | None = None
        if self.booked:
            dtp = dt_util.parse_datetime(str(self.booked[-1].get("start", "")))
            last_booked_start = dt_util.as_utc(dtp) if dtp else None

        # sample points
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
            cursor = last_booked_start + timedelta(minutes=15)

        end_slot = self._slot_start_utc(cutoff)

        newly = 0
        while cursor < end_slot:
            slot_end = cursor + timedelta(minutes=15)
            if slot_end > cutoff:
                break

            kwh_start = kwh_at(cursor)
            kwh_end = kwh_at(slot_end)

            if kwh_start is None or kwh_end is None:
                self._append_booked(cursor, 0.0, "missing_samples")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            delta = float(kwh_end - kwh_start)
            if delta < 0:
                self._append_booked(cursor, 0.0, "invalid")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            dyn_prices, base_prices, _api_int = self.get_price_components(cursor)
            if not dyn_prices:
                self._append_booked(cursor, delta, "unpriced")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            dyn_cost: dict[str, float] = {}
            base_cost: dict[str, float] = {}
            sav_cost: dict[str, float] = {}

            for comp, p in dyn_prices.items():
                if isinstance(p, (int, float)) and float(p) != 0.0:
                    dyn_cost[comp] = delta * float(p)

            if base_prices:
                for comp, p in base_prices.items():
                    if isinstance(p, (int, float)) and float(p) != 0.0:
                        base_cost[comp] = delta * float(p)

            for comp, bchf in base_cost.items():
                dchf = dyn_cost.get(comp)
                if dchf is not None:
                    sav_cost[comp] = bchf - dchf

            status = "ok" if dyn_cost else "unpriced"
            self._append_booked(cursor, delta, status, dyn_cost, base_cost, sav_cost)
            newly += 1
            cursor += timedelta(minutes=15)

        if newly:
            self._trim_booked(keep_days=400)
            self.dirty = True
        return newly

    # -------------------------
    # Period breakdown totals
    # -------------------------
    def _sum_between_local(self, start_local: datetime, end_local: datetime) -> dict[str, dict[str, float]]:
        start_utc = dt_util.as_utc(start_local)
        end_utc = dt_util.as_utc(end_local)

        out: dict[str, dict[str, float]] = {"dyn": {}, "base": {}, "sav": {}}

        for b in self.booked:
            dtp = dt_util.parse_datetime(str(b.get("start", "")))
            if dtp is None:
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

    @staticmethod
    def _period_bounds(period: str) -> tuple[datetime, datetime]:
        now = dt_util.now()
        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=1)
        if period == "week":
            start = (now - timedelta(days=now.isoweekday() - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=7)
        if period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
            return start, end
        if period == "year":
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return start, start.replace(year=start.year + 1)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    def compute_period_breakdown(self, period: str) -> dict[str, dict[str, float]]:
        start, end = self._period_bounds(period)
        return self._sum_between_local(start, end)

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
