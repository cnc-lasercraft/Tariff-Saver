"""Lightweight persistent storage for Tariff Saver (samples + booked slots + price slots)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util


@dataclass
class BookedSlot:
    """A finalized 15-min slot with consumption and costs."""
    kwh: float
    dyn_chf: float
    base_chf: float
    status: str  # "ok" | "unpriced" | "invalid" | "missing_samples"


class TariffSaverStore:
    """Persists recent energy samples, price slots and finalized 15-min slots."""

    # IMPORTANT:
    # Do NOT bump version unless you also provide a storage migration.
    # We keep the existing version and load the new field opportunistically.
    STORAGE_VERSION = 2

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._store = Store(
            hass,
            self.STORAGE_VERSION,
            f"tariff_saver_{entry_id}.json",
        )

        # in-memory
        self.samples: list[tuple[datetime, float]] = []  # (utc_dt, kwh_total)
        self.booked_slots: dict[datetime, BookedSlot] = {}  # slot_end (local tz aligned) -> booked

        # price slots (UTC start -> prices)
        # key: slot_start_utc (datetime, tz-aware UTC)
        # val: {"dyn": float, "base": float}
        self.price_slots: dict[datetime, dict[str, float]] = {}

        # NEW: last successful API fetch timestamp (UTC)
        self.last_api_success_utc: datetime | None = None

        # runtime flags
        self.dirty: bool = False
        self.last_sample_ts: datetime | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return

        # ---- last api success (optional) ----
        self.last_api_success_utc = None
        try:
            ts_str = data.get("last_api_success_utc")
            if isinstance(ts_str, str) and ts_str:
                ts = dt_util.parse_datetime(ts_str)
                if ts is not None:
                    self.last_api_success_utc = dt_util.as_utc(ts)
        except Exception:
            self.last_api_success_utc = None

        # ---- samples ----
        self.samples = []
        for ts_str, kwh in data.get("samples", []):
            try:
                ts = dt_util.parse_datetime(ts_str)
                if ts is None:
                    continue
                # stored as UTC
                if ts.tzinfo is None:
                    ts = dt_util.as_utc(ts)
                self.samples.append((ts, float(kwh)))
            except Exception:
                continue

        # ---- booked slots ----
        self.booked_slots = {}
        for end_str, payload in data.get("booked_slots", {}).items():
            try:
                end_dt = dt_util.parse_datetime(end_str)
                if end_dt is None:
                    continue
                # slot_end stored as local time with tz
                if end_dt.tzinfo is None:
                    end_dt = dt_util.as_local(dt_util.as_utc(end_dt))
                self.booked_slots[end_dt] = BookedSlot(
                    kwh=float(payload.get("kwh", 0.0)),
                    dyn_chf=float(payload.get("dyn_chf", 0.0)),
                    base_chf=float(payload.get("base_chf", 0.0)),
                    status=str(payload.get("status", "ok")),
                )
            except Exception:
                continue

        # ---- price slots ----
        self.price_slots = {}
        for start_str, payload in data.get("price_slots", {}).items():
            try:
                start_dt = dt_util.parse_datetime(start_str)
                if start_dt is None:
                    continue
                start_dt = dt_util.as_utc(start_dt)
                dyn = payload.get("dyn")
                base = payload.get("base")
                if isinstance(dyn, (int, float)) and isinstance(base, (int, float)):
                    self.price_slots[start_dt] = {"dyn": float(dyn), "base": float(base)}
            except Exception:
                continue

    async def async_save(self) -> None:
        payload: dict[str, Any] = {
            "last_api_success_utc": self.last_api_success_utc.isoformat() if self.last_api_success_utc else None,
            "samples": [[dt_util.as_utc(ts).isoformat(), kwh] for ts, kwh in self.samples],
            "booked_slots": {
                end.isoformat(): {
                    "kwh": b.kwh,
                    "dyn_chf": b.dyn_chf,
                    "base_chf": b.base_chf,
                    "status": b.status,
                }
                for end, b in self.booked_slots.items()
            },
            "price_slots": {
                dt_util.as_utc(start).isoformat(): {
                    "dyn": p["dyn"],
                    "base": p["base"],
                }
                for start, p in self.price_slots.items()
            },
        }
        await self._store.async_save(payload)
        self.dirty = False

    # ---------- api success ----------
    def set_last_api_success(self, ts_utc: datetime) -> None:
        """Set timestamp of last successful API fetch (UTC)."""
        self.last_api_success_utc = dt_util.as_utc(ts_utc)
        self.dirty = True

    # ---------- samples ----------
    def trim_samples(self, keep_hours: int = 48) -> None:
        cutoff = dt_util.utcnow() - timedelta(hours=keep_hours)
        self.samples = [(ts, kwh) for ts, kwh in self.samples if ts >= cutoff]

    def add_sample(self, ts_utc: datetime, kwh_total: float, min_interval_s: int = 10) -> bool:
        """Add sample if time since last saved sample >= min_interval_s. Returns True if stored."""
        ts_utc = dt_util.as_utc(ts_utc)
        if self.last_sample_ts and (ts_utc - self.last_sample_ts).total_seconds() < min_interval_s:
            return False

        self.samples.append((ts_utc, kwh_total))
        self.last_sample_ts = ts_utc
        self.dirty = True
        return True

    def _last_kwh_before(self, t_utc: datetime) -> float | None:
        """Return last kWh sample <= t_utc."""
        t_utc = dt_util.as_utc(t_utc)
        for ts, kwh in reversed(self.samples):
            if ts <= t_utc:
                return kwh
        return None

    def delta_kwh(self, start_local: datetime, end_local: datetime) -> float | None:
        """Compute kWh delta between local times [start, end]."""
        start_utc = dt_util.as_utc(start_local)
        end_utc = dt_util.as_utc(end_local)

        kwh_end = self._last_kwh_before(end_utc)
        kwh_start = self._last_kwh_before(start_utc)
        if kwh_end is None or kwh_start is None:
            return None
        return kwh_end - kwh_start

    # ---------- price slots ----------
    def set_price_slot(self, slot_start_utc: datetime, dyn_chf_per_kwh: float, base_chf_per_kwh: float) -> None:
        slot_start_utc = dt_util.as_utc(slot_start_utc)
        self.price_slots[slot_start_utc] = {
            "dyn": float(dyn_chf_per_kwh),
            "base": float(base_chf_per_kwh),
        }
        self.dirty = True

    def get_price_slot(self, slot_start_utc: datetime) -> dict[str, float] | None:
        slot_start_utc = dt_util.as_utc(slot_start_utc)
        return self.price_slots.get(slot_start_utc)

    def trim_price_slots(self, keep_days: int = 3) -> None:
        """Keep only recent price slots (UTC)."""
        cutoff = dt_util.utcnow() - timedelta(days=keep_days)
        self.price_slots = {ts: p for ts, p in self.price_slots.items() if ts >= cutoff}

    # ---------- booked slots ----------
    def is_slot_booked(self, slot_end_local: datetime) -> bool:
        return slot_end_local in self.booked_slots

    def book_slot_ok(self, slot_end_local: datetime, kwh: float, dyn_chf: float, base_chf: float) -> None:
        self.booked_slots[slot_end_local] = BookedSlot(
            kwh=kwh,
            dyn_chf=dyn_chf,
            base_chf=base_chf,
            status="ok",
        )
        self.dirty = True

    def book_slot_status(self, slot_end_local: datetime, status: str) -> None:
        self.booked_slots[slot_end_local] = BookedSlot(
            kwh=0.0,
            dyn_chf=0.0,
            base_chf=0.0,
            status=status,
        )
        self.dirty = True

    def compute_today_totals(self) -> tuple[float, float, float]:
        """Return (dyn_total, base_total, savings) for today's booked OK slots."""
        now_local = dt_util.now()
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        dyn_total = 0.0
        base_total = 0.0

        for end_local, b in self.booked_slots.items():
            if b.status != "ok":
                continue
            if start_local < end_local <= now_local:
                dyn_total += b.dyn_chf
                base_total += b.base_chf

        return dyn_total, base_total, (base_total - dyn_total)

    # ---------- slot finalization ----------
    @staticmethod
    def _floor_15min_utc(ts: datetime) -> datetime:
        ts = dt_util.as_utc(ts)
        minute = (ts.minute // 15) * 15
        return ts.replace(minute=minute, second=0, microsecond=0)

    def finalize_due_slots(self, now_utc: datetime) -> int:
        """Finalize all fully completed 15-min slots up to now.
        Returns number of newly booked slots.
        """
        now_utc = dt_util.as_utc(now_utc)
        slot_end_utc = self._floor_15min_utc(now_utc)  # end boundary of current slot (not included)
        booked = 0

        # Try last 3 hours (handles restarts)
        for i in range(1, 12 + 1):  # 12 * 15min = 3h
            end_utc = slot_end_utc - timedelta(minutes=15 * (i - 1))
            start_utc = end_utc - timedelta(minutes=15)

            end_local = dt_util.as_local(end_utc)
            start_local = dt_util.as_local(start_utc)

            if self.is_slot_booked(end_local):
                continue

            kwh = self.delta_kwh(start_local, end_local)
            if kwh is None:
                self.book_slot_status(end_local, "missing_samples")
                booked += 1
                continue

            p = self.get_price_slot(start_utc)
            if not p:
                self.book_slot_status(end_local, "unpriced")
                booked += 1
                continue

            dyn = p.get("dyn")
            base = p.get("base")
            if (
                not isinstance(dyn, (int, float))
                or not isinstance(base, (int, float))
                or dyn <= 0
                or base <= 0
            ):
                self.book_slot_status(end_local, "invalid")
                booked += 1
                continue

            dyn_chf = float(kwh) * float(dyn)
            base_chf = float(kwh) * float(base)
            self.book_slot_ok(end_local, float(kwh), dyn_chf, base_chf)
            booked += 1

        return booked
