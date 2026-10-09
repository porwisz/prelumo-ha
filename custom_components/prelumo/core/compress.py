"""Hourly optimiser output -> at most 6 inverter slots, with minimal cost loss.

A small inverter simulator models how the Deye actually behaves for a given slot
setting (mode + SoC); adjacent segments are merged greedily, always choosing the merge
whose simulated cost increase is the smallest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .optimizer import BatteryParams, HourInput, HourMode, HourPlan
from .plan import MAX_POWER_W, SLOT_COUNT, DayPlan, Slot, SlotMode

CAP_PENALTY = 1e4  # PLN; makes a merge that grid-charges above the price cap unacceptable

_MODE_TO_SLOT = {
    HourMode.NORMAL: SlotMode.NONE,
    HourMode.GRID_CHARGE: SlotMode.GRID_CHARGE,
    HourMode.SELL: SlotMode.SELL,
}


@dataclass(frozen=True)
class Setting:
    mode: HourMode
    soc: int


@dataclass
class Segment:
    start: int  # index into hours
    length: int
    setting: Setting


def hour_setting(h: HourPlan) -> Setting:
    if h.mode is HourMode.GRID_CHARGE:
        return Setting(h.mode, h.soc_end)
    if h.mode is HourMode.SELL:
        return Setting(h.mode, h.soc_end)
    return Setting(HourMode.NORMAL, h.soc_floor)


def simulate(
    hours: list[HourInput], settings: list[Setting], soc_now: float, p: BatteryParams,
    sell_power_kw: float | None = None,
) -> tuple[float, list[float]]:
    """Inverter behaviour per hour; returns (cost, soc trajectory %)."""
    cap = p.capacity_kwh
    soc = soc_now / 100 * cap
    eta = p.eta
    sell_kw = sell_power_kw if sell_power_kw is not None else p.max_discharge_kw
    cost = 0.0
    traj = []
    for h, st in zip(hours, settings):
        target = st.soc / 100 * cap
        hi = p.max_soc / 100 * cap
        lo = max(p.min_soc / 100 * cap, target) if st.mode is not HourMode.GRID_CHARGE else p.min_soc / 100 * cap
        net = h.load + h.ev - h.pv  # + deficit / - surplus
        bus = 0.0  # + charging
        if st.mode is HourMode.SELL:
            lo = max(lo, p.min_sell_soc / 100 * cap)
        # the inverter charges from the grid whenever a grid-charge slot is active - it knows
        # nothing about prices - so a merge that charges above the cap must be rejected
        over_cap = p.max_grid_charge_price is not None and h.buy > p.max_grid_charge_price + 1e-9
        if st.mode is HourMode.GRID_CHARGE and over_cap:
            # hard rule, independent of the forecast: if the battery is lower than predicted,
            # the inverter would charge from the grid at this price (and the house runs on grid)
            cost += CAP_PENALTY
        if st.mode is HourMode.GRID_CHARGE and soc < target:
            e = min(target - soc, p.max_charge_kw * eta)
            bus = e / eta
        elif st.mode is HourMode.SELL and soc > lo:
            e = min(soc - lo, sell_kw / eta, p.max_discharge_kw)
            bus = -e * eta
        elif net < 0 and soc < hi:
            e = min(-net * eta, hi - soc, p.max_charge_kw * eta)
            bus = e / eta
        elif net > 0 and soc > lo:
            e = min(net / eta, soc - lo, p.max_discharge_kw)
            bus = -e * eta
        if st.mode is HourMode.GRID_CHARGE and soc >= target and net > 0 and soc > lo:
            e = min(net / eta, soc - target, p.max_discharge_kw)
            bus = -e * eta
        soc += bus * eta if bus > 0 else bus / eta
        g = net + bus
        imp, exp = max(0.0, g), min(max(0.0, -g), p.grid_export_kw)
        wear = (-bus / eta) * p.wear_cost if bus < 0 else 0.0
        cost += imp * h.buy - (exp * h.sell if h.sell > 0 else 0.0) + wear
        traj.append(soc / cap * 100)
    tp = min((h.buy for h in hours), default=0.0) * eta
    cost -= (soc - p.min_soc / 100 * cap) * tp
    return cost, traj


_SLOT_TO_MODE = {v: k for k, v in _MODE_TO_SLOT.items()}


def settings_from_plan(plan: DayPlan, hours: list[HourInput]) -> list[Setting]:
    """What an existing inverter schedule does in each hour (slot active at the hour start)."""
    out = []
    for h in hours:
        slot = plan.active_slot(h.start)
        out.append(Setting(_SLOT_TO_MODE[slot.mode], slot.soc))
    return out


def _expand(segments: list[Segment]) -> list[Setting]:
    out: list[Setting] = []
    for s in segments:
        out.extend([s.setting] * s.length)
    return out


def _candidates(a: Setting, b: Setting) -> list[Setting]:
    c = {a, b}
    if a.mode == b.mode:
        c |= {Setting(a.mode, min(a.soc, b.soc)), Setting(a.mode, max(a.soc, b.soc))}
    return list(c)


def compress(
    plan: list[HourPlan],
    hours: list[HourInput],
    soc_now: float,
    params: BatteryParams,
    slots: int = SLOT_COUNT,
) -> tuple[list[Segment], float]:
    """Greedy cost-aware merge of hourly settings into ``slots`` segments (first 24 h)."""
    n = min(24, len(plan))
    plan, hours = plan[:n], hours[:n]
    segs: list[Segment] = []
    for i, hp in enumerate(plan):
        st = hour_setting(hp)
        if segs and segs[-1].setting == st:
            segs[-1].length += 1
        else:
            segs.append(Segment(i, 1, st))

    def cost_of(ss: list[Segment]) -> float:
        return simulate(hours, _expand(ss), soc_now, params)[0]

    current = cost_of(segs)
    while len(segs) > slots:
        best = None
        for i in range(len(segs) - 1):
            a, b = segs[i], segs[i + 1]
            for cand in _candidates(a.setting, b.setting):
                trial = segs[:i] + [Segment(a.start, a.length + b.length, cand)] + segs[i + 2 :]
                c = cost_of(trial)
                if best is None or c < best[0]:
                    best = (c, trial)
        current, segs = best  # type: ignore[misc]
        # re-join neighbours that became identical
        joined: list[Segment] = []
        for s in segs:
            if joined and joined[-1].setting == s.setting:
                joined[-1].length += s.length
            else:
                joined.append(s)
        segs = joined
    while len(segs) < slots:  # pad: split the longest segment
        i = max(range(len(segs)), key=lambda k: segs[k].length)
        s = segs[i]
        if s.length < 2:
            break
        half = s.length // 2
        segs[i : i + 1] = [Segment(s.start, half, s.setting), Segment(s.start + half, s.length - half, s.setting)]
    return segs, current


def to_day_plan(
    segments: list[Segment], hours: list[HourInput], params: BatteryParams,
    current: DayPlan | None = None,
) -> DayPlan:
    power = int(min(MAX_POWER_W, params.max_discharge_kw * 1000))
    slots = []
    for seg in segments:
        dt: datetime = hours[seg.start].start
        soc = int(seg.setting.soc)
        if seg.setting.mode is HourMode.SELL:  # sell slot SoC = discharge floor
            soc = max(soc, params.min_sell_soc)
        slot = Slot(dt.hour * 100 + dt.minute, power, soc, 0)
        slots.append(slot.with_mode(_MODE_TO_SLOT[seg.setting.mode]))
    return DayPlan(tuple(slots)).merge_preserved_flags(current)
