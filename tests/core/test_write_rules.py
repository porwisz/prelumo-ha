from datetime import datetime, timedelta

from core.diff import RuleCheck, WriteGuard, WritePolicy, roll_due, rule_check
from core.forecast.ev import EvForecast
from core.optimizer import BatteryParams, Strategy
from core.planner import PlannerInputs, parse_fallback_plan, run_planner
from core.tariff import G13Tariff

D0 = datetime(2026, 10, 5)  # Monday
A = parse_fallback_plan("00:00 20; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 grid_charge; 21:00 20")
C = parse_fallback_plan("00:00 20; 05:00 80 grid_charge; 07:00 20; 13:00 20; 16:00 50 sell; 21:00 20")
M = parse_fallback_plan("00:00 20; 05:00 80 grid_charge; 07:00 50 sell; 13:00 20; 16:00 50 grid_charge; 21:00 20")


def test_rule_check():
    near = {"02:00", "03:00", "04:00"}
    soon = rule_check({"03:00 sell_then_buy_in_hour"}, set(), near)
    assert soon == RuleCheck(urgent=True, worse=False, fixes=True)
    far = rule_check({"12:00 sell_then_buy_in_hour"}, set(), near)
    assert far == RuleCheck(urgent=False, worse=False, fixes=True)
    # the proposal breaks something the schedule keeps: worse, whatever it fixes far ahead
    swap = rule_check({"12:00 sell_then_buy_in_hour"}, {"16:00 grid_charge_above_price_cap"}, near)
    assert swap.worse and swap.fixes and not swap.urgent
    # fixing one near hour while breaking another near hour is not urgent
    near_swap = rule_check({"03:00 a"}, {"04:00 b"}, near)
    assert not near_swap.urgent
    assert rule_check({"full_charge_missing"}, set(), near).fixes


def test_distant_rule_fix_waits_for_the_write_budget():
    g = WriteGuard(WritePolicy(max_writes_per_day=6, min_gain=0.5))
    night = D0 + timedelta(hours=2)
    g.history = [D0 + timedelta(minutes=3), D0 + timedelta(hours=1)]  # budget at 02:00 is 2
    far = RuleCheck(fixes=True)
    assert g.decide(A, M, night, gain=0.0, rules=far)[1][0] == "write_budget_paced"
    assert g.decide(A, M, D0 + timedelta(hours=7), gain=0.0, rules=far)[0]  # no gain needed
    ok, why = g.decide(A, M, night, gain=0.0, rules=RuleCheck(urgent=True, fixes=True))
    assert ok and why[0] == "urgent_rule_fix"
    assert g.decide(A, M, night, gain=50.0, rules=RuleCheck(worse=True))[1][0] == "proposal_breaks_rules"
    # the daily limit holds even for an urgent fix (flash wear)
    g.history = [D0 + timedelta(hours=h) for h in range(6)]
    assert g.decide(A, M, D0 + timedelta(hours=7), rules=RuleCheck(urgent=True))[1][0] == "daily_write_limit"


RCE = {0: 0.7, 1: 0.7, 2: 0.66, 3: 0.65, 4: 0.63, 5: 0.67, 6: 0.88, 7: 1.1, 8: 1.27, 9: 1.25,
       10: 1.24, 11: 1.39, 12: 1.28, 13: 1.31, 14: 1.26, 15: 1.36, 16: 1.39, 17: 1.26, 18: 1.39,
       19: 1.4, 20: 1.26, 21: 1.01, 22: 0.89, 23: 0.81}
LIVE = BatteryParams(capacity_kwh=26.25, min_soc=20, max_soc=98, max_charge_kw=10, max_discharge_kw=10,
                     soc_step=2, max_grid_charge_price=0.63, battery_first_above=0.63, min_sell_soc=20,
                     sell_min_power_kw=6)
TARIFF = G13Tariff(0.9048, 1.4961, 0.6257)
START = datetime(2026, 10, 22)  # Thursday


def _plan_at(now, soc, current=None):
    hours = [START + timedelta(hours=i) for i in range(72)]
    known = now.date() + timedelta(days=1 if now.hour >= 14 else 0)  # tomorrow's RCE at 14:00
    sell = {h: RCE[h.hour] for h in hours if h.date() <= known}
    hs = [h for h in hours if h >= now.replace(minute=0)][:36]
    load = [4.0 if 11 <= h.hour < 16 else 0.8 if 16 <= h.hour < 22 else 0.4 for h in hs]
    inp = PlannerInputs(now, soc, {h: 0.0 for h in hs}, load, [0.0] * 36,
                        EvForecast([0.0] * 36, [], [False] * 36), sell, TARIFF, 36, grid_energy=True)
    return run_planner(inp, LIVE, Strategy.MAX_GRID_TRADING, arbitrage=True, current=current)


def test_rolling_slots_follow_the_hourly_plan():
    r = _plan_at(START + timedelta(minutes=3), 56.0)
    tail = [n for n in r.notes if n.startswith("tail_from_")]
    assert tail, r.notes  # more segments in the day than slots: the last slot is a tail
    assert r.slot_plan_issues == []  # the tail breaks no rule even if never rolled
    assert r.plan.slots[0].start == 0  # slot 1 = the current hour
    for seg in r.segments[:5]:  # slots 1-5 run the hourly plan's settings
        for i in range(seg.start, seg.start + seg.length):
            assert r.slot_hourly[i]["mode"] == r.optimum.hours[i].mode.value or seg.setting.mode.value == "normal"


def test_roll_due():
    assert not roll_due(None, D0)
    assert not roll_due(A, D0 + timedelta(hours=6))  # slot 2
    assert roll_due(A, D0 + timedelta(hours=14))  # slot 4 (13:00)
    g = WriteGuard(WritePolicy(min_gain=0.5))
    g.history = [D0 + timedelta(hours=h) for h in (1, 3)]  # paced: budget at 05:00 is 2
    ok, why = g.decide(A, M, D0 + timedelta(hours=5), gain=0.0, roll=True)
    assert ok and why[0] == "roll_slots"  # no gain needed, past the pacing


def test_a_day_of_hourly_replans_rolls_the_slots_within_the_write_limit():
    """Regression (user report, 2026-10-22): writes at 00, 01, 02, 03, 05 and 06 used the whole
    day's limit - every hour an unplanned hour entered the slot window's end, its broken rule
    (+10 000 PLN penalty) made each re-plan an urgent write.

    Now: the slots follow the plan as far as they reach and roll at slot 4. Over a day with the
    same forecast, the writes are the first one, the rolls and the ones new RCE prices cause,
    and the inverter never runs into the tail."""
    g = WriteGuard(WritePolicy(max_writes_per_day=6, min_gain=0.5))
    current, soc = None, 56.0
    for k in range(29):  # 22.10 00:03 ... 23.10 04:03
        now = START + timedelta(hours=k, minutes=3)
        if current is not None:
            assert current.active_index(now) < 5, f"{now:%H:%M} runs the tail"
        r = _plan_at(now, soc, current)
        ok, _ = g.decide(current, r.plan, now, r.gain_vs_current, r.rules, roll=roll_due(current, now))
        if ok:
            g.record(now)
            current = r.plan
            r.evaluate_current(current)
        soc = r.current_hourly[0]["soc"]  # the inverter runs the schedule it has
    first_day = [t for t in g.history if t.date() == START.date()]
    assert len(first_day) <= 5, first_day


def test_change_only_far_ahead_waits():
    g = WriteGuard(WritePolicy(min_gain=0.5))
    # C differs from A only at 16:00: 16 h after midnight -> waits; from 05:00 it is 11 h ahead
    assert g.decide(A, C, D0, gain=3.0)[1][0] == "change_far_ahead"
    assert g.decide(A, C, D0 + timedelta(hours=5), gain=3.0)[0]
    assert g.decide(None, C, D0)[0]  # nothing on the inverter: write
    assert g.decide(A, C, D0)[0]  # backup plan (gain unknown): write


# live inputs, 2026-10-22 14:03 (diagnostics): (load, ev, pv, buy, sell) per hour from 14:00
LIVE_1403 = [
    (3.4, 7.22, 0.19, .6257, 1.26), (4.51, 8, 0.12, .6257, 1.36), (0.6, 0, 0.07, 1.4961, 1.39),
    (0.72, 0, 0.01, 1.4961, 1.26), (0.92, 0, 0, 1.4961, 1.39), (0.75, 0, 0, 1.4961, 1.4),
    (0.69, 0, 0, 1.4961, 1.26), (0.64, 8, 0, .6257, 1.01), (0.82, 3.9, 0, .6257, 0.89), (0.48, 0, 0, .6257, 0.81),
    *[(x, 0, 0, .6257, 0) for x in (0.49, 0.51, 0.52, 0.53, 0.54, 1.71, 0.62)],
    (0.87, 0, 0.23, .9048, 0), (1.22, 0, 1.6, .9048, 0), (1.14, 0, 3.42, .9048, 0), (3.28, 0, 3.88, .9048, 0),
    (5.56, 0, 4.24, .9048, 0), (1.25, 0, 4.13, .9048, 0), (2.6, 0, 3.76, .6257, 0), (4.70, 0, 3.48, .6257, 0),
    (4.51, 0, 2.56, .6257, 0), *[(x, 0, pv, 1.4961, 0) for x, pv in ((1.40, 1.49), (1.46, 0.07), (1.77, 0),
                                                                      (2.10, 0), (1.24, 0))],
    *[(x, 0, 0, .6257, 0) for x in (0.68, 2.90, 2.65, 2.63, 2.65)],
]


def test_rolling_keeps_the_charge_for_the_evening_sale():
    """Regression (live, 2026-10-22 14:03): optimum = grid charge 14-15 to 100 %, sell 16-20.
    The proposal was one "sell to 76 %" slot from 14:00 to 19:00 - no charge at all."""
    from core.compress import rolling
    from core.optimizer import HourInput, HourMode, optimize
    t0 = datetime(2026, 10, 22, 14)
    hin = [HourInput(t0 + timedelta(hours=i), pv, load, ev, buy, sell)
           for i, (load, ev, pv, buy, sell) in enumerate(LIVE_1403)]
    p = BatteryParams(capacity_kwh=26.25, min_soc=20, max_soc=100, require_full=True, battery_first_above=0.62572,
                      max_charge_kw=8, wear_cost=0.1, soc_step=2, max_grid_charge_price=0.62572, min_sell_soc=20,
                      sell_min_power_kw=6, pv_trust=0.5, arbitrage_min_margin=0.5)
    opt = optimize(hin, 78, p, Strategy.MAX_GRID_TRADING, True, grid_energy=True, full_by=24)
    segs, _, tail = rolling(opt.hours, hin, 78, p, grid_energy=True)
    assert segs[0].setting.mode is HourMode.GRID_CHARGE and segs[0].setting.soc == 100 and segs[0].length == 2
    assert any(s.setting.mode is HourMode.SELL for s in segs[1:5])
    assert all(h.battery_export > 4.5 for h in opt.hours if h.mode is HourMode.SELL)  # ~ a full hour at 6 kW
