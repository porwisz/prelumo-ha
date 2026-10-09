from datetime import datetime, timedelta

from core.compress import compress, simulate, to_day_plan
from core.diff import WriteGuard, WritePolicy, material_changes
from core.optimizer import (
    BatteryParams, HourInput, HourMode, Strategy, optimize, simulate_baseline,
)
from core.plan import SlotMode
from core.planner import parse_fallback_plan

D0 = datetime(2026, 10, 5)  # Monday
P = BatteryParams(capacity_kwh=20, min_soc=10, max_soc=100, max_charge_kw=10, max_discharge_kw=10,
                  roundtrip_efficiency=0.9, wear_cost=0.05, grid_import_kw=17, grid_export_kw=12,
                  soc_step=5)


def day(buy, sell, pv=None, load=None, ev=None, allowed=None):
    n = len(buy)
    return [HourInput(D0 + timedelta(hours=i), (pv or [0] * n)[i], (load or [1] * n)[i],
                      (ev or [0] * n)[i], buy[i], sell[i], (allowed or [False] * n)[i])
            for i in range(n)]


G13_BUY = [0.6] * 7 + [1.0] * 6 + [0.6] * 3 + [1.4] * 5 + [0.6] * 3
FLAT_SELL = [0.3] * 24


def test_grid_charge_off_peak_for_peak_self_use():
    r = optimize(day(G13_BUY, FLAT_SELL), 10, P, Strategy.SELF_CONSUMPTION)
    assert any(h.mode is HourMode.GRID_CHARGE for h in r.hours[:7])
    assert all(h.battery_export < 0.06 for h in r.hours)
    assert r.total_cost < simulate_baseline(day(G13_BUY, FLAT_SELL), 10, P)


def test_trading_sells_into_price_spike_only_when_allowed():
    sell = [0.3] * 24
    sell[19] = 3.0
    hrs = day([0.6] * 24, sell)
    sc = optimize(hrs, 90, P, Strategy.SELF_CONSUMPTION)
    tr = optimize(hrs, 90, P, Strategy.MAX_GRID_TRADING)
    assert sc.hours[19].mode is not HourMode.SELL
    assert tr.hours[19].mode is HourMode.SELL
    assert tr.total_cost < sc.total_cost


def test_arbitrage_switch_blocks_selling_grid_energy():
    sell = [0.3] * 24
    sell[20] = 3.0
    hrs = day([0.5] * 24, sell)
    no_arb = optimize(hrs, 10, P, Strategy.MAX_GRID_TRADING, arbitrage=False)
    arb = optimize(hrs, 10, P, Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert arb.hours[20].battery_export > 1
    assert no_arb.hours[20].battery_export < 0.06


def test_battery_does_not_feed_ev_unless_allowed():
    ev = [0] * 24
    ev[18] = 10
    buy = [0.6] * 24
    buy[18] = 1.4
    hrs = day(buy, FLAT_SELL, ev=ev)
    r = optimize(hrs, 90, P, Strategy.SELF_CONSUMPTION)
    assert r.hours[18].soc_start - r.hours[18].soc_end <= 6  # only house load (1 kWh)
    allowed = [False] * 24
    allowed[18] = True
    r2 = optimize(day(buy, FLAT_SELL, ev=ev, allowed=allowed), 90, P)
    assert r2.hours[18].soc_start - r2.hours[18].soc_end > 30


def test_pv_surplus_charges_battery():
    pv = [0] * 9 + [6] * 6 + [0] * 9
    r = optimize(day([1.0] * 24, [0.1] * 24, pv=pv), 10, P)
    assert max(h.soc_end for h in r.hours) >= 80


def test_compress_to_six_valid_slots_and_cost_close():
    hrs = day(G13_BUY, FLAT_SELL, pv=[0] * 9 + [3] * 6 + [0] * 9)
    r = optimize(hrs, 30, P, Strategy.SELF_CONSUMPTION)
    segs, cost = compress(r.hours, hrs, 30, P)
    assert len(segs) == 6
    plan = to_day_plan(segs, hrs, P)
    assert plan.slots[0].start == 0
    assert any(s.mode is SlotMode.GRID_CHARGE for s in plan.slots)
    hourly_settings_cost = simulate(hrs, [s.setting for s in segs for _ in range(s.length)], 30, P)[0]
    assert hourly_settings_cost == cost
    assert cost <= simulate_baseline(hrs, 30, P) + 0.01


def test_write_guard():
    a = parse_fallback_plan("00:00 20 none; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 grid_charge; 21:00 20")
    b = parse_fallback_plan("00:00 22 none; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 grid_charge; 21:00 20")
    c = parse_fallback_plan("00:00 20 none; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 sell; 21:00 20")
    pol = WritePolicy(max_writes_per_day=2)
    assert material_changes(a, b, pol, D0) == []
    assert material_changes(a, c, pol, D0)[0] == "16:00:mode"
    rolled = parse_fallback_plan("03:00 20; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 grid_charge; 21:00 20")
    assert material_changes(a, rolled, pol, D0) == []  # same behaviour, different slot starts
    shifted = parse_fallback_plan("00:00 20; 05:15 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 grid_charge; 21:00 20")
    assert material_changes(a, shifted, pol, D0) == []  # 15-min boundary shift is not material
    g = WriteGuard(pol)
    assert g.decide(a, c, D0)[0]
    g.record(D0)
    assert g.decide(a, c, D0 + timedelta(minutes=10))[1][0] == "min_interval"
    g.record(D0 + timedelta(hours=1))
    assert g.decide(a, c, D0 + timedelta(hours=3))[1][0] == "daily_write_limit"


def test_max_grid_charge_price_blocks_expensive_charging():
    from dataclasses import replace
    buy = [1.0] * 7 + [1.4] * 17  # cheapest hours cost 1.0
    hrs = day(buy, FLAT_SELL)
    free = optimize(hrs, 10, P, Strategy.SELF_CONSUMPTION)
    assert any(h.mode is HourMode.GRID_CHARGE for h in free.hours)
    capped = optimize(hrs, 10, replace(P, max_grid_charge_price=0.65), Strategy.SELF_CONSUMPTION)
    assert all(h.grid_charge < 0.06 for h in capped.hours)
    ok = optimize(hrs, 10, replace(P, max_grid_charge_price=1.0), Strategy.SELF_CONSUMPTION)
    assert all(h.grid_charge < 0.06 for h in ok.hours if h.start.hour >= 7)  # only at <= 1.0


def test_min_sell_soc_floor():
    from dataclasses import replace
    sell = [0.3] * 24
    sell[19] = 3.0
    hrs = day([0.6] * 24, sell)
    r = optimize(hrs, 60, replace(P, min_sell_soc=40), Strategy.MAX_GRID_TRADING)
    assert r.hours[19].mode is HourMode.SELL
    assert all(h.soc_end >= 40 for h in r.hours if h.battery_export > 0.06)
    segs, _ = compress(r.hours, hrs, 60, replace(P, min_sell_soc=40))
    plan = to_day_plan(segs, hrs, replace(P, min_sell_soc=40))
    assert all(s.soc >= 40 for s in plan.slots if s.mode is SlotMode.SELL)


def test_compress_never_grid_charges_above_price_cap():
    """Regression: an expensive 'normal' hour must not be merged into a grid-charge slot."""
    from dataclasses import replace
    from core.optimizer import HourPlan
    cap = replace(P, max_grid_charge_price=0.65)
    # 20:00 is peak (1.45), the rest off-peak; alternate charge/sell to force many merges
    buy = [1.45, 1.45, 1.45] + [0.65] * 21
    sell = [2.0, 2.0, 0.3] + [0.8, 0.3] * 10 + [0.3]
    hrs = day(buy, sell)
    r = optimize(hrs, 70, cap, Strategy.MAX_GRID_TRADING, arbitrage=True)
    segs, _ = compress(r.hours, hrs, 70, cap)
    for s in segs:
        if s.setting.mode is HourMode.GRID_CHARGE:
            assert all(hrs[i].buy <= 0.65 for i in range(s.start, s.start + s.length)), s


def test_write_guard_min_gain():
    a = parse_fallback_plan("00:00 20 none; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 grid_charge; 21:00 20")
    c = parse_fallback_plan("00:00 20 none; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 sell; 21:00 20")
    g = WriteGuard(WritePolicy(min_gain=0.5))
    assert g.decide(a, c, D0, gain=0.2) == (False, ["gain_below_threshold", "0.20"])
    assert g.decide(a, c, D0, gain=1.0)[0]
    assert g.decide(None, c, D0, gain=0.0)[0]  # nothing readable on the inverter -> write


def test_current_plan_cost_and_gain():
    from datetime import datetime as _dt
    from core.forecast.ev import EvForecast
    from core.planner import PlannerInputs, run_planner
    from core.tariff import G13Tariff
    now = _dt(2026, 10, 5, 14, 0)
    hours = [now + timedelta(hours=i) for i in range(36)]
    inp = PlannerInputs(now, 50, {h: (3.0 if 9 <= h.hour < 15 else 0.0) for h in hours}, [0.6] * 36,
                        [0.4] * 36, EvForecast([0.0] * 36, [], [False] * 36),
                        {h: (1.5 if h.hour in (18, 19) else 0.3) for h in hours}, G13Tariff(1.0, 1.4, 0.6))
    first = run_planner(inp, P, Strategy.MAX_GRID_TRADING, arbitrage=False)
    again = run_planner(inp, P, Strategy.MAX_GRID_TRADING, arbitrage=False, current=first.plan)
    assert abs(again.gain_vs_current) < 0.01  # re-planning onto itself gains nothing
    worse = parse_fallback_plan("00:00 10 none; 05:00 10; 07:00 10; 13:00 10; 16:00 10; 21:00 10")
    vs_worse = run_planner(inp, P, Strategy.MAX_GRID_TRADING, arbitrage=False, current=worse)
    assert vs_worse.gain_vs_current > 0.5


def test_plan_violations():
    from dataclasses import replace
    from core.planner import plan_violations
    hrs = day(G13_BUY, FLAT_SELL)
    p = parse_fallback_plan("00:00 20; 05:00 80 grid_charge; 08:00 20; 13:00 20; 18:00 10 sell; 21:00 20")
    v = plan_violations(p, hrs, replace(P, max_grid_charge_price=0.65, min_sell_soc=20), Strategy.SELF_CONSUMPTION)
    assert v == ["grid_charge_above_price_cap", "sell_below_min_soc", "sell_in_self_consumption"]
    ok = parse_fallback_plan("00:00 20; 01:00 80 grid_charge; 06:00 20; 13:00 20; 18:00 20; 21:00 20")
    assert plan_violations(ok, hrs, replace(P, max_grid_charge_price=0.65), Strategy.SELF_CONSUMPTION) == []


def test_grid_charge_slot_never_covers_expensive_hour_even_if_battery_full():
    """Regression (user report): slot 15-17 'grid charge to 100%' covered the 16:00 peak hour.

    The forecast said the battery is full by 16:00, so no grid import was simulated - but if
    the forecast is off, the inverter charges at peak price. The cap is a hard rule.
    """
    from dataclasses import replace
    from core.compress import Setting, simulate
    cap = replace(P, max_grid_charge_price=0.65)
    hrs = day([0.65, 1.45], [0.3, 0.3], pv=[0, 2], load=[0, 0])
    full = simulate(hrs, [Setting(HourMode.GRID_CHARGE, 100)] * 2, 100, cap)[0]
    assert full > 5e3  # penalised although nothing is imported at 1.45
    ok = simulate(hrs, [Setting(HourMode.GRID_CHARGE, 100), Setting(HourMode.NORMAL, 100)], 100, cap)[0]
    assert ok < 1e3


def test_expensive_hour_battery_covers_house_down_to_min_soc():
    """Regression (user report): 17:00 at 1.45 PLN got floor 98% because the forecast load was
    tiny; a higher real load would be bought at peak while the energy was sold at 0.86 at 18:00.
    """
    from core.compress import Setting, hour_setting, stored_energy_value
    from core.optimizer import HourPlan
    hrs = day([0.65, 1.45, 1.45, 1.45], [0.5, 0.71, 0.86, 0.85], load=[0.2, 0.4, 0.5, 0.5])
    plan = [
        HourPlan(hrs[0].start, 90, 100, HourMode.GRID_CHARGE, 2, 0, 2, 0, 1.3),
        HourPlan(hrs[1].start, 100, 98, HourMode.NORMAL, 0, 0, 0, 0, 0),
        HourPlan(hrs[2].start, 98, 50, HourMode.SELL, 0, 9, 0, 9, -7),
        HourPlan(hrs[3].start, 50, 24, HourMode.SELL, 0, 4, 0, 4, -3),
    ]
    v = stored_energy_value(plan, hrs, 1, P)
    assert v < 1.45  # selling later earns 0.86 at most
    assert hour_setting(plan[1], hrs[1], v, P) == Setting(HourMode.NORMAL, P.min_soc)
    # cheap now, expensive later -> holding is right, keep the trajectory floor
    cheap = day([0.65, 0.65, 1.45], [0.3, 0.3, 0.3])
    hold = [HourPlan(cheap[0].start, 80, 80, HourMode.NORMAL, 0.2, 0, 0, 0, 0.1),
            HourPlan(cheap[1].start, 80, 80, HourMode.NORMAL, 0.2, 0, 0, 0, 0.1),
            HourPlan(cheap[2].start, 80, 60, HourMode.NORMAL, 0, 0, 0, 0, 0)]
    v2 = stored_energy_value(hold, cheap, 0, P)
    assert hour_setting(hold[0], cheap[0], v2, P) == Setting(HourMode.NORMAL, 80)
