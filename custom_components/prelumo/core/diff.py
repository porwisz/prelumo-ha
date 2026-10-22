"""Decide whether a new plan is worth writing to inverter flash."""

from __future__ import annotations

import math
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
    min_gain: float = 0.5  # PLN per window; smaller improvements are not worth a flash write
    window_h: int = 24  # behaviour is compared over this many hours
    # Writes are paced over the day: by a given hour only part of the daily limit may be used
    # (1 + the rest pro rata), unless the current plan breaks rules or the gain is this many
    # times min_gain. Small night-time tweaks no longer use up the writes needed in the day.
    urgent_gain_factor: float = 5.0
    # A change whose first difference is this many hours ahead waits: the hour is re-planned
    # (with fresher prices and forecasts) every hour until then. With a 24 h window the clock
    # hour just passed re-enters at the far end each hour - a tweak there is not worth a write.
    defer_after_h: int = 12


URGENT_HOURS = 3  # a rule the inverter schedule breaks this soon is fixed at once


@dataclass(frozen=True)
class RuleCheck:
    """The proposal vs the inverter schedule on the hard rules (same forecast, same window).

    A rule broken in the next URGENT_HOURS is fixed at once; one further ahead is fixed whatever
    the money gain, but within the pacing of writes over the day - so a broken hour far ahead
    no longer spends the day's writes in the night.
    """

    urgent: bool = False  # the schedule breaks a rule within URGENT_HOURS, the proposal does not
    worse: bool = False  # the proposal breaks a rule the schedule keeps
    fixes: bool = False  # the proposal keeps a rule the schedule breaks (any hour, full charge)


def rule_check(current: set[str], proposal: set[str], near: set[str]) -> RuleCheck:
    """``current``/``proposal``: broken rules as "HH:MM rule" (or a bare rule with no hour);
    ``near``: the "HH:MM" of the next URGENT_HOURS hours."""
    def soon(issues: set[str]) -> set[str]:
        return {v for v in issues if v.split(" ", 1)[0] in near}

    return RuleCheck(
        urgent=bool(soon(current) - soon(proposal)) and not soon(proposal) - soon(current),
        worse=bool(proposal - current),
        fixes=bool(current - proposal),
    )


def material_changes(
    old: DayPlan | None, new: DayPlan, policy: WritePolicy, now: datetime | None = None
) -> list[str]:
    """Compare what the inverter would *do* over the next 24 h, sampled every 15 min.

    Slot fields are not compared directly: the planner anchors slot 1 at the current hour,
    so an unchanged plan "rolls" every hour without any behavioural difference.
    """
    if old is None:
        return ["no_current_plan"]
    reasons = [r for _, r in _differences(old, new, policy, now)]
    return reasons[:6] + ([f"+{len(reasons) - 6}"] if len(reasons) > 6 else [])


def _differences(
    old: DayPlan, new: DayPlan, policy: WritePolicy, now: datetime | None
) -> list[tuple[int, str]]:
    """(minutes from now, "HH:MM:what") of each 15-min sample where the behaviour differs."""
    t0 = (now or datetime(2000, 1, 1)).replace(second=0, microsecond=0)
    out: list[tuple[int, str]] = []
    for k in range(max(1, policy.window_h) * 4):
        t = t0 + timedelta(minutes=15 * k)
        a, b = old.active_slot(t), new.active_slot(t)
        when = f"{t:%H:%M}"
        if (a.flags & MANAGED_FLAGS) != (b.flags & MANAGED_FLAGS):
            out.append((15 * k, f"{when}:mode"))
        elif abs(a.soc - b.soc) >= policy.soc_tolerance:
            out.append((15 * k, f"{when}:soc"))
        elif abs(a.power - b.power) >= policy.power_tolerance_w:
            out.append((15 * k, f"{when}:power"))
    # a short difference (< start tolerance) is just a boundary shift -> not material
    if len(out) * 15 < policy.start_tolerance_min:
        return []
    return out


@dataclass
class WriteGuard:
    policy: WritePolicy = field(default_factory=WritePolicy)
    history: list[datetime] = field(default_factory=list)

    def allowed(self, now: datetime) -> tuple[bool, str | None]:
        self.history = [t for t in self.history if now - t < timedelta(days=2)]
        # the daily limit counts writes since local midnight (not a rolling 24 h window)
        today = [t for t in self.history if t.astimezone(now.tzinfo).date() == now.date()]
        if len(today) >= self.policy.max_writes_per_day:
            return False, "daily_write_limit"
        if self.history and now - self.history[-1] < self.policy.min_interval:
            return False, "min_interval"
        return True, None

    def budget(self, now: datetime) -> int:
        """Writes the pacing allows so far today."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        frac = (now - midnight) / timedelta(days=1)
        return min(self.policy.max_writes_per_day, 1 + math.ceil((self.policy.max_writes_per_day - 1) * frac))

    def _today(self, now: datetime) -> list[datetime]:
        return [t for t in self.history if t.astimezone(now.tzinfo).date() == now.date()]

    def status(self, now: datetime) -> dict:
        """Writes today and the earliest time the next automatic write is allowed."""
        today = sorted(t for t in self.history if t.astimezone(now.tzinfo).date() == now.date())
        nxt = now
        if len(today) >= self.policy.max_writes_per_day:
            nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo)
        elif self.history and now - max(self.history) < self.policy.min_interval:
            nxt = max(self.history) + self.policy.min_interval
        return {
            "writes_today": [t.isoformat() for t in today],
            "max_writes_per_day": self.policy.max_writes_per_day,
            "min_interval_min": int(self.policy.min_interval.total_seconds() // 60),
            "min_gain": self.policy.min_gain,
            "write_budget_now": self.budget(now),
            "next_write_allowed": nxt.isoformat(),
        }

    def record(self, now: datetime) -> None:
        self.history.append(now)

    def decide(
        self, old: DayPlan | None, new: DayPlan, now: datetime, gain: float | None = None,
        rules: RuleCheck | None = None,
    ) -> tuple[bool, list[str]]:
        """``gain``: money only (PLN over the window, no rule penalties); ``None`` = unknown
        (backup plan), which is written like an urgent one."""
        reasons = material_changes(old, new, self.policy, now)
        if not reasons:
            return False, ["no_material_change"]
        rules = rules or RuleCheck()
        if (old is not None and gain is not None and not rules.urgent
                and _differences(old, new, self.policy, now)[0][0] >= self.policy.defer_after_h * 60):
            return False, ["change_far_ahead", *reasons]
        if old is not None and rules.worse and not rules.urgent:
            return False, ["proposal_breaks_rules", *reasons]
        if (old is not None and gain is not None and gain < self.policy.min_gain
                and not (rules.urgent or rules.fixes)):
            return False, ["gain_below_threshold", f"{gain:.2f}"]
        ok, why = self.allowed(now)
        if not ok:
            return False, [why or "blocked", *reasons]
        urgent = (old is None or gain is None or rules.urgent
                  or gain >= self.policy.urgent_gain_factor * self.policy.min_gain)
        if not urgent and len(self._today(now)) >= self.budget(now):
            return False, ["write_budget_paced", *reasons]
        return True, (["urgent_rule_fix"] if rules.urgent else []) + reasons
