from datetime import datetime, timedelta

import pytest

from core.forecast.ev import EvForecast
from core.optimizer import BatteryParams, Strategy
from core.plan import PlanError, SlotMode
from core.planner import PlannerInputs, parse_fallback_plan, run_planner
from core.tariff import G13Tariff

NOW = datetime(2026, 10, 5, 14, 20)


def test_end_to_end_plan_is_valid_and_not_worse_than_baseline():
    hours = [NOW.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    pv = {h: (3.0 if 9 <= h.hour < 15 else 0.0) for h in hours}
    sell = {h: (1.2 if h.hour in (19, 20) else 0.35) for h in hours[:34]}
    inp = PlannerInputs(NOW, 40, pv, [0.6] * 36, [0.4] * 36, EvForecast([0.0] * 36, [], [False] * 36),
                        sell, G13Tariff(1.0, 1.4, 0.6))
    res = run_planner(inp, BatteryParams(soc_step=5), Strategy.MAX_GRID_TRADING, arbitrage=False)
    assert len(res.plan.slots) == 6
    assert res.plan.slots[0].start == 1400
    assert res.missing_sell_hours == 2
    assert res.expected_savings >= -0.01


def test_fallback_plan_parsing():
    p = parse_fallback_plan("22:00 90 grid_charge; 06:00 20; 13:00 20; 16:00 40; 19:00 20 sell; 21:00 20")
    assert p.slots[0].mode is SlotMode.GRID_CHARGE
    assert p.slots[4].mode is SlotMode.SELL
    with pytest.raises(PlanError):
        parse_fallback_plan("22:00 90")


def test_slot_window_and_inverter_view():
    hours = [NOW.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    pv = {h: (3.0 if 9 <= h.hour < 15 else 0.0) for h in hours}
    sell = {h: (1.2 if h.hour in (19, 20) else 0.35) for h in hours}
    inp = PlannerInputs(NOW, 40, pv, [0.6] * 36, [0.4] * 36, EvForecast([0.0] * 36, [], [False] * 36),
                        sell, G13Tariff(1.0, 1.4, 0.6), 36, window_h=18)
    res = run_planner(inp, BatteryParams(soc_step=5), Strategy.MAX_GRID_TRADING, arbitrage=False,
                      current=res_plan(inp))
    assert sum(s.length for s in res.segments) == 18  # slots built from the window only
    assert len(res.slot_hourly) == 18 and len(res.current_hourly) == 18
    assert {"mode", "floor", "soc", "import", "export", "cost"} <= set(res.slot_hourly[0])
    assert abs(sum(h["cost"] for h in res.slot_hourly) - res.slot_plan_cost) < 50  # minus terminal value


def res_plan(inp):
    return parse_fallback_plan("00:00 20; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50; 21:00 20")


def test_no_sales_planned_into_hours_without_rce_price():
    """Regression: missing RCE hours were filled with the day average (0.94) -> fake night
    arbitrage (grid charge at 01:00, sell at 02:00)."""
    hours = [NOW.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    known = {h: 0.5 for h in hours[:10]}  # only the next 10 h are known
    inp = PlannerInputs(NOW, 50, {h: 0.0 for h in hours}, [0.5] * 36, [0.0] * 36,
                        EvForecast([0.0] * 36, [], [False] * 36), known, G13Tariff(0.9, 1.5, 0.63), 36)
    res = run_planner(inp, BatteryParams(soc_step=5), Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert res.missing_sell_hours == 26
    assert all(h.battery_export < 0.06 for h in res.optimum.hours[10:])


def test_violation_sell_below_expensive_reserve():
    from core.optimizer import HourInput
    from core.plan import DayPlan, Slot
    from core.planner import plan_violations
    d0 = datetime(2026, 10, 12, 19)
    hrs = [HourInput(d0 + timedelta(hours=i), 0.0, l, 0.0, b, 1.4)
           for i, (l, b) in enumerate([(1.0, 1.5), (3.0, 1.5), (0.5, 0.63)])]
    p = BatteryParams(capacity_kwh=20, min_soc=20, max_soc=98, max_charge_kw=10, max_discharge_kw=10,
                      min_sell_soc=20, max_grid_charge_price=0.63, battery_first_above=0.63,
                      reserve_margin=0.2)
    def plan(soc):
        s = [Slot(0, 10000, 20, 0), Slot(1900, 10000, soc, 0).with_mode(SlotMode.SELL),
             Slot(2100, 10000, 20, 0), Slot(2200, 10000, 20, 0), Slot(2300, 10000, 20, 0),
             Slot(2330, 10000, 20, 0)]
        return DayPlan(tuple(s))
    assert "sell_below_expensive_reserve" in plan_violations(plan(20), hrs, p, Strategy.MAX_GRID_TRADING)
    assert "sell_below_expensive_reserve" not in plan_violations(plan(50), hrs, p, Strategy.MAX_GRID_TRADING)
