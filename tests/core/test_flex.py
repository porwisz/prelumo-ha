from datetime import date, datetime, timedelta

import pytest

from core.flex import FlexLoad, LoadError, TimedLoad, parse_loads, place, spread, window_starts
from core.forecast.ev import EvForecast
from core.optimizer import BatteryParams, HourMode, Strategy
from core.planner import PlannerInputs, run_planner
from core.tariff import G13Tariff

NOW = datetime(2026, 11, 9, 9, 30)  # a Monday
HOURS = [NOW.replace(minute=0) + timedelta(hours=i) for i in range(36)]


def test_spread_fills_hours_at_the_power_and_respects_a_partial_first_hour():
    assert spread(2.0, 2.5, 3, 6) == [0, 0, 0, 2.0, 0, 0]
    assert spread(4.0, 2.5, 3, 6) == [0, 0, 0, 2.5, 1.5, 0]
    assert spread(2.0, 2.0, 0, 3, first_fraction=0.25) == [0.5, 1.5, 0]
    assert spread(5.0, 2.0, 4, 6) == [0, 0, 0, 0, 2.0, 3.0]  # past the horizon: kept in the last hour


def test_window_starts_today_then_tomorrow():
    idx, day = window_starts(HOURS, NOW, (13, 16), 0.8)
    assert [HOURS[i].hour for i in idx] == [13, 14, 15] and day == date(2026, 11, 9)
    # a run of 1.5 h must end inside the window
    assert [HOURS[i].hour for i in window_starts(HOURS, NOW, (13, 16), 1.5)[0]] == [13, 14]
    # too long for the window: its first hour
    assert [HOURS[i].hour for i in window_starts(HOURS, NOW, (13, 16), 5)[0]] == [13]
    # done today -> tomorrow's window
    idx, day = window_starts(HOURS, NOW, (13, 16), 0.8, done=date(2026, 11, 9))
    assert day == date(2026, 11, 10) and HOURS[idx[0]] == datetime(2026, 11, 10, 13)
    # inside the window: from the running hour on
    later = NOW.replace(hour=14, minute=20)
    hrs = [later.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    idx, day = window_starts(hrs, later, (13, 16), 0.5)
    assert [hrs[i].hour for i in idx] == [14, 15] and day == date(2026, 11, 9)
    # the morning window is already over today
    idx, day = window_starts(HOURS, NOW, (3, 7), 1.0)
    assert day == date(2026, 11, 10) and [HOURS[i].hour for i in idx] == [3, 4, 5, 6]
    # days that do not qualify are skipped
    sat = datetime(2026, 11, 14, 9, 30)
    hrs = [sat.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    assert window_starts(hrs, sat, (13, 16), 0.8, day_ok=lambda d: d.weekday() < 5) == ([], None)


def test_place_takes_the_cheapest_start_and_reports_the_saving():
    price = [1.0, 1.0, 0.2, 0.5, 3.0, 1.0]

    def evaluate(extra):
        return sum(e * p for e, p in zip(extra, price))

    load = FlexLoad("a", 2.0, 2.0, candidates=(2, 3, 4), usual=4)
    extra, (pl,) = place([load], evaluate, 6)
    assert pl.start == 2 and pl.planned == 2 and extra[2] == 2.0
    assert pl.cost_by_start == {2: 0.0, 3: 0.6, 4: 5.6} and pl.saving_vs_usual == 5.6
    # only watching: the plan counts the load at its usual hour, the best start is still reported
    extra, (pl,) = place([load], evaluate, 6, controlled=lambda key: False)
    assert pl.start == 2 and pl.planned == 4 and extra[4] == 2.0
    # the previous choice stays while it is within a few grosze of the best
    flat = FlexLoad("a", 2.0, 2.0, candidates=(2, 3), previous=2)
    price[3] = 0.19
    assert place([flat], evaluate, 6)[1][0].start == 2
    price[3] = 0.1
    assert place([flat], evaluate, 6)[1][0].start == 3
    # equal starts: the latest one
    price[2] = price[3] = 0.3
    assert place([FlexLoad("a", 2.0, 2.0, candidates=(2, 3))], evaluate, 6)[1][0].start == 3
    # a load already running is not moved and not priced
    extra, (pl,) = place([FlexLoad("a", 2.0, 2.0, candidates=(2, 3), fixed=1)], evaluate, 6)
    assert pl.planned == 1 and extra[1] == 2.0 and pl.cost_by_start == {}


def test_planner_moves_a_boost_out_of_the_hour_the_battery_sells_in():
    """9 Nov: the hot water boost ran 15:00-16:00, the hour with the highest RCE price - every
    kWh it took was a kWh not sold. With cheap grid power at 13:00 the boost belongs there."""
    now = datetime(2026, 11, 9, 9, 30)
    hours = [now.replace(minute=0) + timedelta(hours=i) for i in range(36)]
    sell = {h: (3.8 if h == datetime(2026, 11, 9, 15) else 0.4) for h in hours}
    load = FlexLoad("13-16", 2.0, 2.5, candidates=(4, 5, 6), usual=6)  # 13:00, 14:00, 15:00
    kw = dict(now=now, soc=95, pv={h: 0.0 for h in hours}, base_load=[0.4] * 36, heat_pump=[0.0] * 36,
              ev=EvForecast([0.0] * 36, [], [False] * 36), sell=sell, tariff=G13Tariff(1.05, 1.45, 0.65))
    p = BatteryParams(soc_step=5, max_discharge_kw=5.0, grid_export_kw=5.0, sell_min_power_kw=5.0)
    res = run_planner(PlannerInputs(**kw, flex=[load], flex_controlled=frozenset({"13-16"})), p,
                      Strategy.MAX_GRID_TRADING, arbitrage=False)
    (pl,) = res.flex
    assert res.optimum.hours[6].mode is HourMode.SELL
    assert pl.start in (4, 5) and pl.planned == pl.start and pl.saving_vs_usual > 1.0
    assert res.hours[pl.start].load > 2.0 and res.hours[6].load < 0.5
    # only watching: the battery plan still counts the boost at 15:00
    res = run_planner(PlannerInputs(**kw, flex=[load]), p, Strategy.MAX_GRID_TRADING, arbitrage=False)
    assert res.flex[0].start in (4, 5) and res.flex[0].planned == 6 and res.hours[6].load > 2.0


def test_a_load_nobody_starts_and_with_no_usual_hour_is_only_priced():
    def evaluate(extra):
        return sum(e * p for e, p in zip(extra, [1.0, 0.2, 0.5]))

    extra, (pl,) = place([FlexLoad("a", 1.0, 1.0, candidates=(0, 1, 2))], evaluate, 3, controlled=lambda k: False)
    assert pl.start == 1 and pl.planned is None and extra == [0.0, 0.0, 0.0]


def test_parse_loads():
    loads = parse_loads("zmywarka 22:00-06:00 1.2 2.0 switch.zmywarka\nbasen 10:00-16:00 3 1,5 peak")
    assert loads == [TimedLoad("zmywarka", 22, 6, 1.2, 2.0, "switch.zmywarka"),
                     TimedLoad("basen", 10, 16, 3.0, 1.5, None, True)]
    assert loads[0].key == "load:zmywarka" and loads[0].duration.total_seconds() == 36 * 60
    assert loads[0].covers(23) and loads[0].covers(5) and not loads[0].covers(6)
    assert parse_loads("") == []
    for bad in ("a 10:00-16:00 3", "a 10:30-16:00 3 1", "a 10:00-16:00 0 1", "a 10:00-16:00 3 1 often",
                "a 10:00-16:00 3 1; a 11:00-12:00 1 1"):
        with pytest.raises(LoadError):
            parse_loads(bad)
