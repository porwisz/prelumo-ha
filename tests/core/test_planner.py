from datetime import datetime, timedelta

import pytest

from core.forecast.ev import EvForecast
from core.optimizer import BatteryParams, Strategy
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
