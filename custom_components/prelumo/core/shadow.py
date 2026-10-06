"""Shadow mode: what would Prelumo have saved, and what did reality cost."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


def energy_cost(
    imports: dict[datetime, float], exports: dict[datetime, float],
    buy: dict[datetime, float], sell: dict[datetime, float],
) -> float:
    cost = sum(v * buy.get(h, 0.0) for h, v in imports.items())
    cost -= sum(v * max(0.0, sell.get(h, 0.0)) for h, v in exports.items())
    return cost


@dataclass
class ShadowLedger:
    """Daily record of expected plan cost vs baseline, plus metered actual cost."""

    days: dict[str, dict[str, float]] = field(default_factory=dict)

    def record_plan(self, day: date, plan_cost: float, baseline_cost: float) -> None:
        d = self.days.setdefault(day.isoformat(), {})
        d["plan_cost"] = round(plan_cost, 3)
        d["baseline_cost"] = round(baseline_cost, 3)
        d["expected_savings"] = round(baseline_cost - plan_cost, 3)

    def record_actual(self, day: date, actual_cost: float) -> None:
        self.days.setdefault(day.isoformat(), {})["actual_cost"] = round(actual_cost, 3)

    def total_expected_savings(self) -> float:
        return round(sum(d.get("expected_savings", 0.0) for d in self.days.values()), 2)

    def trim(self, keep: int = 60) -> None:
        for k in sorted(self.days)[:-keep]:
            del self.days[k]

    def to_dict(self) -> dict:
        return {"days": self.days}

    @classmethod
    def from_dict(cls, data: dict | None) -> ShadowLedger:
        return cls(dict((data or {}).get("days", {})))
