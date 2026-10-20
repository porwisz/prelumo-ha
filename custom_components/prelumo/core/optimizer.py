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
from functools import lru_cache

EPS = 0.05  # kWh
INFEASIBLE = 1e6
FULL_CHARGE_PENALTY = 1e3  # PLN; a due full charge is done whenever it is possible at all
# Objective only (not in reported costs): stored energy "costs" a little per hour, more above
# HIGH_SOC. Among equally cheap hours the battery is charged as late as possible - just before
# the expensive / sell hours - instead of filling up at once and sitting full all night.
HOLD_COST = 0.002  # PLN per kWh above min SoC per hour
HIGH_SOC = 90  # %
HIGH_SOC_HOLD_COST = 0.01  # PLN per kWh above HIGH_SOC per hour


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
    # kW; a Deye sell slot (Export First) caps ALL battery output - the house too - at the slot
    # power, so a sell slot never gets less (a kettle + oven above it would run on the grid)
    sell_min_power_kw: float = 6.0
    # Share of the PV forecast trusted for the house reserve in expensive hours (1.0 = off): before
    # an expensive run the battery should hold what the house needs there if PV gave only this
    # share; the reserve kept while selling is counted the same way.
    pv_trust: float = 1.0
    require_full: bool = False  # periodic 100% charge due: plan must reach max_soc once
    # Above this buy price the battery covers the house first: no buying for the house while the
    # battery is above min SoC (no "buy expensive to sell later"). None = off.
    battery_first_above: float | None = None
    # With battery-first: sell only above a reserve for the house in the following expensive
    # hours (forecast deficit until the next cheap hour) x (1 + margin).
    reserve_margin: float = 0.3
    # With arbitrage on: grid-charged energy is sold only if it earns at least this per kWh
    arbitrage_min_margin: float = 0.0
    # Sell price from which battery-first stops holding energy back: at this price selling beats
    # saving the expensive purchase, so the hour may export down to ``min_sell_soc``. None = off.
    force_sell_above: float | None = None
    # BMS charge taper: (from SoC %, max battery-side kW) - the last % charge slowly (measured
    # on the Deye: 99 -> 100 % takes ~30 min)
    charge_taper: tuple[tuple[int, float], ...] = ((90, 5.5), (95, 3.0), (98, 1.5), (99, 0.5))

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


def charge_kw_at(soc: float, p: BatteryParams) -> float:
    """Max battery-side charge power (kW) at ``soc`` %."""
    kw = p.max_charge_kw * p.eta
    for start, lim in p.charge_taper:
        if soc >= start - 1e-9:
            kw = min(kw, lim)
    return kw


@lru_cache(maxsize=4096)
def max_charge_kwh(soc: float, p: BatteryParams, hours: float = 1.0) -> float:
    """Battery-side kWh that fit into ``hours`` starting at ``soc`` %, along the taper."""
    pct_kwh = p.capacity_kwh / 100
    edges = sorted({float(a) for a, _ in p.charge_taper if a > soc} | {float(p.max_soc)})
    out, t, cur = 0.0, hours, soc
    for edge in edges:
        if cur >= p.max_soc - 1e-9 or t <= 1e-9:
            break
        kw = charge_kw_at(cur, p)
        need = (min(edge, p.max_soc) - cur) * pct_kwh
        if kw <= 0:
            break
        if need / kw >= t:
            return out + kw * t
        out += need
        t -= need / kw
        cur = min(edge, p.max_soc)
    return out


def hold_cost(soc: float, p: BatteryParams) -> float:
    """Objective-only cost (PLN) of keeping the battery at ``soc`` % for one hour."""
    return (p.capacity_kwh / 100) * (
        HOLD_COST * max(0.0, soc - p.min_soc) + HIGH_SOC_HOLD_COST * max(0.0, soc - HIGH_SOC))


def force_sell(h: HourInput, p: BatteryParams) -> bool:
    """Is the sell price high enough to sell instead of holding the energy for the house?"""
    return p.force_sell_above is not None and h.sell >= p.force_sell_above - 1e-9


def expensive_reserve(hours: list[HourInput], p: BatteryParams) -> list[float]:
    """Battery-side kWh to keep while selling in each hour for the house in the expensive hours.

    Covers the following expensive hours and, in an expensive hour, that hour itself: a sell
    slot exports at full power and reaches its floor within minutes, so the rest of the hour
    would run on the grid at the expensive price unless the floor keeps this hour's need too.
    """
    out = [0.0] * len(hours)
    if p.battery_first_above is None:
        return out
    acc = 0.0
    for t in range(len(hours) - 1, -1, -1):
        h = hours[t]
        if force_sell(h, p):
            # this hour may monetise the whole reserve: at this price the sale beats saving the
            # expensive purchases it was kept for (``min_sell_soc`` still protects the battery).
            # Earlier hours keep reserving for the later expensive hours - they are not at a spike.
            out[t] = 0.0
            if h.buy <= p.battery_first_above + 1e-9:
                acc = 0.0
        elif h.buy > p.battery_first_above + 1e-9:
            acc += max(0.0, h.load - h.pv * p.pv_trust)  # house + heat pump, without the EV
            out[t] = acc * (1 + p.reserve_margin) / p.eta
        else:
            out[t] = acc * (1 + p.reserve_margin) / p.eta
            acc = 0.0  # a cheap hour ends the expensive run
    return out


def pv_risk(hours: list[HourInput], p: BatteryParams) -> list[tuple[float, float] | None]:
    """At index t (SoC entering hour t, the first hour of an expensive run after a cheap hour):
    (battery-side kWh the house needs in the run with PV at ``pv_trust``, the run's buy price).

    Objective only: a shortfall is priced as if bought at that price - the battery is charged
    beforehand when it is cheaper, so a cloudy morning does not run on the expensive grid.
    """
    out: list[tuple[float, float] | None] = [None] * (len(hours) + 1)
    if p.battery_first_above is None or p.pv_trust >= 1.0:
        return out
    thr = p.battery_first_above + 1e-9
    room = (p.max_soc - p.min_soc) / 100 * p.capacity_kwh
    for t in range(1, len(hours)):
        if hours[t].buy > thr and hours[t - 1].buy <= thr:
            need, k = 0.0, t
            while k < len(hours) and hours[k].buy > thr:
                need += max(0.0, hours[k].load - hours[k].pv * p.pv_trust)
                k += 1
            out[t] = (min(room, need / p.eta), hours[t].buy)
    return out


def risk_cost(risk: tuple[float, float] | None, stored_kwh: float) -> float:
    """Price of the reserve shortfall (``stored_kwh``: battery energy above min SoC)."""
    if risk is None:
        return 0.0
    need, price = risk
    return max(0.0, need - stored_kwh) * price


def _transition(
    p: BatteryParams, h: HourInput, s0: int, s1: int, strategy: Strategy, may_export: bool,
    reserve: float = 0.0, grid_energy: bool = False,
) -> _Step | None:
    delta = (s1 - s0) / 100 * p.capacity_kwh  # battery side
    if delta > 0:
        bus = delta / p.eta
        if bus > p.max_charge_kw + 1e-9:
            return None
        # BMS taper: a step of the SoC grid may round up by up to half a step
        if delta > max_charge_kwh(s0, p) + 0.5 * max(1, p.soc_step) / 100 * p.capacity_kwh + 1e-9:
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
    ev_blocked = h.ev > EPS and not h.ev_battery_allowed
    rounding = 0.0  # battery energy the SoC grid forces beyond the need (not sold, not to EV)

    # Expensive hour + battery-first: the battery covers the house fully (down to min SoC)
    if p.battery_first_above is not None and h.buy > p.battery_first_above + 1e-9:
        if bus > 0 and s0 > p.min_soc and base_deficit > EPS:
            return None  # charging while the house would buy at this price
        # at a high enough sell price the battery exports instead of covering the house
        if bus <= 0 and not force_sell(h, p):
            need = min(base_deficit if ev_blocked else deficit, p.max_discharge_kw * p.eta)
            step_kwh = max(1, p.soc_step) / 100 * p.capacity_kwh * p.eta
            # SoC-grid rounding either way: a shortfall below one step is bought (the inverter
            # really covers it from the battery). Only rounding up would waste up to a step
            # every expensive hour and make cheap-hour charging for the house look unprofitable.
            if s1 > p.min_soc and delivered < need - step_kwh - EPS:
                return None
            over = delivered - need
            if EPS < over <= step_kwh + EPS:  # SoC-grid rounding, never a "sale"
                rounding = over
                delivered = need

    if (grid_energy and p.max_grid_charge_price is not None
            and h.buy <= p.max_grid_charge_price + 1e-9 and min(delivered, deficit) > EPS):
        # cheap hour: energy bought from the grid waits for the expensive hours - a battery
        # cycle to save the cheap price makes no sense (rounding below one SoC step is allowed)
        if min(delivered, deficit) > max(1, p.soc_step) / 100 * p.capacity_kwh * p.eta + EPS:
            return None
    battery_export = max(0.0, delivered - deficit)
    if battery_export > EPS and h.sell <= 0:
        # no (or unknown) sell price: never a sale; up to one SoC step is grid rounding
        step_kwh = max(1, p.soc_step) / 100 * p.capacity_kwh * p.eta
        if battery_export > step_kwh + EPS:
            return None
        rounding += battery_export
        delivered -= battery_export
        battery_export = 0.0
    if battery_export > EPS and (strategy is Strategy.SELF_CONSUMPTION or not may_export or s1 < p.min_sell_soc):
        return None
    if battery_export > EPS and (s1 - p.min_soc) / 100 * p.capacity_kwh < reserve - 1e-9:
        return None  # keep energy for the house in the coming expensive hours
    if grid_charge > EPS and p.max_grid_charge_price is not None and h.buy > p.max_grid_charge_price + 1e-9:
        return None
    if ev_blocked and delivered > base_deficit + EPS:
        return None

    net = total_load - h.pv + bus + rounding
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
    grid_energy: bool = False,
    full_by: int | None = None,
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

    def hold(s: int) -> float:
        return hold_cost(s, p)

    T = len(hours)
    reserve = expensive_reserve(hours, p)
    risk = pv_risk(hours, p)
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
                if t == 0 and tainted != grid_energy:
                    continue
                for full in fulls:
                    if t == 0 and full != done(start_soc):
                        continue
                    may_export = arbitrage or not tainted
                    best = None
                    for s1 in grid:
                        st = _transition(p, h, s0, s1, strategy, may_export, reserve[t], tainted)
                        if st is None:
                            continue
                        nt = tainted or st.grid_charge > EPS
                        if s1 <= lo:
                            nt = False
                        # a due full charge counts only within the slot window (``full_by`` hours) -
                        # the 6 slots must contain it, a 100 % later in the horizon is not written
                        nf = full or (done(s1) and (full_by is None or t < full_by))
                        # hurdle (objective only): selling grid-charged energy must earn the margin
                        hurdle = p.arbitrage_min_margin * st.battery_export if tainted else 0.0
                        rk = risk_cost(risk[t + 1], (s1 - lo) / 100 * p.capacity_kwh)
                        v = st.cost + hurdle + hold(s1) + rk + V[t + 1][(s1, nt, nf)]
                        if best is None or v < best[0]:
                            best = (v, s1, nt, nf, st)
                    if best is None:  # should not happen: idle is always allowed unless limits
                        best = (INFEASIBLE, s0, tainted, full, _Step(INFEASIBLE, 0, 0, 0, 0))
                    V[t][(s0, tainted, full)] = best[0]
                    choice[t][(s0, tainted, full)] = (best[1], best[2], best[3], best[4])

    plan: list[HourPlan] = []
    s_, tainted, full = start_soc, grid_energy, done(start_soc)
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
            charge = min(-net * p.eta, hi_kwh - soc_kwh, p.max_charge_kw,
                         max_charge_kwh(soc_kwh / p.capacity_kwh * 100, p))
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
