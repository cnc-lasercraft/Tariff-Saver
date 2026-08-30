"""Data models for Tariff Saver."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class PriceSlot:
    """Normalized 15-minute tariff slot used by Tariff Saver."""

    start: datetime
    electricity: float
    grid: float = 0.0
    regional_fees: float = 0.0
    integrated: float = 0.0
    components: dict[str, float] = field(default_factory=dict)

    @property
    def electricity_chf_per_kwh(self) -> float:
        return float(self.electricity)

    @property
    def components_chf_per_kwh(self) -> dict[str, float]:
        base = {
            "electricity": float(self.electricity),
            "grid": float(self.grid),
            "regional_fees": float(self.regional_fees),
            "integrated": float(self.integrated),
        }
        for key, value in (self.components or {}).items():
            if isinstance(value, (int, float)):
                base[str(key)] = float(value)
        return base



CONSUMER_KIND_DAILY = "daily_fixed"
CONSUMER_KIND_SESSION = "session"


@dataclass(frozen=True)
class ConsumerConfig:
    """Configuration for one flexible consumer."""

    slot: int
    enabled: bool = False
    name: str = ""
    mode: str = "auto"
    power_kw: float = 0.0
    duration_minutes: int = 0
    energy_kwh: float = 0.0
    measurement_entity: str = ""
    priority: int = 5
    pv_required: bool = False
    learning_enabled: bool = True
    min_days: int = 0
    max_days: int = 0
    max_grid_score: int = 100
    tariff_only: bool = False
    pv_opportunist: bool = False
    min_runtime_minutes: int = 0
    run_order: int = 0  # 0=keine Einschränkung, 1=zuerst, 2=danach, etc.
    trigger_entity: str = ""  # if set → on_demand consumer (handled by on_demand.py, skipped by global scheduler)
    skip_next_run: bool = False  # OneShot: skip next planned run, auto-reset at midnight
    allowed_from: str = ""  # "HH:MM" — earliest planning time; empty = anytime
    allowed_until: str = ""  # "HH:MM" — latest planning time (can cross midnight); empty = anytime
    demand_entity: str = ""  # Bedarfs-Signal (binary_sensor/input_boolean): on → fällig (forced, auch mehrfach/Tag), off → waiting_demand. Ersetzt days_since-Fälligkeit (Audit 9.1 / WW1).
    runtime_sensor: str = ""  # entity_id of a sensor reporting today's runtime (hours). Skip planning for today if value*60 >= duration_minutes.
    pause_on_vacation: bool = False  # Overlay: im Ferienmodus nicht planen/starten (enabled bleibt unangetastet)
    is_battery_charger: bool = False  # If true, battery assessment decides whether consumer runs (Consumer 9 pattern). Otherwise ignored.

    # --- Session-Consumer (kind=session, e.g. Wallbox) ---
    kind: str = CONSUMER_KIND_DAILY  # "daily_fixed" (default) | "session"

    @property
    def configured_name(self) -> str:
        return self.name.strip() or f"Consumer {self.slot}"

    @property
    def manual_energy_kwh(self) -> float | None:
        if self.energy_kwh > 0:
            return float(self.energy_kwh)
        if self.power_kw > 0 and self.duration_minutes > 0:
            return float(self.power_kw) * float(self.duration_minutes) / 60.0
        return None


@dataclass
class SessionState:
    """Transient session state for kind=session consumers (e.g. Wallbox).

    Set via tariff_saver.session_start service, updated via session_update,
    cleared via session_end. Persisted in storage so an HA-restart while
    a session is active doesn't lose context.
    """

    slot: int
    active: bool = False
    energy_needed_kwh: float = 0.0
    deadline_utc: datetime | None = None
    min_power_w: int = 1380  # Huawei SCharger 1-phase minimum
    max_power_w: int = 11000  # Huawei SCharger 3-phase nominal
    prefer_pv: bool = True
    started_utc: datetime | None = None
    last_update_utc: datetime | None = None
    # Heartbeat from huawei_solar — if stale (>5 min) the session is auto-ended
    last_heartbeat_utc: datetime | None = None


@dataclass(frozen=True)
class ConsumerLearning:
    """Learned values for one flexible consumer."""

    sample_count: int = 0
    avg_energy_kwh: float = 0.0
    avg_duration_minutes: float = 0.0
    avg_power_kw: float = 0.0
    last_run_end_utc: datetime | None = None
