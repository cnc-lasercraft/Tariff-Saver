"""Lightweight persistent storage for Tariff Saver.

Persists:
- price slots (UTC 15-min): component breakdown (CHF/kWh) for active and optional baseline
- energy samples (UTC timestamps of cumulative kWh)
- booked 15-min slots (UTC start) with kWh + computed CHF totals (actual/baseline/savings)
- last API success timestamp

Design goals:
- Backwards compatible (no store version bump).
- Minimal schema: keep dyn/base totals for existing sensors, but also store components for better cost accuracy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util


# Components we treat as "all-in" parts of the price (CHF/kWh).
# The EKZ API may provide some or all of these fields per slot.
IMPORT_ALLIN_COMPONENTS: tuple[str, ...] = (
    "integrated",
    "electricity",
    "grid",
    "regional_fees",
    "metering",
)


class TariffSaverStore:
    """Persists recent energy samples, price slots and finalized 15-min slots."""

    STORAGE_VERSION = 3
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

        self.last_api_success_utc: datetime | None = None
        self.energy_baseline_kwh: float | None = None
        self.energy_baseline_timestamp_utc: datetime | None = None
        self.dirty: bool = False

    async def _async_migrate(self, old_version: int, old_minor_version: int, old_data: dict) -> dict:
        data = dict(old_data or {})

        data.setdefault("price_slots", {})
        data.setdefault("samples", [])
        data.setdefault("booked", [])
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
                v.setdefault("dyn", None)
                v.setdefault("base", None)
                v.setdefault("components", {})
                v.setdefault("baseline_components", {})
                v.setdefault("total", None)
                v.setdefault("baseline_total", None)
                v.setdefault("a_total", v.get("total"))
                v.setdefault("b_total", v.get("baseline_total"))
                v.setdefault("a_comp", v.get("components", {}))
                v.setdefault("b_comp", v.get("baseline_components", {}))
        else:
            data["price_slots"] = {}

        return data

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}

        self.price_slots = dict(data.get("price_slots") or {})
        self.samples = list(data.get("samples") or [])
        self.booked = list(data.get("booked") or [])

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
            "last_api_success_utc": self.last_api_success_utc.isoformat() if self.last_api_success_utc else None,
            "energy_baseline_kwh": self.energy_baseline_kwh,
            "energy_baseline_timestamp_utc": self.energy_baseline_timestamp_utc.isoformat() if self.energy_baseline_timestamp_utc else None,
        }

    def set_last_api_success(self, when_utc: datetime) -> None:
        self.last_api_success_utc = dt_util.as_utc(when_utc)
        self.dirty = True

    @staticmethod
    def _total_from_components(comps: dict[str, float] | None) -> float | None:
        if not comps:
            return None
        integrated = comps.get("integrated")
        if isinstance(integrated, (int, float)) and float(integrated) > 0:
            return float(integrated)
        total = 0.0
        found = False
        for v in comps.values():
            if isinstance(v, (int, float)):
                total += float(v)
                found = True
        return float(total) if found and total > 0 else None

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

    def set_price_slot(
        self,
        start_utc: datetime,
        *,
        dyn_components_chf_per_kwh: dict[str, float],
        base_components_chf_per_kwh: dict[str, float] | None = None,
    ) -> None:
        start_utc = dt_util.as_utc(start_utc)
        key = start_utc.isoformat()

        a_comp = {str(k): float(v) for k, v in (dyn_components_chf_per_kwh or {}).items() if isinstance(v, (int, float))}
        b_comp = (
            {str(k): float(v) for k, v in (base_components_chf_per_kwh or {}).items() if isinstance(v, (int, float))}
            if base_components_chf_per_kwh
            else None
        )

        a_total = self._total_from_components(a_comp)
        b_total = self._total_from_components(b_comp) if b_comp else None

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
            if a_total is None or a_total <= 0:
                self._append_booked(cursor, delta, 0.0, 0.0, 0.0, "unpriced")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            dyn_chf = delta * float(a_total)
            base_chf = delta * float(b_total) if isinstance(b_total, (int, float)) and b_total > 0 else 0.0
            sav = base_chf - dyn_chf if base_chf > 0 else 0.0

            self._append_booked(cursor, delta, dyn_chf, base_chf, sav, "ok")
            newly += 1
            cursor += timedelta(minutes=15)

        self._trim_booked(keep_days=400)
        if newly:
            self.dirty = True
        return newly

    def _append_booked(self, start_utc: datetime, kwh: float, dyn_chf: float, base_chf: float, sav: float, status: str) -> None:
        self.booked.append(
            {
                "start": dt_util.as_utc(start_utc).isoformat(),
                "kwh": float(kwh),
                "dyn_chf": float(dyn_chf),
                "base_chf": float(base_chf),
                "savings_chf": float(sav),
                "status": str(status),
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

            try:
                dyn = float(b.get("dyn_chf", 0.0))
                base = float(b.get("base_chf", 0.0))
                sav = float(b.get("savings_chf", 0.0))
            except Exception:
                continue

            out["dyn"]["electricity"] = out["dyn"].get("electricity", 0.0) + dyn
            out["base"]["electricity"] = out["base"].get("electricity", 0.0) + base
            out["sav"]["electricity"] = out["sav"].get("electricity", 0.0) + sav

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
