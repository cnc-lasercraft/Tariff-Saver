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
