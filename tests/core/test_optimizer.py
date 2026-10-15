from datetime import datetime, timedelta

from core.compress import compress, simulate, to_day_plan
from core.diff import WriteGuard, WritePolicy, material_changes
from core.optimizer import (
    BatteryParams, HourInput, HourMode, Strategy, max_charge_kwh, optimize, simulate_baseline,
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
    # the limit resets at midnight, not 24 h after the writes
    g.history = [D0 + timedelta(hours=22), D0 + timedelta(hours=23)]
    assert g.decide(a, c, D0 + timedelta(hours=25))[0]


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
        assert on.hours[i].grid_import < P.soc_step / 100 * P.capacity_kwh, on.hours[i]  # at most SoC-grid rounding
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
    assert res[3] > 3.0 * 1.3  # 20:00 itself: a sell slot drains in minutes
    assert res[4] == 0.0 and res[0] > res[2]
    r = optimize(hrs, 90, p, Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert r.hours[3].grid_import < P.soc_step / 100 * P.capacity_kwh, r.hours[3]  # 20:00 covered by the battery
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


def test_sell_slot_at_full_power_hits_floor_and_house_buys():
    """Regression (user test on the inverter): sell 98 -> 95 % at full power exported the 3 % in
    minutes, then the house bought from the grid for the rest of the slot."""
    from dataclasses import replace
    from core.compress import Setting, simulate
    hrs = day([1.5, 0.63], [1.65, 0.9], load=[1.5, 0.5])
    p = replace(P, max_soc=98, min_sell_soc=20, battery_first_above=0.63, max_grid_charge_price=0.63)
    detail: list[dict] = []
    full = [Setting(HourMode.SELL, 95, 10.0), Setting(HourMode.NORMAL, 20)]
    cost, _ = simulate(hrs, full, 98, p, detail=detail)
    assert detail[0]["import"] > 1.0 and cost > 5e3  # hard rule: house on expensive grid


def test_sell_slot_power_spreads_the_sale_over_the_slot():
    from dataclasses import replace
    from core.compress import Segment, Setting, simulate, to_day_plan
    from core.plan import SlotMode
    hrs = day([1.5, 1.5, 0.63], [1.65, 1.6, 0.9], load=[1.0, 1.0, 0.5])
    p = replace(P, max_soc=98, min_sell_soc=20, battery_first_above=0.63, max_grid_charge_price=0.63,
                sell_min_power_kw=0)
    sts = [Setting(HourMode.SELL, 60), Setting(HourMode.SELL, 60), Setting(HourMode.NORMAL, 20)]
    detail: list[dict] = []
    powers: dict[int, float] = {}
    cost, _ = simulate(hrs, sts, 98, p, detail=detail, powers=powers)
    assert cost < 1e3 and detail[0]["import"] < 0.05 and detail[1]["import"] < 0.05
    assert 1.0 < powers[0] < 10.0  # not full power
    hrs7 = day([1.5, 1.5] + [0.63] * 5, [1.65, 1.6] + [0.9] * 5, load=[1.0, 1.0] + [0.5] * 5)
    segs = [Segment(0, 2, sts[0])] + [Segment(k, 1, sts[2]) for k in range(2, 7)]
    plan = to_day_plan(segs, hrs7, p, soc_now=98)
    assert plan.slots[0].mode is SlotMode.SELL and plan.slots[0].power == round(powers[0] * 10) * 100


def test_sell_export_capped_by_inverter_sell_limit():
    """The Deye 'max sell power' (5.39 kW) caps the export: a sell slot discharges at most
    house + limit, so the SoC falls slower than the slot power suggests."""
    from dataclasses import replace
    from core.compress import Setting, simulate
    hrs = day([0.63, 0.63], [1.0, 0.9], load=[1.0, 0.5])
    p = replace(P, max_soc=98, min_sell_soc=20, grid_export_kw=5.39)
    detail: list[dict] = []
    simulate(hrs, [Setting(HourMode.SELL, 20, 10.0), Setting(HourMode.NORMAL, 20)], 98, p, detail=detail)
    assert detail[0]["export"] <= 5.39 + 1e-6 and detail[0]["import"] < 0.05
    assert detail[0]["soc"] > 60  # ~6.4 kWh out of 20 kWh, not 10 kWh


def test_arbitrage_needs_min_margin():
    """Regression (user report): buy at 0.63 at 21:00, sell at RCE 0.93 at 22:00 - a full cycle
    for ~0.10 zl/kWh. With a 0.5 zl/kWh minimum margin this is not done; a big spread still is."""
    from dataclasses import replace
    buy = [0.63, 0.63, 0.63, 0.63]
    p = replace(P, min_soc=20, max_soc=98, min_sell_soc=20, wear_cost=0.1, max_grid_charge_price=0.63)
    small = day(buy, [0.6, 0.6, 0.93, 0.6], load=[0.5] * 4)
    r = optimize(small, 20, replace(p, arbitrage_min_margin=0.0), Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert any(h.battery_export > 1 for h in r.hours)
    r = optimize(small, 20, replace(p, arbitrage_min_margin=0.5), Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert all(h.battery_export < 0.5 for h in r.hours)
    big = day(buy, [0.6, 0.6, 1.8, 0.6], load=[0.5] * 4)
    r = optimize(big, 20, replace(p, arbitrage_min_margin=0.5), Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert any(h.battery_export > 1 for h in r.hours)


def test_sell_slot_never_exports_and_imports_in_one_hour():
    """Regression (user report): a sell slot at a cheap hour sold to its floor, then the house
    bought for the rest of the hour - selling and buying in the same slot makes no sense."""
    from dataclasses import replace
    from core.compress import Setting, simulate
    hrs = day([0.63, 0.63], [0.93, 0.8], load=[2.0, 0.5])
    p = replace(P, max_soc=98, min_sell_soc=20)
    detail: list[dict] = []
    cost, _ = simulate(hrs, [Setting(HourMode.SELL, 20, 10.0), Setting(HourMode.NORMAL, 20)], 26, p,
                       detail=detail)
    assert detail[0]["import"] > 0.05 and detail[0]["export"] > 0.05 and cost > 5e3


def test_cheap_night_charge_for_expensive_morning():
    """Regression (user report): no night charge at 0.63 for the 7-13 hours at 0.90 - SoC-grid
    rounding wasted up to a step every expensive hour and ate the ~0.10 zl/kWh margin."""
    from dataclasses import replace
    buy = [0.63] * 6 + [0.9048] * 6
    load = [0.3] * 6 + [0.25, 0.7, 0.3, 0.4, 1.2, 1.0]
    hrs = day(buy, [0.6] * 12, load=load)
    p = replace(P, min_soc=20, max_soc=98, soc_step=2, wear_cost=0.1, max_grid_charge_price=0.63,
                battery_first_above=0.63, arbitrage_min_margin=0.5)
    r = optimize(hrs, 20, p, Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert any(h.mode is HourMode.GRID_CHARGE for h in r.hours[:6])
    assert sum(h.grid_import for h in r.hours[6:]) < 1.5  # < half of the 3.85 kWh morning need


def test_adjacent_grid_charge_slots_are_joined():
    """Grid charge to 38 %, 40 %, 48 % in three slots becomes one grid-charge setting."""
    from dataclasses import replace
    from core.compress import compress
    from core.optimizer import HourPlan
    buy = [0.63] * 4 + [0.9] * 4
    hrs = day(buy, [0.5] * 8, load=[0.4] * 4 + [1.0] * 4)
    p = replace(P, min_soc=20, max_soc=98, soc_step=2, max_grid_charge_price=0.63)
    socs = [(30, 38), (38, 40), (40, 48), (48, 48), (48, 43), (43, 38), (38, 33), (33, 28)]
    plan = [HourPlan(hrs[i].start, a, b, HourMode.GRID_CHARGE if i < 3 else HourMode.NORMAL,
                     0, 0, 1 if i < 3 else 0, 0, 0) for i, (a, b) in enumerate(socs)]
    segs, _ = compress(plan, hrs, 30, p, window=8)
    charge = [s for s in segs if s.setting.mode is HourMode.GRID_CHARGE]
    assert len({s.setting for s in charge}) == 1 and charge[0].start == 0, segs  # one setting
    assert 30 < charge[0].setting.soc <= 48


def test_grid_energy_not_used_in_cheap_hours():
    """Regression (user report): energy charged from the grid fed the house at night (cheap
    hours) - a battery cycle to save the cheap price makes no sense; it waits for 7-13."""
    from dataclasses import replace
    from core.compress import Setting, simulate
    buy = [0.63] * 4 + [0.9048] * 4
    hrs = day(buy, [0.5] * 8, load=[0.6] * 8)
    p = replace(P, min_soc=20, max_soc=98, soc_step=2, max_grid_charge_price=0.63,
                battery_first_above=0.63)
    r = optimize(hrs, 40, p, Strategy.SELF_CONSUMPTION, grid_energy=True)
    assert all(h.soc_end >= 38 for h in r.hours[:4]), [h.soc_end for h in r.hours]
    free = optimize(hrs, 40, p, Strategy.SELF_CONSUMPTION, grid_energy=False)  # PV energy: free use
    assert sum(h.grid_import for h in free.hours[:4]) <= sum(h.grid_import for h in r.hours[:4])
    drain = [Setting(HourMode.NORMAL, 20)] * 8
    assert simulate(hrs, drain, 40, p, grid_energy=True)[0] > 5e3
    assert simulate(hrs, drain, 40, p, grid_energy=False)[0] < 1e3


def test_slots_keep_the_due_full_charge():
    """Regression (user report): the hourly optimum charged to 100 % at 13-16 (full charge due),
    but the 6 slots cut it to 44 % - the slot simulator did not know about the requirement."""
    from dataclasses import replace
    from core.compress import Setting, simulate
    hrs = day([0.63] * 3 + [1.5] * 3, [0.5] * 6, load=[0.5] * 6)
    p = replace(P, min_soc=20, max_soc=100, require_full=True, max_charge_kw=10)
    part = [Setting(HourMode.GRID_CHARGE, 44)] * 3 + [Setting(HourMode.NORMAL, 20)] * 3
    full = [Setting(HourMode.GRID_CHARGE, 100)] * 3 + [Setting(HourMode.NORMAL, 20)] * 3
    assert simulate(hrs, full, 30, p)[0] < simulate(hrs, part, 30, p)[0]
    assert simulate(hrs, part, 30, replace(p, require_full=False))[0] < 1e3


def test_cheap_hold_after_grid_charge_survives_slot_building():
    """Regression (user report): the optimum held the battery at night (grid energy waits for
    the morning) but the slot floor fell to min SoC, as the window had no later expensive
    hour - the slot plan then broke the grid-energy rule and was never written."""
    from dataclasses import replace
    from core.compress import compress
    from core.optimizer import HourPlan
    buy = [0.63] * 3 + [1.5] * 3 + [0.63] * 4
    hrs = day(buy, [0.5] * 10, load=[0.5] * 10)
    p = replace(P, min_soc=20, max_soc=98, soc_step=2, max_grid_charge_price=0.63,
                battery_first_above=0.63)
    socs = [(30, 60), (60, 90), (90, 90), (90, 87), (87, 84), (84, 81)] + [(81, 81)] * 4
    plan = [HourPlan(hrs[i].start, a, b, HourMode.GRID_CHARGE if i < 2 else HourMode.NORMAL,
                     0, 0, 1 if i < 2 else 0, 0, 0) for i, (a, b) in enumerate(socs)]
    segs, cost = compress(plan, hrs, 30, p, window=10)
    assert cost < 1e3, segs
    night = [s for s in segs if s.start >= 6]
    assert night and all(s.setting.soc >= 78 for s in night), segs


def test_full_charge_must_fall_within_the_slot_window():
    """Regression (user report): the optimum put the due 100 % at night (beyond the 18 h slot
    window); the slots could not contain it and the plan was rejected with a penalty."""
    from dataclasses import replace
    buy = [0.63] * 2 + [1.5] * 4 + [0.63] * 6
    hrs = day(buy, [0.6] * 12, load=[0.5] * 12)
    p = replace(P, min_soc=20, max_soc=100, require_full=True, soc_step=2, max_grid_charge_price=0.63)
    r = optimize(hrs, 80, p, Strategy.SELF_CONSUMPTION, full_by=6)
    assert max(h.soc_end for h in r.hours[:6]) == 100


def test_charge_just_before_the_expensive_hours():
    # flat cheap night, expensive morning: charge in the last cheap hours, not at once
    buy = [0.6] * 8 + [1.4] * 6
    hrs = day(buy, [0.3] * 14, load=[0.3] * 8 + [2] * 6)
    r = optimize(hrs, 10, P, Strategy.SELF_CONSUMPTION)
    charged = [i for i, h in enumerate(r.hours) if h.mode is HourMode.GRID_CHARGE]
    assert charged and min(charged) >= 6
    assert r.hours[7].soc_end > 50


def test_due_full_charge_reached_just_before_the_sale():
    buy = [0.6] * 10 + [1.0] * 2
    sell = [0.3] * 10 + [2.0] * 2
    p = BatteryParams(**{**P.__dict__, "require_full": True})
    r = optimize(day(buy, sell), 30, p, Strategy.MAX_GRID_TRADING, arbitrage=True)
    full = [i for i, h in enumerate(r.hours) if h.soc_end >= 100]
    assert full and min(full) == 9  # 100 % in the last hour before the sale, not overnight


def test_slots_hold_overnight_and_charge_late():
    # battery already at 80 %, full charge due before the 07-08 sale: hold overnight, top up at 06
    buy = [1.4] + [0.6] * 10 + [0.9] * 7
    sell = [0.3] * 11 + [1.35] * 2 + [0.7] * 5
    hrs = day(buy, sell, load=[0.4] * 18)
    p = BatteryParams(**{**P.__dict__, "min_soc": 20, "soc_step": 2, "max_grid_charge_price": 0.65,
                         "battery_first_above": 0.7, "require_full": True, "sell_min_power_kw": 0})
    r = optimize(hrs, 80, p, Strategy.MAX_GRID_TRADING, arbitrage=True, grid_energy=True, full_by=18)
    segs, cost = compress(r.hours, hrs, 80, p, window=18, grid_energy=True)
    plan = to_day_plan(segs, hrs, p, soc_now=80)
    charge = [s for s in plan.slots if s.mode is SlotMode.GRID_CHARGE]
    assert charge and min(s.start for s in charge) >= 500
    assert cost < 100  # no hard-rule / full-charge penalty


def test_charge_taper_matches_the_measured_bms_curve():
    p = BatteryParams(capacity_kwh=26.25, max_charge_kw=8)
    pct = p.capacity_kwh / 100
    assert 18 < max_charge_kwh(80, p) / pct < 20  # 80 -> 100 % does not fit in one hour
    assert 0.9 < max_charge_kwh(99, p) / pct <= 1.0  # the last % takes ~30 min
    assert abs(max_charge_kwh(99, p, hours=0.5) / pct - 0.95) < 0.05


def test_full_charge_starts_early_enough_for_the_taper():
    buy = [0.6] * 8 + [1.0] * 2
    sell = [0.3] * 8 + [2.0] * 2
    p = BatteryParams(**{**P.__dict__, "capacity_kwh": 26.25, "max_charge_kw": 8, "soc_step": 2,
                         "require_full": True})
    r = optimize(day(buy, sell), 80, p, Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert r.hours[7].soc_end == 100
    assert r.hours[6].soc_end < 100 and r.hours[6].mode is HourMode.GRID_CHARGE  # starts an hour earlier


def test_writes_are_paced_over_the_day():
    a = parse_fallback_plan("00:00 20; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 grid_charge; 21:00 20")
    c = parse_fallback_plan("00:00 20 none; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 sell; 21:00 20")
    g = WriteGuard(WritePolicy(max_writes_per_day=6))
    night = D0 + timedelta(hours=2)
    g.history = [D0 + timedelta(minutes=3)]
    # 02:00: budget 1 + ceil(5 * 2/24) = 2 -> one more small write allowed, then paced
    assert g.decide(a, c, night, gain=1.0)[0]
    g.record(night)
    assert g.decide(a, c, night + timedelta(hours=1), gain=1.0)[1][0] == "write_budget_paced"
    assert g.decide(a, c, night + timedelta(hours=1), gain=3.0)[0]  # large gain: urgent
    assert g.decide(a, c, night + timedelta(hours=1), gain=None)[0]  # current plan breaks rules
    assert g.decide(a, c, D0 + timedelta(hours=7), gain=1.0)[0]  # by 07:00 the budget is 3


def test_sell_slot_power_never_below_the_house_peak():
    # Export First caps all battery output at the slot power: a kettle + oven above it would
    # come from the grid, so the sale is not spread below sell_min_power_kw
    from dataclasses import replace
    from core.compress import Setting, simulate
    hrs = day([1.5, 1.5, 0.63], [1.65, 1.6, 0.9], load=[1.0, 1.0, 0.5])
    p = replace(P, max_soc=98, min_sell_soc=20, sell_min_power_kw=6.0)
    powers: dict[int, float] = {}
    simulate(hrs, [Setting(HourMode.SELL, 60)] * 2 + [Setting(HourMode.NORMAL, 20)], 98, p, powers=powers)
    assert powers[0] >= 6.0


def test_same_behaviour_ignores_power_and_spots_a_stale_flag():
    from core.plan import same_behaviour
    a = parse_fallback_plan("10:00 20; 13:00 100 grid_charge; 16:00 20; 18:00 46 sell; 20:00 24 sell; 21:00 24")
    b = parse_fallback_plan("10:00 20; 13:00 100 grid_charge; 16:00 20; 18:00 46 sell; 20:00 24 sell; 21:00 24", 6000)
    mixed = parse_fallback_plan("10:00 20 sell; 13:00 100 grid_charge; 16:00 20; 18:00 46 sell; 20:00 24 sell; 21:00 24")
    assert same_behaviour(a, b)
    assert not same_behaviour(a, mixed)
