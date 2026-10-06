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
