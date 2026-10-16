"""End-to-end planning pipeline: forecasts + prices -> hourly optimum -> 6-slot DayPlan."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .compress import CAP_PENALTY, Segment, battery_held, compress, settings_from_plan, simulate, to_day_plan
from .forecast.ev import EvForecast
from .optimizer import (
    FULL_CHARGE_PENALTY, BatteryParams, HourInput, OptimizeResult, Strategy, expensive_reserve, optimize,
    simulate_baseline,
)
from .plan import MAX_POWER_W, DayPlan, PlanError, Slot, SlotMode, parse_hhmm
from .tariff import G13Tariff


@dataclass
class PlannerInputs:
    now: datetime
    soc: float
    pv: dict[datetime, float]  # corrected, kWh/h
    base_load: list[float]  # per hour (house excl. HP and EV)
    heat_pump: list[float]
    ev: EvForecast
    sell: dict[datetime, float]
    tariff: G13Tariff
    horizon_h: int = 36
    window_h: int = 18  # the 6 slots are built from (and judged on) this many hours
    grid_energy: bool = False  # the battery holds energy bought from the grid


@dataclass
class PlannerResult:
    plan: DayPlan
    hours: list[HourInput]
    optimum: OptimizeResult
    segments: list[Segment]
    slot_plan_cost: float
    baseline_cost: float
    missing_sell_hours: int
    notes: list[str] = field(default_factory=list)
    current_plan_cost: float | None = None  # same forecast, schedule currently on the inverter
    slot_hourly: list[dict] = field(default_factory=list)  # proposed slots, as the inverter runs them
    current_hourly: list[dict] = field(default_factory=list)  # schedule now on the inverter
    # money only (no hard-rule penalties) and the hard rules each schedule breaks
    slot_plan_money: float = 0.0
    slot_plan_issues: list[str] = field(default_factory=list)
    current_plan_money: float | None = None
    current_plan_issues: list[str] = field(default_factory=list)
    # cost (PLN, money only) of each of the 6 slots within the window; None = slot not in the window
    slot_costs: list[float | None] = field(default_factory=list)
    current_slot_costs: list[float | None] = field(default_factory=list)
    # what the current_* fields were computed from, so they can follow the inverter between runs
    current: DayPlan | None = None
    _ctx: tuple | None = field(default=None, repr=False)

    def evaluate_current(self, current: DayPlan | None) -> None:
        """(Re)compute the current_* fields for the schedule now on the inverter (no-op if unchanged)."""
        if self._ctx is None or current == self.current:
            return
        day, soc, params, strategy, grid_energy = self._ctx
        self.current = current
        self.current_plan_cost = self.current_plan_money = None
        self.current_hourly, self.current_plan_issues, self.current_slot_costs = [], [], []
        self.notes = [n for n in self.notes if not n.startswith("current_plan_violates:")]
        if current is None:
            return
        cost = simulate(day, settings_from_plan(current, day), soc, params, detail=self.current_hourly,
                        grid_energy=grid_energy, violations=self.current_plan_issues)[0]
        self.current_plan_money = cost - _penalties(self.current_plan_issues)
        self.current_slot_costs = slot_costs(current, day, self.current_hourly)
        broken = plan_violations(current, day, params, strategy)
        if broken:  # must be replaceable regardless of cost
            self.notes.append("current_plan_violates:" + ",".join(broken))
        else:
            self.current_plan_cost = cost

    @property
    def gain_vs_current(self) -> float | None:
        if self.current_plan_cost is None:
            return None
        return self.current_plan_cost - self.slot_plan_cost

    @property
    def expected_savings(self) -> float:
        return self.baseline_cost - self.slot_plan_cost


def horizon_hours(now: datetime, n: int) -> list[datetime]:
    h0 = now.replace(minute=0, second=0, microsecond=0)
    return [h0 + timedelta(hours=i) for i in range(n)]


def build_hours(inp: PlannerInputs, hours: list[datetime]) -> tuple[list[HourInput], int]:
    missing = 0
    # Unknown RCE (tomorrow is published ~14:00): never plan a sale on a made-up price.
    # 0 = no revenue, so the optimiser does not charge or hold energy to sell into those hours.
    fallback = 0.0
    out = []
    for i, h in enumerate(hours):
        sell = inp.sell.get(h)
        if sell is None:
            missing += 1
            sell = fallback
        out.append(
            HourInput(
                start=h,
                pv=inp.pv.get(h, 0.0),
                load=inp.base_load[i] + inp.heat_pump[i],
                ev=inp.ev.hourly_kwh[i] if i < len(inp.ev.hourly_kwh) else 0.0,
                buy=inp.tariff.price(h),
                sell=sell,
                ev_battery_allowed=inp.ev.battery_allowed[i] if i < len(inp.ev.battery_allowed) else False,
            )
        )
    # first hour is partial
    frac = 1 - (inp.now - hours[0]).total_seconds() / 3600
    if out and 0 < frac < 1:
        f = out[0]
        out[0] = HourInput(f.start, f.pv * frac, f.load * frac, f.ev * frac, f.buy, f.sell, f.ev_battery_allowed)
    return out, missing


def run_planner(
    inp: PlannerInputs,
    params: BatteryParams,
    strategy: Strategy,
    arbitrage: bool,
    current: DayPlan | None = None,
) -> PlannerResult:
    hours = horizon_hours(inp.now, inp.horizon_h)
    hin, missing = build_hours(inp, hours)
    window = max(6, min(inp.window_h, 24, len(hin)))
    opt = optimize(hin, inp.soc, params, strategy, arbitrage, grid_energy=inp.grid_energy,
                   full_by=window)
    segs, _ = compress(opt.hours, hin, inp.soc, params, window=window, grid_energy=inp.grid_energy)
    plan = to_day_plan(segs, hin, params, current, soc_now=inp.soc)
    day = hin[:window]
    slot_hourly: list[dict] = []
    slot_issues: list[str] = []
    slot_cost = simulate(day, settings_from_plan(plan, day), inp.soc, params, detail=slot_hourly,
                         grid_energy=inp.grid_energy, violations=slot_issues)[0]
    base = simulate_baseline(day, inp.soc, params)
    notes = list(opt.notes)
    if missing:
        notes.append(f"sell_price_missing_{missing}h")
    res = PlannerResult(plan, hin, opt, segs, slot_cost, base, missing, notes, None,
                        slot_hourly, [], slot_cost - _penalties(slot_issues), slot_issues,
                        slot_costs=slot_costs(plan, day, slot_hourly),
                        _ctx=(day, inp.soc, params, strategy, inp.grid_energy))
    res.evaluate_current(current)
    return res


def slot_costs(plan: DayPlan, hours: list[HourInput], detail: list[dict]) -> list[float | None]:
    """Sum the simulated hourly costs per slot (the slot active at each hour's start)."""
    out: list[float | None] = [None] * len(plan.slots)
    for h, d in zip(hours, detail):
        i = plan.active_index(h.start)
        out[i] = (out[i] or 0.0) + d["cost"]
    return [None if c is None else round(c, 2) for c in out]


def _penalties(issues: list[str]) -> float:
    return sum(FULL_CHARGE_PENALTY if v == "full_charge_missing" else CAP_PENALTY for v in issues)


def plan_violations(
    plan: DayPlan, hours: list[HourInput], params: BatteryParams, strategy: Strategy
) -> list[str]:
    """Rules an existing inverter schedule breaks under the current settings."""
    out: set[str] = set()
    reserve = expensive_reserve(hours, params)
    half_step = 0.5 * max(1, params.soc_step)
    for h, res in zip(hours, reserve):
        slot = plan.active_slot(h.start)
        if slot.mode is SlotMode.SELL:
            if strategy is Strategy.SELF_CONSUMPTION:
                out.add("sell_in_self_consumption")
            if slot.soc < params.min_sell_soc:
                out.add("sell_below_min_soc")
            if slot.soc < params.min_soc + res / params.capacity_kwh * 100 - half_step:
                out.add("sell_below_expensive_reserve")
        if (slot.mode is SlotMode.GRID_CHARGE and params.max_grid_charge_price is not None
                and h.buy > params.max_grid_charge_price + 1e-9):
            out.add("grid_charge_above_price_cap")
        if slot.mode is SlotMode.NONE and battery_held(h, slot.soc, params):
            out.add("battery_held_in_expensive_hour")
    return sorted(out)


def parse_fallback_plan(text: str, power_w: int = MAX_POWER_W) -> DayPlan:
    """'HH:MM soc mode; ...' (6 entries), mode in none|grid_charge|sell."""
    parts = [p.strip() for p in text.replace("\n", ";").split(";") if p.strip()]
    if len(parts) != 6:
        raise PlanError("backup plan needs exactly 6 entries 'HH:MM SOC MODE'")
    slots = []
    for p in parts:
        bits = p.replace(",", " ").split()
        if len(bits) < 2:
            raise PlanError(f"bad entry: {p}")
        mode = SlotMode(bits[2]) if len(bits) > 2 else SlotMode.NONE
        slots.append(Slot(parse_hhmm(bits[0]), power_w, int(bits[1]), 0).with_mode(mode))
    return DayPlan(tuple(slots))
