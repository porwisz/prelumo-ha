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


def test_battery_first_in_expensive_hours():
    """Regression (user report): 16-18 at 1.45 the battery was held at 98% and the house bought
    from the grid, to sell the energy at 18-20 for RCE 1.53-1.65. With battery-first on, the
    house runs on the battery in expensive hours and only the rest is sold.
    """
    from dataclasses import replace
    from core.compress import compress, to_day_plan
    buy = [0.65, 0.65] + [1.45] * 5 + [0.65] * 3
    sell = [0.6, 0.7, 0.9, 1.24, 1.53, 1.65, 1.41, 1.0, 0.9, 0.8]
    load = [1.0] * 10
    hrs = day(buy, sell, load=load)
    base = replace(P, max_soc=98, max_grid_charge_price=0.65, min_sell_soc=20)
    off = optimize(hrs, 98, base, Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert off.hours[2].grid_import > 0.5  # old behaviour: buys at 1.45 to sell later
    on_p = replace(base, battery_first_above=0.65)
    on = optimize(hrs, 98, on_p, Strategy.MAX_GRID_TRADING, arbitrage=True)
    for i in (2, 3):  # 16:00-18:00
        assert on.hours[i].grid_import < 0.06, on.hours[i]
    assert any(h.battery_export > 1 for h in on.hours[4:6])  # still sells what is left
    segs, _ = compress(on.hours, hrs, 98, on_p)
    plan = to_day_plan(segs, hrs, on_p)
    for slot in plan.slots:
        if slot.start // 100 in (2, 3) and slot.mode.value == "none":
            assert slot.soc == on_p.min_soc


def test_battery_first_survives_slot_merging_and_replaces_bad_plan():
    """Regression (user report, 2nd): cheap 15:00 (hold 98% is right there) was merged with the
    expensive 16-18 into one 'normal 98%' slot, and the gain threshold kept the bad schedule.
    """
    from dataclasses import replace
    from datetime import datetime as _dt
    from core.forecast.ev import EvForecast
    from core.planner import PlannerInputs, plan_violations, run_planner
    from core.tariff import G13Tariff
    now = _dt(2026, 10, 12, 12, 0)  # Monday
    hours = [now + timedelta(hours=i) for i in range(36)]
    rce = {12: 0.58, 13: 0.61, 14: 0.65, 15: 0.72, 16: 0.9, 17: 1.24, 18: 1.53, 19: 1.65, 20: 1.41}
    sell = {h: rce.get(h.hour, 0.6) for h in hours[:12]}
    pv = {h: max(0.0, 2.0 - abs(h.hour - 12) * 0.5) for h in hours}
    inp = PlannerInputs(now, 30, pv, [0.6] * 36, [0.4] * 36, EvForecast([0.0] * 36, [], [False] * 36),
                        sell, G13Tariff(1.05, 1.45, 0.65), 36)
    p = replace(P, max_soc=98, max_grid_charge_price=0.65, min_sell_soc=20, soc_step=2,
                battery_first_above=0.65)
    r = run_planner(inp, p, Strategy.MAX_GRID_TRADING, True)
    for h in r.hours[4:6]:  # 16:00, 17:00
        slot = r.plan.active_slot(h.start)
        assert not (slot.mode.value == "none" and slot.soc > p.min_soc + 1), r.plan.as_list()
    bad = parse_fallback_plan("11:00 10; 14:00 98 grid_charge; 16:00 98; 18:00 20 sell; 00:00 98 grid_charge; 07:00 22 sell")
    assert "battery_held_in_expensive_hour" in plan_violations(bad, r.hours[:24], p, Strategy.MAX_GRID_TRADING)
    again = run_planner(inp, p, Strategy.MAX_GRID_TRADING, True, current=bad)
    assert again.gain_vs_current is None  # cost comparison does not protect a rule-breaking plan


def test_reserve_for_expensive_hours_after_selling():
    """Regression (user report): sold down to 20% at 19:00 (RCE 1.65), then the heat pump bought
    at 20:00 for 1.4961. With battery-first, selling keeps the forecast need of the remaining
    expensive hours (+ margin).
    """
    from dataclasses import replace
    from core.optimizer import expensive_reserve
    buy = [0.63, 1.5, 1.5, 1.5, 0.63, 0.63]
    sell = [0.6, 1.53, 1.65, 1.41, 0.9, 0.8]
    load = [0.5, 0.5, 0.5, 3.0, 0.5, 0.5]  # heat pump at 20:00
    hrs = day(buy, sell, load=load)
    p = replace(P, max_soc=98, min_sell_soc=20, max_grid_charge_price=0.63,
                battery_first_above=0.63, reserve_margin=0.3)
    res = expensive_reserve(hrs, p)
    assert res[2] > 3.0 * 1.3  # after 19:00: 20:00 needs 3 kWh + 30 %
    assert res[3] == 0.0 and res[0] > res[2]
    r = optimize(hrs, 90, p, Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert r.hours[3].grid_import < 0.06, r.hours[3]  # 20:00 covered by the battery
    assert any(h.battery_export > 1 for h in r.hours[1:3])  # still sells the surplus
    assert r.hours[2].soc_end >= p.min_soc + res[2] / p.capacity_kwh * 100 - P.soc_step
    off = optimize(hrs, 90, replace(p, battery_first_above=None), Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert off.hours[3].grid_import > 1  # without the rule it sells everything and buys back


def test_sell_slots_not_merged_through_the_reserve():
    """Regression (user report, 2nd): sell 19:00 (floor 28%) and 20:00 (floor 20%) were merged
    into one 'sell to 20%' slot - the inverter would sell everything at 19:00 and the house would
    buy at 20:00. A sale followed by house import in the same expensive run is a hard rule.
    """
    from dataclasses import replace
    from core.compress import Setting, simulate
    hrs = day([1.5, 1.5, 0.63], [1.65, 1.41, 0.9], load=[0.5, 1.5, 0.5])
    p = replace(P, max_soc=98, min_sell_soc=20, battery_first_above=0.63, max_grid_charge_price=0.63)
    split = [Setting(HourMode.SELL, 30), Setting(HourMode.NORMAL, 10), Setting(HourMode.NORMAL, 10)]
    merged = [Setting(HourMode.SELL, 20), Setting(HourMode.SELL, 20), Setting(HourMode.NORMAL, 10)]
    assert simulate(hrs, merged, 60, p)[0] > 5e3  # sells to 20%, then buys at 1.5
    assert simulate(hrs, split, 60, p)[0] < 1e3


def test_sell_slot_floor_respects_reserve_regardless_of_forecast_soc():
    from dataclasses import replace
    from core.compress import Setting, simulate
    hrs = day([1.5, 1.5, 0.63], [1.65, 1.41, 0.9], load=[0.5, 3.0, 0.5])
    p = replace(P, max_soc=98, min_sell_soc=20, battery_first_above=0.63, max_grid_charge_price=0.63)
    # floor 20 % at 19:00 would leave nothing for the 3 kWh at 20:00 if the SoC were lower
    low_floor = [Setting(HourMode.SELL, 20), Setting(HourMode.NORMAL, 10), Setting(HourMode.NORMAL, 10)]
    assert simulate(hrs, low_floor, 98, p)[0] > 5e3
    ok_floor = [Setting(HourMode.SELL, 40), Setting(HourMode.NORMAL, 10), Setting(HourMode.NORMAL, 10)]
    assert simulate(hrs, ok_floor, 98, p)[0] < 1e3


def test_idle_sell_tail_becomes_normal_slot():
    """Regression (user report): the sell slot 20:00 ran all night at the floor (nothing to sell)
    and padding duplicated it - the inverter would sit in Export First the whole night."""
    from dataclasses import replace
    from core.compress import Segment, Setting, compress
    from core.optimizer import HourPlan
    buy = [1.5] * 3 + [0.63] * 9
    sell = [1.53, 1.65, 1.41] + [0.0] * 9
    hrs = day(buy, sell, load=[0.7] * 12)
    p = replace(P, min_soc=20, max_soc=98, min_sell_soc=20)
    plan = [HourPlan(hrs[0].start, 90, 64, HourMode.SELL, 0, 5, 0, 5, -8),
            HourPlan(hrs[1].start, 64, 26, HourMode.SELL, 0, 8, 0, 8, -12),
            HourPlan(hrs[2].start, 26, 20, HourMode.SELL, 0, 0.7, 0, 0.7, -0.8)] + [
            HourPlan(h.start, 20, 20, HourMode.NORMAL, 0.7, 0, 0, 0, 0.44) for h in hrs[3:]]
    segs, _ = compress(plan, hrs, 90, p, slots=6)
    for sg in segs:
        if sg.setting.mode is HourMode.SELL:
            assert sg.start + sg.length <= 3, segs  # no sell slot reaches into the night


def test_no_export_first_where_price_unknown():
    from core.compress import Setting, simulate
    hrs = day([0.63, 0.63], [0.9, 0.0])  # second hour: RCE not published yet
    assert simulate(hrs, [Setting(HourMode.SELL, 20)] * 2, 20, P)[0] > 5e3
    assert simulate(hrs, [Setting(HourMode.SELL, 20), Setting(HourMode.NORMAL, 20)], 20, P)[0] < 1e3
