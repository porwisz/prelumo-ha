"""Hourly optimiser output -> at most 6 inverter slots, with minimal cost loss.

A small inverter simulator models how the Deye actually behaves for a given slot
setting (mode + SoC); adjacent segments are merged greedily, always choosing the merge
whose simulated cost increase is the smallest.
"""

from __future__ import annotations

import math

from dataclasses import dataclass
from datetime import datetime

from .optimizer import (
    FULL_CHARGE_PENALTY, BatteryParams, HourInput, HourMode, HourPlan, expensive_reserve, hold_cost,
    max_charge_kwh,
)
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
    power_kw: float | None = None  # sell slot power; None = chosen to spread the sale over the slot


@dataclass
class Segment:
    start: int  # index into hours
    length: int
    setting: Setting


def battery_held(h: HourInput, floor: int, p: BatteryParams) -> bool:
    """A normal-slot floor above min SoC in an expensive hour, while battery-first is on."""
    return (
        p.battery_first_above is not None
        and h.buy > p.battery_first_above + 1e-9
        and floor > p.min_soc + 1
        and (h.ev <= 0.05 or h.ev_battery_allowed)
    )


def stored_energy_value(plan: list[HourPlan], hours: list[HourInput], i: int, p: BatteryParams) -> float:
    """What 1 kWh kept in the battery after hour ``i`` is worth later (PLN, at the AC side).

    Looks ahead until the next grid-charge hour (after that the battery is refilled cheaply):
    a later Sell hour is worth its sell price, any other later hour is worth the buy price it
    displaces. Beyond the horizon: the cheapest buy price (energy left for tomorrow).
    """
    best = 0.0
    for k in range(i + 1, len(plan)):
        if plan[k].mode is HourMode.GRID_CHARGE:
            return best * p.eta
        h = hours[k]
        best = max(best, h.sell if plan[k].mode is HourMode.SELL else h.buy)
    return max(best, min((h.buy for h in hours), default=0.0)) * p.eta


def hour_setting(
    h: HourPlan, hour: HourInput | None = None, keep_value: float | None = None,
    p: BatteryParams | None = None,
) -> Setting:
    if h.mode is HourMode.GRID_CHARGE:
        return Setting(h.mode, h.soc_end)
    if h.mode is HourMode.SELL:
        return Setting(h.mode, h.soc_end)
    if (hour is not None and p is not None and p.battery_first_above is not None
            and hour.buy > p.battery_first_above + 1e-9
            and (hour.ev <= 0.05 or hour.ev_battery_allowed)):
        return Setting(HourMode.NORMAL, p.min_soc)  # expensive hour: battery covers the house
    if (hour is not None and keep_value is not None and p is not None and hour.buy >= keep_value
            and h.soc_end < h.soc_start):  # only where the optimum itself uses the battery
        # grid power now costs more than the stored energy will ever earn: never import while
        # the battery has energy - floor at min SoC, not at the forecast trajectory (a higher
        # real load would otherwise be bought at this price)
        return Setting(HourMode.NORMAL, p.min_soc)
    return Setting(HourMode.NORMAL, h.soc_floor)


def simulate(
    hours: list[HourInput], settings: list[Setting], soc_now: float, p: BatteryParams,
    sell_power_kw: float | None = None,
    detail: list[dict] | None = None,
    powers: dict[int, float] | None = None,
    grid_energy: bool = False,
    violations: list[str] | None = None,
) -> tuple[float, list[float]]:
    """Inverter behaviour per hour; returns (cost, soc trajectory %).

    ``violations``: if given, each hard rule the schedule breaks is appended as "HH:MM rule"
    (every one adds CAP_PENALTY to the cost).

    ``detail``: if given, one dict per hour is appended (what the inverter is expected to do).
    ``powers``: if given, the chosen power (kW) of each sell run, keyed by its first hour.

    Export First discharges at the slot power until the slot floor and then the battery stops
    covering the house for the rest of the slot, so a sell slot gets a power that spreads the
    sale over the whole slot (never below the house demand).
    """
    cap = p.capacity_kwh
    soc = soc_now / 100 * cap
    eta = p.eta
    sell_kw = sell_power_kw if sell_power_kw is not None else p.max_discharge_kw
    cost = 0.0
    traj = []
    sold_since_cheap = 0.0  # battery energy exported since the last cheap hour
    reserve = expensive_reserve(hours, p)
    run_kw = sell_kw
    for i, (h, st) in enumerate(zip(hours, settings)):
        def broke(rule: str, _h: HourInput = h) -> float:
            if violations is not None:
                violations.append(f"{_h.start:%H:%M} {rule}")
            return CAP_PENALTY

        expensive = p.battery_first_above is not None and h.buy > p.battery_first_above + 1e-9
        if not expensive:
            sold_since_cheap = 0.0
        target = st.soc / 100 * cap
        hi = p.max_soc / 100 * cap
        lo = max(p.min_soc / 100 * cap, target) if st.mode is not HourMode.GRID_CHARGE else p.min_soc / 100 * cap
        net = h.load + h.ev - h.pv  # + deficit / - surplus
        bus = 0.0  # + charging
        if st.mode is HourMode.SELL:
            lo = max(lo, p.min_sell_soc / 100 * cap)
            if i == 0 or settings[i - 1] != st:  # a new sell run starts here
                if st.power_kw is not None or sell_power_kw is not None:
                    run_kw = min(st.power_kw if st.power_kw is not None else sell_kw, p.max_discharge_kw)
                else:
                    run = 1
                    while i + run < len(settings) and settings[i + run] == st:
                        run += 1
                    peak = max(max(0.0, x.load + x.ev - x.pv) for x in hours[i:i + run])
                    spread = max(0.0, soc - lo) * eta / run
                    want = max(math.floor(spread * 10) / 10, math.ceil((peak + 0.3) * 10) / 10)
                    run_kw = min(p.max_discharge_kw, p.grid_export_kw + peak, want)
                if powers is not None:
                    powers[i] = run_kw
            if h.sell <= 0:
                # hard rule: no Export First where the RCE price is unknown (or not positive)
                cost += broke("sell_without_price")
            if target < p.min_soc / 100 * cap + reserve[i] - 0.5 * max(1, p.soc_step) / 100 * cap:
                # hard rule (reserve), independent of the forecast SoC: a sell slot may never
                # sell below what the house needs in the following expensive hours
                cost += broke("sell_below_reserve")
        # the inverter charges from the grid whenever a grid-charge slot is active - it knows
        # nothing about prices - so a merge that charges above the cap must be rejected
        over_cap = p.max_grid_charge_price is not None and h.buy > p.max_grid_charge_price + 1e-9
        if (st.mode is HourMode.NORMAL and battery_held(h, st.soc, p)):
            # hard rule (battery first in expensive hours): no floor above min SoC there
            cost += broke("battery_held_in_expensive_hour")
        if st.mode is HourMode.GRID_CHARGE and over_cap:
            # hard rule, independent of the forecast: if the battery is lower than predicted,
            # the inverter would charge from the grid at this price (and the house runs on grid)
            cost += broke("grid_charge_above_price_cap")
        if st.mode is HourMode.GRID_CHARGE and soc < target:
            e = min(target - soc, p.max_charge_kw * eta, max_charge_kwh(soc / cap * 100, p))
            bus = e / eta
        elif st.mode is HourMode.SELL and soc > lo:
            # the battery output covers the house and exports at most the inverter's sell limit
            out_kw = max(0.0, min(run_kw, p.grid_export_kw + net))
            e = min(soc - lo, out_kw / eta, p.max_discharge_kw)
            bus = -e * eta
        elif net < 0 and soc < hi:
            e = min(-net * eta, hi - soc, p.max_charge_kw * eta, max_charge_kwh(soc / cap * 100, p))
            bus = e / eta
        elif net > 0 and soc > lo:
            e = min(net / eta, soc - lo, p.max_discharge_kw)
            bus = -e * eta
        if st.mode is HourMode.GRID_CHARGE and soc >= target and net > 0 and soc > lo:
            e = min(net / eta, soc - target, p.max_discharge_kw)
            bus = -e * eta
        soc += bus * eta if bus > 0 else bus / eta
        g = net + bus
        imp, exp = max(0.0, g), max(0.0, -g)
        if st.mode is HourMode.SELL and bus < 0 and net > 0:
            # Export First discharges at full slot power until the floor, then the battery stops
            # covering the house for the rest of the slot: the house buys from the grid
            rate = min(run_kw, p.grid_export_kw + net)
            tau = min(1.0, -bus / rate) if rate > 0 else 0.0
            shift = net * (1.0 - tau)
            if shift > 0.05:
                # hard rule: a sell slot never exports and then buys within the same hour - it
                # would hit its floor early and leave the house on the grid for the rest
                cost += broke("sell_then_buy_in_hour")
            imp += shift
            exp += shift
        exp = min(exp, p.grid_export_kw)
        if bus < 0:
            sold_since_cheap += max(0.0, min(-bus, -g))  # the part of the discharge that was exported
        cheap = p.max_grid_charge_price is not None and h.buy <= p.max_grid_charge_price + 1e-9
        to_house = min(-bus, max(0.0, net)) if bus < 0 else 0.0
        if grid_energy and cheap and to_house > max(1, p.soc_step) / 100 * cap * eta + 0.1:
            # hard rule: grid energy in the battery waits for the expensive hours (no cycle to
            # save the cheap price); below one SoC step it is rounding, as in the optimiser
            cost += broke("grid_energy_used_in_cheap_hour")
        if st.mode is HourMode.GRID_CHARGE and bus > 0:
            grid_energy = True
        if soc <= p.min_soc / 100 * cap + 0.1:
            grid_energy = False
        house_import = min(imp, max(0.0, h.load - h.pv))
        if expensive and sold_since_cheap > 0.1 and house_import > 0.1:
            # hard rule (reserve): battery energy was sold and now the house buys at this price
            cost += broke("house_buys_after_sale")
        wear = (-bus / eta) * p.wear_cost if bus < 0 else 0.0
        hour_cost = imp * h.buy - (exp * h.sell if h.sell > 0 else 0.0) + wear
        cost += hour_cost
        traj.append(soc / cap * 100)
        if detail is not None:
            detail.append({
                "start": h.start.isoformat(), "mode": st.mode.value, "floor": st.soc,
                "soc": round(soc / cap * 100), "import": round(imp, 2), "export": round(exp, 2),
                "cost": round(hour_cost, 3), "buy": round(h.buy, 4), "sell": round(h.sell, 4),
                "load": round(h.load, 2), "ev": round(h.ev, 2), "pv": round(h.pv, 2),
                "battery": round(-bus if bus < 0 else 0.0, 2),  # battery output to the house bus
            })
    tp = min((h.buy for h in hours), default=0.0) * eta
    cost -= (soc - p.min_soc / 100 * cap) * tp
    if p.require_full and hours and max(traj, default=0.0) < p.max_soc - 1:
        # the periodic 100 % charge is due in this window: schedules that skip it lose
        cost += FULL_CHARGE_PENALTY
        if violations is not None:
            violations.append("full_charge_missing")
    return cost, traj


_SLOT_TO_MODE = {v: k for k, v in _MODE_TO_SLOT.items()}


def settings_from_plan(plan: DayPlan, hours: list[HourInput]) -> list[Setting]:
    """What an existing inverter schedule does in each hour (slot active at the hour start)."""
    out = []
    for h in hours:
        slot = plan.active_slot(h.start)
        mode = _SLOT_TO_MODE[slot.mode]
        out.append(Setting(mode, slot.soc, slot.power / 1000 if mode is HourMode.SELL else None))
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
    # deterministic order (a set of enums iterates in hash order, which changes between runs)
    return sorted(c, key=lambda st: (st.mode.value, st.soc, st.power_kw or 0.0))


TIDY_TOLERANCE = 0.05  # PLN: a simpler schedule is worth this much


def compress(
    plan: list[HourPlan],
    hours: list[HourInput],
    soc_now: float,
    params: BatteryParams,
    slots: int = SLOT_COUNT,
    window: int = 24,
    grid_energy: bool = False,
) -> tuple[list[Segment], float]:
    """Greedy cost-aware merge of hourly settings into ``slots`` segments (first 24 h)."""
    n = min(window, len(plan))
    plan, hours = plan[:n], hours[:n]
    segs: list[Segment] = []
    for i, hp in enumerate(plan):
        st = hour_setting(hp, hours[i], stored_energy_value(plan, hours, i, params), params)
        if segs and segs[-1].setting == st:
            segs[-1].length += 1
        else:
            segs.append(Segment(i, 1, st))

    def cost_of(ss: list[Segment]) -> float:
        # + the optimiser's holding cost: of two equally cheap schedules, the one that charges
        # later (just before the energy is needed) wins
        cost, traj = simulate(hours, _expand(ss), soc_now, params, grid_energy=grid_energy)
        return cost + sum(hold_cost(s, params) for s in traj)

    current = cost_of(segs)
    while len(segs) > slots:
        best = None
        for i in range(len(segs) - 1):
            a, b = segs[i], segs[i + 1]
            for cand in _candidates(a.setting, b.setting):
                trial = segs[:i] + [Segment(a.start, a.length + b.length, cand)] + segs[i + 2 :]
                c = cost_of(trial)
                # on a tie prefer the passive setting: a "sell"/"grid charge" slot that changes
                # nothing would still put the inverter in Export First / grid-charge mode
                key = (round(c, 3), 0 if cand.mode is HourMode.NORMAL else 1)
                if best is None or key < best[0]:
                    best = (key, trial)
        segs = best[1]  # type: ignore[index]
        current = cost_of(segs)
        # re-join neighbours that became identical
        joined: list[Segment] = []
        for s in segs:
            if joined and joined[-1].setting == s.setting:
                joined[-1].length += s.length
            else:
                joined.append(s)
        segs = joined
    # Tidy: join neighbours of the same mode when it costs (almost) nothing - e.g. grid charge
    # to 38 %, 40 %, 48 % in three slots becomes one slot to 48 % - which frees slots below.
    merged = True
    while merged:
        merged = False
        for i in range(len(segs) - 1):
            a, b = segs[i], segs[i + 1]
            if a.setting.mode is not b.setting.mode:
                continue
            best = None
            for cand in _candidates(a.setting, b.setting):
                if cand.mode is not a.setting.mode:
                    continue
                trial = segs[:i] + [Segment(a.start, a.length + b.length, cand)] + segs[i + 2 :]
                c = cost_of(trial)
                if best is None or c < best[0] - 1e-9:
                    best = (c, trial)
            if best is not None and best[0] <= current + TIDY_TOLERANCE:
                current, segs, merged = best[0], best[1], True
                break
    # Free slots: cut the idle tail off sell slots (e.g. sell 20:00, then the battery is at the
    # floor all night - the night part becomes a normal slot instead of Export First).
    changed = True
    while changed and len(segs) < slots:
        changed = False
        detail: list[dict] = []
        simulate(hours, _expand(segs), soc_now, params, detail=detail, grid_energy=grid_energy)
        for i, sg in enumerate(segs):
            if sg.setting.mode is not HourMode.SELL or sg.length < 2:
                continue
            idx = range(sg.start, sg.start + sg.length)
            active = [k for k in idx if detail[k]["export"] > 0.05]
            last = active[-1] if active else sg.start - 1
            tail = sg.start + sg.length - 1 - last
            if tail < 1:
                continue
            head_len = sg.length - tail
            parts = ([Segment(sg.start, head_len, sg.setting)] if head_len else []) + [
                Segment(sg.start + head_len, tail, Setting(HourMode.NORMAL, sg.setting.soc))]
            trial = segs[:i] + parts + segs[i + 1 :]
            if len(trial) <= slots and cost_of(trial) <= current + TIDY_TOLERANCE:
                segs, current, changed = trial, cost_of(trial), True
                break
    while len(segs) < slots:  # pad: split the longest segment
        i = max(range(len(segs)), key=lambda k: segs[k].length)
        s = segs[i]
        if s.length < 2:
            break
        half = s.length // 2
        segs[i : i + 1] = [Segment(s.start, half, s.setting), Segment(s.start + half, s.length - half, s.setting)]
    return segs, simulate(hours, _expand(segs), soc_now, params, grid_energy=grid_energy)[0]


def to_day_plan(
    segments: list[Segment], hours: list[HourInput], params: BatteryParams,
    current: DayPlan | None = None,
    soc_now: float | None = None,
) -> DayPlan:
    max_w = int(min(MAX_POWER_W, params.max_discharge_kw * 1000))
    powers: dict[int, float] = {}
    if soc_now is not None:
        simulate(hours, _expand(segments) + [Setting(HourMode.NORMAL, 0)] * (len(hours) - sum(
            s.length for s in segments)), soc_now, params, powers=powers)
    slots = []
    for seg in segments:
        dt: datetime = hours[seg.start].start
        soc = int(seg.setting.soc)
        if seg.setting.mode is HourMode.SELL:  # sell slot SoC = discharge floor
            soc = max(soc, params.min_sell_soc)
        power = max_w
        if seg.setting.mode is HourMode.SELL and seg.start in powers:
            power = min(max_w, int(round(powers[seg.start] * 10)) * 100)
        slot = Slot(dt.hour * 100 + dt.minute, power, soc, 0)
        slots.append(slot.with_mode(_MODE_TO_SLOT[seg.setting.mode]))
    return DayPlan(tuple(slots)).merge_preserved_flags(current)
