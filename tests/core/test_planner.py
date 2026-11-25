from datetime import datetime, timedelta

import pytest

from core.forecast.ev import EvForecast
from core.optimizer import BatteryParams, HourMode, Strategy
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
    hrs = [HourInput(d0 + timedelta(hours=i), 0.0, ld, 0.0, b, 1.4)
           for i, (ld, b) in enumerate([(1.0, 1.5), (3.0, 1.5), (0.5, 0.63)])]
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


def test_shadow_ledger_ignores_penalty_costs():
    from datetime import date

    from core.shadow import ShadowLedger
    led = ShadowLedger()
    led.record_plan(date(2026, 10, 13), 5.0, 10.0)
    led.record_plan(date(2026, 10, 13), 80009.9, 10.9)  # penalised replan: not money
    assert led.total_expected_savings() == 5.0
    old = ShadowLedger.from_dict({"days": {"2026-10-13": {"plan_cost": 80009.9, "baseline_cost": 10.9,
                                                          "expected_savings": -79999.0, "actual_cost": -10.5}}})
    assert old.total_expected_savings() == 0.0 and old.days["2026-10-13"] == {"actual_cost": -10.5}


def test_full_charge_deadline_limits_the_window():
    """A charge for a coming power cut must land before the cut, not anywhere in the window."""
    from core.optimizer import BatteryParams, HourInput, HourMode, Strategy, optimize
    hrs = [HourInput(datetime(2026, 1, 5, 0) + timedelta(hours=i), 0.0, 0.5, 0.0,
                     1.5 if i < 3 else 0.3, 0.2) for i in range(12)]
    p = BatteryParams(capacity_kwh=10, min_soc=10, max_soc=100, max_charge_kw=10,
                      max_discharge_kw=10, soc_step=10, require_full=True,
                      charge_taper=((100, 10.0),))
    # without a deadline the cheap hours later are used
    late = optimize(hrs, 20, p, Strategy.SELF_CONSUMPTION, full_by=12)
    assert max(h.soc_end for h in late.hours[:3]) < 100
    # with a 3 h deadline it charges at once, expensive hours and all
    soon = optimize(hrs, 20, p, Strategy.SELF_CONSUMPTION, full_by=3)
    assert max(h.soc_end for h in soon.hours[:3]) == 100
    assert "full_charge_planned" in soon.notes
    assert any(h.mode is HourMode.GRID_CHARGE for h in soon.hours[:3])


def _morning_after_grid_charge(charged: bool):
    """9 Nov: grid charge 05:00-05:48 to 95 %, sell price at 05 high, cheap night until 07,
    a better sale at 07-08 - the planner sold 05:48-06:00 and recharged from 06:00."""
    now = datetime(2026, 11, 9, 5, 48)
    hours = [now.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    sell = {h: (1.6 if h.hour == 5 else 2.4 if h.hour in (7, 8) else 0.4) for h in hours[:20]}
    inp = PlannerInputs(now, 95, {h: 0.0 for h in hours}, [0.8] * 36, [0.2] * 36,
                        EvForecast([0.0] * 36, [], [False] * 36), sell, G13Tariff(0.9, 1.5, 0.63),
                        grid_energy=True, grid_charged_this_hour=charged)
    return run_planner(inp, BatteryParams(soc_step=2, arbitrage_min_margin=0.5), Strategy.MAX_GRID_TRADING,
                       arbitrage=True)


def test_no_sale_in_the_hour_the_battery_charged_from_the_grid():
    free = _morning_after_grid_charge(False)
    assert free.optimum.hours[0].mode is HourMode.SELL  # the scenario: without the rule it sells now
    res = _morning_after_grid_charge(True)
    assert res.hours[0].no_sell and not res.hours[1].no_sell
    assert res.optimum.hours[0].battery_export < 0.06
    assert res.plan.slots[0].mode is not SlotMode.SELL
    assert not any("sell_after_grid_charge_in_hour" in v for v in res.slot_plan_issues)
    assert any(h.mode is HourMode.SELL for h in res.optimum.hours[1:4])  # later sales still allowed


def test_current_schedule_selling_after_grid_charge_breaks_the_rule():
    from core.compress import Setting, simulate
    from core.optimizer import HourInput
    h0 = datetime(2026, 11, 9, 5)
    hrs = [HourInput(h0, 0.0, 0.2, 0.0, 0.63, 1.6, no_sell=True), HourInput(h0 + timedelta(hours=1), 0.0, 1.0, 0.0, 0.63, 0.4)]
    v: list[str] = []
    simulate(hrs, [Setting(HourMode.SELL, 70), Setting(HourMode.NORMAL, 20)], 95, BatteryParams(), violations=v)
    assert "05:00 sell_after_grid_charge_in_hour" in v


def test_estimated_sell_price_keeps_energy_for_tomorrow_and_is_noted():
    """Before tomorrow's RCE is out, a guessed morning price lets the plan hold energy for it
    instead of valuing those hours at nothing."""
    hours = [NOW.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    known = {h: 0.4 for h in hours[:10]}  # today only
    est = {h: (2.0 if h.hour == 7 else 0.4) for h in hours[10:]}
    kw = dict(now=NOW, soc=90, pv={h: 0.0 for h in hours}, base_load=[0.3] * 36, heat_pump=[0.0] * 36,
              ev=EvForecast([0.0] * 36, [], [False] * 36), sell=known, tariff=G13Tariff(0.9, 1.5, 0.63))
    p = BatteryParams(soc_step=5)
    plain = run_planner(PlannerInputs(**kw), p, Strategy.MAX_GRID_TRADING, arbitrage=False)
    guess = run_planner(PlannerInputs(**kw, sell_estimate=est), p, Strategy.MAX_GRID_TRADING, arbitrage=False)
    assert "sell_price_estimated_26h" in guess.notes and guess.missing_sell_hours == 0
    assert plain.missing_sell_hours == 26
    seven = next(i for i, h in enumerate(hours) if h.hour == 7 and i > 10)
    assert guess.optimum.hours[seven].mode is HourMode.SELL
    assert plain.optimum.hours[seven].mode is not HourMode.SELL


def test_pessimistic_pv_raises_the_reserve_kept_for_expensive_hours():
    from core.optimizer import HourInput, expensive_reserve

    d0 = datetime(2026, 11, 10, 8)
    p = BatteryParams(battery_first_above=0.65, pv_trust=0.5, reserve_margin=0.0)
    hrs = [HourInput(d0 + timedelta(hours=i), 2.0, 1.5, 0.0, 1.05, 0.5) for i in range(3)]
    low = [HourInput(h.start, h.pv, h.load, h.ev, h.buy, h.sell, pv_low=0.2) for h in hrs]
    assert expensive_reserve(low, p)[0] > expensive_reserve(hrs, p)[0]
    # never more PV than the forecast itself
    high = [HourInput(h.start, h.pv, h.load, h.ev, h.buy, h.sell, pv_low=5.0) for h in hrs]
    assert expensive_reserve(high, p)[0] == 0.0


# --- "sell no more than the PV produced in the month" (23 Nov 2026) -------------------------
from datetime import datetime as _dt  # noqa: E402

from core.compress import Setting, simulate  # noqa: E402
from core.optimizer import BatteryParams, HourMode, HourInput, Strategy  # noqa: E402
from core.planner import PlannerInputs, export_budgets, run_planner  # noqa: E402
from core.plan import SlotMode  # noqa: E402
from core.tariff import G13Tariff  # noqa: E402

_LIVE = BatteryParams(capacity_kwh=26.25, min_soc=20, max_soc=98, max_charge_kw=10, max_discharge_kw=10,
                      soc_step=2, max_grid_charge_price=0.63, battery_first_above=0.63, min_sell_soc=20,
                      sell_min_power_kw=6)
_RCE_23 = {0: 0.62, 1: 0.6, 2: 0.58, 3: 0.57, 4: 0.6, 5: 0.75, 6: 1.1, 7: 1.47, 8: 1.54, 9: 1.5,
           10: 1.34, 11: 1.2, 12: 1.1, 13: 1.0, 14: 1.05, 15: 1.2, 16: 1.45, 17: 1.6, 18: 1.55,
           19: 1.3, 20: 1.0, 21: 0.8, 22: 0.7, 23: 0.65}


def _morning_23(room):
    """The re-plan of 23 Nov 07:53: SoC 60 %, the morning's RCE 1.47-1.54, month over its PV."""
    now = _dt(2026, 11, 23, 7, 53)
    hs = [now.replace(minute=0) + timedelta(hours=i) for i in range(24)]
    sell = {h: _RCE_23[h.hour] for h in hs}
    pv = {h: (0.3 if 9 <= h.hour < 15 else 0.0) for h in hs}
    load = [1.3] * 24
    inp = PlannerInputs(now, 60.0, pv, load, [0.0] * 24, EvForecast([0.0] * 24, [], [False] * 24), sell,
                        G13Tariff(0.9048, 1.4961, 0.6257), 24, export_room=room)
    return run_planner(inp, _LIVE, Strategy.MAX_GRID_TRADING, arbitrage=True)


def test_oversold_month_writes_no_sale():
    """Regression (23 Nov): the month had exported 43 kWh more than its PV, the optimiser kept
    0.11 kWh of sale, and the merged slots wrote "sell to 40 %" for 07:00-13:00 - the battery
    sold ~3 kWh. A month over its PV gets no sale at all, in the plan and in the slots."""
    r = _morning_23({(2026, 11): 204.9 - 247.7})
    assert "sell_within_pv_blocked" in r.notes
    assert all(h.battery_export < 1e-6 for h in r.optimum.hours)
    assert all(s.mode is not SlotMode.SELL for s in r.plan.slots), r.plan.slots
    assert all(h["mode"] != "sell" for h in r.slot_hourly)


def test_month_with_room_sells_within_it_in_the_slots():
    budget = 1.0
    r = _morning_23({(2026, 11): budget})
    sold = sum(h["export"] for h in r.slot_hourly if h["mode"] == "sell")
    assert sold <= budget + 1.8 + 0.02 * _LIVE.capacity_kwh + 1e-6  # room + the plan's own PV
    assert not any("sell_over_month_pv" in v for v in r.slot_plan_issues)
    unlimited = _morning_23(None)  # the limit off: the morning prices do sell
    assert any(s.mode is SlotMode.SELL for s in unlimited.plan.slots)


def test_export_budgets():
    hs = [HourInput(_dt(2026, 11, 30, 22) + timedelta(hours=i), 1.0, 0.5, 0, 0.9, 1.0) for i in range(4)]
    b = [h.export_budget for h in export_budgets(hs, {(2026, 11): 3.0})]
    assert b == [5.0, 5.0, 0.0, 0.0]  # December: nothing produced yet, no room
    assert [h.export_budget for h in export_budgets(hs, {(2026, 11): -1.0})][:2] == [0.0, 0.0]


def test_running_hour_has_only_its_minutes_of_power():
    """Regression (23 Nov 07:53): the slot simulation sold 5.1 kWh in the 7 minutes left of the
    hour (~44 kW), so a sell slot to 40 % looked like a sale that ends at 08:00."""
    h = HourInput(_dt(2026, 11, 23, 7), 0.0, 0.15, 0, 0.9048, 1.47, duration_h=7 / 60)
    detail: list[dict] = []
    simulate([h], [Setting(HourMode.SELL, 40, 6.0)], 60, _LIVE, detail=detail)
    assert detail[0]["battery"] <= 6.0 * 7 / 60 + 1e-6
    assert detail[0]["soc"] >= 57  # ~0.7 kWh of 26 kWh, not 60 -> 40 %
