"""End-to-end planning pipeline: forecasts + prices -> hourly optimum -> 6-slot DayPlan."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .compress import Segment, compress, simulate, to_day_plan
from .forecast.ev import EvForecast
from .optimizer import BatteryParams, HourInput, OptimizeResult, Strategy, optimize, simulate_baseline
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

    @property
    def expected_savings(self) -> float:
        return self.baseline_cost - self.slot_plan_cost


def horizon_hours(now: datetime, n: int) -> list[datetime]:
    h0 = now.replace(minute=0, second=0, microsecond=0)
    return [h0 + timedelta(hours=i) for i in range(n)]


def build_hours(inp: PlannerInputs, hours: list[datetime]) -> tuple[list[HourInput], int]:
    missing = 0
    known = [v for v in inp.sell.values()]
    fallback = (sum(known) / len(known)) if known else 0.0
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
    opt = optimize(hin, inp.soc, params, strategy, arbitrage)
    segs, _ = compress(opt.hours, hin, inp.soc, params)
    plan = to_day_plan(segs, hin, params, current)
    day = hin[:24]
    slot_cost = simulate(day, [s.setting for s in segs for _ in range(s.length)], inp.soc, params)[0]
    base = simulate_baseline(day, inp.soc, params)
    notes = list(opt.notes)
    if missing:
        notes.append(f"sell_price_missing_{missing}h")
    return PlannerResult(plan, hin, opt, segs, slot_cost, base, missing, notes)


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
