from datetime import datetime, timedelta

from core.optimizer import BatteryParams, HourMode, Strategy, optimize
from core.planner import build_hours
from core.sell_guard import LoadMeter, boost_sell_load

T0 = datetime(2026, 11, 8, 18, 0)


def test_meter_needs_some_minutes_and_keeps_a_window():
    m = LoadMeter()
    for k in range(4):
        m.add(T0 + timedelta(minutes=k), 3.0)
    assert m.mean_kw(T0 + timedelta(minutes=3)) is None  # under 5 min measured
    for k in range(4, 30):
        m.add(T0 + timedelta(minutes=k), 1.0 if k < 20 else 4.0)
    now = T0 + timedelta(minutes=29)
    assert abs(m.mean_kw(now) - (6 * 1.0 + 10 * 4.0) / 16) < 1e-9  # only the last 15 min


def test_boost_only_above_the_margin_and_only_the_running_sale():
    base, hp = [1.0, 1.0, 1.0, 1.0], [0.5, 0.5, 0.5, 0.5]
    sell = [True, True, False, True]
    same, b = boost_sell_load(base, hp, sell, 1.7, 0.2)  # 1.7 <= 1.5 * 1.2
    assert b is None and same == base
    out, b = boost_sell_load(base, hp, sell, 3.0, 0.2)
    assert out == [2.5, 2.5, 1.0, 1.0]  # base + hp = measured in the sale, later sale untouched
    assert b.hours == 2 and b.measured_kw == 3.0 and b.forecast_kw == 1.5
    assert boost_sell_load(base, hp, [False, True, True, True], 3.0, 0.2)[1] is None  # no sale now
    assert boost_sell_load(base, hp, sell, None, 0.2)[1] is None


def test_higher_house_draw_while_selling_sells_less():
    from core.forecast.ev import EvForecast
    from core.planner import PlannerInputs
    from core.tariff import G13Tariff

    hours = [T0 + timedelta(hours=i) for i in range(6)]
    p = BatteryParams(capacity_kwh=20, min_soc=10, soc_step=2, min_sell_soc=22, sell_min_power_kw=0,
                      battery_first_above=0.7)
    tariff = G13Tariff(0.6, 1.4, 0.6, morning_peak=(7, 13), afternoon_peak_summer=(19, 22),
                       afternoon_peak_winter=(16, 21))
    sell = {h: (2.0 if i < 2 else 0.3) for i, h in enumerate(hours)}
    ev = EvForecast(hourly_kwh=[0.0] * 6, battery_allowed=[False] * 6)

    def export(base):
        inp = PlannerInputs(T0, 80, {h: 0.0 for h in hours}, base, [0.0] * 6, ev, sell, tariff, 6, 6)
        hin, _ = build_hours(inp, hours)
        r = optimize(hin, 80, p, Strategy.MAX_GRID_TRADING)
        assert r.hours[0].mode is HourMode.SELL
        return sum(h.battery_export for h in r.hours[:2]), r.hours[1].soc_end

    sold, floor = export([0.5] * 6)
    out, boost = boost_sell_load([0.5] * 6, [0.0] * 6, [True, True] + [False] * 4, 2.5, 0.2)
    sold_hi, floor_hi = export(out)
    assert boost is not None
    assert sold_hi < sold - 1  # the extra house draw is taken from the sale
    assert floor_hi >= floor  # never deeper into the battery
