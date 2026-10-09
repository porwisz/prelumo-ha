"""Hourly battery optimiser — dynamic programming over a discrete SoC grid.

Pure Python. State = (SoC step, "holds grid energy" flag). The flag is used when
grid arbitrage is disabled: after charging from the grid, the battery may not export
until it has been drained to the minimum SoC (the grid energy is assumed used up).

Strategies
- ``self_consumption``: battery never exports; it may still charge from the grid when
  that lowers cost (G13 off-peak -> peak).
- ``max_grid_trading``: battery may export when profitable.
``grid_arbitrage`` (separate switch): may grid-charged energy be sold back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

EPS = 0.05  # kWh
INFEASIBLE = 1e6
FULL_CHARGE_PENALTY = 1e3  # PLN; a due full charge is done whenever it is possible at all


class Strategy(StrEnum):
    SELF_CONSUMPTION = "self_consumption"
    MAX_GRID_TRADING = "max_grid_trading"


class HourMode(StrEnum):
    NORMAL = "normal"
    GRID_CHARGE = "grid_charge"
    SELL = "sell"


@dataclass(frozen=True)
class BatteryParams:
    capacity_kwh: float = 20.0
    min_soc: int = 10
    max_soc: int = 100
    max_charge_kw: float = 10.0
    max_discharge_kw: float = 10.0
    roundtrip_efficiency: float = 0.9
    wear_cost: float = 0.10  # PLN per kWh discharged
    grid_import_kw: float = 17.0
    grid_export_kw: float = 12.0
    soc_step: int = 2
    max_grid_charge_price: float | None = None  # PLN/kWh; no grid charging above it (None = no limit)
    min_sell_soc: int = 20  # %; battery never exports below this SoC
    require_full: bool = False  # periodic 100% charge due: plan must reach max_soc once

    @property
    def eta(self) -> float:
        return self.roundtrip_efficiency**0.5


@dataclass(frozen=True)
class HourInput:
    start: datetime
    pv: float  # kWh
    load: float  # kWh, house + heat pump (excl. EV)
    ev: float  # kWh
    buy: float  # PLN/kWh
    sell: float  # PLN/kWh
    ev_battery_allowed: bool = False


@dataclass(frozen=True)
class HourPlan:
    start: datetime
    soc_start: int
    soc_end: int
    mode: HourMode
    grid_import: float
    grid_export: float
    grid_charge: float
    battery_export: float
    cost: float

    @property
    def soc_floor(self) -> int:
        return min(self.soc_start, self.soc_end)

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "soc": self.soc_end,
            "mode": self.mode.value,
            "import": round(self.grid_import, 2),
            "export": round(self.grid_export, 2),
            "cost": round(self.cost, 3),
        }


@dataclass
class OptimizeResult:
    hours: list[HourPlan]
    total_cost: float
    terminal_value: float
    strategy: Strategy
    arbitrage: bool
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Step:
    cost: float
    grid_import: float
    grid_export: float
    grid_charge: float
    battery_export: float


def _transition(
    p: BatteryParams, h: HourInput, s0: int, s1: int, strategy: Strategy, may_export: bool
) -> _Step | None:
    delta = (s1 - s0) / 100 * p.capacity_kwh  # battery side
    if delta > 0:
        bus = delta / p.eta
        if bus > p.max_charge_kw + 1e-9:
            return None
    else:
        bus = delta * p.eta  # negative = delivered to the house bus
        if -delta > p.max_discharge_kw + 1e-9:
            return None

    total_load = h.load + h.ev
    surplus = max(0.0, h.pv - total_load)
    deficit = max(0.0, total_load - h.pv)
    base_deficit = max(0.0, h.load - h.pv)

    grid_charge = max(0.0, bus - surplus) if bus > 0 else 0.0
    delivered = -bus if bus < 0 else 0.0
    battery_export = max(0.0, delivered - deficit)

    if battery_export > EPS and (strategy is Strategy.SELF_CONSUMPTION or not may_export or s1 < p.min_sell_soc):
        return None
    if grid_charge > EPS and p.max_grid_charge_price is not None and h.buy > p.max_grid_charge_price + 1e-9:
        return None
    if h.ev > EPS and not h.ev_battery_allowed and delivered > base_deficit + EPS:
        return None

    net = total_load - h.pv + bus
    imp, exp = max(0.0, net), max(0.0, -net)
    penalty = 0.0
    if imp > p.grid_import_kw + 1e-9:
        penalty = INFEASIBLE * (imp - p.grid_import_kw)
    exp = min(exp, p.grid_export_kw)  # the rest is curtailed
    revenue = exp * h.sell if h.sell > 0 else 0.0  # negative price -> curtail
    wear = (-delta) * p.wear_cost if delta < 0 else 0.0
    return _Step(imp * h.buy - revenue + wear + penalty, imp, exp, grid_charge, battery_export)


def _mode(step: _Step) -> HourMode:
    if step.grid_charge > EPS:
        return HourMode.GRID_CHARGE
    if step.battery_export > EPS:
        return HourMode.SELL
    return HourMode.NORMAL


def optimize(
    hours: list[HourInput],
    soc_now: float,
    params: BatteryParams,
    strategy: Strategy = Strategy.SELF_CONSUMPTION,
    arbitrage: bool = False,
    terminal_price: float | None = None,
) -> OptimizeResult:
    if not hours:
        return OptimizeResult([], 0.0, 0.0, strategy, arbitrage)
    p = params
    step = max(1, p.soc_step)
    lo, hi = p.min_soc, p.max_soc
    grid = list(range(lo, hi + 1, step))
    if grid[-1] != hi:
        grid.append(hi)
    start_soc = int(round(min(hi, max(lo, soc_now))))
    if terminal_price is None:
        terminal_price = min(h.buy for h in hours) * p.eta
    # terminal value: energy left in battery is worth something tomorrow
    def term(s: int) -> float:
        return -(s - lo) / 100 * p.capacity_kwh * terminal_price

    T = len(hours)
    # State = (soc, holds grid energy, has reached full). The "full" flag is only tracked when a
    # periodic full charge (BMS balancing) is required; otherwise it stays True.
    track_full = p.require_full
    full_at = hi

    def done(s: int) -> bool:
        return (not track_full) or s >= full_at

    V: list[dict[tuple[int, bool, bool], float]] = [dict() for _ in range(T + 1)]
    choice: list[dict[tuple[int, bool, bool], tuple[int, bool, bool, _Step]]] = [dict() for _ in range(T)]
    fulls = (False, True) if track_full else (True,)
    for s_ in grid:
        for tainted in (False, True):
            for full in fulls:
                V[T][(s_, tainted, full)] = term(s_) + (0.0 if full else FULL_CHARGE_PENALTY)

    for t in range(T - 1, -1, -1):
        h = hours[t]
        states = grid if t > 0 else [start_soc]
        for s0 in states:
            for tainted in (False, True):
                if t == 0 and tainted:
                    continue
                for full in fulls:
                    if t == 0 and full != done(start_soc):
                        continue
                    may_export = arbitrage or not tainted
                    best = None
                    for s1 in grid:
                        st = _transition(p, h, s0, s1, strategy, may_export)
                        if st is None:
                            continue
                        nt = tainted or st.grid_charge > EPS
                        if s1 <= lo or arbitrage:
                            nt = False
                        nf = full or done(s1)
                        v = st.cost + V[t + 1][(s1, nt, nf)]
                        if best is None or v < best[0]:
                            best = (v, s1, nt, nf, st)
                    if best is None:  # should not happen: idle is always allowed unless limits
                        best = (INFEASIBLE, s0, tainted, full, _Step(INFEASIBLE, 0, 0, 0, 0))
                    V[t][(s0, tainted, full)] = best[0]
                    choice[t][(s0, tainted, full)] = (best[1], best[2], best[3], best[4])

    plan: list[HourPlan] = []
    s_, tainted, full = start_soc, False, done(start_soc)
    total = 0.0
    for t in range(T):
        s1, nt, nf, st = choice[t][(s_, tainted, full)]
        plan.append(
            HourPlan(hours[t].start, s_, s1, _mode(st), st.grid_import, st.grid_export,
                     st.grid_charge, st.battery_export, st.cost)
        )
        total += st.cost
        s_, tainted, full = s1, nt, nf
    notes = []
    if total >= INFEASIBLE:
        notes.append("grid_import_limit_exceeded")
    if track_full:
        notes.append("full_charge_planned" if full else "full_charge_not_possible_in_horizon")
    return OptimizeResult(plan, total, -term(s_), strategy, arbitrage, notes)


def simulate_baseline(hours: list[HourInput], soc_now: float, params: BatteryParams) -> float:
    """Plain self-consumption (no optimiser): charge from surplus, cover deficit to min SoC.

    Returns cost incl. wear minus terminal value — comparable with ``optimize`` totals.
    """
    p = params
    soc_kwh = soc_now / 100 * p.capacity_kwh
    lo_kwh, hi_kwh = p.min_soc / 100 * p.capacity_kwh, p.max_soc / 100 * p.capacity_kwh
    cost = 0.0
    for h in hours:
        net = h.load + h.ev - h.pv
        if net < 0:
            charge = min(-net * p.eta, hi_kwh - soc_kwh, p.max_charge_kw)
            soc_kwh += charge
            exp = min(p.grid_export_kw, -net - charge / p.eta)
            cost -= exp * h.sell if h.sell > 0 else 0.0
        else:
            need = h.load - h.pv if h.ev > 0 else net
            need = max(0.0, need)
            dis = min(need / p.eta, soc_kwh - lo_kwh, p.max_discharge_kw)
            soc_kwh -= dis
            cost += (net - dis * p.eta) * h.buy + dis * p.wear_cost
    terminal_price = min(h.buy for h in hours) * p.eta if hours else 0.0
    return cost - (soc_kwh - lo_kwh) * terminal_price
