from datetime import datetime, timedelta

from core.diff import RuleCheck, WriteGuard, WritePolicy, rule_check
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


def test_unchanged_forecast_does_not_rewrite_the_schedule_every_night_hour():
    """Regression (user report, 2026-10-22): writes at 00, 01, 02, 03, 05 and 06 used the whole
    day's limit. The 6 slots were built from a 12 h window; each hour a new hour entered at the
    window's end, run by a slot planned for the hour before - a "broken rule" there (+10 000 PLN
    penalty) made every re-plan an urgent write past the pacing."""
    start = datetime(2026, 10, 22)  # Thursday
    hours = [start + timedelta(hours=i) for i in range(48)]
    rce = {0: 0.7, 1: 0.7, 2: 0.66, 3: 0.65, 4: 0.63, 5: 0.67, 6: 0.88, 7: 1.1, 8: 1.27, 9: 1.25,
           10: 1.24, 11: 1.39, 12: 1.28, 13: 1.31, 14: 1.26, 15: 1.36, 16: 1.39, 17: 1.26, 18: 1.39,
           19: 1.4, 20: 1.26, 21: 1.01, 22: 0.89, 23: 0.81}
    sell = {h: rce[h.hour] for h in hours[:24]}  # tomorrow's RCE not published yet
    load = {h: (4.0 if 11 <= h.hour < 16 else 0.8 if 16 <= h.hour < 22 else 0.4) for h in hours}
    p = BatteryParams(capacity_kwh=26.25, min_soc=20, max_soc=98, max_charge_kw=10, max_discharge_kw=10,
                      soc_step=2, max_grid_charge_price=0.63, battery_first_above=0.63, min_sell_soc=20,
                      sell_min_power_kw=6)
    tariff = G13Tariff(0.9048, 1.4961, 0.6257)
    g = WriteGuard(WritePolicy(max_writes_per_day=6, min_gain=0.5))
    current, soc, written = None, 56.0, []
    for k in range(8):  # 00:03 ... 07:03
        now = start + timedelta(hours=k, minutes=3)
        hs = [h for h in hours if h >= now.replace(minute=0)][:36]
        inp = PlannerInputs(now, soc, {h: 0.0 for h in hs}, [load[h] for h in hs], [0.0] * 36,
                            EvForecast([0.0] * 36, [], [False] * 36), sell, tariff, 36, grid_energy=True)
        r = run_planner(inp, p, Strategy.MAX_GRID_TRADING, arbitrage=True, current=current)
        ok, why = g.decide(current, r.plan, now, r.gain_vs_current, r.rules)
        if ok:
            g.record(now)
            written.append((f"{now:%H:%M}", why[:2]))
            current = r.plan
            r.evaluate_current(current)
        soc = r.current_hourly[0]["soc"]  # the inverter runs the schedule it has
    assert len(written) <= 2, written


def test_change_only_far_ahead_waits():
    g = WriteGuard(WritePolicy(min_gain=0.5))
    # C differs from A only at 16:00: 16 h after midnight -> waits; from 05:00 it is 11 h ahead
    assert g.decide(A, C, D0, gain=3.0)[1][0] == "change_far_ahead"
    assert g.decide(A, C, D0 + timedelta(hours=5), gain=3.0)[0]
    assert g.decide(None, C, D0)[0]  # nothing on the inverter: write
    assert g.decide(A, C, D0)[0]  # backup plan (gain unknown): write
