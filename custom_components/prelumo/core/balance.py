"""Periodic full charge (BMS balancing / SoC calibration) - when it is due, and tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

FULL_SOC = 99.5  # what counts as "full" (some BMS report 99 % for a long time)


@dataclass
class FullChargeTracker:
    interval_days: int = 7  # 0 = never force a full charge
    last_full: datetime | None = None

    def observe(self, soc: float | None, now: datetime) -> bool:
        """Feed the current SoC; returns True when a new full charge was registered."""
        if soc is None or soc < FULL_SOC:
            return False
        first = self.last_full is None or now - self.last_full > timedelta(minutes=30)
        self.last_full = now
        return first

    def next_due(self) -> datetime | None:
        if self.interval_days <= 0 or self.last_full is None:
            return None
        return self.last_full + timedelta(days=self.interval_days)

    def due(self, now: datetime) -> bool:
        if self.interval_days <= 0:
            return False
        nd = self.next_due()
        return nd is None or now >= nd  # never seen full -> due

    def days_since(self, now: datetime) -> float | None:
        if self.last_full is None:
            return None
        return round((now - self.last_full).total_seconds() / 86400, 1)
