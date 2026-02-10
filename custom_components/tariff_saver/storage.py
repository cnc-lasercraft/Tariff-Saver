"""Lightweight persistent storage for Tariff Saver.

Backwards compatible storage.

Supports legacy formats:
- v2: samples as list[[iso_ts, kwh_total], ...]
- v2: booked_slots as dict[slot_start_iso -> {kwh, dyn_chf, base_chf, status, ...}]
- v2: price_slots as dict[slot_start_iso -> {dyn, base}]

Current format (v3+):
- samples: list[{"ts": epoch_seconds, "kwh": float}, ...]
- booked: list[{"start": iso_utc, "kwh": float, "dyn_chf": float, "base_chf": float, "savings_chf": float, "status": str}, ...]
- price_slots: unchanged (iso -> {"dyn": float, "base": float|None})
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util


class TariffSaverStore:
    """Persists recent energy samples, price slots and finalized 15-min slots."""

    STORAGE_VERSION = 3
    STORAGE_KEY = "tariff_saver"

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._store = Store(hass, self.STORAGE_VERSION, f"{self.STORAGE_KEY}.{entry_id}")

        self.price_slots: dict[str, dict[str, float | None]] = {}
        self.samples: list[dict[str, float]] = []
        self.booked: list[dict[str, Any]] = []
        self.last_api_success_utc: datetime | None = None

        self.dirty: bool = False

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        raw_price_slots = data.get("price_slots") or {}
        raw_samples = data.get("samples") or []
        raw_booked = data.get("booked")
        raw_booked_slots = data.get("booked_slots")

        # price_slots: legacy and current are both dict[iso -> {"dyn":..,"base":..}]
        self.price_slots = dict(raw_price_slots) if isinstance(raw_price_slots, dict) else {}

        # samples: accept v3 dicts OR v2 pairs [iso, kwh]
        self.samples = []
        if isinstance(raw_samples, list):
            for item in raw_samples:
                if isinstance(item, dict) and "ts" in item and "kwh" in item:
                    try:
                        self.samples.append({"ts": float(item["ts"]), "kwh": float(item["kwh"])})
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
                    self.samples.append({"ts": dt_util.as_utc(dtp).timestamp(), "kwh": kwh})

        # booked: accept v3 list OR v2 booked_slots dict
        self.booked = []
        if isinstance(raw_booked, list):
            for b in raw_booked:
                if isinstance(b, dict) and "start" in b:
                    self.booked.append(dict(b))
        elif isinstance(raw_booked_slots, dict):
            for start_iso, payload in raw_booked_slots.items():
                if not isinstance(start_iso, str) or not isinstance(payload, dict):
                    continue
                try:
                    kwh = float(payload.get("kwh", 0.0))
                except Exception:
                    kwh = 0.0
                try:
                    dyn = float(payload.get("dyn_chf", payload.get("dyn", 0.0)))
                except Exception:
                    dyn = 0.0
                try:
                    base = float(payload.get("base_chf", payload.get("base", 0.0)))
                except Exception:
                    base = 0.0
                try:
                    sav = float(payload.get("savings_chf", payload.get("sav", 0.0)))
                except Exception:
                    sav = 0.0
                status = str(payload.get("status", "ok" if dyn or base else "unpriced"))
                self.booked.append(
                    {
                        "start": start_iso,
                        "kwh": kwh,
                        "dyn_chf": dyn,
                        "base_chf": base,
                        "savings_chf": sav,
                        "status": status,
                    }
                )
            self.booked.sort(key=lambda x: str(x.get("start", "")))

        ts = data.get("last_api_success_utc")
        if isinstance(ts, str):
            dtp = dt_util.parse_datetime(ts)
            self.last_api_success_utc = dt_util.as_utc(dtp) if dtp else None
        else:
            self.last_api_success_utc = None

        # mark dirty so it gets saved in v3 format
        self.dirty = True

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

    def set_last_api_success(self, when_utc: datetime) -> None:
        self.last_api_success_utc = dt_util.as_utc(when_utc)
        self.dirty = True

    def set_price_slot(self, start_utc: datetime, dyn_chf_per_kwh: float, base_chf_per_kwh: float | None) -> None:
        start_utc = dt_util.as_utc(start_utc)
        key = start_utc.isoformat()
        self.price_slots[key] = {
            "dyn": float(dyn_chf_per_kwh),
            "base": float(base_chf_per_kwh) if base_chf_per_kwh is not None else None,
        }
        self.dirty = True

    def get_price_slot(self, start_utc: datetime) -> tuple[float | None, float | None]:
        key = dt_util.as_utc(start_utc).isoformat()
        slot = self.price_slots.get(key)
        if not slot:
            return None, None
        dyn = slot.get("dyn")
        base = slot.get("base")
        return (
            float(dyn) if isinstance(dyn, (int, float)) else None,
            float(base) if isinstance(base, (int, float)) else None,
        )

    def trim_price_slots(self, keep_days: int = 7) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=keep_days)
        cutoff_iso = cutoff.isoformat()
        before = len(self.price_slots)
        self.price_slots = {k: v for k, v in self.price_slots.items() if k >= cutoff_iso}
        if len(self.price_slots) != before:
            self.dirty = True

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

    @staticmethod
    def _slot_start_utc(ts_utc: datetime) -> datetime:
        ts_utc = dt_util.as_utc(ts_utc)
        minute = (ts_utc.minute // 15) * 15
        return ts_utc.replace(minute=minute, second=0, microsecond=0)

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

    def finalize_due_slots(self, now_utc: datetime) -> int:
        now_utc = dt_util.as_utc(now_utc)
        cutoff = now_utc - timedelta(minutes=1)

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

            dyn_p, base_p = self.get_price_slot(cursor)
            if dyn_p is None or dyn_p <= 0:
                self._append_booked(cursor, delta, 0.0, 0.0, 0.0, "unpriced")
                newly += 1
                cursor += timedelta(minutes=15)
                continue

            dyn_chf = delta * float(dyn_p)
            base_chf = delta * float(base_p) if base_p is not None and base_p > 0 else 0.0
            sav = base_chf - dyn_chf if base_chf > 0 else 0.0

            self._append_booked(cursor, delta, dyn_chf, base_chf, sav, "ok")
            newly += 1
            cursor += timedelta(minutes=15)

        if newly:
            self._trim_booked(keep_days=400)
            self.dirty = True
        return newly

    def _sum_between_local(self, start_local: datetime, end_local: datetime) -> tuple[float, float, float]:
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
        return self._sum_between_local(start, end)

    def compute_week_totals(self) -> tuple[float, float, float]:
        now = dt_util.now()
        start = (now - timedelta(days=now.isoweekday() - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        return self._sum_between_local(start, end)

    def compute_month_totals(self) -> tuple[float, float, float]:
        now = dt_util.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return self._sum_between_local(start, end)

    def compute_year_totals(self) -> tuple[float, float, float]:
        now = dt_util.now()
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
        return self._sum_between_local(start, end)
