"""Decide whether a new plan is worth writing to inverter flash."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .plan import MANAGED_FLAGS, DayPlan


@dataclass(frozen=True)
class WritePolicy:
    soc_tolerance: int = 5  # %
    start_tolerance_min: int = 30
    power_tolerance_w: int = 500
    max_writes_per_day: int = 6
    min_interval: timedelta = timedelta(minutes=50)
    min_gain: float = 0.5  # PLN per 24 h; smaller improvements are not worth a flash write


def material_changes(
    old: DayPlan | None, new: DayPlan, policy: WritePolicy, now: datetime | None = None
) -> list[str]:
    """Compare what the inverter would *do* over the next 24 h, sampled every 15 min.

    Slot fields are not compared directly: the planner anchors slot 1 at the current hour,
    so an unchanged plan "rolls" every hour without any behavioural difference.
    """
    if old is None:
        return ["no_current_plan"]
    t0 = (now or datetime(2000, 1, 1)).replace(second=0, microsecond=0)
    reasons: list[str] = []
    for k in range(96):
        t = t0 + timedelta(minutes=15 * k)
        a, b = old.active_slot(t), new.active_slot(t)
        when = f"{t:%H:%M}"
        if (a.flags & MANAGED_FLAGS) != (b.flags & MANAGED_FLAGS):
            reasons.append(f"{when}:mode")
        elif abs(a.soc - b.soc) >= policy.soc_tolerance:
            reasons.append(f"{when}:soc")
        elif abs(a.power - b.power) >= policy.power_tolerance_w:
            reasons.append(f"{when}:power")
    # a short difference (< start tolerance) is just a boundary shift -> not material
    if len(reasons) * 15 < policy.start_tolerance_min:
        return []
    return reasons[:6] + ([f"+{len(reasons) - 6}"] if len(reasons) > 6 else [])


@dataclass
class WriteGuard:
    policy: WritePolicy = field(default_factory=WritePolicy)
    history: list[datetime] = field(default_factory=list)

    def allowed(self, now: datetime) -> tuple[bool, str | None]:
        self.history = [t for t in self.history if now - t < timedelta(days=1)]
        if len(self.history) >= self.policy.max_writes_per_day:
            return False, "daily_write_limit"
        if self.history and now - self.history[-1] < self.policy.min_interval:
            return False, "min_interval"
        return True, None

    def record(self, now: datetime) -> None:
        self.history.append(now)

    def decide(
        self, old: DayPlan | None, new: DayPlan, now: datetime, gain: float | None = None
    ) -> tuple[bool, list[str]]:
        reasons = material_changes(old, new, self.policy, now)
        if not reasons:
            return False, ["no_material_change"]
        if old is not None and gain is not None and gain < self.policy.min_gain:
            return False, ["gain_below_threshold", f"{gain:.2f}"]
        ok, why = self.allowed(now)
        if not ok:
            return False, [why or "blocked", *reasons]
        return True, reasons
