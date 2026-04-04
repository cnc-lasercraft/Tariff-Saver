"""Lightweight persistent storage for Tariff Saver."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util


IMPORT_ALLIN_COMPONENTS: tuple[str, ...] = (
    "electricity",
    "grid",
    "regional_fees",
)

_FALLBACK_TOTAL_KEYS: tuple[str, ...] = ("integrated", "all_in")


class TariffSaverStore:
    """Persists recent energy samples, price slots and finalized 15-min slots."""

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
        )

        setattr(self._store, "_async_migrate_func", self._async_migrate)

        self.price_slots: dict[str, dict[str, Any]] = {}
        self.samples: list[dict[str, float]] = []
        self.booked: list[dict[str, Any]] = []
        self.base_load_daily: list[dict[str, Any]] = []
        self.consumption_slot_buffer: dict[str, float] = {}
        self.historical_price_min: float = 999.0
        self.historical_price_max: float = 0.0
        self.consumer_slot_buffers: dict[str, dict[str, float]] = {}
        self.load_profiles: list[dict[str, Any]] = []
        self.day_average_prices: dict[str, float] = {}
        self.consumer_learning: dict[str, dict[str, Any]] = {}
        self.consumer_runs: dict[str, dict[str, Any]] = {}

        self.last_api_success_utc: datetime | None = None
        self.energy_baseline_kwh: float | None = None
        self.energy_baseline_timestamp_utc: datetime | None = None
        self.activity_log: list[dict] = []
        self.consumer_plans: dict = {}
        self.plans_tariff_date: str | None = None
        self.battery_stats: dict[str, dict[str, Any]] = {}  # keyed by date "YYYY-MM-DD"
        self.dirty: bool = False

    async def _async_migrate(self, old_version: int, old_minor_version: int, old_data: dict) -> dict:
        data = dict(old_data or {})

        data.setdefault("price_slots", {})
        data.setdefault("samples", [])
        data.setdefault("booked", [])
        data.setdefault("day_average_prices", {})
        data.setdefault("consumer_learning", {})
        data.setdefault("consumer_runs", {})
        data.setdefault("last_api_success_utc", None)
        data.setdefault("energy_baseline_kwh", None)
        data.setdefault("energy_baseline_timestamp_utc", None)

        if "booked_slots" in data and "booked" not in data:
            bs = data.get("booked_slots") or {}
            if isinstance(bs, dict):
                data["booked"] = list(bs.values())
        if "booked" not in data and "booked_slots" in data:
            data["booked"] = list((data.get("booked_slots") or {}).values())

        ps = data.get("price_slots") or {}
        if isinstance(ps, dict):
            for _k, v in ps.items():
                if not isinstance(v, dict):
                    continue
                v.setdefault("a_total", v.get("total"))
                v.setdefault("b_total", v.get("baseline_total"))
                v.setdefault("a_comp", v.get("components", {}))
                v.setdefault("b_comp", v.get("baseline_components", {}))
        else:
            data["price_slots"] = {}

        dap = data.get("day_average_prices")
        if not isinstance(dap, dict):
            data["day_average_prices"] = {}

        return data

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}

        self.price_slots = dict(data.get("price_slots") or {})
        self.samples = list(data.get("samples") or [])
        self.booked = list(data.get("booked") or [])
        self.base_load_daily = list(data.get("base_load_daily") or [])
        self.consumption_slot_buffer = dict(data.get("consumption_slot_buffer") or {})
        _raw_min = data.get("historical_price_min")
        _raw_max = data.get("historical_price_max")
        self.historical_price_min = float(_raw_min) if _raw_min is not None and float(_raw_min) >= 0.15 else 999.0
        self.historical_price_max = float(_raw_max) if _raw_max is not None and float(_raw_max) > 0 else 0.0
        self.consumer_slot_buffers = dict(data.get("consumer_slot_buffers") or {})
        self.load_profiles = list(data.get("load_profiles") or [])
        self.day_average_prices = {
            str(k): float(v)
            for k, v in (data.get("day_average_prices") or {}).items()
            if isinstance(v, (int, float))
        }
        self.activity_log = list(data.get("activity_log") or [])
        self.consumer_plans = dict(data.get("consumer_plans") or {})
        self.plans_tariff_date = data.get("plans_tariff_date")
        self.consumer_learning = {
            str(k): dict(v)
            for k, v in (data.get("consumer_learning") or {}).items()
            if isinstance(v, dict)
        }
        self.battery_stats = {
            str(k): dict(v)
            for k, v in (data.get("battery_stats") or {}).items()
            if isinstance(v, dict)
        }
        self.consumer_runs = {
            str(k): dict(v)
            for k, v in (data.get("consumer_runs") or {}).items()
            if isinstance(v, dict)
        }

        ts = data.get("last_api_success_utc")
        if isinstance(ts, str):
            dt = dt_util.parse_datetime(ts)
            self.last_api_success_utc = dt_util.as_utc(dt) if dt else None
        else:
            self.last_api_success_utc = None

        eb = data.get("energy_baseline_kwh")
        self.energy_baseline_kwh = float(eb) if isinstance(eb, (int, float)) else None

        tsb = data.get("energy_baseline_timestamp_utc")
        if isinstance(tsb, str):
            dtb = dt_util.parse_datetime(tsb)
            self.energy_baseline_timestamp_utc = dt_util.as_utc(dtb) if dtb else None
        else:
            self.energy_baseline_timestamp_utc = None

        self.dirty = False

    async def async_save(self) -> None:
        await self._store.async_save(self._as_dict())
        self.dirty = False

    def _as_dict(self) -> dict[str, Any]:
        return {
            "price_slots": self.price_slots,
            "samples": self.samples,
            "booked": self.booked,
            "base_load_daily": self.base_load_daily,
            "consumption_slot_buffer": self.consumption_slot_buffer,
            "historical_price_min": self.historical_price_min,
            "historical_price_max": self.historical_price_max,
            "consumer_slot_buffers": self.consumer_slot_buffers,
            "load_profiles": self.load_profiles,
            "day_average_prices": self.day_average_prices,
            "activity_log": self.activity_log,
            "consumer_plans": self.consumer_plans,
            "plans_tariff_date": self.plans_tariff_date,
            "consumer_learning": self.consumer_learning,
            "consumer_runs": self.consumer_runs,
            "last_api_success_utc": self.last_api_success_utc.isoformat() if self.last_api_success_utc else None,
            "energy_baseline_kwh": self.energy_baseline_kwh,
            "energy_baseline_timestamp_utc": self.energy_baseline_timestamp_utc.isoformat() if self.energy_baseline_timestamp_utc else None,
            "battery_stats": self.battery_stats,
        }

    def set_last_api_success(self, when_utc: datetime) -> None:
        self.last_api_success_utc = dt_util.as_utc(when_utc)
        self.dirty = True

    def set_day_average_price(self, local_day: date, avg_price_chf_per_kwh: float) -> None:
        key = local_day.isoformat()
        value = float(avg_price_chf_per_kwh)
        if self.day_average_prices.get(key) != value:
            self.day_average_prices[key] = value
            self.dirty = True

    def trim_day_average_prices(self, keep_days: int = 400) -> None:
        cutoff = dt_util.now().date() - timedelta(days=keep_days)
        before = len(self.day_average_prices)
        self.day_average_prices = {
            k: v for k, v in self.day_average_prices.items() if k >= cutoff.isoformat()
        }
        if len(self.day_average_prices) != before:
            self.dirty = True

    def get_year_day_average_prices(self, reference_local_day: date | None = None) -> list[float]:
        ref = reference_local_day or dt_util.now().date()
        cutoff = ref - timedelta(days=365)
        values: list[float] = []
        for key, value in self.day_average_prices.items():
            try:
                day = date.fromisoformat(str(key))
            except ValueError:
                continue
            if cutoff <= day <= ref and isinstance(value, (int, float)):
                values.append(float(value))
        return values

    @staticmethod
    def _work_total_from_components(comps: dict[str, float] | None) -> float | None:
        if not comps:
            return None
        total = 0.0
        found = False
        for key in IMPORT_ALLIN_COMPONENTS:
            value = comps.get(key)
            if isinstance(value, (int, float)):
                total += float(value)
                found = True
        return float(total) if found and total > 0 else None

    @classmethod
    def _total_from_components(cls, comps: dict[str, float] | None) -> float | None:
        if not comps:
            return None
        work_total = cls._work_total_from_components(comps)
        if isinstance(work_total, (int, float)) and work_total > 0:
            return float(work_total)
        for key in _FALLBACK_TOTAL_KEYS:
            value = comps.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                return float(value)
        return None

    @staticmethod
    def sum_components(comps: dict[str, float] | None, keys: tuple[str, ...]) -> float:
        if not comps:
            return 0.0
        total = 0.0
        for key in keys:
            v = comps.get(key)
            if isinstance(v, (int, float)):
                total += float(v)
        return total

    @classmethod
    def all_in_from_components(cls, comps: dict[str, float] | None) -> float:
        if not comps:
            return 0.0
        work_total = cls._work_total_from_components(comps)
        if isinstance(work_total, (int, float)) and work_total > 0:
            return float(work_total)
        fallback = cls._total_from_components(comps)
        return float(fallback) if isinstance(fallback, (int, float)) and fallback > 0 else 0.0

    @staticmethod
    def _normalize_components(comps: dict[str, float] | None) -> dict[str, float]:
        if not comps:
            return {}
        return {str(k): float(v) for k, v in comps.items() if isinstance(v, (int, float))}

    def set_price_slot(
        self,
        start_utc: datetime,
        *,
        dyn_components_chf_per_kwh: dict[str, float],
        base_components_chf_per_kwh: dict[str, float] | None = None,
    ) -> None:
        start_utc = dt_util.as_utc(start_utc)
        key = start_utc.isoformat()

        a_comp = self._normalize_components(dyn_components_chf_per_kwh)
        b_comp = self._normalize_components(base_components_chf_per_kwh)

        a_total = self._total_from_components(a_comp)
        b_total = self._total_from_components(b_comp)

        self.price_slots[key] = {
            "a_total": float(a_total) if isinstance(a_total, (int, float)) else None,
            "b_total": float(b_total) if isinstance(b_total, (int, float)) else None,
            "a_comp": a_comp,
            "b_comp": b_comp,
        }
        self.dirty = True

    def get_price_totals(self, start_utc: datetime) -> tuple[float | None, float | None]:
        key = dt_util.as_utc(start_utc).isoformat()
        slot = self.price_slots.get(key) or {}
        a = slot.get("a_total")
        b = slot.get("b_total")
        return (
            float(a) if isinstance(a, (int, float)) else None,
            float(b) if isinstance(b, (int, float)) else None,
        )

    def get_price_components(self, start_utc: datetime) -> tuple[dict[str, float] | None, dict[str, float] | None]:
        key = dt_util.as_utc(start_utc).isoformat()
        slot = self.price_slots.get(key) or {}
        a = slot.get("a_comp")
        b = slot.get("b_comp")
        return (a if isinstance(a, dict) else None, b if isinstance(b, dict) else None)

    def trim_price_slots(self, keep_days: int = 7) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=keep_days)
        cutoff_iso = cutoff.isoformat()
        before = len(self.price_slots)
        self.price_slots = {k: v for k, v in self.price_slots.items() if k >= cutoff_iso}
        if len(self.price_slots) != before:
            self.dirty = True

    def reset_energy_baseline(self, ts_utc: datetime, kwh_total: float, *, clear_booked: bool = True) -> None:
        ts_utc = dt_util.as_utc(ts_utc)
        self.energy_baseline_kwh = float(kwh_total)
        self.energy_baseline_timestamp_utc = ts_utc
        self.samples = [{"ts": ts_utc.timestamp(), "kwh": float(kwh_total)}]
        if clear_booked:
            self.booked = []
        self.dirty = True

    def add_sample(self, ts_utc: datetime, kwh_total: float) -> bool:
        ts_utc = dt_util.as_utc(ts_utc)
        if not isinstance(kwh_total, (int, float)):
            return False
        kwh_total = float(kwh_total)

        if self.energy_baseline_kwh is None or self.energy_baseline_timestamp_utc is None:
            self.energy_baseline_kwh = kwh_total
            self.energy_baseline_timestamp_utc = ts_utc

        epoch = ts_utc.timestamp()
        if self.samples and abs(self.samples[-1].get("ts", 0.0) - epoch) < 1e-6:
            return False

        self.samples.append({"ts": epoch, "kwh": kwh_total})
        self._trim_samples(keep_days=14)
        self.dirty = True
        return True

    def _trim_samples(self, keep_days: int = 14) -> None:
        cutoff = (dt_util.utcnow() - timedelta(days=keep_days)).timestamp()
        self.samples = [s for s in self.samples if float(s.get("ts", 0)) >= cutoff]

    @staticmethod
    def _slot_start_utc(ts_utc: datetime) -> datetime:
        ts_utc = dt_util.as_utc(ts_utc)
        minute = (ts_utc.minute // 15) * 15
        return ts_utc.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def _cost_breakdown(kwh: float, comps: dict[str, float] | None) -> dict[str, float]:
        out: dict[str, float] = {}
        if not comps or kwh <= 0:
            return out
        for key, value in comps.items():
            if isinstance(value, (int, float)):
                out[str(key)] = float(kwh) * float(value)
        return out

    @staticmethod
    def _diff_breakdown(base: dict[str, float], dyn: dict[str, float]) -> dict[str, float]:
        keys = set(base) | set(dyn)
        return {key: float(base.get(key, 0.0)) - float(dyn.get(key, 0.0)) for key in keys}

    def finalize_due_slots(self, now_utc: datetime) -> int:
        now_utc = dt_util.as_utc(now_utc)
        cutoff = now_utc - timedelta(minutes=1)

        if len(self.samples) < 2:
            return 0

        last_booked_start: datetime | None = None
        if self.booked:
            dtp = dt_util.parse_datetime(str(self.booked[-1].get("start", "")))
            last_booked_start = dt_util.as_utc(dtp) if dtp else None

        end_slot = self._slot_start_utc(cutoff)

        sample_points: list[tuple[datetime, float]] = []
        for s in self.samples:
            try:
                dtp = dt_util.as_utc(datetime.fromtimestamp(float(s["ts"])))
                sample_points.append((dtp, float(s["kwh"])))
            except Exception:
                continue
        sample_points.sort(key=lambda x: x[0])

        def kwh_at(t: datetime) -> float | None:
            prev = None
            for dtp, kwh in sample_points:
                if dtp <= t:
                    prev = kwh
                else:
                    break
            return prev

        first_allowed_start = self._slot_start_utc(sample_points[0][0])
        if self.energy_baseline_timestamp_utc is not None:
            first_allowed_start = max(first_allowed_start, self._slot_start_utc(self.energy_baseline_timestamp_utc))
        cursor = (last_booked_start + timedelta(minutes=15)) if last_booked_start else first_allowed_start

        newly = 0
        while cursor < end_slot:
            slot_end = cursor + timedelta(minutes=15)
            if slot_end > cutoff:
                break

            kwh_start = kwh_at(cursor)
            kwh_end = kwh_at(slot_end)

            if kwh_start is None or kwh_end is None:
                self._append_booked(cursor, 0.0, 0.0, 0.0, 0.0, "missing_samples")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            delta = float(kwh_end - kwh_start)
            if delta < 0:
                self._append_booked(cursor, 0.0, 0.0, 0.0, 0.0, "invalid")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            a_total, b_total = self.get_price_totals(cursor)
            a_comp, b_comp = self.get_price_components(cursor)
            if a_total is None or a_total <= 0:
                self._append_booked(cursor, delta, 0.0, 0.0, 0.0, "unpriced")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            dyn_breakdown = self._cost_breakdown(delta, a_comp)
            base_breakdown = self._cost_breakdown(delta, b_comp)
            sav_breakdown = self._diff_breakdown(base_breakdown, dyn_breakdown)

            dyn_chf = self.all_in_from_components(dyn_breakdown)
            base_chf = self.all_in_from_components(base_breakdown)
            sav = base_chf - dyn_chf if base_chf > 0 else 0.0

            self._append_booked(
                cursor,
                delta,
                dyn_chf,
                base_chf,
                sav,
                "ok",
                dyn_components=dyn_breakdown,
                base_components=base_breakdown,
                savings_components=sav_breakdown,
            )
            newly += 1
            cursor += timedelta(minutes=15)

        self._trim_booked(keep_days=400)
        if newly:
            self.dirty = True
        return newly

    def _append_booked(
        self,
        start_utc: datetime,
        kwh: float,
        dyn_chf: float,
        base_chf: float,
        sav: float,
        status: str,
        *,
        dyn_components: dict[str, float] | None = None,
        base_components: dict[str, float] | None = None,
        savings_components: dict[str, float] | None = None,
    ) -> None:
        self.booked.append(
            {
                "start": dt_util.as_utc(start_utc).isoformat(),
                "kwh": float(kwh),
                "dyn_chf": float(dyn_chf),
                "base_chf": float(base_chf),
                "savings_chf": float(sav),
                "status": str(status),
                "dyn_components": self._normalize_components(dyn_components),
                "base_components": self._normalize_components(base_components),
                "savings_components": self._normalize_components(savings_components),
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

    def _sum_between(self, start_local: datetime, end_local: datetime) -> tuple[float, float, float]:
        start_utc = dt_util.as_utc(start_local)
        end_utc = dt_util.as_utc(end_local)

        dyn = base = sav = 0.0
        for b in self.booked:
            dtp = dt_util.parse_datetime(str(b.get("start", "")))
            if dtp is None:
                continue
            s_utc = dt_util.as_utc(dtp)
            if not (start_utc <= s_utc < end_utc):
                continue
            try:
                dyn += float(b.get("dyn_chf", 0.0))
                base += float(b.get("base_chf", 0.0))
                sav += float(b.get("savings_chf", 0.0))
            except Exception:
                continue
        return dyn, base, sav

    def compute_today_totals(self) -> tuple[float, float, float]:
        now = dt_util.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return self._sum_between(start, end)

    def compute_week_totals(self) -> tuple[float, float, float]:
        now = dt_util.now()
        start = (now - timedelta(days=now.isoweekday() - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        return self._sum_between(start, end)

    def compute_month_totals(self) -> tuple[float, float, float]:
        now = dt_util.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return self._sum_between(start, end)

    def compute_year_totals(self) -> tuple[float, float, float]:
        now = dt_util.now()
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
        return self._sum_between(start, end)

    def _breakdown_between(self, start_local: datetime, end_local: datetime) -> dict[str, dict[str, float]]:
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

            dyn_comp = b.get("dyn_components") if isinstance(b.get("dyn_components"), dict) else {}
            base_comp = b.get("base_components") if isinstance(b.get("base_components"), dict) else {}
            sav_comp = b.get("savings_components") if isinstance(b.get("savings_components"), dict) else {}

            if dyn_comp or base_comp or sav_comp:
                for bucket_name, source in (("dyn", dyn_comp), ("base", base_comp), ("sav", sav_comp)):
                    for key, value in source.items():
                        if isinstance(value, (int, float)):
                            out[bucket_name][str(key)] = out[bucket_name].get(str(key), 0.0) + float(value)
                continue

            try:
                dyn = float(b.get("dyn_chf", 0.0))
                base = float(b.get("base_chf", 0.0))
                sav = float(b.get("savings_chf", 0.0))
            except Exception:
                continue

            out["dyn"]["integrated"] = out["dyn"].get("integrated", 0.0) + dyn
            out["base"]["integrated"] = out["base"].get("integrated", 0.0) + base
            out["sav"]["integrated"] = out["sav"].get("integrated", 0.0) + sav

        return out

    def compute_today_breakdown(self) -> dict[str, dict[str, float]]:
        now = dt_util.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return self._breakdown_between(start, end)

    def compute_week_breakdown(self) -> dict[str, dict[str, float]]:
        now = dt_util.now()
        start = (now - timedelta(days=now.isoweekday() - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        return self._breakdown_between(start, end)

    def compute_month_breakdown(self) -> dict[str, dict[str, float]]:
        now = dt_util.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return self._breakdown_between(start, end)

    def compute_year_breakdown(self) -> dict[str, dict[str, float]]:
        now = dt_util.now()
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
        return self._breakdown_between(start, end)


    def update_historical_prices(self, prices: list, min_valid: float = 0.05, max_valid: float = 1.00) -> None:
        changed = False
        for p in prices:
            if isinstance(p, (int, float)) and min_valid <= float(p) <= max_valid:
                if float(p) < self.historical_price_min:
                    self.historical_price_min = round(float(p), 6)
                    changed = True
                if float(p) > self.historical_price_max:
                    self.historical_price_max = round(float(p), 6)
                    changed = True
        if changed:
            self.dirty = True

    def log_activity(self, icon: str, message: str) -> None:
        from homeassistant.util import dt as dt_util
        entry = {
            "time": dt_util.now().isoformat(),
            "icon": icon,
            "msg": message,
        }
        self.activity_log.insert(0, entry)
        self.activity_log = self.activity_log[:30]  # keep last 30
        self.dirty = True

    def record_battery_event(self, event_type: str, kwh: float = 0.0, cost_chf: float = 0.0, avg_price: float = 0.0, case: str = "") -> None:
        """Record a battery management event for daily statistics.

        event_type: 'grid_charge', 'hold', 'case'
        """
        from homeassistant.util import dt as dt_util
        today = dt_util.now().strftime("%Y-%m-%d")
        if today not in self.battery_stats:
            self.battery_stats[today] = {
                "grid_charge_kwh": 0.0,
                "grid_charge_cost_chf": 0.0,
                "grid_charge_avg_price": 0.0,
                "hold_minutes": 0,
                "hold_saved_kwh": 0.0,
                "case": "",
                "events": [],
            }
        stats = self.battery_stats[today]

        if event_type == "grid_charge":
            stats["grid_charge_kwh"] = round(stats["grid_charge_kwh"] + kwh, 2)
            stats["grid_charge_cost_chf"] = round(stats["grid_charge_cost_chf"] + cost_chf, 4)
            if kwh > 0:
                total_kwh = stats["grid_charge_kwh"]
                total_cost = stats["grid_charge_cost_chf"]
                stats["grid_charge_avg_price"] = round(total_cost / total_kwh, 4) if total_kwh > 0 else 0.0
        elif event_type == "hold":
            stats["hold_minutes"] += 5  # each tick = 5 min
            stats["hold_saved_kwh"] = round(stats["hold_saved_kwh"] + kwh, 2)
        elif event_type == "case":
            stats["case"] = case

        ts = dt_util.now().strftime("%H:%M")
        stats["events"].append(f"{ts} {event_type}")
        stats["events"] = stats["events"][-50:]  # keep last 50

        # Cleanup: keep only last 30 days
        cutoff = (dt_util.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        self.battery_stats = {k: v for k, v in self.battery_stats.items() if k >= cutoff}
        self.dirty = True

    def get_battery_stats_summary(self) -> dict:
        """Return summary of battery stats for the last 30 days."""
        if not self.battery_stats:
            return {"days": 0, "total_grid_kwh": 0.0, "total_grid_cost": 0.0, "avg_price": 0.0, "total_hold_min": 0}
        total_kwh = sum(d.get("grid_charge_kwh", 0) for d in self.battery_stats.values())
        total_cost = sum(d.get("grid_charge_cost_chf", 0) for d in self.battery_stats.values())
        total_hold = sum(d.get("hold_minutes", 0) for d in self.battery_stats.values())
        return {
            "days": len(self.battery_stats),
            "total_grid_kwh": round(total_kwh, 1),
            "total_grid_cost": round(total_cost, 2),
            "avg_price": round(total_cost / total_kwh, 4) if total_kwh > 0 else 0.0,
            "total_hold_min": total_hold,
        }

    def record_consumer_slot(self, entity_id: str, slot_start_iso: str, cumulative_kwh: float) -> None:
        if entity_id not in self.consumer_slot_buffers:
            self.consumer_slot_buffers[entity_id] = {}
        self.consumer_slot_buffers[entity_id][slot_start_iso] = round(cumulative_kwh, 4)
        self.dirty = True

    def record_consumption_slot(self, slot_start_iso: str, cumulative_kwh: float) -> None:
        """Record cumulative consumption reading for a 30-min slot."""
        self.consumption_slot_buffer[slot_start_iso] = round(cumulative_kwh, 4)
        self.dirty = True

    def compile_load_profile(self, date_str: str, season: str) -> dict[str, Any] | None:
        """Compile 48-slot profile from buffer. Consumer consumption from measurement entity diffs."""
        sorted_keys = sorted(self.consumption_slot_buffer.keys())
        if len(sorted_keys) < 2:
            return None

        slots = []
        for i in range(len(sorted_keys) - 1):
            current_key = sorted_keys[i]
            next_key = sorted_keys[i + 1]
            current_val = self.consumption_slot_buffer[current_key]
            next_val = self.consumption_slot_buffer[next_key]
            delta = max(0.0, next_val - current_val)

            # Sum consumer measurement diffs for this slot
            consumer_kwh = 0.0
            for entity_id, buf in self.consumer_slot_buffers.items():
                c_cur = buf.get(current_key)
                c_nxt = buf.get(next_key)
                if c_cur is not None and c_nxt is not None:
                    consumer_kwh += max(0.0, c_nxt - c_cur)

            consumer_kwh = min(consumer_kwh, delta)
            base_kwh = max(0.0, delta - consumer_kwh)
            # Compute slot index (0-47) from time
            try:
                slot_dt = dt_util.parse_datetime(current_key)
                if slot_dt:
                    local_dt = dt_util.as_local(slot_dt)
                    slot_index = local_dt.hour * 2 + (1 if local_dt.minute >= 30 else 0)
                else:
                    slot_index = i
            except Exception:
                slot_index = i
            slots.append({
                "start": current_key,
                "slot_index": slot_index,
                "total_kwh": round(delta, 4),
                "consumer_kwh": round(consumer_kwh, 4),
                "base_kwh": round(base_kwh, 4),
            })

        profile = {
            "date": date_str,
            "season": season,
            "slots": slots,
            "total_kwh": round(sum(s["total_kwh"] for s in slots), 3),
            "consumer_kwh": round(sum(s["consumer_kwh"] for s in slots), 3),
            "base_kwh": round(sum(s["base_kwh"] for s in slots), 3),
        }

        self.load_profiles = [p for p in self.load_profiles if p.get("date") != date_str]
        self.load_profiles.append(profile)
        self.dirty = True
        return profile

    def clear_consumption_slot_buffer(self) -> None:
        self.consumption_slot_buffer = {}
        self.consumer_slot_buffers = {}
        self.dirty = True

    def get_seasonal_load_profile(self, season: str | None = None) -> dict[int, float]:
        """Get average base load per 30-min slot index (0-47) for a season.

        Returns dict {slot_index: avg_base_kwh}.
        If season is None, averages across all seasons.
        """
        from collections import defaultdict
        slot_totals: dict[int, list[float]] = defaultdict(list)

        for profile in self.load_profiles:
            if season and profile.get("season") != season:
                continue
            for slot in profile.get("slots", []):
                idx = slot.get("slot_index")
                if idx is None:
                    try:
                        slot_dt = dt_util.parse_datetime(slot.get("start", ""))
                        if slot_dt:
                            local_dt = dt_util.as_local(slot_dt)
                            idx = local_dt.hour * 2 + (1 if local_dt.minute >= 30 else 0)
                    except Exception:
                        continue
                if idx is None:
                    continue
                base = slot.get("base_kwh")
                if isinstance(base, (int, float)):
                    slot_totals[int(idx)].append(float(base))

        return {
            idx: round(sum(vals) / len(vals), 4)
            for idx, vals in slot_totals.items()
            if vals
        }

    def add_base_load_daily(
        self, date_str: str, emma_kwh: float, consumer_kwh: float,
        base_load_kwh: float, season: str,
    ) -> None:
        self.base_load_daily = [r for r in self.base_load_daily if r.get("date") != date_str]
        self.base_load_daily.append({
            "date": date_str,
            "emma_kwh": round(emma_kwh, 3),
            "consumer_kwh": round(consumer_kwh, 3),
            "base_load_kwh": round(base_load_kwh, 3),
            "season": season,
        })
        self.dirty = True

    def compute_consumer_kwh_today(self) -> float:
        now = dt_util.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        start_utc = dt_util.as_utc(start)
        end_utc = dt_util.as_utc(end)
        total = 0.0
        for b in self.booked:
            dtp = dt_util.parse_datetime(str(b.get("start", "")))
            if dtp is None:
                continue
            s_utc = dt_util.as_utc(dtp)
            if start_utc <= s_utc < end_utc:
                try:
                    total += float(b.get("kwh", 0.0))
                except Exception:
                    continue
        return total

    def get_base_load_season_averages(self) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for rec in self.base_load_daily:
            season = str(rec.get("season", ""))
            kwh = rec.get("base_load_kwh")
            if not season or not isinstance(kwh, (int, float)):
                continue
            buckets.setdefault(season, []).append(float(kwh))
        return {
            season: round(sum(vals) / len(vals), 3)
            for season, vals in buckets.items()
            if vals
        }

    def _finalize_consumer_run(self, consumer_id: str) -> None:
        run = self.consumer_runs.get(str(consumer_id))
        if not isinstance(run, dict) or not run.get("active"):
            return
        start_kwh = float(run.get("start_kwh", run.get("last_kwh", 0.0)) or 0.0)
        last_kwh = float(run.get("last_kwh", start_kwh) or start_kwh)
        delta = last_kwh - start_kwh
        if delta <= 0.01:
            self.consumer_runs.pop(str(consumer_id), None)
            self.dirty = True
            return
        start_ts = dt_util.parse_datetime(str(run.get("start_ts", "")))
        last_ts = dt_util.parse_datetime(str(run.get("last_ts", "")))
        duration_min = 0.0
        if isinstance(start_ts, datetime) and isinstance(last_ts, datetime):
            duration_min = max(0.0, (dt_util.as_utc(last_ts) - dt_util.as_utc(start_ts)).total_seconds() / 60.0)
        learned = dict(self.consumer_learning.get(str(consumer_id), {}) or {})
        samples = int(learned.get("sample_count", 0) or 0) + 1
        prev_energy = float(learned.get("avg_energy_kwh", 0.0) or 0.0)
        prev_duration = float(learned.get("avg_duration_minutes", 0.0) or 0.0)
        prev_power = float(learned.get("avg_power_kw", 0.0) or 0.0)
        avg_energy = ((prev_energy * (samples - 1)) + delta) / samples
        avg_duration = ((prev_duration * (samples - 1)) + duration_min) / samples if duration_min > 0 else prev_duration
        run_power = (delta / (duration_min / 60.0)) if duration_min > 0 else 0.0
        avg_power = ((prev_power * (samples - 1)) + run_power) / samples if run_power > 0 else prev_power
        learned.update(
            {
                "sample_count": samples,
                "avg_energy_kwh": avg_energy,
                "avg_duration_minutes": avg_duration,
                "avg_power_kw": avg_power,
                "last_run_end_utc": dt_util.as_utc(last_ts).isoformat() if isinstance(last_ts, datetime) else None,
            }
        )
        self.consumer_learning[str(consumer_id)] = learned
        self.consumer_runs.pop(str(consumer_id), None)
        self.dirty = True

    def add_consumer_sample(self, consumer_id: str, ts_utc: datetime, kwh_total: float) -> bool:
        ts_utc = dt_util.as_utc(ts_utc)
        if not isinstance(kwh_total, (int, float)):
            return False
        key = str(consumer_id)
        run = dict(self.consumer_runs.get(key) or {})
        if not run:
            self.consumer_runs[key] = {
                "active": True,
                "start_kwh": float(kwh_total),
                "last_kwh": float(kwh_total),
                "start_ts": ts_utc.isoformat(),
                "last_ts": ts_utc.isoformat(),
            }
            self.dirty = True
            return True
        last_kwh = float(run.get("last_kwh", kwh_total) or kwh_total)
        if float(kwh_total) + 1e-9 < last_kwh:
            self._finalize_consumer_run(key)
            self.consumer_runs[key] = {
                "active": True,
                "start_kwh": float(kwh_total),
                "last_kwh": float(kwh_total),
                "start_ts": ts_utc.isoformat(),
                "last_ts": ts_utc.isoformat(),
            }
            self.dirty = True
            return True
        if abs(float(kwh_total) - last_kwh) > 1e-6:
            run["active"] = True
            run["last_kwh"] = float(kwh_total)
            run["last_ts"] = ts_utc.isoformat()
            self.consumer_runs[key] = run
            self.dirty = True
            return True
        # unchanged reading: if stale for >20 min, finalize
        last_ts = dt_util.parse_datetime(str(run.get("last_ts", "")))
        if isinstance(last_ts, datetime) and (ts_utc - dt_util.as_utc(last_ts)).total_seconds() >= 20 * 60:
            self._finalize_consumer_run(key)
            return True
        return False


    def record_consumer_efficiency(
        self,
        consumer_id: str,
        *,
        energy_kwh: float,
        source: str,
        actual_price_chf_per_kwh: float,
        baseline_price_chf_per_kwh: float,
    ) -> None:
        """Record efficiency stats for a consumer run."""
        key = str(consumer_id)
        learned = dict(self.consumer_learning.get(key, {}) or {})
        energy_kwh = max(0.0, float(energy_kwh or 0))
        actual_price = max(0.0, float(actual_price_chf_per_kwh or 0))
        baseline_price = max(0.0, float(baseline_price_chf_per_kwh or 0))
        savings = max(0.0, energy_kwh * (baseline_price - actual_price))
        if str(source).lower() == "pv":
            learned["pv_runs"] = int(learned.get("pv_runs", 0) or 0) + 1
            learned["pv_kwh"] = float(learned.get("pv_kwh", 0.0) or 0.0) + energy_kwh
        else:
            learned["grid_runs"] = int(learned.get("grid_runs", 0) or 0) + 1
            learned["grid_kwh"] = float(learned.get("grid_kwh", 0.0) or 0.0) + energy_kwh
        learned["total_savings_chf"] = float(learned.get("total_savings_chf", 0.0) or 0.0) + savings
        self.consumer_learning[key] = learned
        self.dirty = True

    def get_consumer_learning(self, consumer_id: str) -> dict[str, Any]:
        learned = dict(self.consumer_learning.get(str(consumer_id), {}) or {})
        return {
            "sample_count": int(learned.get("sample_count", 0) or 0),
            "avg_energy_kwh": learned.get("avg_energy_kwh"),
            "avg_duration_minutes": learned.get("avg_duration_minutes"),
            "avg_power_kw": learned.get("avg_power_kw"),
            "last_run_end_utc": learned.get("last_run_end_utc"),
        }

    def set_consumer_last_run(self, consumer_id: str, ts_utc: datetime) -> None:
        ts_utc = dt_util.as_utc(ts_utc)
        key = str(consumer_id)
        learned = dict(self.consumer_learning.get(key, {}) or {})
        learned.setdefault("sample_count", int(learned.get("sample_count", 0) or 0))
        learned.setdefault("avg_energy_kwh", learned.get("avg_energy_kwh"))
        learned.setdefault("avg_duration_minutes", learned.get("avg_duration_minutes"))
        learned.setdefault("avg_power_kw", learned.get("avg_power_kw"))
        learned["last_run_end_utc"] = ts_utc.isoformat()
        self.consumer_learning[key] = learned
        self.dirty = True

