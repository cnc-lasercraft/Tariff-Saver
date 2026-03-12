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
    STORAGE_MINOR_VERSION = 0
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
        self.day_average_prices: dict[str, float] = {}
        self.consumer_learning: dict[str, dict[str, Any]] = {}
        self.consumer_last_samples: dict[str, dict[str, Any]] = {}
        self.consumer_active_runs: dict[str, dict[str, Any]] = {}

        self.last_api_success_utc: datetime | None = None
        self.energy_baseline_kwh: float | None = None
        self.energy_baseline_timestamp_utc: datetime | None = None
        self.dirty: bool = False

    async def _async_migrate(self, old_version: int, old_minor_version: int, old_data: dict) -> dict:
        data = dict(old_data or {})

        data.setdefault("price_slots", {})
        data.setdefault("samples", [])
        data.setdefault("booked", [])
        data.setdefault("day_average_prices", {})
        data.setdefault("consumer_learning", {})
        data.setdefault("consumer_last_samples", {})
        data.setdefault("consumer_active_runs", {})
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
        self.day_average_prices = {
            str(k): float(v)
            for k, v in (data.get("day_average_prices") or {}).items()
            if isinstance(v, (int, float))
        }
        self.consumer_learning = {
            str(k): dict(v)
            for k, v in (data.get("consumer_learning") or {}).items()
            if isinstance(v, dict)
        }
        self.consumer_last_samples = {
            str(k): dict(v)
            for k, v in (data.get("consumer_last_samples") or {}).items()
            if isinstance(v, dict)
        }
        self.consumer_active_runs = {
            str(k): dict(v)
            for k, v in (data.get("consumer_active_runs") or {}).items()
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
            "day_average_prices": self.day_average_prices,
            "consumer_learning": self.consumer_learning,
            "consumer_last_samples": self.consumer_last_samples,
            "consumer_active_runs": self.consumer_active_runs,
            "last_api_success_utc": self.last_api_success_utc.isoformat() if self.last_api_success_utc else None,
            "energy_baseline_kwh": self.energy_baseline_kwh,
            "energy_baseline_timestamp_utc": self.energy_baseline_timestamp_utc.isoformat() if self.energy_baseline_timestamp_utc else None,
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
        target_year = (reference_local_day or dt_util.now().date()).year
        values: list[float] = []
        for key, value in self.day_average_prices.items():
            try:
                day = date.fromisoformat(str(key))
            except ValueError:
                continue
            if day.year == target_year and isinstance(value, (int, float)):
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


def get_consumer_learning(self, consumer_id: str) -> dict[str, Any]:
    data = self.consumer_learning.get(str(consumer_id))
    return dict(data) if isinstance(data, dict) else {}

def add_consumer_sample(self, consumer_id: str, ts_utc: datetime, kwh_total: float) -> bool:
    consumer_id = str(consumer_id)
    ts_utc = dt_util.as_utc(ts_utc)
    if not isinstance(kwh_total, (int, float)):
        return False

    kwh_total = float(kwh_total)
    epoch = ts_utc.timestamp()
    last = self.consumer_last_samples.get(consumer_id)
    if isinstance(last, dict):
        prev_ts = float(last.get("ts", 0.0))
        prev_kwh = float(last.get("kwh", 0.0))
        if abs(prev_ts - epoch) < 1e-6:
            return False

        delta = kwh_total - prev_kwh
        gap_seconds = max(0.0, epoch - prev_ts)

        if delta > 0 and gap_seconds <= 900:
            run = self.consumer_active_runs.get(consumer_id)
            if not isinstance(run, dict):
                run = {"start_ts": prev_ts, "end_ts": epoch, "energy_kwh": delta}
            else:
                run["end_ts"] = epoch
                run["energy_kwh"] = float(run.get("energy_kwh", 0.0)) + delta
            self.consumer_active_runs[consumer_id] = run
            self.dirty = True
        else:
            self._finalize_consumer_run(consumer_id)

    self.consumer_last_samples[consumer_id] = {"ts": epoch, "kwh": kwh_total}
    self.dirty = True
    return True

def _finalize_consumer_run(self, consumer_id: str) -> None:
    run = self.consumer_active_runs.pop(str(consumer_id), None)
    if not isinstance(run, dict):
        return

    energy_kwh = float(run.get("energy_kwh", 0.0) or 0.0)
    start_ts = float(run.get("start_ts", 0.0) or 0.0)
    end_ts = float(run.get("end_ts", 0.0) or 0.0)
    duration_minutes = max(0.0, (end_ts - start_ts) / 60.0)

    if energy_kwh <= 0 or duration_minutes <= 0:
        return

    avg_power_kw = energy_kwh / (duration_minutes / 60.0) if duration_minutes > 0 else 0.0
    info = self.consumer_learning.get(str(consumer_id), {})
    sample_count = int(info.get("sample_count", 0) or 0)
    new_count = sample_count + 1

    def _avg(old_value: float, new_value: float) -> float:
        return ((old_value * sample_count) + new_value) / new_count if new_count > 0 else new_value

    updated = {
        "sample_count": new_count,
        "avg_energy_kwh": _avg(float(info.get("avg_energy_kwh", 0.0) or 0.0), energy_kwh),
        "avg_duration_minutes": _avg(float(info.get("avg_duration_minutes", 0.0) or 0.0), duration_minutes),
        "avg_power_kw": _avg(float(info.get("avg_power_kw", 0.0) or 0.0), avg_power_kw),
        "last_run_end_utc": dt_util.as_utc(datetime.fromtimestamp(end_ts)).isoformat(),
    }
    self.consumer_learning[str(consumer_id)] = updated
    self.dirty = True
