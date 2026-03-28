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


@dataclass(frozen=True)
class ConsumerLearning:
    """Learned values for one flexible consumer."""

    sample_count: int = 0
    avg_energy_kwh: float = 0.0
    avg_duration_minutes: float = 0.0
    avg_power_kw: float = 0.0
    last_run_end_utc: datetime | None = None
