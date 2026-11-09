"""``rolling`` is what the planner writes to the inverter: 5 slots of the hourly plan + a tail."""

from datetime import datetime, timedelta

from core.compress import _expand, rolling, simulate
from core.optimizer import BatteryParams, HourInput, HourMode, Strategy, optimize
from core.plan import SLOT_COUNT

D0 = datetime(2026, 11, 10, 0)
P = BatteryParams(capacity_kwh=20, min_soc=10, max_soc=98, soc_step=2, max_grid_charge_price=0.65,
                  battery_first_above=0.65, min_sell_soc=20, sell_min_power_kw=6.0)


def _day(sell: dict[int, float], n: int = 24) -> list[HourInput]:
    out = []
    for i in range(n):
        t = D0 + timedelta(hours=i)
        peak = 7 <= t.hour < 13 or 16 <= t.hour < 21
        out.append(HourInput(t, 0.0, 0.6, 0.0, 1.2 if peak else 0.65, sell.get(t.hour, 0.3)))
    return out


def test_quiet_day_fits_the_slots_without_a_tail():
    hrs = _day({})
    opt = optimize(hrs, 50, P, Strategy.SELF_CONSUMPTION)
    segs, cost, tail = rolling(opt.hours, hrs, 50, P)
    assert len(segs) == SLOT_COUNT and tail is None
    assert sum(s.length for s in segs) == 24 and segs[0].start == 0
    # the slots cost what the simulator says they cost
    assert abs(cost - simulate(hrs, _expand(segs), 50, P)[0]) < 1e-6


def test_busy_day_follows_the_plan_in_the_first_slots_and_reports_the_tail():
    # sales at 08, 12 and 17 with grid charging between them: more segments than slots
    hrs = _day({8: 2.4, 12: 2.2, 17: 3.4, 19: 2.0})
    opt = optimize(hrs, 30, P, Strategy.MAX_GRID_TRADING, arbitrage=True)
    assert sum(h.mode is HourMode.SELL for h in opt.hours) >= 3
    segs, _, tail = rolling(opt.hours, hrs, 30, P)
    assert len(segs) == SLOT_COUNT and sum(s.length for s in segs) == 24
    assert tail is not None and tail == segs[-1].start
    # before the tail the slots charge and sell in the hours the hourly plan chose
    settings = _expand(segs)
    for i in range(tail):
        if opt.hours[i].mode is not HourMode.NORMAL:
            assert settings[i].mode is opt.hours[i].mode, i
    # the first planned sale is inside the slots that follow the plan
    first_sale = next(i for i, h in enumerate(opt.hours) if h.mode is HourMode.SELL)
    assert first_sale < tail and settings[first_sale].mode is HourMode.SELL


def test_tail_never_sells_without_a_price():
    hrs = _day({8: 2.4, 12: 2.2, 17: 3.4})
    hrs = [h if i < 18 else HourInput(h.start, h.pv, h.load, h.ev, h.buy, 0.0) for i, h in enumerate(hrs)]
    opt = optimize(hrs, 30, P, Strategy.MAX_GRID_TRADING, arbitrage=True)
    segs, _, _ = rolling(opt.hours, hrs, 30, P)
    assert all(st.mode is not HourMode.SELL for st in _expand(segs)[18:])
