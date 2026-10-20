"""Shadow mode: what would Prelumo have saved, and what did reality cost."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

PENALTY_LEVEL = 1e3  # PLN: a plan cost this large carries a hard-rule penalty (CAP_PENALTY)


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
        if abs(plan_cost) >= PENALTY_LEVEL or abs(baseline_cost) >= PENALTY_LEVEL:
            return  # a hard-rule penalty, not money: keep the last real figure for the day
        d = self.days.setdefault(day.isoformat(), {})
        d["plan_cost"] = round(plan_cost, 3)
        d["baseline_cost"] = round(baseline_cost, 3)
        d["expected_savings"] = round(baseline_cost - plan_cost, 3)

    def record_actual(self, day: date, actual_cost: float) -> None:
        self.days.setdefault(day.isoformat(), {})["actual_cost"] = round(actual_cost, 3)

    def record_energy(self, day: date, **kwh: float) -> None:
        """Metered energy of a finished day: house, pv, grid_import, grid_export, discharged."""
        d = self.days.setdefault(day.isoformat(), {})
        for k, v in kwh.items():
            d[k] = round(v, 3)

    def _window(self, days: int) -> list[dict[str, float]]:
        return [self.days[k] for k in sorted(self.days)[-days:]]

    def self_sufficiency(self, days: int = 30) -> float | None:
        """Share of the house's own consumption not bought from the grid (%).

        House energy here is what the inverter metered for the house, so EV charging and the
        heat pump are in it — the same denominator the inverter's own counters use.
        """
        house = sum(d.get("house", 0.0) for d in self._window(days))
        imp = sum(d.get("grid_import", 0.0) for d in self._window(days))
        if house <= 0:
            return None
        return round(max(0.0, min(100.0, (house - imp) / house * 100)), 1)

    def cycles(self, capacity_kwh: float, days: int = 30) -> float | None:
        """Full battery cycles in the window (discharged kWh / capacity)."""
        if capacity_kwh <= 0:
            return None
        dis = sum(d.get("discharged", 0.0) for d in self._window(days))
        return round(dis / capacity_kwh, 2) if dis else 0.0

    def by_month(self, capacity_kwh: float) -> dict[str, dict[str, float]]:
        """Per-month totals: cost, savings, energy, cycles and self-sufficiency."""
        out: dict[str, dict[str, float]] = {}
        for key in sorted(self.days):
            d = self.days[key]
            m = out.setdefault(key[:7], {})
            for name in ("actual_cost", "expected_savings", "house", "pv",
                         "grid_import", "grid_export", "discharged"):
                if name in d:
                    m[name] = round(m.get(name, 0.0) + d[name], 2)
        for m in out.values():
            if capacity_kwh > 0 and "discharged" in m:
                m["cycles"] = round(m["discharged"] / capacity_kwh, 2)
            if m.get("house", 0.0) > 0:
                m["self_sufficiency"] = round(
                    max(0.0, min(100.0, (m["house"] - m.get("grid_import", 0.0)) / m["house"] * 100)), 1)
        return out

    def total_expected_savings(self) -> float:
        return round(sum(d.get("expected_savings", 0.0) for d in self.days.values()), 2)

    def trim(self, keep: int = 60) -> None:
        for k in sorted(self.days)[:-keep]:
            del self.days[k]

    def to_dict(self) -> dict:
        return {"days": self.days}

    @classmethod
    def from_dict(cls, data: dict | None) -> ShadowLedger:
        days = dict((data or {}).get("days", {}))
        for d in days.values():  # drop penalty figures recorded by older versions
            if any(abs(d.get(k, 0.0)) >= PENALTY_LEVEL for k in ("plan_cost", "baseline_cost")):
                for k in ("plan_cost", "baseline_cost", "expected_savings"):
                    d.pop(k, None)
        return cls(days)
